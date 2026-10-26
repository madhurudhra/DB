import unittest
import tempfile
from pathlib import Path
from decimal import Decimal
import exit_attribution_ledger as j

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/"exit.sqlite"
        self.ledger=j.ExitLedger(self.path,{"crypto":"TEST_ACCOUNT"})
    def tearDown(self):self.temp.cleanup()
    def plan(self,cid="A",botq="0",extq="1",**changes):
        d=dict(cid=cid,bot="crypto",account="TEST_ACCOUNT",exchange="CRYPTO",symbol="BTC-USD",product="SPOT",planned_bot_quantity=botq,planned_external_quantity=extq)
        d.update(changes)
        return self.ledger.plan(**d)
    def update(self,cid="A",filled="1",fee="1",status="FILLED",**changes):
        d=dict(cid=cid,bot="crypto",account="TEST_ACCOUNT",broker_id="BROKER_A",status=status,cumulative_filled_quantity=filled,cumulative_average_price="100",cumulative_exit_fee=fee)
        d.update(changes)
        return self.ledger.update(**d)
    def test_manual_sale_and_duplicate(self):
        self.plan();r=self.update()
        self.assertEqual(r["bot_credit_delta"],0)
        self.assertEqual(r["external_credit_delta"],99)
        self.assertEqual(self.update()["external_credit_delta"],0)
        self.assertEqual(self.ledger.attributed_proceeds("crypto"),{"bot":Decimal(0),"external":Decimal(99)})
    def test_restart_retains_split(self):
        self.plan(botq="0.6",extq="0.4")
        self.update(filled="0.5",fee="0.5",status="PARTIALLY_FILLED")
        self.ledger=j.ExitLedger(self.path,{"crypto":"TEST_ACCOUNT"})
        self.assertEqual(self.update()["bot_credit_delta"],Decimal("29.70"))
        self.assertEqual(self.ledger.attributed_proceeds("crypto")["bot"],Decimal("59.4"))
    def test_late_fee(self):
        self.plan(botq="1",extq="0");self.update()
        self.assertEqual(self.update(fee="2")["bot_credit_delta"],-1)
    def test_plan_idempotency(self):
        self.plan();self.assertEqual(self.plan()["cid"],"A")
        with self.assertRaises(ValueError):self.plan(botq="1",extq="0")
    def test_unresolved_prevents_duplicate_exit(self):
        self.plan()
        with self.assertRaises(ValueError):self.plan(cid="B")
    def test_account_binding(self):
        with self.assertRaises(ValueError):self.plan(account="OTHER")
        with self.assertRaises(ValueError):j.ExitLedger(self.path,{"crypto":"OTHER"})
    def test_broker_id_binding(self):
        self.plan();self.update()
        with self.assertRaises(ValueError):self.update(broker_id="OTHER")
    def test_overfill_and_decrease(self):
        self.plan()
        with self.assertRaises(ValueError):self.update(filled="2")
        self.update(filled="0.5",fee="0.5",status="PARTIALLY_FILLED")
        with self.assertRaises(ValueError):self.update(filled="0.4",status="PARTIALLY_FILLED")
    def test_terminal_not_reopened(self):
        self.plan();self.update()
        with self.assertRaises(ValueError):self.update(status="PENDING")
    def test_invalid_status_rolls_back(self):
        self.plan()
        with self.assertRaises(ValueError):self.update(filled="0.5",status="FILLED")
        self.assertEqual(self.ledger.attributed_proceeds("crypto")["external"],0)
        self.update();self.assertEqual(self.ledger.attributed_proceeds("crypto")["external"],99)

if __name__=="__main__":unittest.main()
