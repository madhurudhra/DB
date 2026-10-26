"""Crypto terminal-settlement/recovery CORE; no broker calls or order retries.
Inputs must be authenticated/normalized by trusted adapter code, not browser JSON.
Only reviewed prepared-limit intents with a durable claim are supported.
Historical market intents/fills are NOT silently migrated or adopted.
"""
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime,timezone
from decimal import Decimal,localcontext

BROKER='robinhood_crypto'
EVENT='CRYPTO_TERMINAL_SETTLED_V1'
class SettlementBlocked(RuntimeError):pass

def require(ok,code):
    if not ok:raise SettlementBlocked(code)
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def numeric(value,*,money=False):
    require(isinstance(value,str),'DECIMAL_STRING_REQUIRED')
    try:n=Decimal(value)
    except Exception:raise SettlementBlocked('INVALID_NUMBER') from None
    require(n.is_finite() and n>=0 and n<=Decimal('1000000000000'),'INVALID_NUMBER')
    n=n.normalize() if n else Decimal(0)
    require(n.as_tuple().exponent>=(-12 if money else -18),'UNSUPPORTED_PRECISION')
    return n

def text(value):return format(value if value else Decimal(0),'f')
def stamp(value):
    require(isinstance(value,str),'TIMESTAMP_REQUIRED')
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    require(parsed.tzinfo is not None,'TIMEZONE_REQUIRED');return parsed.astimezone(timezone.utc)

