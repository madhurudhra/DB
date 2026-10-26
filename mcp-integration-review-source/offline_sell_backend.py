"""OFFLINE integration candidate, not a live trading deployment.

No concrete broker adapter, inventory discovery, fee estimator or UI integration
is included. The normalized snapshot must be verified by a real adapter before
this coordinator may ever be used live. Long positions only; fractional quantities
are supported. Account bindings and a real cross-process SELL guard are required.
Do not combine its attributed proceeds with legacy capital balances without migration.
"""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation,localcontext
from pathlib import Path

BOTS=frozenset({"crypto","us_trading","zerodha"})
TERMINAL=frozenset({"FILLED","CANCELED","REJECTED","ABORTED"})

class BackendError(RuntimeError):pass

def number(value,name):
    if isinstance(value,bool) or value is None:raise BackendError("Invalid "+name)
    try:value=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError):raise BackendError("Invalid "+name) from None
    if not value.is_finite() or value<0:raise BackendError("Invalid "+name)
    return value

class Ledger:
    def __init__(self,filename,accounts):
        if not accounts or any(k not in BOTS or not isinstance(v,str) or not v for k,v in accounts.items()):
            raise BackendError("Explicit bot/account bindings required")
        self.path=str(Path(filename).resolve());self.accounts=dict(accounts)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS bindings(bot TEXT PRIMARY KEY,account TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS exits(
                cid TEXT PRIMARY KEY,bot TEXT NOT NULL,account TEXT NOT NULL,
                instrument TEXT NOT NULL,plan TEXT NOT NULL,state TEXT NOT NULL,
                broker_id TEXT,filled TEXT NOT NULL DEFAULT '0',
                bot_credit TEXT NOT NULL DEFAULT '0',external_credit TEXT NOT NULL DEFAULT '0')""")
            stored=dict(db.execute("SELECT bot,account FROM bindings"))
            if stored and stored!=self.accounts:raise BackendError("Account bindings changed")
            if not stored:db.executemany("INSERT INTO bindings VALUES (?,?)",self.accounts.items())
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10,isolation_level=None);db.row_factory=sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL");db.execute("BEGIN IMMEDIATE")
            yield db;db.commit()
        except Exception:db.rollback();raise
        finally:db.close()
    def reserve(self,plan):
        encoded=json.dumps(plan,sort_keys=True)
        with self.db() as db:
            if self.accounts.get(plan["bot"])!=plan["account"]:raise BackendError("Account mismatch")
            prior=db.execute("SELECT * FROM exits WHERE cid=?",(plan["cid"],)).fetchone()
            if prior:
                if prior["plan"]!=encoded:raise BackendError("Intent ID reused")
                return False
            conflict=db.execute("SELECT 1 FROM exits WHERE account=? AND instrument=? AND state NOT IN ('FILLED','CANCELED','REJECTED','ABORTED') LIMIT 1",(plan["account"],plan["instrument"])).fetchone()
            if conflict:return False
            db.execute("INSERT INTO exits(cid,bot,account,instrument,plan,state) VALUES (?,?,?,?,?,'INTENT')",(plan["cid"],plan["bot"],plan["account"],plan["instrument"],encoded))
            return True
    def claim(self,cid):
        with self.db() as db:
            return db.execute("UPDATE exits SET state='SUBMITTING' WHERE cid=? AND state='INTENT' AND broker_id IS NULL",(cid,)).rowcount==1
    def mark_uncertain(self,cid):
        with self.db() as db:
            db.execute("UPDATE exits SET state='UNCERTAIN' WHERE cid=? AND state IN ('SUBMITTING','PENDING','PARTIALLY_FILLED')",(cid,))
    def abort_before_request(self,cid):
        with self.db() as db:
            db.execute("UPDATE exits SET state='ABORTED' WHERE cid=? AND state='SUBMITTING' AND broker_id IS NULL",(cid,))
    def reconcile(self,cid,receipt):
        with self.db() as db,localcontext() as context:
            context.prec=50
            row=db.execute("SELECT * FROM exits WHERE cid=?",(cid,)).fetchone()
            if row is None:raise BackendError("Unknown exit")
            plan=json.loads(row["plan"])
            if not isinstance(receipt,dict) or receipt.get("cid")!=cid or receipt.get("account")!=row["account"]:
                raise BackendError("Receipt identity mismatch")
            broker_id=receipt.get("broker_id")
            if not isinstance(broker_id,str) or not broker_id or row["broker_id"] not in (None,broker_id):raise BackendError("Broker ID mismatch")
            state=receipt.get("state")
            if state not in {"PENDING","PARTIALLY_FILLED","FILLED","CANCELED","REJECTED"}:raise BackendError("Unknown receipt state")
            if row["state"] in TERMINAL and state!=row["state"]:raise BackendError("Terminal order cannot reopen")
            filled=number(receipt.get("filled"),"fill quantity")
            average=number(receipt.get("average"),"average price")
            fee=number(receipt.get("fee"),"exit fee")
            qty=Decimal(plan["quantity"]);bot_q=Decimal(plan["bot_quantity"])
            if filled<Decimal(row["filled"]) or filled>qty:raise BackendError("Decreasing fill or overfill")
            if filled>0 and average<=0:raise BackendError("Filled order lacks price")
            if filled==0 and fee!=0:raise BackendError("Unattributed fee requires reconciliation")
            if state=="FILLED" and filled!=qty:raise BackendError("Incomplete FILLED receipt")
            if state=="PARTIALLY_FILLED" and not (0<filled<qty):raise BackendError("Invalid partial fill")
            if state=="REJECTED" and filled!=0:raise BackendError("Rejected order reports fills")
            net=filled*average-fee
            bot_credit=net*bot_q/qty;external_credit=net-bot_credit
            delta={"bot":bot_credit-Decimal(row["bot_credit"]),"external":external_credit-Decimal(row["external_credit"])}
            db.execute("UPDATE exits SET state=?,broker_id=?,filled=?,bot_credit=?,external_credit=? WHERE cid=?",(state,broker_id,str(filled),str(bot_credit),str(external_credit),cid))
            return delta
    def totals(self,bot):
        if bot not in self.accounts:raise BackendError("Unknown bot")
        with self.db() as db,localcontext() as context:
            context.prec=50
            rows=db.execute("SELECT bot_credit,external_credit FROM exits WHERE bot=?",(bot,)).fetchall()
            return {"bot":sum((Decimal(r[0]) for r in rows),Decimal(0)),"external":sum((Decimal(r[1]) for r in rows),Decimal(0))}

class Coordinator:
    def __init__(self,ledger,broker,permission_guard,clock=None):
        self.ledger=ledger;self.broker=broker;self.guard=permission_guard
        self.clock=clock or (lambda:datetime.now(timezone.utc))
    def evaluate(self,snapshot):
        s=snapshot
        if not isinstance(s,dict):raise BackendError("Snapshot required")
        for key in ("bot","account","exchange","symbol","product","snapshot_id","reference_id"):
            if not isinstance(s.get(key),str) or not s[key] or s[key]!=s[key].strip():raise BackendError("Missing "+key)
        if s["bot"] not in BOTS or self.ledger.accounts.get(s["bot"])!=s["account"]:raise BackendError("Account mismatch")
        for key in ("reconciled","pending_order","reference_verified","execution_approved"):
            if type(s.get(key)) is not bool:raise BackendError("Explicit Boolean required: "+key)
        if not s["reconciled"] or s["pending_order"] or not s["reference_verified"] or not s["execution_approved"]:
            return {"state":"BLOCKED","reason":"SNAPSHOT_SAFETY_CHECK"}
        stamp=s.get("as_of")
        if not isinstance(stamp,datetime) or stamp.tzinfo is None:raise BackendError("Timezone-aware snapshot timestamp required")
        age=(self.clock()-stamp).total_seconds()
        if not -5<=age<=30:return {"state":"BLOCKED","reason":"STALE_SNAPSHOT"}
        with localcontext() as context:
            context.prec=50
            held=number(s.get("held_quantity"),"held quantity")
            available=number(s.get("available_quantity"),"available quantity")
            quantity=number(s.get("quantity"),"exit quantity")
            bot_q=number(s.get("bot_quantity"),"bot quantity")
            external_q=number(s.get("external_quantity"),"external quantity")
            cost=number(s.get("entry_cost_for_exit_quantity"),"matched entry cost")
            credit=number(s.get("estimated_net_credit"),"estimated net credit")
            increment=number(s.get("quantity_increment"),"quantity increment")
            if held<=0 or quantity<=0 or cost<=0 or increment<=0:raise BackendError("Positive quantity, cost and increment required")
            if available>held or quantity>available or quantity>held:raise BackendError("Exit exceeds available holdings")
            if bot_q+external_q!=quantity:raise BackendError("Ownership quantities do not match exit quantity")
            if quantity%increment!=0:raise BackendError("Exit quantity is not increment-aligned")
            reason="STOP_LOSS_1PCT_NET" if credit<=cost*Decimal("0.99") else "TAKE_PROFIT_6PCT_NET" if credit>=cost*Decimal("1.06") else None
            if reason is None:return {"state":"HOLD","reason":"WITHIN_THRESHOLDS"}
            instrument=json.dumps([s["exchange"],s["symbol"],s["product"]],separators=(",",":"))
            identity=json.dumps([s["bot"],s["account"],instrument,s["snapshot_id"],s["reference_id"]])
            cid=str(uuid.uuid5(uuid.NAMESPACE_URL,identity))
            return {"state":"EXIT_DUE","plan":{"cid":cid,"bot":s["bot"],"account":s["account"],"instrument":instrument,
                "quantity":str(quantity),"bot_quantity":str(bot_q),"external_quantity":str(external_q),"side":"SELL",
                "reference_id":s["reference_id"],"reason":reason}}
    def step(self,snapshot):
        evaluation=self.evaluate(snapshot)
        if evaluation["state"]!="EXIT_DUE":return evaluation
        plan=evaluation["plan"];cid=plan["cid"]
        with self.guard(plan["bot"]) as enabled:
            if enabled is not True:return {"state":"BLOCKED","reason":"SELL_OFF"}
            if not self.ledger.reserve(plan):return {"state":"BLOCKED","reason":"EXISTING_INTENT"}
            if not self.ledger.claim(cid):return {"state":"BLOCKED","reason":"ALREADY_CLAIMED"}
            try:
                refreshed=self.evaluate(snapshot)
                if refreshed.get("state")!="EXIT_DUE" or self.broker.revalidate(snapshot) is not True:
                    self.ledger.abort_before_request(cid)
                    return {"state":"BLOCKED","reason":"PRE_SUBMISSION_RECHECK"}
            except Exception:
                self.ledger.abort_before_request(cid)
                raise
            try:
                receipt=self.broker.submit(plan)
                delta=self.ledger.reconcile(cid,receipt)
            except Exception:
                self.ledger.mark_uncertain(cid)
                raise
            return {"state":receipt["state"],"cid":cid,"credit_delta":delta}
