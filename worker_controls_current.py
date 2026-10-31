
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

DB = Path("/var/lib/si-crypto-r1/runtime.sqlite3")
KEY = "dashboard_controls_v1"
FIELDS = {"bot", "auto_trade", "buy", "sell"}
PREFIX = "dashboard-v1:"

def now():
    return datetime.now(timezone.utc).isoformat()

def value(db, key, default=None):
    row = db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default

def store(db, key, item):
    db.execute(
        "INSERT INTO kv(k,v) VALUES(?,?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, json.dumps(item, allow_nan=False)),
    )

def controls(db):
    item = value(db, KEY)
    if item is None:
        item = {
            "bot": True,
            "auto_trade": True,
            "buy": value(db, "entries_enabled", False) is True,
            "sell": True,
        }
    if (
        not isinstance(item, dict)
        or set(item) != FIELDS
        or any(type(v) is not bool for v in item.values())
    ):
        raise RuntimeError("INVALID_CONTROL_STATE")
    return item

def on_start(db):
    item = controls(db)
    item["buy"] = False
    store(db, KEY, item)
    store(db, "entries_enabled", False)

def apply_commands(db):
    current = controls(db)
    rows = db.execute(
        "SELECT * FROM commands WHERE state='QUEUED' ORDER BY id"
    ).fetchall()

    for row in rows:
        action = row["action"]
        update = None
        if action in {"pause", "resume"}:
            update = {"buy": action == "resume"}
        elif action.startswith(PREFIX):
            try:
                update = json.loads(action[len(PREFIX):])
            except ValueError:
                pass

        valid = (
            isinstance(update, dict)
            and bool(update)
            and set(update) <= FIELDS
            and all(type(v) is bool for v in update.values())
        )
        if valid:
            current = {**current, **update}
            store(db, KEY, current)
            store(
                db, "entries_enabled",
                current["bot"] and current["auto_trade"] and current["buy"],
            )
            store(db, "dashboard_control_ack_v1", {
                "command_id": row["id"],
                "applied_at": now(),
                "worker_pid": os.getpid(),
                "controls": current,
            })

        db.execute(
            "UPDATE commands SET state=?,applied_at=? WHERE id=?",
            ("APPLIED" if valid else "REJECTED", now(), row["id"]),
        )
        db.commit()

    store(db, KEY, current)
    db.commit()

def permitted(db, side):
    item = controls(db)
    return (
        side in {"buy", "sell"}
        and item["bot"]
        and item["auto_trade"]
        and item[side]
        and (side != "buy" or value(db, "entries_enabled", False) is True)
    )

def queue(body):
    if (
        not isinstance(body, dict)
        or set(body) != {"bot", "updates", "request_id"}
        or body["bot"] != "crypto"
    ):
        raise ValueError("Unsupported request.")
    request_id = body["request_id"]
    if not isinstance(request_id, str):
        raise ValueError("Invalid request ID.")
    UUID(request_id)
    update = body["updates"]
    if (
        not isinstance(update, dict)
        or not update
        or not set(update) <= FIELDS
        or any(type(v) is not bool for v in update.values())
    ):
        raise ValueError("Require Boolean control values.")

    payload = json.dumps(update, sort_keys=True)
    with sqlite3.connect(
        "file:" + str(DB) + "?mode=rw", uri=True, timeout=5
    ) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS dashboard_requests_v1("
            "request_id TEXT PRIMARY KEY,payload TEXT NOT NULL,"
            "command_id INTEGER NOT NULL)"
        )
        saved = db.execute(
            "SELECT payload,command_id FROM dashboard_requests_v1 "
            "WHERE request_id=?", (request_id,)
        ).fetchone()
        if saved:
            if saved[0] != payload:
                raise ValueError("Request ID already used for another command.")
            command_id = saved[1]
        else:
            cursor = db.execute(
                "INSERT INTO commands(at,action,state) VALUES(?,?,?)",
                (now(), PREFIX + payload, "QUEUED"),
            )
            command_id = cursor.lastrowid
            db.execute(
                "INSERT INTO dashboard_requests_v1 VALUES(?,?,?)",
                (request_id, payload, command_id),
            )
        state = db.execute(
            "SELECT state FROM commands WHERE id=?", (command_id,)
        ).fetchone()[0]

    return {
        "request_id": request_id,
        "command_id": command_id,
        "command_state": state,
        "worker_applied": state == "APPLIED",
        "order_submitted_by_this_http_request": False,
    }

