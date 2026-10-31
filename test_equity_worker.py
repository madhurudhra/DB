import asyncio
import contextlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
import equity_worker as w
import equity_commands_v1 as controls

POLICY={"allocated_capital":"130","capital_utilization_pct":"90","stop_loss_pct":"1","profit_start_pct":"1.5","profit_goal_pct":"2","profit_floor_pct":"4","daily_loss_limit_pct":"2","max_positions":10,"revision":2}
CFG={"account_number":"SYNTHETIC-ACCOUNT","cost_buffer_pct":2,"minimum_order_usd":1,"programmatic_execution_authorized":True,"legacy_guard_database":"/nonexistent/synthetic.sqlite3"}
FLAGS={"bot":True,"auto_trade":True,"buy":True,"sell":True}

def insert(db,ref,side,qty,avg,fees="0",state="filled",symbol="TEST",reservation="0",created=None):
    db.execute("INSERT INTO intents(ref,symbol,side,quantity,reservation,state,created,filled,average,fees) VALUES(?,?,?,?,?,?,?,?,?,?)",(ref,symbol,side,str(qty),reservation,state,created or time.time(),str(qty) if state=="filled" else "0",str(avg),fees))
    db.commit()

class FakeBroker:
    def __init__(self):
        self.calls=[]
        self.orders=[]
        self.positions=[]
        self.alert={}
        self.app=False
        self.timeout=False
        self.disable_after_review=None
        self.bid="60.95"
        self.ask="61.00"
    async def pages(self,name,key):
        return list(self.orders if key=="orders" else self.positions)
    async def call(self,name,args):
        self.calls.append((name,dict(args)))
        if name=="get_accounts":
            return {"accounts":[{"account_number":CFG["account_number"],"agentic_allowed":True,"state":"active","deactivated":False,"permanently_deactivated":False,"type":"cash"}]}
        if name=="get_portfolio":
            return {"cash":"200","buying_power":{"buying_power":"200","unleveraged_buying_power":"200"}}
        if name=="get_equity_quotes":
            return {"results":[{"quote":{"symbol":args["symbols"][0],"bid_price":self.bid,"ask_price":self.ask,"venue_bid_time":w.stamp(),"venue_ask_time":w.stamp(),"has_traded":True,"state":"active"}}]}
        if name=="get_equity_tradability":
            return {"results":[{"symbol":args["symbols"][0],"tradeable":True,"state":"active","fractional_tradability":"tradable"}]}
        if name=="review_equity_order":
            if self.disable_after_review: self.disable_after_review()
            return {**args,"order_checks":self.alert,"customer_approval_required":self.app}
        if name=="place_equity_order":
            if self.timeout: raise TimeoutError("synthetic uncertain outcome")
            o={**args,"id":"SYNTHETIC-ORDER","state":"queued","cumulative_quantity":"0","average_price":None,"fees":"0"}
            self.orders.append(o)
            return {"order":o}
        raise AssertionError(name)

class PureTests(unittest.TestCase):
    def setUp(self): self.db=w.connect(":memory:")
    def tearDown(self): self.db.close()
    def test_score(self): self.assertEqual(w.score(list(range(1,61))),100)
    def test_flat_score(self): self.assertEqual(w.score([100]*60),0)
    def test_bad_numbers(self):
        for x in (True,"NaN","Infinity",None):
            with self.subTest(x=x),self.assertRaises(w.Blocked): w.number(x)
    def test_history_length(self):
        with self.assertRaises(w.Blocked): w.score([1]*59)
    def test_budget(self): self.assertEqual(w.buy_capacity(self.db,130,90,200),117)
    def test_pending_reservation(self):
        insert(self.db,"r","buy",1,0,state="UNKNOWN",reservation="20")
        self.assertEqual(w.buy_capacity(self.db,130,90,200),97)
    def test_loss_reduces_owned_cash(self):
        insert(self.db,"b","buy",1,100,fees="1",created=1)
        insert(self.db,"s","sell",1,50,fees="1",created=2)
        self.assertEqual(w.buy_capacity(self.db,130,90,200),78)
    def test_owned_basis(self):
        insert(self.db,"b","buy",2,10,fees="1",created=1)
        insert(self.db,"s","sell",1,12,fees="1",created=2)
        h,r=w.owned_book(self.db)
        self.assertEqual(h["TEST"]["qty"],1)
        self.assertEqual(h["TEST"]["cost"],Decimal("10.5"))
        self.assertEqual(r,Decimal("0.5"))
    def test_no_ticker_cap(self):
        insert(self.db,"b","buy",1,80,created=1)
        self.assertEqual(w.buy_capacity(self.db,130,90,200),37)
    def test_zero_utilization(self): self.assertEqual(w.buy_capacity(self.db,130,0,200),0)
    def test_broker_cash_bound(self): self.assertEqual(w.buy_capacity(self.db,130,90,3),3)

