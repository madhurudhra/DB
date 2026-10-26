import concurrent.futures
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
import uuid
from datetime import datetime,timezone,timedelta
from decimal import Decimal

ROOT=Path(__file__).resolve().parents[1]
for name in ('app','app.core'):
    m=types.ModuleType(name);m.__path__=[];sys.modules[name]=m
settings=types.SimpleNamespace(database_path='',live_trading_enabled=True)
config_module=types.ModuleType('app.core.config');config_module.get_settings=lambda:settings
sys.modules['app.core.config']=config_module

def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module);return module

guard=load('app.core.automatic_order_guard','automatic_order_guard.py')
policy=load('app.core.write_policy','write_policy.py')
# Simulated capital-reader boundary. This is NOT a live broker or a test
# of the deployed capital_ledger_v1 implementation.
capital=types.ModuleType('app.core.capital_ledger_v1')
def snapshot(db,bot):
    row=db.execute('SELECT * FROM capital_verified_states WHERE bot=?',(bot,)).fetchone()
    return {'reservation_checks_ready':row is not None,'available_automatic_budget':row['budget'] if row else None}
def reserve(db,*,bot,account_ref,request_id,amount_total):
    db.execute("INSERT INTO capital_buy_reservations VALUES (?,?,?,?,'HELD')",(request_id,bot,account_ref,amount_total))
    return {'state':'HELD','replayed':False}
capital.snapshot=snapshot;capital.reserve_buy=reserve
sys.modules['app.core.capital_ledger_v1']=capital

SCHEMA="\nCREATE TABLE dashboard_control_state(singleton INTEGER PRIMARY KEY,revision INTEGER,state_json TEXT);\nCREATE TABLE independent_bot_configurations(bot TEXT PRIMARY KEY,revision INTEGER,config_json TEXT);\nCREATE TABLE order_intents(id INTEGER PRIMARY KEY,idempotency_key TEXT UNIQUE,bot_name TEXT,broker TEXT,account_ref TEXT,symbol TEXT,side TEXT,quantity TEXT,order_type TEXT,status TEXT,created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),decision_id INTEGER,metadata_json TEXT);\nCREATE TABLE decisions(id INTEGER PRIMARY KEY,bot_name TEXT,symbol TEXT,decision TEXT);\nCREATE TABLE risk_checks(id INTEGER PRIMARY KEY,decision_id INTEGER,bot_name TEXT,passed INTEGER,checked_at TEXT,rules_version TEXT);\nCREATE TABLE reconciliation_runs(id INTEGER PRIMARY KEY,broker TEXT,account_ref TEXT,status TEXT,finished_at TEXT,discrepancies_json TEXT);\nCREATE TABLE broker_orders(id INTEGER PRIMARY KEY,intent_id INTEGER,broker TEXT,broker_order_id TEXT);\nCREATE TABLE fills(id INTEGER PRIMARY KEY,broker TEXT,account_ref TEXT,symbol TEXT,broker_order_id TEXT,side TEXT,quantity TEXT);\nCREATE TABLE position_snapshots(id INTEGER PRIMARY KEY,broker TEXT,account_ref TEXT,symbol TEXT,quantity TEXT,observed_at TEXT,raw_json TEXT);\nCREATE TABLE capital_verified_states(bot TEXT PRIMARY KEY,account_ref TEXT,reconciliation_id INTEGER,budget TEXT);\nCREATE TABLE capital_buy_reservations(request_id TEXT PRIMARY KEY,bot TEXT,account_ref TEXT,amount_total TEXT,state TEXT);\nCREATE TABLE audit_events(id INTEGER PRIMARY KEY,event_type TEXT,bot_name TEXT,correlation_id TEXT,details_json TEXT);\n"

class EnforcementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();settings.database_path=str(Path(self.temp.name)/'test.sqlite3');settings.live_trading_enabled=True
        self.original_env=dict(os.environ)
        os.environ.update(WRITE_ENABLED='true',CRYPTO_BOT_ENABLED='true',RUNNER_MAX_ORDER_USD='10',RUNNER_MAX_ORDERS_PER_DAY='3')
        self.now=datetime.now(timezone.utc).isoformat();self.bot='crypto';self.symbol='BTC-USD';self.account='test-account';self.side='BUY'
        self.config={'account_ref':self.account,'currency':'USD','allocated_capital':'100','cash_reserve_pct':'10','buy_restrictions':[],'sell_protections':[]}
        self.states={'crypto':{'bot_enabled':True,'buy_enabled':True,'sell_enabled':True}}
        self.id=str(uuid.uuid4())
        self.payload={'client_order_id':self.id,'symbol':self.symbol,'side':'buy','type':'limit','limit_order_config':{'asset_quantity':'0.1','limit_price':'10'}}
        self.plan={'config_revision':1,'control_revision':1,'prepared_at':self.now,'quote_at':self.now,'fees_at':self.now,'account_verified_at':self.now,'instrument_verified_at':self.now,'account_ref':self.account,'asset_quantity':'0.1','limit_price':'10','asset_increment':'0.01','quote_increment':'0.01','min_order_amount':'0.5','max_order_size':'100','fee_ratio':'0.01','fee_bound':'0.01','other_charges_bound':'0','reservation_total':'1.01','payload':self.payload,'payload_sha256':hashlib.sha256(guard.canonical(self.payload).encode()).hexdigest()}
        with self.db() as db:
            db.executescript(SCHEMA)
            db.execute('INSERT INTO dashboard_control_state VALUES (1,1,?)',(json.dumps(self.states),))
            db.execute('INSERT INTO independent_bot_configurations VALUES (?,1,?)',(self.bot,json.dumps(self.config)))
            db.execute('INSERT INTO decisions VALUES (1,?,?,?)',(self.bot,self.symbol,self.side))
            db.execute('INSERT INTO risk_checks VALUES (1,1,?,1,?,?)',(self.bot,self.now,'test-fixture-v1'))
            db.execute("INSERT INTO reconciliation_runs VALUES (1,?,?,'PASS',?,'[]')",('robinhood_crypto',self.account,self.now))
            db.execute('INSERT INTO capital_verified_states VALUES (?,?,1,?)',(self.bot,self.account,'90'))
            db.execute("INSERT INTO order_intents(id,idempotency_key,bot_name,broker,account_ref,symbol,side,quantity,order_type,status,decision_id,metadata_json) VALUES (1,?,?,?,?,?,?,'0.1','limit','CREATED',1,?)",(self.id,self.bot,'robinhood_crypto',self.account,self.symbol,self.side,json.dumps({'quantity_is':'asset_quantity','execution_plan_v2':self.plan})))
    def tearDown(self):os.environ.clear();os.environ.update(self.original_env);self.temp.cleanup()
    def db(self):
        db=sqlite3.connect(settings.database_path);db.row_factory=sqlite3.Row;return db
    def save(self):
        self.plan['payload_sha256']=hashlib.sha256(guard.canonical(self.payload).encode()).hexdigest()
        with self.db() as db:
            db.execute('UPDATE independent_bot_configurations SET config_json=?',(json.dumps(self.config),))
            db.execute('UPDATE dashboard_control_state SET state_json=?',(json.dumps(self.states),))
            db.execute('UPDATE order_intents SET metadata_json=? WHERE id=1',(json.dumps({'quantity_is':'asset_quantity','execution_plan_v2':self.plan}),))
    def call(self,**kwargs):
        values=dict(side=self.side,symbol=self.symbol,account_ref=self.account,intent_id=1);values.update(kwargs)
        return guard.automatic_order_preflight(self.bot,**values)
    def assertBlocked(self,reason=None):
        result=self.call();self.assertFalse(result[0],result)
        if reason:self.assertEqual(result[1],reason)
        with self.db() as db:
            self.assertEqual(db.execute('SELECT status FROM order_intents WHERE id=1').fetchone()[0],'CREATED')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM capital_buy_reservations').fetchone()[0],0)
    def test_valid_buy_claim(self):
        self.assertEqual(self.call(),(True,'SUBMISSION_CLAIM_ACQUIRED'))
        with self.db() as db:
            self.assertEqual(db.execute('SELECT status FROM order_intents WHERE id=1').fetchone()[0],'SUBMITTING')
            self.assertEqual(db.execute('SELECT amount_total FROM capital_buy_reservations').fetchone()[0],'1.01')
    def test_replay_denied(self):self.assertTrue(self.call()[0]);self.assertFalse(self.call()[0])
    def test_concurrent_claim_single_winner(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.call(),range(2)))
        self.assertEqual(sum(r[0] for r in results),1,results)
    def test_missing_intent(self):self.assertEqual(self.call(intent_id=None),(False,'ORDER_INTENT_CONTEXT_REQUIRED'))
    def test_buy_disabled(self):self.states['crypto']['buy_enabled']=False;self.save();self.assertBlocked('AUTOMATIC_BUY_NOT_ENABLED')
    def test_buy_restricted(self):self.config['buy_restrictions']=[{'symbol':self.symbol,'mode':'block_all'}];self.save();self.assertBlocked('SYMBOL_AUTOMATIC_BUY_RESTRICTED')
    def test_changed_configuration(self):self.plan['config_revision']=0;self.save();self.assertBlocked('PLAN_CONFIGURATION_CHANGED')
    def test_changed_controls(self):self.plan['control_revision']=0;self.save();self.assertBlocked('PLAN_CONTROLS_CHANGED')
    def test_stale_quote(self):self.plan['quote_at']=(datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat();self.save();self.assertBlocked('EVIDENCE_STALE_OR_FUTURE_DATED')
    def test_future_quote(self):self.plan['quote_at']=(datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat();self.save();self.assertBlocked('EVIDENCE_STALE_OR_FUTURE_DATED')
    def test_fee_understatement(self):self.plan['fee_bound']='0';self.plan['reservation_total']='1';self.save();self.assertBlocked('FEE_BOUND_INSUFFICIENT')
    def test_invalid_increment(self):self.plan['asset_increment']='0.03';self.save();self.assertBlocked('BROKER_INCREMENT_VIOLATION')
    def test_market_order_rejected(self):
        with self.db() as db:db.execute("UPDATE order_intents SET order_type='market' WHERE id=1")
        self.assertBlocked('BOUNDED_LIMIT_ORDER_REQUIRED')
    def test_missing_risk_record(self):
        with self.db() as db:db.execute('DELETE FROM risk_checks')
        self.assertBlocked('RECORDED_RISK_CHECK_REQUIRED')
    def test_failed_reconciliation(self):
        with self.db() as db:db.execute("UPDATE reconciliation_runs SET status='FAIL'")
        self.assertBlocked('PASSING_ACCOUNT_RECONCILIATION_REQUIRED')
    def test_insufficient_capital(self):
        with self.db() as db:db.execute("UPDATE capital_verified_states SET budget='0.5'")
        self.assertBlocked('INSUFFICIENT_AUTOMATIC_BUDGET')
    def test_payload_mismatch(self):self.payload['symbol']='OTHER-USD';self.save();self.assertBlocked('PAYLOAD_IDENTITY_MISMATCH')
    def test_pending_order(self):
        with self.db() as db:db.execute("INSERT INTO order_intents(id,bot_name,broker,account_ref,status) VALUES (2,'crypto','robinhood_crypto',?,'UNKNOWN')",(self.account,))
        self.assertBlocked('PENDING_OR_AMBIGUOUS_ORDER_REQUIRES_RECONCILIATION')
    def test_audit_failure_rolls_back_claim_and_reservation(self):
        with self.db() as db:db.execute("CREATE TRIGGER no_audit BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT,'test'); END")
        self.assertBlocked()
    def prepare_sell(self):
        self.side='SELL';self.payload['side']='sell';self.states['crypto']['bot_enabled']=False;self.states['crypto']['buy_enabled']=False;self.save()
        with self.db() as db:
            db.execute("UPDATE order_intents SET side='SELL' WHERE id=1");db.execute("UPDATE decisions SET decision='SELL'")
            db.execute("INSERT INTO order_intents(id,bot_name,broker,account_ref,symbol,side,status,created_at) VALUES (2,'crypto','robinhood_crypto',?,?,'BUY','FILLED','2020-01-01T00:00:00Z')",(self.account,self.symbol))
            db.execute("INSERT INTO broker_orders VALUES (1,2,'robinhood_crypto','owned-buy')")
            db.execute("INSERT INTO fills VALUES (1,'robinhood_crypto',?,?,'owned-buy','BUY','1')",(self.account,self.symbol))
            db.execute("INSERT INTO position_snapshots VALUES (1,'robinhood_crypto',?,?,'1',?,?)",(self.account,self.symbol,self.now,json.dumps({'verified_sellable_quantity':'1'})))
    def test_sell_allowed_with_entry_disabled(self):self.prepare_sell();self.assertTrue(self.call()[0])
    def test_sell_full_protection(self):self.prepare_sell();self.config['sell_protections']=[{'symbol':self.symbol,'mode':'all'}];self.save();self.assertBlocked('POSITION_FULLY_SELL_PROTECTED')
    def test_sell_partial_protection(self):self.prepare_sell();self.config['sell_protections']=[{'symbol':self.symbol,'mode':'quantity','quantity':'0.95'}];self.save();self.assertBlocked('INSUFFICIENT_UNPROTECTED_BOT_OWNED_QUANTITY')
    def test_external_holdings_not_assumed_owned(self):
        self.prepare_sell()
        with self.db() as db:db.execute('DELETE FROM fills')
        self.assertBlocked('INSUFFICIENT_UNPROTECTED_BOT_OWNED_QUANTITY')
    def test_policy_legacy_call_fails_closed(self):
        result=policy.can_submit_order('crypto',risk_passed=True,market_calendar_passed=True,account_verified=True,reconciliation_healthy=True,durable_order_intent_saved=True,side='BUY',symbol=self.symbol,account_ref=self.account)
        self.assertEqual(result,(False,'ORDER_INTENT_CONTEXT_REQUIRED'))
    def test_policy_rejects_truthy_non_boolean(self):
        result=policy.can_submit_order('crypto',risk_passed=1,market_calendar_passed=True,account_verified=True,reconciliation_healthy=True,durable_order_intent_saved=True,side='BUY',symbol=self.symbol,account_ref=self.account,intent_id=1)
        self.assertEqual(result,(False,'RISK_CHECK_FAILED'))
    def test_policy_uses_actual_conditional_claim(self):
        result=policy.can_submit_order('crypto',risk_passed=True,market_calendar_passed=True,account_verified=True,reconciliation_healthy=True,durable_order_intent_saved=True,side='BUY',symbol=self.symbol,account_ref=self.account,intent_id=1)
        self.assertEqual(result,(True,'SUBMISSION_CLAIM_ACQUIRED'))

if __name__=='__main__':unittest.main(verbosity=2)
