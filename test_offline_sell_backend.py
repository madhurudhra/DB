import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
import offline_sell_backend as backend

class FakeBroker:
    def __init__(self):self.calls=[];self.timeout=False;self.fresh=True;self.pending=False
    def revalidate(self,snapshot):return self.fresh
    def submit(self,plan):
        self.calls.append(plan)
        if self.timeout:raise TimeoutError("simulated uncertain submission")
        return {"cid":plan["cid"],"account":plan["account"],"broker_id":"TEST_"+plan["cid"],
            "state":"PENDING" if self.pending else "FILLED",
            "filled":"0" if self.pending else plan["quantity"],
            "average":"0" if self.pending else "100","fee":"0" if self.pending else "1"}

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/"exits.sqlite"
        self.accounts={bot:"TEST_"+bot for bot in backend.BOTS}
        self.ledger=backend.Ledger(self.path,self.accounts)
        self.broker=FakeBroker();self.enabled=True
        self.now=datetime.now(timezone.utc)
        @contextmanager
        def guard(bot):yield self.enabled
        self.coordinator=backend.Coordinator(self.ledger,self.broker,guard,lambda:self.now)
    def tearDown(self):self.temp.cleanup()
    def snapshot(self,bot="crypto",**changes):
        data=dict(bot=bot,account=self.accounts[bot],exchange="TEST_EXCHANGE",symbol="TEST",product="LONG",
            snapshot_id="SNAPSHOT_1",reference_id="VERIFIED_REFERENCE_1",as_of=self.now,
            reconciled=True,pending_order=False,reference_verified=True,execution_approved=True,
            held_quantity="1",available_quantity="1",quantity="1",bot_quantity="0",external_quantity="1",
            entry_cost_for_exit_quantity="100",estimated_net_credit="106",quantity_increment="0.01")
        data.update(changes);return data
    def test_three_bots_exit_and_external_accounting(self):
        for bot in backend.BOTS:
            with self.subTest(bot=bot):
                r=self.coordinator.step(self.snapshot(bot))
                self.assertEqual(r["state"],"FILLED")
                self.assertEqual(self.ledger.totals(bot),{"bot":Decimal(0),"external":Decimal(99)})
        self.assertEqual(len(self.broker.calls),3)
    def test_threshold_boundaries(self):
        for bot in backend.BOTS:
            for credit,state in [("98","EXIT_DUE"),("99","EXIT_DUE"),("99.001","HOLD"),("100","HOLD"),("105.999","HOLD"),("106","EXIT_DUE")]:
                with self.subTest(bot=bot,credit=credit):self.assertEqual(self.coordinator.evaluate(self.snapshot(bot,estimated_net_credit=credit))["state"],state)
    def test_sell_off_no_order_or_credit(self):
        self.enabled=False
        self.assertEqual(self.coordinator.step(self.snapshot())["reason"],"SELL_OFF")
        self.assertEqual(len(self.broker.calls),0)
        self.assertEqual(self.ledger.totals("crypto")["external"],0)
    def test_same_snapshot_not_resubmitted(self):
        s=self.snapshot();self.coordinator.step(s)
        self.assertEqual(self.coordinator.step(s)["reason"],"EXISTING_INTENT")
        self.assertEqual(len(self.broker.calls),1)
    def test_timeout_blocks_retry_even_with_new_snapshot(self):
        self.broker.timeout=True
        with self.assertRaises(TimeoutError):self.coordinator.step(self.snapshot())
        self.assertEqual(self.coordinator.step(self.snapshot(snapshot_id="NEW_SNAPSHOT"))["reason"],"EXISTING_INTENT")
        self.assertEqual(len(self.broker.calls),1)
    def test_pending_receipt_no_premature_credit(self):
        self.broker.pending=True;r=self.coordinator.step(self.snapshot())
        self.assertEqual(r["state"],"PENDING")
        self.assertEqual(self.ledger.totals("crypto")["external"],0)
    def test_duplicate_fill_and_late_fee(self):
        self.broker.pending=True;r=self.coordinator.step(self.snapshot(bot_quantity="0.6",external_quantity="0.4"));cid=r["cid"]
        receipt=dict(cid=cid,account=self.accounts["crypto"],broker_id="TEST_"+cid,state="FILLED",filled="1",average="100",fee="1")
        self.ledger.reconcile(cid,receipt)
        self.assertEqual(self.ledger.reconcile(cid,receipt)["bot"],0)
        receipt["fee"]="2"
        self.assertEqual(self.ledger.reconcile(cid,receipt)["bot"],Decimal("-0.6"))
        self.assertEqual(self.ledger.totals("crypto"),{"bot":Decimal("58.8"),"external":Decimal("39.2")})
    def test_broker_recheck_failure_no_submit(self):
        self.broker.fresh=False
        self.assertEqual(self.coordinator.step(self.snapshot())["reason"],"PRE_SUBMISSION_RECHECK")
        self.assertEqual(len(self.broker.calls),0)
    def test_snapshot_blocks(self):
        for changes in ({"reconciled":False},{"pending_order":True},{"reference_verified":False},{"execution_approved":False},{"as_of":self.now-timedelta(seconds=31)}):
            with self.subTest(changes=changes):self.assertEqual(self.coordinator.step(self.snapshot(**changes))["state"],"BLOCKED")
        self.assertEqual(len(self.broker.calls),0)
    def test_quantity_and_account_fail_closed(self):
        for changes in ({"account":"OTHER"},{"quantity":"2"},{"available_quantity":"0"},{"bot_quantity":"1"},{"quantity_increment":"0.3"},{"estimated_net_credit":"NaN"}):
            with self.subTest(changes=changes):
                with self.assertRaises(backend.BackendError):self.coordinator.step(self.snapshot(**changes))
        self.assertEqual(len(self.broker.calls),0)
    def test_fractional_position(self):
        s=self.snapshot(held_quantity="0.00005973",available_quantity="0.00005973",quantity="0.00005973",external_quantity="0.00005973",entry_cost_for_exit_quantity="5",estimated_net_credit="5.30",quantity_increment="0.00000001")
        self.assertEqual(self.coordinator.step(s)["state"],"FILLED")
        self.assertEqual(self.broker.calls[0]["quantity"],"0.00005973")
    def test_restart_does_not_retry_uncertain(self):
        self.broker.timeout=True
        with self.assertRaises(TimeoutError):self.coordinator.step(self.snapshot())
        reopened=backend.Ledger(self.path,self.accounts)
        self.coordinator.ledger=reopened
        self.assertEqual(self.coordinator.step(self.snapshot(snapshot_id="AFTER_REOPEN"))["reason"],"EXISTING_INTENT")
        self.assertEqual(len(self.broker.calls),1)

if __name__=="__main__":unittest.main()
