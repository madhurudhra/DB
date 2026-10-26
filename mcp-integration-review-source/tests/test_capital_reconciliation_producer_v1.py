import copy
from datetime import datetime,timedelta,timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('reconciliation_producer',ROOT/'capital_reconciliation_producer_v1.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
HASH=hashlib.sha256(b'SYNTHETIC TEST DATA NOT BROKER EVIDENCE').hexdigest()
SCHEMA="""
CREATE TABLE independent_bot_configurations(bot TEXT PRIMARY KEY,revision INTEGER,config_json TEXT);
CREATE TABLE reconciliation_runs(id INTEGER PRIMARY KEY,broker TEXT,account_ref TEXT,started_at TEXT,finished_at TEXT,status TEXT,discrepancies_json TEXT);
CREATE TABLE audit_events(id INTEGER PRIMARY KEY,event_type TEXT,bot_name TEXT,correlation_id TEXT,details_json TEXT);
CREATE TABLE capital_buy_reservations(bot TEXT,account_ref TEXT,state TEXT);
CREATE TABLE order_intents(broker TEXT,account_ref TEXT,status TEXT);
"""
class ProducerTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc);self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'test.sqlite3'
        self.epoch=(self.now-timedelta(hours=1)).isoformat()
        self.data={'bot':'crypto','broker':p.BROKER,'currency':'USD','account_ref':'TEST-5576','config_revision':2,
            'observation_started_at':(self.now-timedelta(seconds=3)).isoformat(),'as_of':(self.now-timedelta(seconds=1)).isoformat(),'source_evidence_sha256':HASH,
            'opening':{'bot':'crypto','broker':p.BROKER,'currency':'USD','account_ref':'TEST-5576','as_of':self.epoch,'funded_capital':'100','cash':'100','realized_net_profit':'0','positions':{},'source_evidence_sha256':HASH},
            'coverage':{key:self.epoch for key in ('funding_complete_since','fills_complete_since','ownership_complete_since')},'events':[],
            'broker_observation':{'account_ref':'TEST-5576','broker':p.BROKER,'currency':'USD','buying_power':'100','reconciled_bot_cash':'100','positions':{},'verified_external_positions':{},'unresolved_order_count':0,'unresolved_ownership_count':0,'unsettled_event_count':0,'open_order_count':0,'source_evidence_sha256':HASH,'external_ownership_evidence_sha256':HASH}}
        with self.db() as db:
            db.executescript(SCHEMA)
            db.execute('INSERT INTO independent_bot_configurations VALUES (?,2,?)',('crypto',json.dumps({'account_ref':'TEST-5576','currency':'USD','allocated_capital':'150'})))
    def tearDown(self):self.temp.cleanup()
    def db(self):
        db=sqlite3.connect(self.path);db.row_factory=sqlite3.Row;return db
    def event(self,kind,**fields):
        i=len(self.data['events'])+1
        result={'event_id':'test-'+str(i),'sequence':i,'kind':kind,'at':(self.now-timedelta(minutes=30-i)).isoformat(),'broker':p.BROKER,'bot':'crypto','account_ref':'TEST-5576','currency':'USD','source_evidence_sha256':HASH,**fields}
        self.data['events'].append(result);return result
    def calculate(self):return p.calculate_result(self.data)
    def record(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');return p.produce_result_in_transaction(db,self.data)
    def rejected(self):
        with self.assertRaises(p.ReconciliationBlocked):self.calculate()
    def test_opening_balance(self):
        r=self.calculate();self.assertEqual(r['funded_capital'],'100');self.assertEqual(r['eligible_cash'],'100');self.assertTrue(all(r['checks'].values()))
    def test_allocation_is_not_funding(self):
        r=self.record()['result'];self.assertEqual(r['funded_capital'],'100');self.assertNotEqual(r['funded_capital'],'150')
    def test_cash_credit(self):
        self.event('CAPITAL_CREDIT',amount='20');self.data['broker_observation'].update(reconciled_bot_cash='120',buying_power='120');self.assertEqual(self.calculate()['funded_capital'],'120')
    def test_cash_debit(self):
        self.event('CAPITAL_DEBIT',amount='20');self.data['broker_observation']['reconciled_bot_cash']='80';self.assertEqual(self.calculate()['funded_capital'],'80')
    def test_buy_and_partial_sell(self):
        self.event('BUY_FILL',symbol='BTC-USD',quantity='2',gross_notional='20',fee='1')
        self.event('SELL_FILL',symbol='BTC-USD',quantity='1',gross_notional='15',fee='0.5')
        self.data['broker_observation'].update(reconciled_bot_cash='93.5',positions={'BTC-USD':'1'})
        r=self.calculate();self.assertEqual(r['position_cost'],'10.500000000000');self.assertEqual(r['realized_net_profit'],'4.000000000000');self.assertEqual(r['eligible_cash'],'93.5')
    def test_full_close_cost_zero(self):
        self.event('BUY_FILL',symbol='BTC-USD',quantity='2',gross_notional='20',fee='1')
        self.event('SELL_FILL',symbol='BTC-USD',quantity='2',gross_notional='30',fee='1')
        self.data['broker_observation'].update(reconciled_bot_cash='108',buying_power='108')
        r=self.calculate();self.assertEqual(r['position_cost'],'0');self.assertEqual(r['realized_net_profit'],'8')
    def test_loss_is_retained(self):
        self.event('BUY_FILL',symbol='BTC-USD',quantity='2',gross_notional='20',fee='1')
        self.event('SELL_FILL',symbol='BTC-USD',quantity='2',gross_notional='15',fee='1')
        self.data['broker_observation']['reconciled_bot_cash']='93';self.assertEqual(self.calculate()['realized_net_profit'],'-7')
    def test_zero_buying_power_does_not_invent_cash(self):
        self.data['broker_observation']['buying_power']='0';self.assertEqual(self.calculate()['eligible_cash'],'0')
    def test_verified_external_holding_not_adopted(self):
        self.data['broker_observation'].update(positions={'BTC-USD':'1'},verified_external_positions={'BTC-USD':'1'})
        self.assertEqual(self.calculate()['position_cost'],'0')
    def test_unattributed_position(self):self.data['broker_observation']['positions']={'BTC-USD':'1'};self.rejected()
    def test_missing_actual_fee(self):self.event('BUY_FILL',symbol='BTC-USD',quantity='1',gross_notional='1',fee=None);self.rejected()
    def test_missing_opening(self):del self.data['opening'];self.rejected()
    def test_wrong_opening_account(self):self.data['opening']['account_ref']='OTHER';self.rejected()
    def test_wrong_event_account(self):self.event('CAPITAL_CREDIT',amount='20')['account_ref']='OTHER';self.rejected()
    def test_history_gap(self):self.data['coverage']['funding_complete_since']=self.data['as_of'];self.rejected()
    def test_missing_coverage(self):del self.data['coverage'];self.rejected()
    def test_duplicate_event(self):e=self.event('CAPITAL_CREDIT',amount='1');self.data['events'].append(copy.deepcopy(e));self.rejected()
    def test_invalid_event_order(self):self.event('CAPITAL_CREDIT',amount='1')['sequence']=-1;self.rejected()
    def test_buys_cannot_exceed_attributed_cash(self):self.event('BUY_FILL',symbol='BTC-USD',quantity='1',gross_notional='101',fee='0');self.rejected()
    def test_cannot_sell_unowned(self):self.event('SELL_FILL',symbol='BTC-USD',quantity='1',gross_notional='1',fee='0');self.rejected()
    def test_current_cash_partition_mismatch(self):self.data['broker_observation']['reconciled_bot_cash']='99';self.rejected()
    def test_stale_broker_observation(self):self.data['as_of']=(self.now-timedelta(minutes=2)).isoformat();self.rejected()
    def test_open_order_requires_handling(self):self.data['broker_observation']['open_order_count']=1;self.rejected()
    def test_unsettled_event_requires_handling(self):self.data['broker_observation']['unsettled_event_count']=1;self.rejected()
    def test_boolean_count_rejected(self):self.data['broker_observation']['unresolved_order_count']=False;self.rejected()
    def test_nan_rejected(self):self.data['opening']['cash']='NaN';self.rejected()
    def test_float_rejected(self):self.data['opening']['cash']=100.0;self.rejected()
    def test_valid_record_is_visible_after_commit(self):
        r=self.record();self.assertEqual(r['status'],'RECONCILIATION_RESULT_RECORDED')
        with self.db() as db:
            self.assertEqual(db.execute('SELECT status FROM reconciliation_runs').fetchone()[0],'PASS')
            proof=json.loads(db.execute('SELECT details_json FROM audit_events').fetchone()[0]);self.assertEqual(proof['reconciliation_id'],r['reconciliation_id'])
    def test_bad_data_never_records_pass(self):
        self.data['opening']['cash']='99'
        with self.assertRaises(p.ReconciliationBlocked):self.record()
        with self.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM reconciliation_runs').fetchone()[0],0)
    def test_local_unknown_blocks(self):
        with self.db() as db:db.execute("INSERT INTO order_intents VALUES ('robinhood_crypto','TEST-5576','UNKNOWN')")
        with self.assertRaises(p.ReconciliationBlocked):self.record()
    def test_local_held_reservation_blocks(self):
        with self.db() as db:db.execute("INSERT INTO capital_buy_reservations VALUES ('crypto','TEST-5576','HELD')")
        with self.assertRaises(p.ReconciliationBlocked):self.record()
    def test_changed_configuration_blocks(self):
        with self.db() as db:db.execute('UPDATE independent_bot_configurations SET revision=3')
        with self.assertRaises(p.ReconciliationBlocked):self.record()
    def test_failed_audit_rolls_back_pass(self):
        with self.db() as db:db.execute("CREATE TRIGGER fail BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT,'test');END;")
        with self.assertRaises(sqlite3.IntegrityError):self.record()
        with self.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM reconciliation_runs').fetchone()[0],0)
    def test_no_commit_inside_producer(self):
        db=self.db()
        try:
            db.execute('BEGIN IMMEDIATE');p.produce_result_in_transaction(db,self.data);self.assertTrue(db.in_transaction)
            with self.db() as other:self.assertEqual(other.execute('SELECT COUNT(*) FROM reconciliation_runs').fetchone()[0],0)
            db.rollback()
        finally:db.close()
    def test_producer_and_existing_publisher_same_transaction(self):
        path=ROOT/'capital_baseline_publisher_v1.py'
        self.assertTrue(path.is_file(),'Previous publisher review module is required')
        spec=importlib.util.spec_from_file_location('publisher_for_producer_test',path)
        pub=importlib.util.module_from_spec(spec);spec.loader.exec_module(pub)
        db=self.db()
        try:
            db.execute('CREATE TABLE capital_verified_states (\n              bot TEXT NOT NULL,account_ref TEXT NOT NULL,config_revision INTEGER NOT NULL,\n              reconciliation_id INTEGER NOT NULL,funded_capital TEXT NOT NULL,\n              eligible_cash TEXT NOT NULL,position_cost TEXT NOT NULL,\n              realized_net_profit TEXT NOT NULL,baseline_pending_buy_commitment TEXT NOT NULL,\n              as_of TEXT NOT NULL,PRIMARY KEY(bot,account_ref),\n              FOREIGN KEY(reconciliation_id) REFERENCES reconciliation_runs(id))')
            db.commit();db.execute('BEGIN IMMEDIATE')
            result=p.produce_result_in_transaction(db,self.data)
            published=pub.publish_capital_baseline_in_transaction(db,bot='crypto',reconciliation_id=result['reconciliation_id'])
            self.assertTrue(published['baseline_updated']);self.assertTrue(db.in_transaction)
            with self.db() as other:self.assertEqual(other.execute('SELECT COUNT(*) FROM reconciliation_runs').fetchone()[0],0)
            db.commit()
            with self.db() as other:
                state=other.execute('SELECT funded_capital,eligible_cash FROM capital_verified_states').fetchone()
                self.assertEqual(tuple(state),('100','100'))
        finally:db.close()

if __name__=='__main__':unittest.main(verbosity=2)

