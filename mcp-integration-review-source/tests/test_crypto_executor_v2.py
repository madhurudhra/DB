import concurrent.futures
import copy
from datetime import datetime,timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('review_crypto_executor',ROOT/'crypto_prepared_executor_v2.py')
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)

SCHEMA="""
CREATE TABLE order_intents(id INTEGER PRIMARY KEY,idempotency_key TEXT,bot_name TEXT,broker TEXT,account_ref TEXT,symbol TEXT,side TEXT,order_type TEXT,status TEXT,decision_id INTEGER,metadata_json TEXT);
CREATE TABLE risk_checks(id INTEGER PRIMARY KEY,decision_id INTEGER,bot_name TEXT,passed INTEGER);
CREATE TABLE reconciliation_runs(id INTEGER PRIMARY KEY,broker TEXT,account_ref TEXT,status TEXT,finished_at TEXT,discrepancies_json TEXT);
CREATE TABLE broker_orders(id INTEGER PRIMARY KEY,intent_id INTEGER,broker TEXT,broker_order_id TEXT,status TEXT,submitted_at TEXT,response_json TEXT,UNIQUE(broker,broker_order_id));
CREATE TABLE capital_buy_reservations(request_id TEXT PRIMARY KEY,state TEXT);
CREATE TABLE audit_events(id INTEGER PRIMARY KEY,event_type TEXT,bot_name TEXT,correlation_id TEXT,details_json TEXT);
"""
class FakeAdapter:
    def __init__(self,payload,account):
        self.sent=[];self.account=account;self.error=False;self.mutate=False
        self.accounts_result={'results':[{'account_number':account,'status':'active'}]}
        self.response=dict(copy.deepcopy(payload),id=str(uuid.uuid4()),account_number=account,state='open')
    def accounts(self):return copy.deepcopy(self.accounts_result)
    def submit_order(self,payload):
        self.sent.append(copy.deepcopy(payload))
        if self.mutate:payload['limit_order_config']['asset_quantity']='999'
        if self.error:raise TimeoutError('simulated network timeout')
        return copy.deepcopy(self.response)

