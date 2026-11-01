"""Compatible Equity control queue. No writes on import."""
import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

DB=Path("/var/lib/si-equity-buy-worker/controls.sqlite3")
FIELDS={"bot","auto_trade","buy","sell"}
ROLE="sell_guard"

def now(): return datetime.now(timezone.utc).isoformat()
def connect():
    db=sqlite3.connect(DB,timeout=10)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA synchronous=FULL")
    return db

def initialize():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS equity_commands(
          id INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT NOT NULL UNIQUE,
          requested_at TEXT NOT NULL,updates TEXT NOT NULL,state TEXT NOT NULL,
          completed_at TEXT,worker_pid INTEGER,result TEXT);
        CREATE TABLE IF NOT EXISTS equity_control_state(
          id INTEGER PRIMARY KEY CHECK(id=1),controls TEXT NOT NULL,
          acknowledged_at TEXT,worker_pid INTEGER);
        CREATE TABLE IF NOT EXISTS equity_worker_presence(
          role TEXT PRIMARY KEY,pid INTEGER NOT NULL,at TEXT NOT NULL);
        """)

def initialize_worker(role="sell_guard"):
    global ROLE
    if role not in {"sell_guard","buy_worker"}: raise ValueError("INVALID_WORKER_ROLE")
    ROLE=role
    initialize()
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO equity_control_state(id,controls) VALUES(1,?)",(json.dumps({"bot":True,"auto_trade":True,"buy":False,"sell":True}),))
        db.execute("INSERT INTO equity_worker_presence VALUES(?,?,?) ON CONFLICT(role) DO UPDATE SET pid=excluded.pid,at=excluded.at",(ROLE,os.getpid(),now()))

def validated_request(body):
    if not isinstance(body,dict) or set(body)!={"bot","updates","request_id"} or body["bot"]!="equity":
        raise ValueError("REQUIRE_EQUITY_COMMAND")
    UUID(body["request_id"])
    updates=body["updates"]
    if not isinstance(updates,dict) or not updates or not set(updates)<=FIELDS or any(type(v) is not bool for v in updates.values()):
        raise ValueError("REQUIRE_BOOLEAN_CONTROLS")
    return body["request_id"],json.dumps(updates,sort_keys=True)

def queue(body):
    ref,payload=validated_request(body)
    db=connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT * FROM equity_commands WHERE request_id=?",(ref,)).fetchone()
        if row:
            if row["updates"]!=payload: raise ValueError("REQUEST_ID_CONFLICT")
            id,state=row["id"],row["state"]
        else:
            if body["updates"].get("buy") is True:
                worker=db.execute("SELECT * FROM equity_worker_presence WHERE role='buy_worker'").fetchone()
                if not worker or not 0 <= (datetime.now(timezone.utc)-datetime.fromisoformat(worker["at"])).total_seconds() <= 210:
                    raise ValueError("BUY_WORKER_NOT_RECENTLY_OBSERVED")
            cur=db.execute("INSERT INTO equity_commands(request_id,requested_at,updates,state) VALUES(?,?,?,'QUEUED')",(ref,now(),payload))
            id,state=cur.lastrowid,"QUEUED"
        db.commit()
        return {"bot":"equity","request_id":ref,"command_id":id,"command_state":state,"worker_applied":state=="APPLIED","order_submitted_by_this_http_request":False}
    except BaseException:
        db.rollback()
        raise
    finally: db.close()

def apply_pending():
    db=connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT controls FROM equity_control_state WHERE id=1").fetchone()
        if not row: raise RuntimeError("EQUITY_CONTROL_STATE_MISSING")
        flags=json.loads(row["controls"])
        if set(flags)!=FIELDS or any(type(v) is not bool for v in flags.values()): raise RuntimeError("INVALID_EQUITY_CONTROL_STATE")
        for cmd in db.execute("SELECT * FROM equity_commands WHERE state='QUEUED' ORDER BY id").fetchall():
            try:
                _,payload=validated_request({"bot":"equity","request_id":cmd["request_id"],"updates":json.loads(cmd["updates"])})
                flags.update(json.loads(payload))
                state,result="APPLIED",{"controls":dict(flags),"applied_by_role":ROLE,"broker_execution_verified":False}
            except (ValueError,TypeError,KeyError):
                state,result="REJECTED",{"error":"INVALID_EQUITY_COMMAND"}
            db.execute("UPDATE equity_commands SET state=?,completed_at=?,worker_pid=?,result=? WHERE id=?",(state,now(),os.getpid(),json.dumps(result),cmd["id"]))
        db.execute("UPDATE equity_control_state SET controls=?,acknowledged_at=?,worker_pid=? WHERE id=1",(json.dumps(flags,sort_keys=True),now(),os.getpid()))
        db.execute("INSERT INTO equity_worker_presence VALUES(?,?,?) ON CONFLICT(role) DO UPDATE SET pid=excluded.pid,at=excluded.at",(ROLE,os.getpid(),now()))
        db.commit()
        return flags
    except BaseException:
        db.rollback()
        raise
    finally: db.close()

def require_sell_permission():
    c=apply_pending()
    if not all(c[k] for k in ("bot","auto_trade","sell")):
        raise RuntimeError("EQUITY_SELL_BLOCKED_BY_WORKER_CONTROLS")

def require_buy_permission():
    c=apply_pending()
    if not all(c[k] for k in ("bot","auto_trade","buy")):
        raise RuntimeError("EQUITY_BUY_BLOCKED_BY_WORKER_CONTROLS")

def snapshot():
    if not DB.is_file(): return {"bot":"equity","controls":None,"worker_report_fresh":False}
    with sqlite3.connect(DB.as_uri()+"?mode=ro",uri=True,timeout=5) as db:
        db.row_factory=sqlite3.Row
        db.execute("BEGIN")
        r=db.execute("SELECT * FROM equity_control_state WHERE id=1").fetchone()
        workers=[dict(x) for x in db.execute("SELECT * FROM equity_worker_presence")]
        commands=[dict(x) for x in db.execute("SELECT * FROM equity_commands ORDER BY id DESC LIMIT 30")]
    elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(r["acknowledged_at"])).total_seconds() if r and r["acknowledged_at"] else None
    for c in commands:
        c["updates"]=json.loads(c["updates"])
        c["result"]=json.loads(c["result"]) if c["result"] else None
    return {"bot":"equity","controls":json.loads(r["controls"]) if r else None,"worker_report_fresh":elapsed is not None and 0<=elapsed<=210,"reported_worker_pid":r["worker_pid"] if r else None,"workers":workers,"commands":commands,"process_liveness_verified":False,"broker_execution_verified":False}

def history(): return snapshot().get("commands",[])

def main():
    import uuid
    p=argparse.ArgumentParser()
    p.add_argument("action",choices=["status","queue"])
    p.add_argument("--db",required=True)
    p.add_argument("--updates",help='JSON Boolean updates, e.g. {"buy":true}')
    args=p.parse_args()
    global DB
    DB=Path(args.db)
    result=snapshot() if args.action=="status" else queue({"bot":"equity","updates":json.loads(args.updates),"request_id":str(uuid.uuid4())})
    print(json.dumps(result,indent=2))

if __name__=="__main__": main()
