"""Crypto accounting/reconciliation producer CORE; no network or activation.
Trust boundary: inputs MUST be normalized/verified by a trusted evidence adapter,
not copied from dashboard JSON. Hashes identify evidence, not its authenticity.
The adapter/opening-checkpoint verification is not implemented in this module.
Weighted-average internal cost accounting; not a tax-accounting implementation.
"""
import hashlib
import json
import re
import sqlite3
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation,ROUND_HALF_EVEN,localcontext

BROKER='robinhood_crypto'
RESULT_EVENT='CAPITAL_RECONCILIATION_RESULT_V1'
CHECKS=('funding_verified','ownership_verified','fees_verified','positions_verified','orders_verified','history_complete_for_bot')
CATEGORIES=('fees','funding','ownership','orders','positions')
class ReconciliationBlocked(RuntimeError):pass

def require(condition,code):
    if not condition:raise ReconciliationBlocked(code)

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):
    require(isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None,'EVIDENCE_DIGEST_REQUIRED')
    return value

def number(value,*,signed=False,quantity=False):
    require(isinstance(value,str),'DECIMAL_STRING_REQUIRED')
    try:n=Decimal(value)
    except InvalidOperation:raise ReconciliationBlocked('INVALID_NUMBER') from None
    require(n.is_finite() and (signed or n>=0),'INVALID_NUMBER')
    require(abs(n)<=Decimal('1000000000000') and n.as_tuple().exponent>=(-18 if quantity else -12),'UNSUPPORTED_PRECISION_OR_RANGE')
    return n

def stamp(value):
    require(isinstance(value,str),'TIMESTAMP_REQUIRED')
    try:t=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:raise ReconciliationBlocked('INVALID_TIMESTAMP') from None
    require(t.tzinfo is not None,'TIMEZONE_REQUIRED');return t.astimezone(timezone.utc)

def symbol(value):
    require(isinstance(value,str) and re.fullmatch('[A-Z0-9]{1,30}-USD',value) is not None,'NORMALIZED_USD_SYMBOL_REQUIRED')
    return value

def text(n):return format(n if n else Decimal(0),'f')

