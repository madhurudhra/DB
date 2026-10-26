"""Prepared Crypto limit-order executor candidate. No import-time execution.
No producer, polling loop, retry, cancellation, settlement or activation here.
An explicit call can submit ONE real order through the supplied adapter.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
from datetime import datetime,timezone
from decimal import Decimal

BROKER='robinhood_crypto'
class InvalidPreparedOrder(Exception):pass

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'))
def connect(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw',uri=True,timeout=10)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON');return db

def close(db):
    if db is not None:
        if db.in_transaction:db.rollback()
        db.close()

def load_prepared(db,intent_id):
    if type(intent_id) is not int or intent_id<=0:raise InvalidPreparedOrder('INVALID_INTENT_ID')
    row=db.execute('SELECT * FROM order_intents WHERE id=?',(intent_id,)).fetchone()
    if row is None:raise InvalidPreparedOrder('INTENT_NOT_FOUND')
    if row['bot_name']!='crypto' or row['broker']!=BROKER:raise InvalidPreparedOrder('CRYPTO_INTENT_REQUIRED')
    if row['status']!='CREATED' or row['order_type']!='limit':raise InvalidPreparedOrder('UNCLAIMED_LIMIT_INTENT_REQUIRED')
    metadata=json.loads(row['metadata_json']);plan=metadata.get('execution_plan_v2')
    if metadata.get('quantity_is')!='asset_quantity' or not isinstance(plan,dict):raise InvalidPreparedOrder('PREPARED_PLAN_REQUIRED')
    payload=plan.get('payload')
    if not isinstance(payload,dict):raise InvalidPreparedOrder('PREPARED_PAYLOAD_REQUIRED')
    digest=hashlib.sha256(canonical(payload).encode()).hexdigest()
    if digest!=plan.get('payload_sha256'):raise InvalidPreparedOrder('PAYLOAD_DIGEST_MISMATCH')
    if payload.get('client_order_id')!=str(uuid.UUID(row['idempotency_key'])):raise InvalidPreparedOrder('CLIENT_ORDER_ID_MISMATCH')
    if (payload.get('symbol'),payload.get('side'),payload.get('type'))!=(row['symbol'],row['side'].lower(),'limit'):raise InvalidPreparedOrder('PAYLOAD_CONTEXT_MISMATCH')
    return dict(row),copy.deepcopy(payload),digest

def verify_account(adapter,account):
    # Existing v1 adapter only. Never treat a nonempty response as verification.
    response=adapter.accounts()
    if not isinstance(response,dict):return False
    rows=response.get('results') if 'results' in response else [response]
    if not isinstance(rows,list):return False
    matches=[r for r in rows if isinstance(r,dict) and r.get('account_number')==account]
    return len(matches)==1 and matches[0].get('status')=='active'

def receipt_matches(response,payload,account):
    try:
        if not isinstance(response,dict):return False
        uuid.UUID(response['id'])
        if response.get('account_number')!=account:return False
        for field in ('client_order_id','symbol','side','type'):
            if response.get(field)!=payload[field]:return False
        if response.get('state') not in {'open','pending','filled','canceled','failed'}:return False
        returned=response.get('limit_order_config');sent=payload['limit_order_config']
        if not isinstance(returned,dict):return False
        for field in ('asset_quantity','limit_price'):
            left=Decimal(str(returned[field]));right=Decimal(str(sent[field]))
            if not left.is_finite() or not right.is_finite() or left!=right:return False
        if 'time_in_force' in sent and returned.get('time_in_force')!=sent['time_in_force']:return False
        return True
    except (ValueError,KeyError,TypeError,AttributeError,ArithmeticError):return False

def event(db,event_type,intent,details):
    db.execute('INSERT INTO audit_events(event_type,bot_name,correlation_id,details_json) VALUES (?,?,?,?)',(event_type,'crypto',intent['idempotency_key'],canonical(details)))

def mark_unknown(path,intent,error_code):
    db=None
    try:
        db=connect(path);db.execute('BEGIN IMMEDIATE')
        changed=db.execute("UPDATE order_intents SET status='UNKNOWN' WHERE id=? AND status='SUBMITTING'",(intent['id'],))
        if changed.rowcount!=1:raise InvalidPreparedOrder('CLAIM_STATE_CHANGED')
        for table in ('capital_buy_reservations','automatic_sell_reservations_v2'):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                db.execute("UPDATE "+table+" SET state='UNKNOWN' WHERE request_id=? AND state='HELD'",(intent['idempotency_key'],))
        event(db,'AUTOMATIC_SUBMISSION_UNCERTAIN',intent,{'intent_id':intent['id'],'error_code':error_code})
        db.commit();return True
    except Exception:return False
    finally:close(db)

def verify_claim(db,intent,payload,digest):
    row=db.execute('SELECT * FROM order_intents WHERE id=?',(intent['id'],)).fetchone()
    if row is None or row['status']!='SUBMITTING':raise InvalidPreparedOrder('CLAIM_NOT_PRESENT')
    current=json.loads(row['metadata_json']).get('execution_plan_v2',{}).get('payload')
    if canonical(current)!=canonical(payload):raise InvalidPreparedOrder('CLAIMED_PAYLOAD_CHANGED')
    proof=db.execute("SELECT details_json FROM audit_events WHERE event_type='AUTOMATIC_SUBMISSION_CLAIMED' AND correlation_id=? AND bot_name='crypto' ORDER BY id DESC LIMIT 1",(intent['idempotency_key'],)).fetchone()
    if proof is None:raise InvalidPreparedOrder('CLAIM_AUDIT_MISSING')
    details=json.loads(proof['details_json'])
    if details.get('intent_id')!=intent['id'] or details.get('payload_sha256')!=digest:raise InvalidPreparedOrder('CLAIM_DIGEST_MISMATCH')

def record_response(path,intent,response):
    db=None
    try:
        db=connect(path);db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM order_intents WHERE id=? AND status='SUBMITTING'",(intent['id'],)).fetchone() is None:raise InvalidPreparedOrder('CLAIM_STATE_CHANGED')
        # No INSERT OR REPLACE: a conflicting broker order must never be reassigned.
        db.execute("INSERT INTO broker_orders(intent_id,broker,broker_order_id,status,submitted_at,response_json) VALUES (?,?,?,?,?,?)",(intent['id'],BROKER,response['id'],response['state'],datetime.now(timezone.utc).isoformat(),canonical(response)))
        db.execute("UPDATE order_intents SET status='SUBMITTED' WHERE id=? AND status='SUBMITTING'",(intent['id'],))
        # Even 'filled'/'failed' remains SUBMITTED locally until reconciled.
        event(db,'AUTOMATIC_SUBMISSION_RESPONSE_RECORDED',intent,{'intent_id':intent['id'],'broker_order_id':response['id'],'broker_state':response['state'],'settlement_complete':False})
        db.commit()
    finally:close(db)

def execute_prepared_crypto(path,intent_id,*,adapter,claim_fn=None):
    """Explicitly submit at most once. adapter ownership/closure stays with caller.
    claim_fn is an INTERNAL test seam, never a client-configurable input.
    Production must use installed conditional write_policy (claim_fn=None).
    Caller owns a dedicated worker; do not expose this function as a generic API.
    """
    if os.getenv('ROBINHOOD_CRYPTO_WRITE_ENABLED','false').strip().lower()!='true':return {'outcome':'BLOCKED','reason':'CRYPTO_BROKER_WRITE_DISABLED','send_attempted':False}
    db=None
    try:
        db=connect(path);intent,payload,digest=load_prepared(db,intent_id)
        risk=db.execute('SELECT passed FROM risk_checks WHERE decision_id=? AND bot_name=? ORDER BY id DESC LIMIT 1',(intent['decision_id'],'crypto')).fetchone()
        rec=db.execute('SELECT status,finished_at,discrepancies_json FROM reconciliation_runs WHERE broker=? AND account_ref=? ORDER BY id DESC LIMIT 1',(BROKER,intent['account_ref'])).fetchone()
        pending=db.execute("SELECT 1 FROM order_intents WHERE broker=? AND (account_ref=? OR account_ref IS NULL) AND status IN ('SUBMITTING','SUBMITTED','UNKNOWN') LIMIT 1",(BROKER,intent['account_ref'])).fetchone()
        reconciliation_ok=False
        if rec is not None and rec['status']=='PASS' and not json.loads(rec['discrepancies_json']):
            finished=datetime.fromisoformat(rec['finished_at'].replace('Z','+00:00'))
            reconciliation_ok=finished.tzinfo is not None and 0<=(datetime.now(timezone.utc)-finished).total_seconds()<=60 and pending is None
    except Exception as exc:return {'outcome':'BLOCKED','reason':'PREPARATION_'+type(exc).__name__.upper(),'send_attempted':False}
    finally:close(db)
    try:
        account_ok=verify_account(adapter,intent['account_ref'])
    except Exception:return {'outcome':'BLOCKED','reason':'ACCOUNT_READ_FAILED','send_attempted':False}
    if not account_ok:return {'outcome':'BLOCKED','reason':'CONFIGURED_ACCOUNT_NOT_VERIFIED','send_attempted':False}
    try:
        if claim_fn is None:
            from app.core.config import get_settings
            if Path(get_settings().database_path).resolve()!=Path(path).resolve():raise InvalidPreparedOrder('DATABASE_CONTEXT_MISMATCH')
            from app.core.write_policy import can_submit_order
            claim_fn=can_submit_order
        allowed,reason=claim_fn('crypto',risk_passed=risk is not None and risk['passed']==1,market_calendar_passed=True,account_verified=True,reconciliation_healthy=reconciliation_ok,durable_order_intent_saved=True,side=intent['side'],symbol=intent['symbol'],account_ref=intent['account_ref'],intent_id=intent['id'])
        # Conditional gate independently rechecks risk, latest reconciliation,
        # freshness, controls, limits and reservations in its claim transaction.
    except Exception:return {'outcome':'CLAIM_RESULT_UNKNOWN','reason':'CLAIM_CHECK_EXCEPTION','send_attempted':False}
    if allowed is not True:return {'outcome':'BLOCKED','reason':reason,'send_attempted':False}
    db=None
    try:
        db=connect(path);verify_claim(db,intent,payload,digest)
    except Exception:
        saved=mark_unknown(path,intent,'CLAIM_VERIFICATION_FAILED')
        return {'outcome':'UNKNOWN','reason':'CLAIM_VERIFICATION_FAILED','send_attempted':False,'unknown_persisted':saved}
    finally:close(db)
    try:
        # Exactly this payload, exactly one invocation. No resize and no retry.
        response=adapter.submit_order(copy.deepcopy(payload))
    except Exception:
        saved=mark_unknown(path,intent,'BROKER_SUBMISSION_EXCEPTION')
        return {'outcome':'UNKNOWN','reason':'BROKER_SUBMISSION_EXCEPTION','send_attempted':True,'unknown_persisted':saved}
    if not receipt_matches(response,payload,intent['account_ref']):
        saved=mark_unknown(path,intent,'BROKER_RESPONSE_NOT_VERIFIED')
        return {'outcome':'UNKNOWN','reason':'BROKER_RESPONSE_NOT_VERIFIED','send_attempted':True,'unknown_persisted':saved}
    try:record_response(path,intent,response)
    except Exception:
        saved=mark_unknown(path,intent,'RESPONSE_PERSISTENCE_FAILED')
        return {'outcome':'UNKNOWN','reason':'RESPONSE_PERSISTENCE_FAILED','send_attempted':True,'unknown_persisted':saved}
    return {'outcome':'RESPONSE_RECORDED','broker_order_id':response['id'],'broker_state':response['state'],'send_attempted':True,'settlement_complete':False}