class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db=w.connect(":memory:")
        self.b=FakeBroker()
        self.flags=dict(FLAGS)
        self.worker=w.Worker(self.db,self.b,dict(CFG),lambda:dict(self.flags))
        self.worker.candidate_symbols=["TEST"]
        w.put(self.db,"external_baseline",{"TEST":"0"})
        for i in range(60):
            self.db.execute("INSERT INTO samples VALUES(?,?,?)",("TEST",time.time()-(59-i)*10,str(i+1)))
        self.db.commit()
        self.old_policy=w.policy_snapshot
        self.old_window=w.regular_window
        w.policy_snapshot=lambda cfg: dict(POLICY)
        w.regular_window=lambda: True
        self.stdout=contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__()
    def tearDown(self):
        self.stdout.__exit__(None,None,None)
        w.policy_snapshot=self.old_policy
        w.regular_window=self.old_window
        self.db.close()
    async def test_submission_records_broker_identity(self):
        ref=await self.worker.submit("TEST","buy","0.5","40",dict(POLICY))
        r=self.db.execute("SELECT * FROM intents WHERE ref=?",(ref,)).fetchone()
        self.assertEqual(r["broker_id"],"SYNTHETIC-ORDER")
        self.assertEqual(r["state"],"ACKNOWLEDGED")
    async def test_permission_off_no_broker_write(self):
        self.flags["buy"]=False
        with self.assertRaises(w.Blocked): await self.worker.submit("TEST","buy","0.5","40",dict(POLICY))
        self.assertEqual(self.b.calls,[])
    async def test_app_approval_not_bypassed(self):
        self.b.app=True
        with self.assertRaisesRegex(w.Blocked,"BROKER_APP_APPROVAL_REQUIRED"):
            await self.worker.submit("TEST","buy","0.5","40",dict(POLICY))
        self.assertFalse(any(n=="place_equity_order" for n,_ in self.b.calls))
    async def test_alert_blocks(self):
        self.b.alert={"alert_type":"SYNTHETIC_ALERT"}
        with self.assertRaisesRegex(w.Blocked,"BROKER_REVIEW_ALERT"):
            await self.worker.submit("TEST","buy","0.5","40",dict(POLICY))
    async def test_policy_change_blocks(self):
        w.policy_snapshot=lambda cfg:{**POLICY,"revision":3}
        with self.assertRaisesRegex(w.Blocked,"POLICY_CHANGED"):
            await self.worker.submit("TEST","buy","0.5","40",dict(POLICY))
    async def test_recovery_blocks_entry(self):
        self.b.bid="1"
        self.b.ask="1.001"
        with self.assertRaisesRegex(w.Blocked,"ENTRY_SIGNAL_CHANGED"):
            await self.worker.submit("TEST","buy","0.5","40",dict(POLICY))
        self.assertEqual(self.db.execute("SELECT count(*) FROM intents").fetchone()[0],0)
    async def test_unknown_does_not_resubmit(self):
        self.b.timeout=True
        with self.assertRaises(TimeoutError):
            await self.worker.submit("TEST","buy","0.5","40",dict(POLICY))
        self.assertEqual(w.pending(self.db)[0]["state"],"UNKNOWN")
        with self.assertRaisesRegex(w.Blocked,"SYMBOL_ORDER_PENDING"):
            await self.worker.submit("TEST","buy","0.5","40",dict(POLICY))
        self.assertEqual(sum(n=="place_equity_order" for n,_ in self.b.calls),1)
    async def test_permission_changes_after_review(self):
        self.b.disable_after_review=lambda:self.flags.update(buy=False)
        with self.assertRaisesRegex(w.Blocked,"PERMISSION_BLOCKED"):
            await self.worker.submit("TEST","buy","0.5","40",dict(POLICY))
        self.assertEqual(self.db.execute("SELECT count(*) FROM intents").fetchone()[0],0)
    async def test_partial_fill_reconciled(self):
        ref=await self.worker.submit("TEST","buy","0.5","40",dict(POLICY))
        self.b.orders[0].update(state="partially_filled",cumulative_quantity="0.25",average_price="61",fees="0.01")
        await self.worker.reconcile(self.b.orders)
        h,_=w.owned_book(self.db)
        self.assertEqual(h["TEST"]["qty"],Decimal("0.25"))
        self.assertEqual(w.pending(self.db)[0]["reservation"],"40")
    async def test_unknown_without_match_remains_reserved(self):
        insert(self.db,"r","buy",1,0,state="UNKNOWN",reservation="40")
        await self.worker.reconcile([])
        self.assertEqual(w.pending(self.db)[0]["reservation"],"40")
    async def test_stop_signal(self):
        h={"qty":Decimal("1"),"gross":Decimal("100"),"cost":Decimal("100")}
        self.assertEqual(self.worker.exit_reason("TEST",h,Decimal("99"),None,POLICY),"LOSS_STOP")
    async def test_floor_arms_strictly_above_threshold(self):
        h={"qty":Decimal("1"),"gross":Decimal("100"),"cost":Decimal("100")}
        self.worker.exit_reason("TEST",h,Decimal("104"),100,POLICY)
        r=self.db.execute("SELECT * FROM milestones").fetchone()
        self.assertFalse(r["floor"])
        self.worker.exit_reason("TEST",h,Decimal("105"),100,POLICY)
        self.assertEqual(self.worker.exit_reason("TEST",h,Decimal("104"),100,POLICY),"PROFIT_FLOOR")
    async def test_external_owned_quantity_change_blocks(self):
        insert(self.db,"b","buy",1,10)
        with self.assertRaisesRegex(w.Blocked,"OWNED_HOLDINGS_CHANGED"):
            await self.worker.verify_ownership([{"symbol":"TEST","quantity":"2"}])
    async def test_buy_off_cycle_has_no_submission(self):
        self.flags["buy"]=False
        with self.assertRaisesRegex(w.Blocked,"PERMISSION_BLOCKED_BUY"):
            await self.worker.cycle(dict(POLICY),["TEST"])
        self.assertFalse(any(n=="place_equity_order" for n,_ in self.b.calls))