class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=str(Path(self.temp.name)/'executor.sqlite3')
        self.env=dict(os.environ);os.environ['ROBINHOOD_CRYPTO_WRITE_ENABLED']='true'
        self.cid=str(uuid.uuid4());self.account='TEST-ACCOUNT';self.claim_count=0
        self.payload={'client_order_id':self.cid,'symbol':'BTC-USD','side':'buy','type':'limit','limit_order_config':{'asset_quantity':'0.1','limit_price':'10'}}
        self.digest=hashlib.sha256(worker.canonical(self.payload).encode()).hexdigest()
        self.metadata={'quantity_is':'asset_quantity','execution_plan_v2':{'payload':self.payload,'payload_sha256':self.digest}}
        with self.db() as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO order_intents VALUES (1,?,'crypto','robinhood_crypto',?,'BTC-USD','BUY','limit','CREATED',1,?)",(self.cid,self.account,json.dumps(self.metadata)))
            db.execute("INSERT INTO risk_checks VALUES (1,1,'crypto',1)")
            db.execute("INSERT INTO reconciliation_runs VALUES (1,'robinhood_crypto',?,'PASS',?,'[]')",(self.account,datetime.now(timezone.utc).isoformat()))
        self.adapter=FakeAdapter(self.payload,self.account)
    def tearDown(self):os.environ.clear();os.environ.update(self.env);self.temp.cleanup()
    def db(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row;return db
    def claim(self,bot,**kwargs):
        self.claim_count+=1
        if not all(kwargs[k] is True for k in ('risk_passed','market_calendar_passed','account_verified','reconciliation_healthy','durable_order_intent_saved')):return False,'CHECK_FAILED'
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            cur=db.execute("UPDATE order_intents SET status='SUBMITTING' WHERE id=1 AND status='CREATED'")
            if cur.rowcount!=1:return False,'ALREADY_CLAIMED'
            db.execute("INSERT INTO capital_buy_reservations VALUES (?,'HELD')",(self.cid,))
            db.execute("INSERT INTO audit_events(event_type,bot_name,correlation_id,details_json) VALUES ('AUTOMATIC_SUBMISSION_CLAIMED','crypto',?,?)",(self.cid,json.dumps({'intent_id':1,'payload_sha256':self.digest})))
        return True,'SUBMISSION_CLAIM_ACQUIRED'
    def run_worker(self,claim=None):return worker.execute_prepared_crypto(self.path,1,adapter=self.adapter,claim_fn=claim or self.claim)
    def status(self):
        with self.db() as db:return db.execute('SELECT status FROM order_intents WHERE id=1').fetchone()[0]
    def hold(self):
        with self.db() as db:return db.execute('SELECT state FROM capital_buy_reservations').fetchone()[0]
    def test_success_sends_exact_payload(self):
        result=self.run_worker();self.assertEqual(result['outcome'],'RESPONSE_RECORDED');self.assertEqual(self.adapter.sent,[self.payload]);self.assertEqual(self.status(),'SUBMITTED');self.assertEqual(self.hold(),'HELD');self.assertFalse(result['settlement_complete'])
    def test_disabled_broker_writes(self):
        os.environ['ROBINHOOD_CRYPTO_WRITE_ENABLED']='false';result=self.run_worker();self.assertEqual(result['outcome'],'BLOCKED');self.assertEqual(self.claim_count,0);self.assertEqual(self.adapter.sent,[])
    def test_wrong_account_never_claims(self):
        self.adapter.accounts_result={'account_number':'OTHER','status':'active'};self.assertEqual(self.run_worker()['outcome'],'BLOCKED');self.assertEqual(self.claim_count,0);self.assertEqual(self.adapter.sent,[])
    def test_inactive_account_never_claims(self):
        self.adapter.accounts_result={'account_number':self.account,'status':'closed'};self.assertEqual(self.run_worker()['outcome'],'BLOCKED');self.assertEqual(self.claim_count,0)
    def test_singleton_account_response(self):
        self.adapter.accounts_result={'account_number':self.account,'status':'active'};self.assertEqual(self.run_worker()['outcome'],'RESPONSE_RECORDED')
    def test_duplicate_account_response_denied(self):
        self.adapter.accounts_result['results']*=2;self.assertEqual(self.run_worker()['outcome'],'BLOCKED');self.assertEqual(self.claim_count,0)
    def test_claim_denied_does_not_send(self):
        self.assertEqual(self.run_worker(lambda *a,**k:(False,'DENIED'))['outcome'],'BLOCKED');self.assertEqual(self.adapter.sent,[])
    def test_fake_true_without_claim_does_not_send(self):
        result=self.run_worker(lambda *a,**k:(True,'FAKE'));self.assertEqual(result['outcome'],'UNKNOWN');self.assertEqual(self.adapter.sent,[])
    def test_timeout_retains_unknown_hold_no_retry(self):
        self.adapter.error=True;self.assertEqual(self.run_worker()['outcome'],'UNKNOWN');self.assertEqual(self.status(),'UNKNOWN');self.assertEqual(self.hold(),'UNKNOWN');self.run_worker();self.assertEqual(len(self.adapter.sent),1)
    def test_response_wrong_client_id(self):
        self.adapter.response['client_order_id']=str(uuid.uuid4());self.assertEqual(self.run_worker()['outcome'],'UNKNOWN');self.assertEqual(self.hold(),'UNKNOWN')
    def test_response_wrong_account(self):
        self.adapter.response['account_number']='OTHER';self.assertEqual(self.run_worker()['outcome'],'UNKNOWN')
    def test_response_wrong_quantity(self):
        self.adapter.response['limit_order_config']['asset_quantity']='5';self.assertEqual(self.run_worker()['outcome'],'UNKNOWN')
    def test_response_missing_id(self):
        del self.adapter.response['id'];self.assertEqual(self.run_worker()['outcome'],'UNKNOWN')
    def test_broker_failed_keeps_hold_until_reconciled(self):
        self.adapter.response['state']='failed';result=self.run_worker();self.assertEqual(result['broker_state'],'failed');self.assertEqual(self.hold(),'HELD');self.assertEqual(self.status(),'SUBMITTED')
    def test_immediate_fill_not_mistaken_for_settlement(self):
        self.adapter.response['state']='filled';self.assertFalse(self.run_worker()['settlement_complete']);self.assertEqual(self.hold(),'HELD');self.assertEqual(self.status(),'SUBMITTED')
    def test_receipt_db_failure_keeps_hold(self):
        with self.db() as db:db.execute("CREATE TRIGGER block_order BEFORE INSERT ON broker_orders BEGIN SELECT RAISE(ABORT,'test'); END")
        result=self.run_worker();self.assertEqual(result['outcome'],'UNKNOWN');self.assertEqual(self.hold(),'UNKNOWN');self.assertEqual(len(self.adapter.sent),1)
    def test_all_post_send_writes_fail_remains_submitting(self):
        def claim_then_break(*a,**k):
            result=self.claim(*a,**k)
            with self.db() as db:
                db.execute("CREATE TRIGGER block_receipt BEFORE INSERT ON broker_orders BEGIN SELECT RAISE(ABORT,'test'); END")
                db.execute("CREATE TRIGGER block_status BEFORE UPDATE ON order_intents BEGIN SELECT RAISE(ABORT,'test'); END")
            return result
        result=self.run_worker(claim_then_break);self.assertEqual(result['outcome'],'UNKNOWN');self.assertFalse(result['unknown_persisted']);self.assertEqual(self.status(),'SUBMITTING');self.assertEqual(self.hold(),'HELD')
    def test_adapter_mutation_does_not_change_stored_plan(self):
        self.adapter.mutate=True;self.run_worker()
        with self.db() as db:stored=json.loads(db.execute('SELECT metadata_json FROM order_intents').fetchone()[0])
        self.assertEqual(stored['execution_plan_v2']['payload'],self.payload)
    def test_market_intent_denied_before_account_read(self):
        with self.db() as db:db.execute("UPDATE order_intents SET order_type='market'")
        self.assertEqual(self.run_worker()['outcome'],'BLOCKED');self.assertEqual(self.claim_count,0)
    def test_failed_reconciliation_flag_not_faked(self):
        with self.db() as db:db.execute("UPDATE reconciliation_runs SET status='FAIL'")
        self.assertEqual(self.run_worker()['outcome'],'BLOCKED');self.assertEqual(self.adapter.sent,[])
    def test_concurrent_attempts_send_at_most_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.run_worker(),range(2)))
        self.assertEqual(len(self.adapter.sent),1,results)

if __name__=='__main__':unittest.main(verbosity=2)

