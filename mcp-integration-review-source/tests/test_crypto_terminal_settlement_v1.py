import copy
from datetime import datetime,timedelta,timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('terminal_settlement',ROOT/'crypto_terminal_settlement_v1.py')
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
HASH=hashlib.sha256(b'SYNTHETIC TEST DATA NOT BROKER EVIDENCE').hexdigest()
SCHEMA="""
CREATE TABLE independent_bot_configurations(bot TEXT PRIMARY KEY,revision INTEGER,config_json TEXT);
CREATE TABLE reconciliation_runs(id INTEGER PRIMARY KEY,broker TEXT,account_ref TEXT,started_at TEXT,finished_at TEXT,status TEXT,discrepancies_json TEXT);
CREATE TABLE capital_verified_states(bot TEXT,account_ref TEXT,config_revision INTEGER,reconciliation_id INTEGER,funded_capital TEXT,eligible_cash TEXT,position_cost TEXT,realized_net_profit TEXT,baseline_pending_buy_commitment TEXT,as_of TEXT,PRIMARY KEY(bot,account_ref));
CREATE TABLE order_intents(id INTEGER PRIMARY KEY,idempotency_key TEXT,bot_name TEXT,broker TEXT,account_ref TEXT,symbol TEXT,side TEXT,quantity TEXT,order_type TEXT,status TEXT,metadata_json TEXT);
CREATE TABLE broker_orders(id INTEGER PRIMARY KEY,intent_id INTEGER,broker TEXT,broker_order_id TEXT,status TEXT,submitted_at TEXT,updated_at TEXT,response_json TEXT,UNIQUE(broker,broker_order_id));
CREATE TABLE fills(id INTEGER PRIMARY KEY,broker TEXT,account_ref TEXT,broker_fill_id TEXT,broker_order_id TEXT,symbol TEXT,side TEXT,quantity TEXT,price TEXT,fee TEXT,currency TEXT,filled_at TEXT,raw_json TEXT,UNIQUE(broker,account_ref,broker_fill_id));
CREATE TABLE capital_buy_reservations(request_id TEXT PRIMARY KEY,bot TEXT,account_ref TEXT,amount_total TEXT,state TEXT);
CREATE TABLE automatic_sell_reservations_v2(request_id TEXT PRIMARY KEY,bot TEXT,account_ref TEXT,symbol TEXT,quantity TEXT,state TEXT);
CREATE TABLE audit_events(id INTEGER PRIMARY KEY,event_type TEXT,bot_name TEXT,correlation_id TEXT,details_json TEXT);
"""
class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'test.sqlite3';self.now=datetime.now(timezone.utc)
        self.client=str(uuid.uuid4());self.order=str(uuid.uuid4());self.fill=str(uuid.uuid4());self.account='TEST-5576'
        self.payload={'client_order_id':self.client,'symbol':'BTC-USD','side':'buy','type':'limit','limit_order_config':{'asset_quantity':'1','limit_price':'10'}}
        self.plan={'payload':self.payload,'payload_sha256':s.hashed(self.payload),'asset_quantity':'1','limit_price':'10','reservation_total':'10.1','fee_bound':'0.1','other_charges_bound':'0'}
        self.receipt={'schema_version':1,'intent_id':1,'broker':s.BROKER,'bot':'crypto','currency':'USD','account_ref':self.account,'symbol':'BTC-USD','side':'BUY','order_id':self.order,'client_order_id':self.client,'state':'filled','requested_quantity':'1','filled_quantity':'1','fee_total':'0.09','gross_notional':'9','observed_at':self.now.isoformat(),'source_evidence_sha256':HASH,'fills':[{'fill_id':self.fill,'identifier_kind':'BROKER_ISSUED','quantity':'1','price':'9','gross_notional':'9','fee':'0.09','filled_at':(self.now-timedelta(seconds=30)).isoformat()}]}
        self.evidence={'account_ref':self.account,'events':[{'broker_order_id':self.order,'broker_fill_id':self.fill,'account_ref':self.account,'symbol':'BTC-USD','kind':'BUY_FILL','quantity':'1','gross_notional':'9','fee':'0.09'}]}
        with self.db() as db:
            db.executescript(SCHEMA)
            db.execute('INSERT INTO independent_bot_configurations VALUES (?,3,?)',('crypto',json.dumps({'account_ref':self.account,'currency':'USD','allocated_capital':'130','cash_reserve_pct':'10'})))
            old=(self.now-timedelta(minutes=5)).isoformat()
            db.execute("INSERT INTO reconciliation_runs VALUES (1,?,?,?,?,'PASS','[]')",(s.BROKER,self.account,old,old))
            db.execute("INSERT INTO capital_verified_states VALUES ('crypto',?,3,1,'100','100','0','0','0',?)",(self.account,old))
            db.execute("INSERT INTO order_intents VALUES (1,?,'crypto',?,?,'BTC-USD','BUY','1','limit','UNKNOWN',?)",(self.client,s.BROKER,self.account,json.dumps({'quantity_is':'asset_quantity','execution_plan_v2':self.plan})))
            db.execute("INSERT INTO capital_buy_reservations VALUES (?,'crypto',?,'10.1','UNKNOWN')",(self.client,self.account))
            db.execute("INSERT INTO audit_events(event_type,bot_name,correlation_id,details_json) VALUES ('AUTOMATIC_SUBMISSION_CLAIMED','crypto',?,?)",(self.client,json.dumps({'intent_id':1,'payload_sha256':s.hashed(self.payload)})))
    def tearDown(self):self.temp.cleanup()
    def db(self):
        db=sqlite3.connect(self.path);db.row_factory=sqlite3.Row;return db
    def fake_producer(self,db,evidence):
        pending=db.execute("SELECT 1 FROM order_intents WHERE status IN ('SUBMITTING','SUBMITTED','UNKNOWN')").fetchone()
        if pending:raise s.SettlementBlocked('OTHER_LOCAL_ORDER_UNRESOLVED')
        cursor=db.execute("INSERT INTO reconciliation_runs(broker,account_ref,status) VALUES (?,?,'PASS')",(s.BROKER,self.account))
        return {'reconciliation_id':cursor.lastrowid}
    def fake_publisher(self,db,*,bot,reconciliation_id):
        db.execute("UPDATE capital_verified_states SET reconciliation_id=?,eligible_cash='90.91',position_cost='9.09'",(reconciliation_id,));return {'baseline_updated':True}
    def stage(self,producer=None,publisher=None):
        db=self.db();db.execute('BEGIN IMMEDIATE')
        try:return db,s.settle_reconcile_publish_in_transaction(db,[self.receipt],self.evidence,_producer=producer or self.fake_producer,_publisher=publisher or self.fake_publisher)
        except BaseException:db.close();raise
    def apply(self,**kwargs):
        db,result=self.stage(**kwargs)
        try:db.commit();return result
        finally:db.close()
    def unchanged(self):
        with self.db() as db:
            self.assertEqual(db.execute('SELECT status FROM order_intents WHERE id=1').fetchone()[0],'UNKNOWN')
            self.assertEqual(db.execute('SELECT state FROM capital_buy_reservations').fetchone()[0],'UNKNOWN')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM fills').fetchone()[0],0)
    def blocked(self):
        with self.assertRaises(s.SettlementBlocked):self.apply()
        self.unchanged()
    def test_atomic_success(self):
        db,result=self.stage()
        try:
            self.assertTrue(db.in_transaction);self.assertTrue(result['caller_commit_required']);self.unchanged();db.commit()
        finally:db.close()
        with self.db() as db:
            self.assertEqual(db.execute('SELECT status FROM order_intents').fetchone()[0],'FILLED')
            self.assertEqual(db.execute('SELECT state FROM capital_buy_reservations').fetchone()[0],'CLOSED')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM fills').fetchone()[0],1)
    def test_fee_missing_blocks(self):self.receipt['fee_total']=None;self.blocked()
    def test_fill_fee_missing_blocks(self):self.receipt['fills'][0]['fee']=None;self.blocked()
    def test_wrong_account(self):self.receipt['account_ref']='OTHER';self.blocked()
    def test_wrong_client_id(self):self.receipt['client_order_id']=str(uuid.uuid4());self.blocked()
    def test_wrong_symbol(self):self.receipt['symbol']='ETH-USD';self.blocked()
    def test_overfill(self):self.receipt['filled_quantity']='2';self.blocked()
    def test_incomplete_fills(self):self.receipt['fills']=[];self.blocked()
    def test_duplicate_fill(self):self.receipt['fills']*=2;self.blocked()
    def test_no_identifier_kind(self):del self.receipt['fills'][0]['identifier_kind'];self.blocked()
    def test_missing_claim(self):
        with self.db() as db:db.execute("DELETE FROM audit_events WHERE event_type='AUTOMATIC_SUBMISSION_CLAIMED'")
        self.blocked()
    def test_legacy_market_not_migrated(self):
        with self.db() as db:db.execute("UPDATE order_intents SET order_type='market'")
        self.blocked()
    def test_stale_receipt(self):self.receipt['observed_at']=(self.now-timedelta(minutes=2)).isoformat();self.blocked()
    def test_nonterminal_does_not_release(self):self.receipt['state']='open';self.blocked()
    def test_cost_above_reservation(self):
        self.receipt.update(gross_notional='11',fee_total='0.09');self.receipt['fills'][0].update(gross_notional='11');self.evidence['events'][0]['gross_notional']='11';self.blocked()
    def test_fee_above_verified_bound(self):
        self.receipt['fee_total']='0.2';self.receipt['fills'][0]['fee']='0.2';self.evidence['events'][0]['fee']='0.2';self.blocked()
    def test_execution_outside_limit(self):self.receipt['fills'][0]['price']='11';self.blocked()
    def test_accounting_event_missing(self):self.evidence['events']=[];self.blocked()
    def test_accounting_fee_mismatch(self):self.evidence['events'][0]['fee']='0';self.blocked()
    def test_recovery_absent_never_retries(self):
        result=s.select_recovery_candidate(self.client,[]);self.assertEqual(result['status'],'UNRESOLVED');self.assertFalse(result['retry_allowed']);self.assertFalse(result['release_allowed'])
    def test_recovery_ambiguous_never_retries(self):self.assertEqual(s.select_recovery_candidate(self.client,[self.receipt,self.receipt])['status'],'UNRESOLVED')
    def test_recovery_open_waits(self):r=copy.deepcopy(self.receipt);r['state']='open';self.assertEqual(s.select_recovery_candidate(self.client,[r])['status'],'WAITING_FOR_TERMINAL')
    def test_producer_failure_rolls_back_all(self):
        def fail(*a,**k):raise s.SettlementBlocked('test')
        with self.assertRaises(s.SettlementBlocked):self.apply(producer=fail)
        self.unchanged()
    def test_publisher_failure_rolls_back_all(self):
        def fail(*a,**k):raise s.SettlementBlocked('test')
        with self.assertRaises(s.SettlementBlocked):self.apply(publisher=fail)
        self.unchanged()
    def test_dependency_cannot_commit(self):
        def bad(db,evidence):db.commit()
        with self.assertRaises(s.SettlementBlocked):self.apply(producer=bad)
        self.unchanged()
    def test_dependency_cannot_execute_commit(self):
        def bad(db,evidence):db.execute('COMMIT')
        with self.assertRaises(s.SettlementBlocked):self.apply(producer=bad)
        self.unchanged()
    def test_settlement_audit_failure_rolls_back_all(self):
        with self.db() as db:db.execute("CREATE TRIGGER no_settlement BEFORE INSERT ON audit_events WHEN NEW.event_type='CRYPTO_TERMINAL_SETTLED_V1' BEGIN SELECT RAISE(ABORT,'test');END;")
        with self.assertRaises(sqlite3.IntegrityError):self.apply()
        self.unchanged()
    def test_replay_does_not_duplicate_fill(self):
        self.apply();self.apply()
        with self.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM fills').fetchone()[0],1)
    def test_changed_settled_receipt_rejected(self):
        self.apply();self.receipt.update(fee_total='0.08');self.receipt['fills'][0]['fee']='0.08'
        with self.assertRaises(s.SettlementBlocked):self.apply()
    def test_canceled_unfilled_closes_only_after_baseline(self):
        self.receipt.update(state='canceled',filled_quantity='0',fee_total='0',gross_notional='0',fills=[]);self.evidence['events']=[]
        self.apply()
        with self.db() as db:self.assertEqual(db.execute('SELECT status FROM order_intents').fetchone()[0],'CANCELED')
    def test_partial_cancellation_preserves_executed_fill(self):
        self.receipt.update(state='canceled',filled_quantity='0.5',gross_notional='4.5',fee_total='0.045');self.receipt['fills'][0].update(quantity='0.5',gross_notional='4.5',fee='0.045');self.evidence['events'][0].update(quantity='0.5',gross_notional='4.5',fee='0.045')
        self.apply()
        with self.db() as db:self.assertEqual(db.execute('SELECT quantity FROM fills').fetchone()[0],'0.5')
    def test_unknown_fee_never_defaulted_zero_on_cancel(self):self.receipt.update(state='canceled',filled_quantity='0',gross_notional='0',fee_total=None,fills=[]);self.blocked()
    def test_other_unresolved_order_rolls_back_settlement(self):
        with self.db() as db:db.execute("INSERT INTO order_intents(id,broker,account_ref,status) VALUES (2,?,?,'UNKNOWN')",(s.BROKER,self.account))
        with self.assertRaises(s.SettlementBlocked):self.apply()
        self.unchanged()
    def prepare_sell(self):
        self.payload['side']='sell';self.plan['payload_sha256']=s.hashed(self.payload)
        self.receipt.update(side='SELL',gross_notional='11');self.receipt['fills'][0].update(price='11',gross_notional='11')
        self.evidence['events'][0].update(kind='SELL_FILL',gross_notional='11')
        with self.db() as db:
            db.execute("UPDATE order_intents SET side='SELL',metadata_json=? WHERE id=1",(json.dumps({'quantity_is':'asset_quantity','execution_plan_v2':self.plan}),))
            db.execute("UPDATE audit_events SET details_json=? WHERE event_type='AUTOMATIC_SUBMISSION_CLAIMED'",(json.dumps({'intent_id':1,'payload_sha256':s.hashed(self.payload)}),))
            db.execute('DELETE FROM capital_buy_reservations')
            db.execute("INSERT INTO automatic_sell_reservations_v2 VALUES (?,'crypto',?,'BTC-USD','1','UNKNOWN')",(self.client,self.account))
    def test_sell_uses_quantity_reservation(self):
        self.prepare_sell();self.apply()
        with self.db() as db:
            self.assertEqual(db.execute('SELECT state FROM automatic_sell_reservations_v2').fetchone()[0],'CLOSED')
            self.assertEqual(db.execute('SELECT side FROM fills').fetchone()[0],'SELL')
    def test_sell_execution_below_limit_rejected(self):
        self.prepare_sell();self.receipt['fills'][0]['price']='9'
        with self.assertRaises(s.SettlementBlocked):self.apply()
        with self.db() as db:self.assertEqual(db.execute('SELECT state FROM automatic_sell_reservations_v2').fetchone()[0],'UNKNOWN')
    def test_wrong_sell_reservation_rejected(self):
        self.prepare_sell()
        with self.db() as db:db.execute("UPDATE automatic_sell_reservations_v2 SET quantity='0.5'")
        with self.assertRaises(s.SettlementBlocked):self.apply()
        with self.db() as db:self.assertEqual(db.execute('SELECT status FROM order_intents').fetchone()[0],'UNKNOWN')

    def test_existing_producer_and_publisher_in_same_transaction(self):
        # These are the actual review implementations, not the callback fakes.
        self.assertTrue((ROOT/'capital_reconciliation_producer_v1.py').is_file())
        self.assertTrue((ROOT/'capital_baseline_publisher_v1.py').is_file())
        epoch=(self.now-timedelta(hours=1)).isoformat()
        evidence={
            'bot':'crypto','broker':s.BROKER,'currency':'USD','account_ref':self.account,'config_revision':3,
            'observation_started_at':(self.now-timedelta(seconds=3)).isoformat(),'as_of':(self.now-timedelta(seconds=1)).isoformat(),'source_evidence_sha256':HASH,
            'opening':{'bot':'crypto','broker':s.BROKER,'account_ref':self.account,'currency':'USD','as_of':epoch,'funded_capital':'100','cash':'100','realized_net_profit':'0','positions':{},'source_evidence_sha256':HASH},
            'coverage':{k:epoch for k in ('funding_complete_since','fills_complete_since','ownership_complete_since')},
            'events':[dict(self.evidence['events'][0],event_id='settled-fill-'+self.fill,sequence=1,at=self.receipt['fills'][0]['filled_at'],bot='crypto',broker=s.BROKER,currency='USD',source_evidence_sha256=HASH)],
            'broker_observation':{'broker':s.BROKER,'currency':'USD','account_ref':self.account,'buying_power':'90.91','reconciled_bot_cash':'90.91','positions':{'BTC-USD':'1'},'verified_external_positions':{},'source_evidence_sha256':HASH,'external_ownership_evidence_sha256':HASH,'unresolved_order_count':0,'unresolved_ownership_count':0,'unsettled_event_count':0,'open_order_count':0}}
        db=self.db();db.execute('BEGIN IMMEDIATE')
        try:
            result=s.settle_reconcile_publish_in_transaction(db,[self.receipt],evidence)
            self.assertTrue(result['baseline_updated']);self.assertTrue(db.in_transaction)
            self.unchanged();db.commit()
            with self.db() as check:
                row=check.execute('SELECT eligible_cash,position_cost,reconciliation_id FROM capital_verified_states').fetchone()
                self.assertEqual((row[0],row[1]),('90.91','9.09'));self.assertGreater(row[2],1)
                self.assertEqual(check.execute('SELECT state FROM capital_buy_reservations').fetchone()[0],'CLOSED')
        finally:db.close()

if __name__=='__main__':unittest.main(verbosity=2)