def hashed(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def event(db,kind,intent,details):
    db.execute('INSERT INTO audit_events(event_type,bot_name,correlation_id,details_json) VALUES (?,?,?,?)',(kind,'crypto',intent['idempotency_key'],canonical(details)))

def select_recovery_candidate(client_order_id,orders):
    """Pure matching after a trusted account-scoped broker read.
    Absence/ambiguity is NEVER treated as proof of rejection or non-submission.
    No POST, cancellation, retry or hold release is performed.
    """
    require(isinstance(orders,list),'ORDER_LIST_REQUIRED')
    matches=[o for o in orders if isinstance(o,dict) and o.get('client_order_id')==client_order_id]
    if len(matches)!=1:return {'status':'UNRESOLVED','match_count':len(matches),'receipt':None,'retry_allowed':False,'release_allowed':False}
    row=matches[0]
    status='TERMINAL_CANDIDATE' if row.get('state') in ('filled','canceled','failed') else 'WAITING_FOR_TERMINAL'
    return {'status':status,'match_count':1,'receipt':row,'retry_allowed':False,'release_allowed':False}

def validate_receipt(db,receipt):
    require(isinstance(receipt,dict) and type(receipt.get('schema_version')) is int and receipt['schema_version']==1,'NORMALIZED_TERMINAL_RECEIPT_REQUIRED')
    require(receipt.get('broker')==BROKER and receipt.get('bot')=='crypto' and receipt.get('currency')=='USD','CRYPTO_SCOPE_REQUIRED')
    require(type(receipt.get('intent_id')) is int,'INTENT_ID_REQUIRED')
    row=db.execute('SELECT * FROM order_intents WHERE id=?',(receipt['intent_id'],)).fetchone()
    require(row is not None,'INTENT_NOT_FOUND');intent=dict(row)
    require(intent['bot_name']=='crypto' and intent['broker']==BROKER,'INTENT_SCOPE_MISMATCH')
    require((intent['account_ref'],intent['symbol'],intent['side'])==(receipt.get('account_ref'),receipt.get('symbol'),receipt.get('side')),'RECEIPT_ACCOUNT_SYMBOL_OR_SIDE_MISMATCH')
    require(intent['side'] in ('BUY','SELL'),'INVALID_SIDE')
    require(intent['order_type']=='limit','LEGACY_INTENT_REQUIRES_SEPARATE_MIGRATION')
    key=str(uuid.UUID(intent['idempotency_key']))
    require(key==receipt.get('client_order_id'),'CLIENT_ORDER_ID_MISMATCH')
    order_id=receipt.get('order_id');require(isinstance(order_id,str) and str(uuid.UUID(order_id))==order_id,'CANONICAL_BROKER_ORDER_UUID_REQUIRED')
    state=receipt.get('state');require(state in ('filled','canceled','failed'),'TERMINAL_BROKER_STATE_REQUIRED')
    observed=stamp(receipt.get('observed_at'));age=(datetime.now(timezone.utc)-observed).total_seconds()
    require(0<=age<=60,'STALE_OR_FUTURE_RECEIPT')
    source_hash=receipt.get('source_evidence_sha256')
    require(isinstance(source_hash,str) and re.fullmatch('[0-9a-f]{64}',source_hash) is not None,'SOURCE_EVIDENCE_DIGEST_REQUIRED')
    metadata=json.loads(intent['metadata_json']);plan=metadata.get('execution_plan_v2')
    require(metadata.get('quantity_is')=='asset_quantity' and isinstance(plan,dict),'PREPARED_ASSET_PLAN_REQUIRED')
    payload=plan.get('payload');require(isinstance(payload,dict),'PREPARED_PAYLOAD_REQUIRED')
    require((payload.get('client_order_id'),payload.get('symbol'),payload.get('side'),payload.get('type'))==(key,intent['symbol'],intent['side'].lower(),'limit'),'PREPARED_PAYLOAD_CONTEXT_MISMATCH')
    payload_hash=hashed(payload);require(payload_hash==plan.get('payload_sha256'),'PREPARED_PAYLOAD_DIGEST_MISMATCH')
    claims=db.execute("SELECT details_json FROM audit_events WHERE event_type='AUTOMATIC_SUBMISSION_CLAIMED' AND bot_name='crypto' AND correlation_id=?",(key,)).fetchall()
    require(len(claims)==1,'ONE_DURABLE_CLAIM_REQUIRED')
    claim=json.loads(claims[0]['details_json'])
    require(claim.get('intent_id')==intent['id'] and claim.get('payload_sha256')==payload_hash,'CLAIM_CONTEXT_MISMATCH')
    requested=numeric(receipt.get('requested_quantity'));require(requested>0 and requested==numeric(intent['quantity']) and requested==numeric(plan.get('asset_quantity')),'REQUESTED_QUANTITY_MISMATCH')
    limit_price=numeric(plan.get('limit_price'));require(limit_price>0,'LIMIT_PRICE_REQUIRED')
    cfg=payload.get('limit_order_config');require(isinstance(cfg,dict) and numeric(cfg.get('asset_quantity'))==requested and numeric(cfg.get('limit_price'))==limit_price,'PAYLOAD_LIMIT_OR_QUANTITY_MISMATCH')
    filled=numeric(receipt.get('filled_quantity'));require(filled<=requested,'BROKER_OVERFILL')
    require(state!='filled' or filled==requested,'FILLED_STATE_QUANTITY_MISMATCH')
    fee=numeric(receipt.get('fee_total'),money=True);gross=numeric(receipt.get('gross_notional'),money=True)
    fills=receipt.get('fills');require(isinstance(fills,list),'NORMALIZED_FILLS_REQUIRED')
    seen=set();clean=[]
    for f in fills:
        require(isinstance(f,dict),'INVALID_FILL')
        fid=f.get('fill_id');require(isinstance(fid,str) and bool(fid) and fid not in seen,'MISSING_OR_DUPLICATE_FILL_ID');seen.add(fid)
        kind=f.get('identifier_kind');require(kind in ('BROKER_ISSUED','LOCALLY_DERIVED_NOT_BROKER_ISSUED'),'FILL_IDENTIFIER_KIND_REQUIRED')
        qty=numeric(f.get('quantity'));price=numeric(f.get('price'));notional=numeric(f.get('gross_notional'),money=True);charge=numeric(f.get('fee'),money=True)
        require(qty>0 and price>0 and notional>0,'INVALID_FILL_AMOUNTS')
        require(price<=limit_price if intent['side']=='BUY' else price>=limit_price,'EXECUTION_OUTSIDE_LIMIT_REQUIRES_REVIEW')
        when=stamp(f.get('filled_at'));require(when<=observed,'FILL_AFTER_OBSERVATION')
        clean.append({'fill_id':fid,'identifier_kind':kind,'quantity':text(qty),'price':text(price),'gross_notional':text(notional),'fee':text(charge),'filled_at':when.isoformat()})
    require(sum((numeric(f['quantity']) for f in clean),Decimal(0))==filled,'FILL_QUANTITIES_INCOMPLETE')
    require(sum((numeric(f['gross_notional'],money=True) for f in clean),Decimal(0))==gross,'GROSS_NOTIONAL_MISMATCH')
    require(sum((numeric(f['fee'],money=True) for f in clean),Decimal(0))==fee,'ACTUAL_FEES_MISMATCH')
    require(filled>0 or (gross==0 and fee==0 and not fills),'CHARGE_WITHOUT_FILL_REQUIRES_REVIEW')
    business={'intent_id':intent['id'],'broker':BROKER,'bot':'crypto','currency':'USD','account_ref':intent['account_ref'],'order_id':order_id,'client_order_id':key,'symbol':intent['symbol'],'side':intent['side'],'state':state,'requested_quantity':text(requested),'filled_quantity':text(filled),'fee_total':text(fee),'gross_notional':text(gross),'fills':sorted(clean,key=lambda f:f['fill_id'])}
    return intent,plan,business,hashed(business)

def _apply_terminal_locked(db,receipt):
    intent,plan,business,fingerprint=validate_receipt(db,receipt)
    previous=db.execute('SELECT details_json FROM audit_events WHERE event_type=? AND bot_name=? AND correlation_id=?',(EVENT,'crypto',intent['idempotency_key'])).fetchall()
    require(len(previous)<=1,'DUPLICATE_SETTLEMENT_AUDIT')
    terminal={'filled':'FILLED','canceled':'CANCELED','failed':'FAILED'}[business['state']]
    if previous:
        proof=json.loads(previous[0]['details_json'])
        require(proof.get('receipt_sha256')==fingerprint and intent['status']==terminal,'CONFLICTING_SETTLED_RECEIPT')
        return {'status':'ALREADY_SETTLED','intent_id':intent['id'],'business':business}
    require(intent['status'] in ('SUBMITTING','SUBMITTED','UNKNOWN'),'CLAIMED_OUTSTANDING_INTENT_REQUIRED')
    request=intent['idempotency_key']
    table='capital_buy_reservations' if intent['side']=='BUY' else 'automatic_sell_reservations_v2'
    hold=db.execute('SELECT * FROM '+table+' WHERE request_id=?',(request,)).fetchone()
    require(hold is not None and hold['bot']=='crypto' and hold['account_ref']==intent['account_ref'] and hold['state'] in ('HELD','UNKNOWN'),'MATCHING_ACTIVE_RESERVATION_REQUIRED')
    if intent['side']=='BUY':
        require(numeric(hold['amount_total'],money=True)==numeric(plan.get('reservation_total'),money=True),'RESERVATION_PLAN_MISMATCH')
        require(numeric(business['gross_notional'],money=True)+numeric(business['fee_total'],money=True)<=numeric(hold['amount_total'],money=True),'ACTUAL_COST_EXCEEDS_RESERVATION_REVIEW_REQUIRED')
        require(numeric(business['fee_total'],money=True)<=numeric(plan.get('fee_bound'),money=True)+numeric(plan.get('other_charges_bound'),money=True),'ACTUAL_FEE_EXCEEDS_BOUND_REVIEW_REQUIRED')
    else:require(hold['symbol']==intent['symbol'] and numeric(hold['quantity'])==numeric(plan['asset_quantity']),'SELL_RESERVATION_PLAN_MISMATCH')
    # An unrecognized previous fill import must not be silently duplicated.
    existing=db.execute('SELECT * FROM fills WHERE broker=? AND account_ref=? AND broker_order_id=?',(BROKER,intent['account_ref'],business['order_id'])).fetchall()
    wanted={f['fill_id']:f for f in business['fills']}
    for row in existing:
        require(row['broker_fill_id'] in wanted,'UNRECOGNIZED_EXISTING_FILL_REQUIRES_REVIEW')
        f=wanted[row['broker_fill_id']]
        require(row['symbol']==intent['symbol'] and row['side']==intent['side'] and row['currency']=='USD' and numeric(row['quantity'])==numeric(f['quantity']) and numeric(row['price'])==numeric(f['price']) and numeric(row['fee'],money=True)==numeric(f['fee'],money=True) and stamp(row['filled_at'])==stamp(f['filled_at']),'EXISTING_FILL_CONFLICT')
        raw=json.loads(row['raw_json']);require(raw.get('normalized_fill',{}).get('gross_notional')==f['gross_notional'],'EXISTING_FILL_NOTIONAL_NOT_VERIFIED')
    for f in business['fills']:
        if any(row['broker_fill_id']==f['fill_id'] for row in existing):continue
        conflict=db.execute('SELECT 1 FROM fills WHERE broker=? AND account_ref=? AND broker_fill_id=?',(BROKER,intent['account_ref'],f['fill_id'])).fetchone()
        require(conflict is None,'FILL_ID_ALREADY_ASSIGNED_TO_ANOTHER_ORDER')
        db.execute("INSERT INTO fills(broker,account_ref,broker_fill_id,broker_order_id,symbol,side,quantity,price,fee,currency,filled_at,raw_json) VALUES (?,?,?,?,?,?,?,?,?,'USD',?,?)",(BROKER,intent['account_ref'],f['fill_id'],business['order_id'],intent['symbol'],intent['side'],f['quantity'],f['price'],f['fee'],f['filled_at'],canonical({'normalized_fill':f,'identifier_kind':f['identifier_kind'],'source_evidence_sha256':receipt['source_evidence_sha256']})))
    linked=db.execute('SELECT * FROM broker_orders WHERE broker=? AND broker_order_id=?',(BROKER,business['order_id'])).fetchone()
    require(linked is None or linked['intent_id']==intent['id'],'BROKER_ORDER_OWNERSHIP_CONFLICT')
    payload=canonical({'normalized_terminal_receipt':business,'source_evidence_sha256':receipt['source_evidence_sha256']})
    if linked is None:
        require(db.execute('SELECT 1 FROM broker_orders WHERE broker=? AND intent_id=?',(BROKER,intent['id'])).fetchone() is None,'INTENT_ALREADY_LINKED_TO_DIFFERENT_ORDER')
        db.execute('INSERT INTO broker_orders(intent_id,broker,broker_order_id,status,updated_at,response_json) VALUES (?,?,?,?,?,?)',(intent['id'],BROKER,business['order_id'],business['state'],receipt['observed_at'],payload))
    else:db.execute('UPDATE broker_orders SET status=?,updated_at=?,response_json=? WHERE id=?',(business['state'],receipt['observed_at'],payload,linked['id']))
    db.execute('UPDATE order_intents SET status=? WHERE id=?',(terminal,intent['id']))
    changed=db.execute('UPDATE '+table+" SET state='CLOSED' WHERE request_id=? AND state IN ('HELD','UNKNOWN')",(request,))
    require(changed.rowcount==1,'RESERVATION_CLOSURE_CONFLICT')
    event(db,EVENT,intent,{'intent_id':intent['id'],'receipt_sha256':fingerprint,'source_evidence_sha256':receipt['source_evidence_sha256'],'broker_order_id':business['order_id'],'terminal_status':terminal})
    return {'status':'SETTLED_PENDING_ATOMIC_PUBLICATION','intent_id':intent['id'],'business':business}

def _bind_accounting_events(settled,evidence):
    events=evidence.get('events');require(isinstance(events,list),'ACCOUNTING_EVENTS_REQUIRED')
    for item in settled:
        if item['status']=='ALREADY_SETTLED':continue
        order=item['business']
        for f in order['fills']:
            matches=[e for e in events if isinstance(e,dict) and e.get('broker_order_id')==order['order_id'] and e.get('broker_fill_id')==f['fill_id']]
            require(len(matches)==1,'SETTLED_FILL_MISSING_FROM_ACCOUNTING_HISTORY')
            e=matches[0]
            require((e.get('account_ref'),e.get('symbol'),e.get('kind'))==(order['account_ref'],order['symbol'],order['side']+'_FILL'),'ACCOUNTING_FILL_ATTRIBUTION_MISMATCH')
            for key in ('quantity','gross_notional','fee'):require(numeric(e.get(key),money=key!='quantity')==numeric(f[key],money=key!='quantity'),'ACCOUNTING_FILL_AMOUNT_MISMATCH')
        actual={f['fill_id'] for f in order['fills']}
        require(all(e.get('broker_fill_id') in actual for e in events if isinstance(e,dict) and e.get('broker_order_id')==order['order_id']),'UNRECOGNIZED_ACCOUNTING_FILL')

class _CallerCommitOnly:
    """Limited interface used by known producer/publisher dependencies.
    Prevents accidental commit/transaction SQL inside the atomic coordinator.
    Not a security sandbox against malicious Python code.
    """
    def __init__(self,db):self._db=db
    @property
    def in_transaction(self):return self._db.in_transaction
    @property
    def row_factory(self):return self._db.row_factory
    def execute(self,sql,parameters=()):
        require(isinstance(sql,str) and sql.strip().split(None,1)[0].upper() in ('SELECT','INSERT','UPDATE'),'DEPENDENCY_TRANSACTION_SQL_NOT_ALLOWED')
        return self._db.execute(sql,parameters)
    def rollback(self):self._db.rollback()
    def commit(self):raise SettlementBlocked('COMMIT_OWNED_BY_CALLER')

def settle_reconcile_publish_in_transaction(db,receipts,evidence,*,_producer=None,_publisher=None):
    """Atomic whole-operation coordinator; no commit inside. Caller uses BEGIN IMMEDIATE.
    _producer/_publisher are INTERNAL test seams, never user-configurable inputs.
    Defaults load your review producer/publisher. Production packaging needs wiring.
    Failure rolls back the ENTIRE caller transaction. Never commit private helpers.
    """
    try:
        require(db.in_transaction and db.row_factory is sqlite3.Row,'WRITE_TRANSACTION_WITH_SQLITE_ROW_REQUIRED')
        with localcontext() as ctx:
            ctx.prec=80
            db.execute('UPDATE order_intents SET status=status WHERE 0')
            require(isinstance(receipts,list) and bool(receipts),'TERMINAL_RECEIPTS_REQUIRED')
            ids=[r.get('intent_id') for r in receipts if isinstance(r,dict)]
            require(len(ids)==len(receipts) and all(type(i) is int and i>0 for i in ids) and len(set(ids))==len(ids),'DUPLICATE_OR_INVALID_SETTLEMENT_INPUT')
            settled=[_apply_terminal_locked(db,r) for r in receipts]
            require(all(evidence.get('account_ref')==item['business']['account_ref'] for item in settled),'ACCOUNTING_ACCOUNT_MISMATCH')
            _bind_accounting_events(settled,evidence)
            if _producer is None:
                from capital_reconciliation_producer_v1 import produce_result_in_transaction
                _producer=produce_result_in_transaction
            if _publisher is None:
                from capital_baseline_publisher_v1 import publish_capital_baseline_in_transaction
                _publisher=publish_capital_baseline_in_transaction
            dependency_db=_CallerCommitOnly(db)
            result=_producer(dependency_db,evidence)
            published=_publisher(dependency_db,bot='crypto',reconciliation_id=result['reconciliation_id'])
            require(db.in_transaction,'DEPENDENCY_COMMITTED_UNEXPECTEDLY')
            return {'status':'SETTLEMENT_AND_BASELINE_STAGED','intent_ids':ids,'reconciliation_id':result['reconciliation_id'],'baseline_updated':published['baseline_updated'],'caller_commit_required':True,'order_submission_authorized':False}
    except BaseException:
        if db.in_transaction:db.rollback()
        raise