def calculate_result(evidence):
    """Pure accounting, given verified normalized evidence (see NOTES).
    No default opening balance, no fee estimate substitution, no auto-attribution.
    All positions and events are already explicitly attributed by the adapter.
    """
    with localcontext() as ctx:
        ctx.prec=80
        require(isinstance(evidence,dict),'NORMALIZED_EVIDENCE_REQUIRED')
        require(evidence.get('bot')=='crypto' and evidence.get('broker')==BROKER and evidence.get('currency')=='USD','CRYPTO_SCOPE_REQUIRED')
        account=evidence.get('account_ref')
        require(isinstance(account,str) and bool(account) and account==account.strip(),'ACCOUNT_REQUIRED')
        require(type(evidence.get('config_revision')) is int and evidence['config_revision']>=0,'CONFIG_REVISION_REQUIRED')
        start=stamp(evidence.get('observation_started_at'));observed=stamp(evidence.get('as_of'));now=datetime.now(timezone.utc)
        require(start<=observed<=now and (now-observed).total_seconds()<=60,'OBSERVATION_STALE_OR_TIME_INVALID')
        opening=evidence.get('opening');require(isinstance(opening,dict),'VERIFIED_OPENING_CHECKPOINT_REQUIRED')
        require(opening.get('bot')=='crypto' and opening.get('broker')==BROKER and opening.get('account_ref')==account and opening.get('currency')=='USD','OPENING_SCOPE_MISMATCH')
        epoch=stamp(opening.get('as_of'));require(epoch<=start,'OPENING_AFTER_OBSERVATION_START')
        hashes=[digest(evidence.get('source_evidence_sha256')),digest(opening.get('source_evidence_sha256'))]
        coverage=evidence.get('coverage');require(isinstance(coverage,dict),'HISTORY_COVERAGE_REQUIRED')
        for kind in ('funding_complete_since','fills_complete_since','ownership_complete_since'):
            require(stamp(coverage.get(kind))<=epoch,'HISTORY_DOES_NOT_COVER_OPENING')
        funded=number(opening.get('funded_capital'));cash=number(opening.get('cash'))
        profit=number(opening.get('realized_net_profit'),signed=True)
        opening_positions=opening.get('positions');require(isinstance(opening_positions,dict),'OPENING_POSITIONS_REQUIRED')
        positions={}
        for name,row in opening_positions.items():
            name=symbol(name);require(isinstance(row,dict),'INVALID_OPENING_POSITION')
            qty=number(row.get('quantity'),quantity=True);cost=number(row.get('cost'))
            require(qty>0 or cost==0,'COST_WITHOUT_QUANTITY');positions[name]=[qty,cost]
        require(cash+sum((p[1] for p in positions.values()),Decimal(0))==funded+profit,'OPENING_ACCOUNTING_IDENTITY_FAILED')
        events=evidence.get('events');require(isinstance(events,list),'ATTRIBUTED_EVENT_HISTORY_REQUIRED')
        seen=set();last_sequence=-1;last_time=epoch
        for event in events:
            require(isinstance(event,dict),'INVALID_EVENT')
            identity=event.get('event_id');require(isinstance(identity,str) and bool(identity) and identity not in seen,'MISSING_OR_DUPLICATE_EVENT_ID');seen.add(identity)
            require(event.get('broker')==BROKER and event.get('account_ref')==account and event.get('bot')=='crypto' and event.get('currency')=='USD','EVENT_ATTRIBUTION_MISMATCH')
            seq=event.get('sequence');require(type(seq) is int and seq>last_sequence,'AUTHORITATIVE_EVENT_ORDER_REQUIRED')
            when=stamp(event.get('at'));require(epoch<when<=observed and last_time<=when,'EVENT_TIME_ORDER_INVALID')
            last_sequence=seq;last_time=when;hashes.append(digest(event.get('source_evidence_sha256')))
            kind=event.get('kind')
            if kind in ('CAPITAL_CREDIT','CAPITAL_DEBIT'):
                value=number(event.get('amount'));require(value>0,'POSITIVE_CAPITAL_MOVEMENT_REQUIRED')
                if kind=='CAPITAL_CREDIT':funded+=value;cash+=value
                else:
                    require(value<=cash and value<=funded,'WITHDRAWAL_REQUIRES_DISTRIBUTION_ACCOUNTING')
                    funded-=value;cash-=value
            elif kind in ('BUY_FILL','SELL_FILL'):
                name=symbol(event.get('symbol'));qty=number(event.get('quantity'),quantity=True);require(qty>0,'POSITIVE_FILL_QUANTITY_REQUIRED')
                gross=number(event.get('gross_notional'));require(gross>0,'EXACT_EXECUTED_NOTIONAL_REQUIRED')
                require(event.get('fee') is not None,'ACTUAL_FILL_FEE_MISSING')
                fee=number(event['fee']);position=positions.setdefault(name,[Decimal(0),Decimal(0)])
                if kind=='BUY_FILL':
                    debit=gross+fee;require(debit<=cash,'ATTRIBUTABLE_CASH_INSUFFICIENT')
                    cash-=debit;position[0]+=qty;position[1]+=debit
                else:
                    require(qty<=position[0],'SELL_EXCEEDS_ATTRIBUTABLE_POSITION')
                    released=position[1] if qty==position[0] else (position[1]*qty/position[0]).quantize(Decimal('0.000000000001'),rounding=ROUND_HALF_EVEN)
                    require(released<=position[1],'COST_ALLOCATION_INVALID')
                    position[0]-=qty;position[1]-=released
                    cash+=gross-fee;profit+=gross-fee-released
                    require(cash>=0,'NEGATIVE_ATTRIBUTABLE_CASH')
            else:raise ReconciliationBlocked('UNSUPPORTED_EVENT_KIND')
        require(cash+sum((p[1] for p in positions.values()),Decimal(0))==funded+profit,'FINAL_ACCOUNTING_IDENTITY_FAILED')
        broker=evidence.get('broker_observation');require(isinstance(broker,dict) and broker.get('account_ref')==account and broker.get('broker')==BROKER and broker.get('currency')=='USD','BROKER_OBSERVATION_ACCOUNT_MISMATCH')
        hashes.append(digest(broker.get('source_evidence_sha256')))
        for key in ('unresolved_order_count','unresolved_ownership_count','unsettled_event_count','open_order_count'):
            require(type(broker.get(key)) is int and broker[key]==0,'PENDING_OR_UNRESOLVED_BROKER_STATE')
        # This is a normalized independently reconciled cash partition, NOT an
        # invented field from Robinhood's account response.
        require(number(broker.get('reconciled_bot_cash'))==cash,'BOT_CASH_PARTITION_MISMATCH')
        buying_power=number(broker.get('buying_power'))
        actual=broker.get('positions');external=broker.get('verified_external_positions')
        require(isinstance(actual,dict) and isinstance(external,dict),'POSITION_ATTRIBUTION_REQUIRED')
        hashes.append(digest(broker.get('external_ownership_evidence_sha256')))
        names=set(actual)|set(external)|set(positions)
        for name in names:
            symbol(name)
            total=number(actual[name],quantity=True) if name in actual else Decimal(0)
            outside=number(external[name],quantity=True) if name in external else Decimal(0)
            owned=positions[name][0] if name in positions else Decimal(0)
            require(total==owned+outside,'POSITION_QUANTITIES_DO_NOT_RECONCILE')
        # No pending-buy amount is guessed: this version requires no open orders.
        values={'funded_capital':text(funded),'eligible_cash':text(min(cash,buying_power)),
            'position_cost':text(sum((p[1] for p in positions.values()),Decimal(0))),
            'realized_net_profit':text(profit),'baseline_pending_buy_commitment':'0'}
        for key,value in values.items():number(value,signed=(key=='realized_net_profit'))
        return {'schema_version':1,'bot':'crypto','broker':BROKER,'account_ref':account,'currency':'USD',
            'config_revision':evidence['config_revision'],'as_of':observed.isoformat(),**values,
            'checks':{key:True for key in CHECKS},'unresolved_counts':{key:0 for key in CATEGORIES},
            'source_evidence_sha256':hashlib.sha256(canonical({'evidence_hashes':hashes,'normalized_evidence':evidence}).encode()).hexdigest(),
            'cost_method':'WEIGHTED_AVERAGE_INTERNAL_12DP_HALF_EVEN'}

