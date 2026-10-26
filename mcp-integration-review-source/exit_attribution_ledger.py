"""Separate exit-attribution ledger; NOT integrated with live bots or BUY capital.

Use only broker-verified account/order evidence. INTENT is a reservation, not a
broker submission. Reconcile unresolved intents before retry. Do not add these
balances to legacy capital balances without an explicit integration/migration.
The installer must place the database in a private service-owned directory.
"""
import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, localcontext
from pathlib import Path
from exit_ownership import attribute_fill, _number

BOTS=frozenset({"crypto","us_trading","zerodha"})
TERMINAL=frozenset({"FILLED","CANCELED","REJECTED"})
STATUSES=TERMINAL|{"PENDING","PARTIALLY_FILLED","UNCERTAIN"}

class ExitLedger:
    def __init__(self,filename,accounts):
        if not accounts or any(k not in BOTS or not isinstance(v,str) or not v for k,v in accounts.items()):
            raise ValueError("Explicit per-bot account bindings required")
        self.path=str(Path(filename).resolve())
        self.accounts=dict(accounts)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS account_bindings(bot TEXT PRIMARY KEY,account TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS exit_orders(
                    cid TEXT PRIMARY KEY,bot TEXT NOT NULL,account TEXT NOT NULL,
                    instrument TEXT NOT NULL,plan TEXT NOT NULL,broker_id TEXT,
                    state TEXT NOT NULL,filled TEXT NOT NULL DEFAULT '0',
                    fee TEXT NOT NULL DEFAULT '0',bot_credit TEXT NOT NULL DEFAULT '0',
                    external_credit TEXT NOT NULL DEFAULT '0',last_update TEXT);
            """)
            db.execute("BEGIN IMMEDIATE")
            stored=dict(db.execute("SELECT bot,account FROM account_bindings"))
            if stored and stored!=self.accounts:
                raise ValueError("Stored account bindings differ; migration required")
            if not stored:
                db.executemany("INSERT INTO account_bindings VALUES (?,?)",self.accounts.items())

    @contextmanager
    def transaction(self):
        db=sqlite3.connect(self.path,timeout=10,isolation_level=None)
        db.row_factory=sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def plan(self,*,cid,bot,account,exchange,symbol,product,
             planned_bot_quantity,planned_external_quantity):
        if bot not in self.accounts or self.accounts[bot]!=account:
            raise ValueError("Exit account mismatch")
        if any(not isinstance(v,str) or not v or v!=v.strip() for v in (cid,exchange,symbol,product)):
            raise ValueError("Explicit order and instrument identity required")
        bot_q=_number(planned_bot_quantity,"planned bot quantity")
        external_q=_number(planned_external_quantity,"planned external quantity")
        attribute_fill(planned_bot_quantity=bot_q,planned_external_quantity=external_q,
                       cumulative_filled_quantity=0,cumulative_average_price=0,cumulative_exit_fee=0)
        instrument=json.dumps([exchange,symbol,product],separators=(",",":"))
        plan=json.dumps({"bot_quantity":str(bot_q),"external_quantity":str(external_q)},sort_keys=True)
        with self.transaction() as db:
            existing=db.execute("SELECT * FROM exit_orders WHERE cid=?",(cid,)).fetchone()
            if existing:
                if (existing["bot"],existing["account"],existing["instrument"],existing["plan"])!=(bot,account,instrument,plan):
                    raise ValueError("Client ID reused with different exit intent")
                return dict(existing)
            conflict=db.execute(
                "SELECT cid FROM exit_orders WHERE account=? AND instrument=? "
                "AND state NOT IN ('FILLED','CANCELED','REJECTED') LIMIT 1",
                (account,instrument)).fetchone()
            if conflict:
                raise ValueError("Unresolved exit already reserves this position")
            db.execute("INSERT INTO exit_orders(cid,bot,account,instrument,plan,state) VALUES (?,?,?,?,?,'INTENT')",
                       (cid,bot,account,instrument,plan))
            return dict(db.execute("SELECT * FROM exit_orders WHERE cid=?",(cid,)).fetchone())

    def update(self,*,cid,bot,account,broker_id,status,
               cumulative_filled_quantity,cumulative_average_price,cumulative_exit_fee):
        if bot not in self.accounts or self.accounts[bot]!=account:
            raise ValueError("Exit account mismatch")
        if status not in STATUSES:
            raise ValueError("Unknown normalized order status")
        if not isinstance(broker_id,str) or not broker_id.strip():
            raise ValueError("Verified broker order ID required")
        with self.transaction() as db:
            row=db.execute("SELECT * FROM exit_orders WHERE cid=?",(cid,)).fetchone()
            if row is None or (row["bot"],row["account"])!=(bot,account):
                raise ValueError("Unknown or mismatched exit intent")
            if row["broker_id"] not in (None,broker_id):
                raise ValueError("Broker order ID changed")
            if row["state"] in TERMINAL and status!=row["state"]:
                raise ValueError("Terminal order cannot be reopened or reclassified")
            plan=json.loads(row["plan"])
            result=attribute_fill(planned_bot_quantity=plan["bot_quantity"],
                planned_external_quantity=plan["external_quantity"],
                cumulative_filled_quantity=cumulative_filled_quantity,
                cumulative_average_price=cumulative_average_price,
                cumulative_exit_fee=cumulative_exit_fee)
            with localcontext() as context:
                context.prec=50
                planned=Decimal(plan["bot_quantity"])+Decimal(plan["external_quantity"])
                if result.filled_quantity<Decimal(row["filled"]):
                    raise ValueError("Fill quantity decreased; reconcile before proceeding")
                if status=="FILLED" and result.filled_quantity!=planned:
                    raise ValueError("FILLED status does not match planned quantity")
                if status=="REJECTED" and result.filled_quantity!=0:
                    raise ValueError("Rejected order contains fills; reconciliation required")
                if status=="PARTIALLY_FILLED" and not (0<result.filled_quantity<planned):
                    raise ValueError("Invalid partial-fill status")
                delta_bot=result.bot_net_credit-Decimal(row["bot_credit"])
                delta_external=result.external_net_credit-Decimal(row["external_credit"])
            payload=json.dumps({"broker_id":broker_id,"status":status,
                "filled":str(result.filled_quantity),"average":str(cumulative_average_price),
                "fee":str(result.exit_fee)},sort_keys=True)
            db.execute("UPDATE exit_orders SET broker_id=?,state=?,filled=?,fee=?,bot_credit=?,external_credit=?,last_update=? WHERE cid=?",
                (broker_id,status,str(result.filled_quantity),str(result.exit_fee),
                 str(result.bot_net_credit),str(result.external_net_credit),payload,cid))
            return {"bot_credit_delta":delta_bot,"external_credit_delta":delta_external,
                    "state":status,"cid":cid}

    def attributed_proceeds(self,bot):
        if bot not in self.accounts:raise ValueError("Unknown bot")
        with self.transaction() as db,localcontext() as context:
            context.prec=50
            rows=db.execute("SELECT bot_credit,external_credit FROM exit_orders WHERE bot=?",(bot,)).fetchall()
            return {"bot":sum((Decimal(r[0]) for r in rows),Decimal(0)),
                    "external":sum((Decimal(r[1]) for r in rows),Decimal(0))}
