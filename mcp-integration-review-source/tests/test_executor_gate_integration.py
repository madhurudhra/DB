"""Actual gate + executor interface tests; simulated capital reader and broker."""
import unittest
import test_enforcement as foundation
import test_crypto_executor_v2 as executor_tests

class ExecutorGateIntegrationTests(unittest.TestCase):
    db=foundation.EnforcementTests.db
    tearDown=foundation.EnforcementTests.tearDown
    def setUp(self):
        foundation.EnforcementTests.setUp(self)
        import os
        os.environ['ROBINHOOD_CRYPTO_WRITE_ENABLED']='true'
        with self.db() as db:
            for name in ('status','submitted_at','response_json'):
                db.execute('ALTER TABLE broker_orders ADD COLUMN '+name+' TEXT')
            db.execute('CREATE UNIQUE INDEX fixture_unique_broker_id ON broker_orders(broker,broker_order_id)')
        self.adapter=executor_tests.FakeAdapter(self.payload,self.account)
    def execute(self):
        return executor_tests.worker.execute_prepared_crypto(foundation.settings.database_path,1,adapter=self.adapter)
    def test_actual_conditional_gate_to_executor(self):
        result=self.execute()
        self.assertEqual(result['outcome'],'RESPONSE_RECORDED',result)
        self.assertEqual(self.adapter.sent,[self.payload])
        with self.db() as db:
            self.assertEqual(db.execute('SELECT status FROM order_intents WHERE id=1').fetchone()[0],'SUBMITTED')
            self.assertEqual(db.execute('SELECT amount_total,state FROM capital_buy_reservations').fetchone()[0],'1.01')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM broker_orders').fetchone()[0],1)
    def test_actual_gate_denies_without_risk(self):
        with self.db() as db:db.execute('DELETE FROM risk_checks')
        result=self.execute();self.assertEqual(result['outcome'],'BLOCKED',result);self.assertEqual(self.adapter.sent,[])
    def test_actual_claim_then_timeout_is_not_retried(self):
        self.adapter.error=True
        result=self.execute();self.assertEqual(result['outcome'],'UNKNOWN',result)
        self.execute();self.assertEqual(len(self.adapter.sent),1)
        with self.db() as db:
            self.assertEqual(db.execute('SELECT status FROM order_intents WHERE id=1').fetchone()[0],'UNKNOWN')
            self.assertEqual(db.execute('SELECT state FROM capital_buy_reservations').fetchone()[0],'UNKNOWN')
    def test_actual_gate_keeps_filled_response_unsettled(self):
        self.adapter.response['state']='filled';result=self.execute()
        self.assertEqual(result['outcome'],'RESPONSE_RECORDED',result);self.assertFalse(result['settlement_complete'])
        with self.db() as db:self.assertEqual(db.execute('SELECT state FROM capital_buy_reservations').fetchone()[0],'HELD')