def produce_result_in_transaction(db,evidence):
    """Write a derived PASS/result only after all checks; caller commits.
    Any failure rolls back the ENTIRE caller transaction, including settlement.
    Must run AFTER correct terminal-order settlement/hold closure, in same txn.
    Does not implement that settlement or verify the input adapter's authenticity.
    """
    try:
        require(db.in_transaction and db.row_factory is sqlite3.Row,'WRITE_TRANSACTION_WITH_SQLITE_ROW_REQUIRED')
        db.execute('UPDATE reconciliation_runs SET status=status WHERE 0')
        result=calculate_result(evidence)
        row=db.execute("SELECT revision,config_json FROM independent_bot_configurations WHERE bot='crypto'").fetchone()
        require(row is not None,'BOT_CONFIGURATION_MISSING');config=json.loads(row['config_json'])
        require(row['revision']==result['config_revision'] and config.get('account_ref')==result['account_ref'] and config.get('currency')=='USD','CURRENT_CONFIGURATION_CHANGED')
        for table in ('capital_buy_reservations','automatic_sell_reservations_v2'):
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
            require(exists is not None or table=='automatic_sell_reservations_v2','RESERVATION_SCHEMA_MISSING')
            if exists:require(db.execute('SELECT 1 FROM '+table+" WHERE bot='crypto' AND account_ref=? AND (state IS NULL OR state<>'CLOSED') LIMIT 1",(result['account_ref'],)).fetchone() is None,'LOCAL_RESERVATIONS_NOT_SETTLED')
        require(db.execute("SELECT 1 FROM order_intents WHERE broker=? AND (account_ref=? OR account_ref IS NULL) AND status IN ('SUBMITTING','SUBMITTED','UNKNOWN') LIMIT 1",(BROKER,result['account_ref'])).fetchone() is None,'LOCAL_ORDERS_NOT_SETTLED')
        require(0<=(datetime.now(timezone.utc)-stamp(result['as_of'])).total_seconds()<=60,'OBSERVATION_EXPIRED_DURING_RECONCILIATION')
        finished=datetime.now(timezone.utc).isoformat()
        cursor=db.execute("INSERT INTO reconciliation_runs(broker,account_ref,started_at,finished_at,status,discrepancies_json) VALUES (?,?,?,?,'PASS','[]')",(BROKER,result['account_ref'],evidence['observation_started_at'],finished))
        run_id=cursor.lastrowid;result['reconciliation_id']=run_id
        db.execute('INSERT INTO audit_events(event_type,bot_name,correlation_id,details_json) VALUES (?,?,?,?)',(RESULT_EVENT,'crypto',str(run_id),canonical(result)))
        return {'status':'RECONCILIATION_RESULT_RECORDED','reconciliation_id':run_id,'result':result,'order_submission_authorized':False}
    except BaseException:
        if db.in_transaction:db.rollback()
        raise