def snapshot():
    with sqlite3.connect(
        "file:" + str(DB) + "?mode=ro", uri=True, timeout=5
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        heartbeat = value(db, "heartbeat")
        flags = value(db, KEY)
        ack = value(db, "dashboard_control_ack_v1")
        entries = value(db, "entries_enabled")
        status = value(db, "status", {})
        rows = db.execute(
            "SELECT id,at,action,state,applied_at FROM commands "
            "WHERE action LIKE ? ORDER BY id DESC LIMIT 15",
            (PREFIX + "%",),
        ).fetchall()

    age = None
    try:
        stamp = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            age = round((datetime.now(timezone.utc)-stamp).total_seconds(), 1)
    except (AttributeError, ValueError, TypeError):
        pass

    return {
        "bot": "crypto",
        "checked_at": now(),
        "heartbeat_age_seconds": age,
        "heartbeat_fresh": age is not None and 0 <= age <= 30,
        "worker_controls": flags,
        "entries_enabled": entries,
        "worker_acknowledgement": ack,
        "worker_status": {
            k: status.get(k) for k in (
                "state", "reason", "decision", "score",
                "managed_quantity", "estimated_net_return_pct",
                "last_cycle_completed",
            )
        } if isinstance(status, dict) else {},
        "commands": [dict(row) for row in rows],
    }

def handle(handler):
    from strategy_settings_v1 import TOKEN, secure_request, send
    path = urlsplit(handler.path).path

    if path == "/api/v1/config/update" and handler.command == "POST":
        send(handler, 409, {
            "error": "LEGACY_TOGGLES_NOT_WORKER_CONTROLS",
            "message": "Use /worker-controls for installed Crypto controls.",
        })
        return True

    if path not in {"/worker-controls", "/api/v1/worker-controls"}:
        return False

    post = handler.command == "POST"
    if not secure_request(handler, post=post):
        send(handler, 403, {"error": "LOCAL_SAME_ORIGIN_ACCESS_REQUIRED"})
        return True

    try:
        if path == "/worker-controls" and handler.command == "GET":
            page = PAGE.replace("__NONCE__", TOKEN).replace(
                "__TOKEN__", json.dumps(TOKEN)
            )
            send(handler, 200, page, html=True)
        elif path == "/api/v1/worker-controls" and handler.command == "GET":
            send(handler, 200, snapshot())
        elif path == "/api/v1/worker-controls" and post:
            kind = handler.headers.get("Content-Type", "").split(";")[0].strip()
            if kind != "application/json":
                raise ValueError("JSON required.")
            size = int(handler.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError("Invalid body size.")
            body = json.loads(handler.rfile.read(size))
            send(handler, 202, queue(body))
        else:
            send(handler, 405, {"error": "METHOD_NOT_ALLOWED"})
    except (ValueError, TypeError, UnicodeError):
        send(handler, 400, {"error": "INVALID_CONTROL_REQUEST"})
    except Exception as error:
        send(handler, 503, {
            "error": "CONTROLS_UNAVAILABLE",
            "error_type": type(error).__name__,
        })
    return True

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Worker Control Room</title>
<style>
:root{color-scheme:dark}
body{margin:0;color:#edf4ff;font:15px "Segoe UI",Arial,sans-serif;
background:radial-gradient(at 5% 0%,#124d62,transparent 50%),
radial-gradient(at 95% 10%,#452674,transparent 50%),#091223}
main{max-width:1080px;margin:auto;padding:24px}
h1{font-size:28px}h2{font-size:18px}
section{background:linear-gradient(135deg,#1b2b45,#121e33);
border:1px solid #41577b;border-radius:15px;padding:20px;margin:18px 0;
box-shadow:0 12px 32px #0003}
a{color:#8feaff}button{font:inherit;padding:9px 20px;margin:4px;
border:1px solid #7185ae;border-radius:8px;color:#f5f9ff;background:#273b5d;
cursor:pointer}button:disabled{opacity:.45;cursor:wait}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;
border-bottom:1px solid #364966;padding:10px 0}
.row span{min-width:125px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px monospace}
.green{color:#61f0ac}.red{color:#ff91a6}.orange{color:#ffca7b}
.grey{color:#b2bfd6}.warning{color:#ffd092}
</style></head><body><main>
<a href="/">← Dashboard</a>
<h1>Crypto · Worker Control Room</h1>
<p class="warning">OFF blocks new automatic submissions, including
software stop-loss exits. Existing broker orders are not cancelled.</p>
<section><h2>Worker-confirmed permissions</h2>
<p id="health">Reading worker…</p><div id="controls"></div>
<pre id="runtime"></pre></section>
<section><h2>Action response</h2><pre id="message">No request sent.</pre>
<h2>Durable command history</h2><pre id="history"></pre></section>
<section><h2>Scope</h2>
<p>Bot OFF blocks automatic execution; monitoring and reconciliation remain
running. Bot ON does not mean an order will be submitted.</p>
<p>Equity and Zerodha controls, manual orders, and cancellation of
existing orders are not installed by this release.</p></section>
<script nonce="__NONCE__">
const token=__TOKEN__,el=id=>document.getElementById(id);
let sending=false,reading=false;
async function request(options={}){
 const controller=new AbortController();
 const timer=setTimeout(()=>controller.abort(),12000);
 try{
  const response=await fetch("/api/v1/worker-controls",{
   ...options,cache:"no-store",credentials:"same-origin",
   signal:controller.signal
  });
  const data=await response.json();
  if(!response.ok)throw Error(data.error||response.status);
  return data;
 }finally{clearTimeout(timer)}
}
async function refresh(){
 if(reading)return;reading=true;
 try{
  const data=await request(),fresh=data.heartbeat_fresh;
  el("health").className=fresh?"green":"grey";
  el("health").textContent=fresh?
   "Fresh worker heartbeat · permissions are not proof of broker execution":
   "Stale or unknown heartbeat · current operation is unverified";
  el("controls").replaceChildren();
  for(const key of ["bot","auto_trade","buy","sell"]){
   const row=document.createElement("div");row.className="row";
   const label=document.createElement("span");label.textContent=key;
   const state=document.createElement("span");
   const v=data.worker_controls?.[key];
   const known=fresh&&typeof v==="boolean";
   state.textContent=known?(v?"ON":"OFF"):"UNVERIFIED";
   state.className=!known?"grey":v?"green":"red";
   row.append(label,state);
   for(const value of [true,false]){
    const button=document.createElement("button");
    button.textContent=value?"ON":"OFF";button.disabled=sending;
    button.onclick=()=>send(key,value);row.append(button);
   }
   el("controls").append(row);
  }
  el("runtime").textContent=JSON.stringify({
   entries_enabled:data.entries_enabled,
   status:data.worker_status,
   last_ack:data.worker_acknowledgement
  },null,2);
  el("history").textContent=JSON.stringify(data.commands,null,2);
 }catch(error){
  el("health").className="grey";
  el("health").textContent="Connection failed: "+error.message;
  el("runtime").textContent="Current worker operation is UNVERIFIED.";
  el("controls").replaceChildren();
 }finally{reading=false}
}
async function send(key,value){
 if(sending)return;
 const warning=!value&&["bot","auto_trade","sell"].includes(key)?
  "\nThis also blocks new software stop-loss exits.":"";
 if(!confirm("Set Crypto "+key+" = "+value+"?"+warning))return;
 sending=true;
 document.querySelectorAll("button").forEach(b=>b.disabled=true);
 el("message").className="orange";
 const body={bot:"crypto",updates:{[key]:value},
  request_id:window.crypto.randomUUID()};
 el("message").textContent="Sending "+body.request_id+"…";
 try{
  const data=await request({
   method:"POST",
   headers:{"Content-Type":"application/json","X-SI-CSRF":token},
   body:JSON.stringify(body)
  });
  el("message").textContent=JSON.stringify(data,null,2)+
   "\nQUEUED is not APPLIED. Check command history for worker acknowledgement.";
 }catch(error){
  el("message").className="grey";
  el("message").textContent="Outcome uncertain: "+error.message+
   ". Check command history before sending another request.\nRequest: "+
   body.request_id;
 }finally{sending=false;await refresh()}
}
refresh();setInterval(refresh,3000);
</script></main></body></html>"""