class CycleTests(unittest.IsolatedAsyncioTestCase):
    setUp=WorkerTests.setUp
    tearDown=WorkerTests.tearDown
    async def test_complete_buy_cycle(self):
        self.db.execute("DELETE FROM samples")
        now=time.time()
        for i in range(59):
            self.db.execute("INSERT INTO samples VALUES(?,?,?)",("TEST",now-(59-i)*10,str(i+1)))
        self.db.commit()
        result=await self.worker.cycle(dict(POLICY),["TEST"])
        self.assertEqual(result["state"],"BUY_SUBMITTED_OR_APPROVAL_PENDING")
        self.assertEqual(len(w.pending(self.db)),1)
        self.assertEqual(sum(n=="place_equity_order" for n,_ in self.b.calls),1)
    async def test_complete_exit_cycle(self):
        insert(self.db,"b","buy",1,100,created=1)
        self.b.positions=[{"symbol":"TEST","quantity":"1","shares_available_for_sells":"1"}]
        self.b.orders=[{"ref_id":"b","id":"BUY-ID","symbol":"TEST","side":"buy","state":"filled","quantity":"1","cumulative_quantity":"1","average_price":"100","fees":"0"}]
        self.b.bid="99"
        self.b.ask="99.01"
        result=await self.worker.cycle(dict(POLICY),["TEST"])
        self.assertEqual(result["state"],"ORDER_PENDING")
        self.assertTrue(any(n=="place_equity_order" and a["side"]=="sell" for n,a in self.b.calls))
    async def test_stale_settings_do_not_disable_owned_exits(self):
        self.worker.cfg["_policy_fresh"]=False
        insert(self.db,"b","buy",1,100,created=1)
        self.b.positions=[{"symbol":"TEST","quantity":"1","shares_available_for_sells":"1"}]
        self.b.orders=[{"ref_id":"b","id":"BUY-ID","symbol":"TEST","side":"buy","state":"filled","quantity":"1","cumulative_quantity":"1","average_price":"100","fees":"0"}]
        self.b.bid="99"
        self.b.ask="99.01"
        w.policy_snapshot=lambda cfg: (_ for _ in ()).throw(OSError("synthetic settings outage"))
        result=await self.worker.cycle(dict(POLICY),["TEST"])
        self.assertEqual(result["state"],"ORDER_PENDING")
        self.assertTrue(any(n=="place_equity_order" and a["side"]=="sell" for n,a in self.b.calls))

class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        controls.DB=Path(self.tmp.name)/"controls.sqlite3"
        controls.initialize_worker(role="buy_worker")
    def tearDown(self): self.tmp.cleanup()
    def request(self,updates): return {"bot":"equity","request_id":str(uuid.uuid4()),"updates":updates}
    def test_buy_on_queue_and_ack(self):
        r=controls.queue(self.request({"buy":True}))
        self.assertEqual(r["command_state"],"QUEUED")
        self.assertTrue(controls.apply_pending()["buy"])
    def test_idempotent(self):
        r=self.request({"buy":True})
        self.assertEqual(controls.queue(r)["command_id"],controls.queue(r)["command_id"])
    def test_conflicting_reuse(self):
        r=self.request({"buy":True})
        controls.queue(r)
        r["updates"]={"buy":False}
        with self.assertRaises(ValueError): controls.queue(r)
    def test_auto_off_blocks_sell(self):
        controls.queue(self.request({"auto_trade":False}))
        controls.apply_pending()
        with self.assertRaises(RuntimeError): controls.require_sell_permission()
    def test_buy_does_not_default_on(self): self.assertFalse(controls.apply_pending()["buy"])

if __name__=="__main__": unittest.main(verbosity=2)
