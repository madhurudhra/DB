"""Conditional order claim, not a broker executor.

TRUST BOUNDARY: execution_plan_v2 must be produced by trusted broker-reading
and risk-evaluation code, never copied from a dashboard request. This module
checks persisted records; it cannot authenticate arbitrary fabricated records.
Successful calls atomically mark an intent SUBMITTING and reserve its resources.
No adapter is called. A reconciler must resolve all claims, including crashes.
Legacy callers without intent_id are deliberately rejected.
"""
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from pathlib import Path
from app.core.config import get_settings

BROKERS={'crypto':'robinhood_crypto','equity':'robinhood_equity','zerodha':'zerodha'}
SELL_SCHEMA="""CREATE TABLE IF NOT EXISTS automatic_sell_reservations_v2 (
 request_id TEXT PRIMARY KEY,bot TEXT NOT NULL,account_ref TEXT NOT NULL,
 symbol TEXT NOT NULL,quantity TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('HELD','UNKNOWN','CLOSED')),
 created_at TEXT NOT NULL)"""
class Blocked(Exception):pass

def require(condition,reason):
    if not condition:raise Blocked(reason)

def number(value,positive=False,signed=False):
    require(value is not None and not isinstance(value,bool),'INVALID_NUMERIC_INPUT')
    try:n=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError):raise Blocked('INVALID_NUMERIC_INPUT') from None
    require(n.is_finite() and (signed or n>=0) and (not positive or n>0),'INVALID_NUMERIC_INPUT')
    require(abs(n)<=Decimal('1000000000000') and n.as_tuple().exponent>=-18,'NUMERIC_RANGE_EXCEEDED')
    return n

def fresh(timestamp,maximum=60):
    try:
        t=datetime.fromisoformat(timestamp.replace('Z','+00:00'))
        require(t.tzinfo is not None,'TIMESTAMP_TIMEZONE_REQUIRED')
        age=(datetime.now(timezone.utc)-t).total_seconds()
        require(0<=age<=maximum,'EVIDENCE_STALE_OR_FUTURE_DATED')
    except Blocked:raise
    except (AttributeError,TypeError,ValueError):raise Blocked('INVALID_EVIDENCE_TIMESTAMP') from None

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'))

def owned_quantity(db,bot,account,symbol,broker):
    # Only fills linked to this bot's broker orders establish ownership.
    # Pre-existing/manual holdings are NOT treated as bot-owned.
    rows=db.execute("""SELECT f.side,f.quantity FROM fills f
      JOIN broker_orders bo ON bo.broker=f.broker AND bo.broker_order_id=f.broker_order_id
      JOIN order_intents oi ON oi.id=bo.intent_id AND oi.broker=bo.broker
      WHERE f.broker=? AND f.account_ref=? AND f.symbol=?
      AND oi.bot_name=? AND oi.account_ref=? AND oi.symbol=?""",
      (broker,account,symbol,bot,account,symbol)).fetchall()
    amount=Decimal(0)
    for row in rows:
        require(row['side'] in ('BUY','SELL'),'INVALID_FILL_SIDE')
        amount+=number(row['quantity'])*(1 if row['side']=='BUY' else -1)
    require(amount>=0,'OWNERSHIP_LEDGER_INCONSISTENT')
    return amount

def validate_and_claim(db,bot,side,symbol,account,intent_id):
    require(bot in BROKERS,'UNKNOWN_BOT')
    require(side in ('BUY','SELL'),'ORDER_SIDE_CONTEXT_REQUIRED')
    require(isinstance(symbol,str) and bool(symbol) and symbol==symbol.strip().upper(),'ORDER_SYMBOL_CONTEXT_REQUIRED')
    require(isinstance(account,str) and bool(account) and account==account.strip(),'VERIFIED_ACCOUNT_CONTEXT_REQUIRED')
    require(type(intent_id) is int and intent_id>0,'ORDER_INTENT_CONTEXT_REQUIRED')
    config_row=db.execute('SELECT * FROM independent_bot_configurations WHERE bot=?',(bot,)).fetchone()
    controls_row=db.execute('SELECT * FROM dashboard_control_state WHERE singleton=1').fetchone()
    require(config_row is not None,'BOT_CONFIGURATION_MISSING')
    require(controls_row is not None,'CONTROL_STATE_MISSING')
    config=json.loads(config_row['config_json']);states=json.loads(controls_row['state_json'])
    controls=states.get(bot)
    require(isinstance(config,dict) and isinstance(controls,dict),'SAVED_AUTOMATIC_POLICY_INVALID')
    require(config.get('account_ref')==account,'CONFIGURED_ACCOUNT_MISMATCH')
    require(config.get('currency')==('INR' if bot=='zerodha' else 'USD'),'CONFIGURATION_CURRENCY_INVALID')
    reserve=number(config.get('cash_reserve_pct'));require(reserve<100,'RESERVE_PERCENTAGE_INVALID')
    for key in ('bot_enabled','buy_enabled','sell_enabled'):
        require(type(controls.get(key)) is bool,'CONTROL_STATE_INVALID')
    # Stopping new entries does not implicitly stop exits.
    if side=='BUY':
        require(controls['bot_enabled'] and controls['buy_enabled'],'AUTOMATIC_BUY_NOT_ENABLED')
        if bot!='zerodha':number(config.get('allocated_capital'),positive=True)
    else:require(controls['sell_enabled'],'AUTOMATIC_SELL_NOT_ENABLED')
    buy_rules=config.get('buy_restrictions');sell_rules=config.get('sell_protections')
    require(isinstance(buy_rules,list) and isinstance(sell_rules,list),'RESTRICTIONS_INVALID')
    seen=set();protected=Decimal(0)
    for rule in buy_rules:
        require(isinstance(rule,dict) and isinstance(rule.get('symbol'),str) and rule.get('mode')=='block_all','BUY_RESTRICTIONS_INVALID')
        s=rule['symbol'].strip().upper();require(s and s not in seen,'BUY_RESTRICTIONS_INVALID');seen.add(s)
        require(not(side=='BUY' and s==symbol),'SYMBOL_AUTOMATIC_BUY_RESTRICTED')
    seen=set()
    for rule in sell_rules:
        require(isinstance(rule,dict) and isinstance(rule.get('symbol'),str),'SELL_PROTECTIONS_INVALID')
        s=rule['symbol'].strip().upper();require(s and s not in seen,'SELL_PROTECTIONS_INVALID');seen.add(s)
        require(rule.get('mode') in ('all','quantity'),'SELL_PROTECTIONS_INVALID')
        q=number(rule.get('quantity'),positive=True) if rule['mode']=='quantity' else Decimal(0)
        if side=='SELL' and s==symbol:
            require(rule['mode']!='all','POSITION_FULLY_SELL_PROTECTED');protected=q
    intent=db.execute('SELECT * FROM order_intents WHERE id=?',(intent_id,)).fetchone()
    require(intent is not None,'ORDER_INTENT_NOT_PERSISTED')
    require((intent['bot_name'],intent['broker'],intent['account_ref'],intent['symbol'],intent['side'])==(bot,BROKERS[bot],account,symbol,side),'ORDER_INTENT_CONTEXT_MISMATCH')
    require(intent['status']=='CREATED','ORDER_INTENT_ALREADY_CLAIMED_OR_TERMINAL')
    require(intent['order_type']=='limit','BOUNDED_LIMIT_ORDER_REQUIRED')
    metadata=json.loads(intent['metadata_json']);plan=metadata.get('execution_plan_v2')
    require(metadata.get('quantity_is')=='asset_quantity' and isinstance(plan,dict),'PREPARED_ASSET_QUANTITY_PLAN_REQUIRED')
    require(type(plan.get('config_revision')) is int and plan['config_revision']==config_row['revision'],'PLAN_CONFIGURATION_CHANGED')
    require(type(plan.get('control_revision')) is int and plan['control_revision']==controls_row['revision'],'PLAN_CONTROLS_CHANGED')
    for key,age in [('prepared_at',10),('quote_at',10),('fees_at',60),('account_verified_at',60),('instrument_verified_at',60)]:fresh(plan.get(key),age)
    require(plan.get('account_ref')==account,'PLAN_ACCOUNT_MISMATCH')
    qty=number(plan.get('asset_quantity'),positive=True);price=number(plan.get('limit_price'),positive=True)
    require(qty==number(intent['quantity'],positive=True),'INTENT_QUANTITY_MISMATCH')
    step=number(plan.get('asset_increment'),positive=True);tick=number(plan.get('quote_increment'),positive=True)
    require(qty%step==0 and price%tick==0,'BROKER_INCREMENT_VIOLATION')
    require(qty<number(plan.get('max_order_size'),positive=True),'BROKER_MAXIMUM_SIZE_EXCEEDED')
    require(qty*price>=number(plan.get('min_order_amount')),'BROKER_MINIMUM_NOT_MET')
    fee_rate=number(plan.get('fee_ratio'));require(fee_rate<1,'FEE_RATE_INVALID')
    fee=number(plan.get('fee_bound'));charges=number(plan.get('other_charges_bound'))
    principal=(qty*price).quantize(Decimal('.01'),rounding=ROUND_CEILING)
    require(fee>=(qty*price*fee_rate).quantize(Decimal('.01'),rounding=ROUND_CEILING),'FEE_BOUND_INSUFFICIENT')
    total=principal+fee+charges
    require(total==number(plan.get('reservation_total'),positive=True),'RESERVATION_TOTAL_MISMATCH')
    payload=plan.get('payload')
    require(isinstance(payload,dict) and set(payload)=={'client_order_id','symbol','side','type','limit_order_config'},'PREPARED_PAYLOAD_INVALID')
    try:request_id=str(uuid.UUID(intent['idempotency_key']))
    except (ValueError,TypeError,AttributeError):raise Blocked('UUID_IDEMPOTENCY_KEY_REQUIRED') from None
    require(payload['client_order_id']==request_id and payload['symbol']==symbol and payload['side']==side.lower() and payload['type']=='limit','PAYLOAD_IDENTITY_MISMATCH')
    cfg=payload['limit_order_config']
    require(isinstance(cfg,dict) and {'asset_quantity','limit_price'}<=set(cfg) and set(cfg)<={'asset_quantity','limit_price','time_in_force'},'PREPARED_PAYLOAD_INVALID')
    require(number(cfg['asset_quantity'],positive=True)==qty and number(cfg['limit_price'],positive=True)==price,'PAYLOAD_AMOUNT_MISMATCH')
    if 'time_in_force' in cfg:require(cfg['time_in_force'] in ('gtc','ioc','fok'),'TIME_IN_FORCE_INVALID')
    require(plan.get('payload_sha256')==hashlib.sha256(canonical(payload).encode()).hexdigest(),'PAYLOAD_DIGEST_MISMATCH')
    # Never substitute hardcoded caller booleans for a recorded evaluation.
    decision=db.execute('SELECT * FROM decisions WHERE id=?',(intent['decision_id'],)).fetchone()
    require(decision is not None and (decision['bot_name'],decision['symbol'],decision['decision'])==(bot,symbol,side),'RECORDED_DECISION_REQUIRED')
    risk=db.execute('SELECT * FROM risk_checks WHERE decision_id=? AND bot_name=? ORDER BY id DESC LIMIT 1',(intent['decision_id'],bot)).fetchone()
    require(risk is not None and risk['passed']==1 and bool(risk['rules_version']),'RECORDED_RISK_CHECK_REQUIRED')
    fresh(risk['checked_at'])
    reconciliation=db.execute('SELECT * FROM reconciliation_runs WHERE broker=? AND account_ref=? ORDER BY id DESC LIMIT 1',(BROKERS[bot],account)).fetchone()
    require(reconciliation is not None and reconciliation['status']=='PASS','PASSING_ACCOUNT_RECONCILIATION_REQUIRED')
    fresh(reconciliation['finished_at'])
    require(not json.loads(reconciliation['discrepancies_json']),'RECONCILIATION_DISCREPANCIES_PRESENT')
    pending=db.execute("SELECT 1 FROM order_intents WHERE broker=? AND (account_ref=? OR account_ref IS NULL) AND status IN ('SUBMITTING','SUBMITTED','UNKNOWN') LIMIT 1",(BROKERS[bot],account)).fetchone()
    require(pending is None,'PENDING_OR_AMBIGUOUS_ORDER_REQUIRES_RECONCILIATION')
    if bot=='crypto':
        maximum=number(os.getenv('RUNNER_MAX_ORDER_USD','10'),positive=True)
        require(total<=maximum,'MAXIMUM_FEE_INCLUSIVE_ORDER_VALUE_EXCEEDED')
        cap=int(os.getenv('RUNNER_MAX_ORDERS_PER_DAY','3'));require(cap>0,'DAILY_ORDER_CAP_INVALID')
        count=db.execute("SELECT COUNT(*) FROM order_intents oi WHERE bot_name='crypto' AND created_at>=strftime('%Y-%m-%dT00:00:00Z','now') AND (status IN ('SUBMITTING','SUBMITTED','UNKNOWN','FILLED') OR EXISTS (SELECT 1 FROM broker_orders bo WHERE bo.intent_id=oi.id AND bo.broker=oi.broker))").fetchone()[0]
        require(count<cap,'DAILY_ORDER_CAP_REACHED')
    if side=='BUY':
        from app.core.capital_ledger_v1 import snapshot,reserve_buy
        report=snapshot(db,bot)
        require(report.get('reservation_checks_ready') is True,'VERIFIED_CAPITAL_REQUIRED')
        baseline=db.execute('SELECT reconciliation_id FROM capital_verified_states WHERE bot=? AND account_ref=?',(bot,account)).fetchone()
        require(baseline is not None and baseline['reconciliation_id']==reconciliation['id'],'CAPITAL_RECONCILIATION_MISMATCH')
        require(total<=number(report.get('available_automatic_budget')),'INSUFFICIENT_AUTOMATIC_BUDGET')
        require(db.execute('SELECT 1 FROM capital_buy_reservations WHERE request_id=?',(request_id,)).fetchone() is None,'RESERVATION_ALREADY_EXISTS')
        result=reserve_buy(db,bot=bot,account_ref=account,request_id=request_id,amount_total=str(total))
        require(result.get('state')=='HELD' and result.get('replayed') is False,'CAPITAL_RESERVATION_NOT_ACQUIRED')
    else:
        # This schema has no ownership field: derive it from attributable fills.
        position=db.execute('SELECT * FROM position_snapshots WHERE broker=? AND account_ref=? AND symbol=? ORDER BY id DESC LIMIT 1',(BROKERS[bot],account,symbol)).fetchone()
        require(position is not None,'VERIFIED_POSITION_REQUIRED');fresh(position['observed_at'])
        raw=json.loads(position['raw_json'])
        # Trusted position reader must normalize the broker's actually sellable
        # quantity into this field. Total holding quantity is NOT a substitute.
        sellable=number(raw.get('verified_sellable_quantity'))
        owned=owned_quantity(db,bot,account,symbol,BROKERS[bot])
        require(sellable<=number(position['quantity']) and owned<=number(position['quantity']),'POSITION_LEDGER_INCONSISTENT')
        db.execute(SELL_SCHEMA)
        holds=db.execute("SELECT quantity FROM automatic_sell_reservations_v2 WHERE bot=? AND account_ref=? AND symbol=? AND state IN ('HELD','UNKNOWN')",(bot,account,symbol)).fetchall()
        held=sum((number(r['quantity']) for r in holds),Decimal(0))
        require(qty<=max(Decimal(0),min(sellable,owned)-protected-held),'INSUFFICIENT_UNPROTECTED_BOT_OWNED_QUANTITY')
        require(db.execute('SELECT 1 FROM automatic_sell_reservations_v2 WHERE request_id=?',(request_id,)).fetchone() is None,'RESERVATION_ALREADY_EXISTS')
        db.execute("INSERT INTO automatic_sell_reservations_v2 VALUES (?,?,?,?,?,'HELD',?)",(request_id,bot,account,symbol,str(qty),datetime.now(timezone.utc).isoformat()))
    claimed=db.execute("UPDATE order_intents SET status='SUBMITTING' WHERE id=? AND status='CREATED'",(intent_id,))
    require(claimed.rowcount==1,'ORDER_INTENT_ALREADY_CLAIMED_OR_TERMINAL')
    db.execute('INSERT INTO audit_events(event_type,bot_name,correlation_id,details_json) VALUES (?,?,?,?)',('AUTOMATIC_SUBMISSION_CLAIMED',bot,request_id,canonical({'intent_id':intent_id,'side':side,'quantity':str(qty),'limit_price':str(price),'maximum_cost':str(total),'payload_sha256':plan['payload_sha256'],'config_revision':config_row['revision'],'control_revision':controls_row['revision']})))
    return True,'SUBMISSION_CLAIM_ACQUIRED'

def automatic_order_preflight(bot,*,side=None,symbol=None,account_ref=None,intent_id=None):
    """Atomic, single-use claim. Changed semantics: success WRITES a claim.
    Caller must send EXACT persisted prepared payload once, not resize it.
    Never retry after unknown submission; reconcile using client_order_id.
    This function must not be used by a dashboard status/read endpoint.
    """
    if type(intent_id) is not int or intent_id<=0:return False,'ORDER_INTENT_CONTEXT_REQUIRED'
    db=None
    try:
        with localcontext() as context:
            context.prec=80
            path=Path(get_settings().database_path).resolve()
            # Existing DB only. Never silently create a new live database.
            db=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=10)
            db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE')
            result=validate_and_claim(db,bot,side,symbol,account_ref,intent_id)
            db.commit();return result
    except Blocked as exc:return False,str(exc)
    except Exception as exc:return False,'ENFORCEMENT_ERROR_'+type(exc).__name__.upper()
    finally:
        if db is not None:
            if db.in_transaction:db.rollback()
            db.close()
