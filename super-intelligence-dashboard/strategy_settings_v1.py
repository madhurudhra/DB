import copy
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

DB = Path("/var/lib/si-strategy-settings/settings.sqlite3")
TOKEN = secrets.token_urlsafe(32)
BOTS = ("crypto", "equity", "zerodha")

SCHEMA = {
    "stop_loss_pct": {
        "label": "Stop loss % — excluding fees",
        "min": 0.1, "max": 20, "integer": False,
    },
    "profit_start_pct": {
        "label": "Start evaluating profit exits % — net of fees",
        "min": 0.1, "max": 100, "integer": False,
    },
    "profit_goal_pct": {
        "label": "Preferred profit goal % — not a guaranteed return",
        "min": 0.1, "max": 100, "integer": False,
    },
    "profit_floor_pct": {
        "label": "Profit retracement floor % after exceeding this level",
        "min": 0.1, "max": 100, "integer": False,
    },
    "daily_loss_limit_pct": {
        "label": "Daily loss limit % — denominator still requires integration",
        "min": 0.1, "max": 20, "integer": False,
    },
    "max_positions": {
        "label": "Maximum open positions",
        "min": 1, "max": 100, "integer": True,
    },
}

ROUTES = {
    "/strategy-settings",
    "/api/v1/strategy-settings",
    "/api/v1/strategy-settings/history",
}

class ValidationError(ValueError):
    pass

class RevisionConflict(ValueError):
    pass

def timestamp():
    return datetime.now(timezone.utc).isoformat()

def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous=FULL")
    return db

def initialize(profiles):
    with connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS settings(
                id INTEGER PRIMARY KEY CHECK(id=1),
                revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit(
                revision INTEGER PRIMARY KEY,
                at TEXT NOT NULL,
                source TEXT NOT NULL,
                targets TEXT NOT NULL,
                updates TEXT NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL
            );
        """)
        if db.execute("SELECT 1 FROM settings WHERE id=1").fetchone():
            raise ValidationError("Settings already initialized.")
        db.execute(
            "INSERT INTO settings VALUES(1,1,?,?)",
            (timestamp(), json.dumps(profiles, allow_nan=False)),
        )
        db.execute(
            "INSERT INTO audit VALUES(?,?,?,?,?,?,?)",
            (
                1, timestamp(), "INITIAL_DESIRED_CONFIGURATION",
                json.dumps(list(BOTS)), "{}",
                "{}", json.dumps(profiles, allow_nan=False),
            ),
        )

def read_settings():
    with connect() as db:
        row = db.execute("SELECT * FROM settings WHERE id=1").fetchone()
    if row is None:
        raise RuntimeError("Settings not initialized.")
    return {
        "revision": row["revision"],
        "updated_at": row["updated_at"],
        "profiles": json.loads(row["payload"]),
        "schema": SCHEMA,
        "worker_integration": {
            bot: "NOT_CONNECTED_TO_THIS_SETTINGS_STORE" for bot in BOTS
        },
        "changes_are_live": False,
        "capital_settings_modified": False,
    }

def validated_profiles(profiles, targets, updates):
    if (
        not isinstance(targets, list)
        or not targets
        or any(not isinstance(bot, str) or bot not in BOTS for bot in targets)
        or len(set(targets)) != len(targets)
    ):
        raise ValidationError("Select unique valid bot names.")
    if (
        not isinstance(updates, dict)
        or not updates
        or any(key not in SCHEMA for key in updates)
    ):
        raise ValidationError("Unknown or empty parameter selection.")

    clean = {}
    for key, value in updates.items():
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValidationError("Invalid value for " + key)
        try:
            number = Decimal(str(value))
            rule = SCHEMA[key]
            if (
                not number.is_finite()
                or not Decimal(str(rule["min"])) <= number <= Decimal(str(rule["max"]))
            ):
                raise ValidationError("Out-of-range value for " + key)
            if rule["integer"]:
                if number != number.to_integral_value():
                    raise ValidationError(key + " must be an integer.")
                clean[key] = int(number)
            else:
                if number != number.quantize(Decimal("0.0001")):
                    raise ValidationError(key + " supports up to four decimal places.")
                clean[key] = float(number)
        except (InvalidOperation, ValueError, OverflowError):
            raise ValidationError("Invalid numeric value for " + key) from None

    result = copy.deepcopy(profiles)
    for bot in targets:
        result[bot].update(clean)
        policy = result[bot]
        if not (
            policy["profit_start_pct"]
            <= policy["profit_goal_pct"]
            <= policy["profit_floor_pct"]
        ):
            raise ValidationError(
                bot + ": require profit start <= preferred goal <= retracement floor."
            )
    return result, clean

def update_settings(body):
    if not isinstance(body, dict) or set(body) != {
        "expected_revision", "targets", "updates"
    }:
        raise ValidationError(
            "Require expected_revision, targets, and updates only."
        )
    revision = body["expected_revision"]
    if type(revision) is not int or revision < 1:
        raise ValidationError("Invalid expected_revision.")

    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM settings WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("Settings not initialized.")
        if row["revision"] != revision:
            raise RevisionConflict("Settings changed. Reload before saving.")

        before = json.loads(row["payload"])
        after, clean = validated_profiles(
            before, body["targets"], body["updates"]
        )
        new_revision = revision + 1
        at = timestamp()
        after_json = json.dumps(after, allow_nan=False)

        db.execute(
            "UPDATE settings SET revision=?,updated_at=?,payload=? WHERE id=1",
            (new_revision, at, after_json),
        )
        db.execute(
            "INSERT INTO audit VALUES(?,?,?,?,?,?,?)",
            (
                new_revision, at, "LOCAL_SETTINGS_UI_OR_API",
                json.dumps(body["targets"]), json.dumps(clean),
                row["payload"], after_json,
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return {
        "status": "SAVED_NOT_APPLIED",
        "revision": new_revision,
        "targets": body["targets"],
        "updates": clean,
        "orders_submitted": False,
        "services_restarted": False,
        "capital_settings_modified": False,
    }

def history():
    with connect() as db:
        rows = db.execute(
            "SELECT revision,at,source,targets,updates "
            "FROM audit ORDER BY revision DESC LIMIT 30"
        ).fetchall()
    return [
        {
            "revision": row["revision"],
            "at": row["at"],
            "source": row["source"],
            "targets": json.loads(row["targets"]),
            "updates": json.loads(row["updates"]),
        }
        for row in rows
    ]

def secure_request(handler, post=False):
    if handler.client_address[0] not in ("127.0.0.1", "::1"):
        return False
    host = handler.headers.get("Host", "")
    try:
        parsed = urlsplit("http://" + host)
        if (
            parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
        origin = handler.headers.get("Origin")
        if origin:
            source = urlsplit(origin)
            if (
                source.scheme not in ("http", "https")
                or source.netloc != host
                or source.path not in ("", "/")
                or source.query
                or source.fragment
            ):
                return False
        elif post:
            return False
    except ValueError:
        return False

    if post and not hmac.compare_digest(
        handler.headers.get("X-SI-CSRF", ""), TOKEN
    ):
        return False
    return True

def send(handler, code, payload, html=False):
    raw = (
        payload.encode("utf-8") if html
        else json.dumps(payload, allow_nan=False).encode("utf-8")
    )
    handler.send_response(code)
    handler.send_header(
        "Content-Type",
        "text/html; charset=utf-8" if html else "application/json",
    )
    handler.send_header("Cache-Control", "no-store, private")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'nonce-" + TOKEN + "'; "
        "style-src 'unsafe-inline'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    )
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)

def handle(handler):
    path = urlsplit(handler.path).path
    if path not in ROUTES:
        return False

    post = handler.command == "POST"
    if not secure_request(handler, post=post):
        send(handler, 403, {
            "error": "LOCAL_SAME_ORIGIN_ACCESS_REQUIRED",
            "message": "Use localhost through your SSH tunnel.",
        })
        return True

    try:
        if handler.command == "GET" and path == "/strategy-settings":
            page = PAGE.replace("__NONCE__", TOKEN).replace(
                "__TOKEN_JSON__", json.dumps(TOKEN)
            )
            send(handler, 200, page, html=True)

        elif handler.command == "GET" and path == "/api/v1/strategy-settings":
            send(handler, 200, read_settings())

        elif handler.command == "GET" and path == "/api/v1/strategy-settings/history":
            send(handler, 200, {"history": history()})

        elif post and path == "/api/v1/strategy-settings":
            if handler.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ValidationError("Content-Type must be application/json.")
            try:
                length = int(handler.headers.get("Content-Length", "0"))
            except ValueError:
                raise ValidationError("Invalid Content-Length.") from None
            if not 0 < length <= 16384:
                raise ValidationError("Invalid request size.")
            try:
                body = json.loads(handler.rfile.read(length))
            except (ValueError, UnicodeError):
                raise ValidationError("Invalid JSON.") from None
            send(handler, 200, update_settings(body))

        else:
            send(handler, 405, {"error": "METHOD_NOT_ALLOWED"})

    except RevisionConflict as error:
        send(handler, 409, {"error": str(error)})
    except ValidationError as error:
        send(handler, 400, {"error": str(error)})
    except Exception as error:
        send(handler, 503, {
            "error": "SETTINGS_UNAVAILABLE",
            "error_type": type(error).__name__,
        })
    return True

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Strategy Settings</title>
<style>
:root{color-scheme:dark}
body{font:16px system-ui;background:#0b1220;color:#e5e7eb;margin:0}
main{max-width:1100px;margin:30px auto;padding:20px}
section{background:#152137;padding:22px;border-radius:12px;margin:18px 0}
h1,h2{margin-top:0}
.warning{border-left:5px solid #f59e0b}
button,input{font:inherit}
button{background:#2563eb;color:white;border:0;padding:10px 16px;
border-radius:7px;cursor:pointer;margin:5px}
button:disabled{opacity:.5;cursor:wait}
input[type=number]{width:120px;padding:8px;background:#0b1220;color:white;
border:1px solid #64748b;border-radius:6px}
label{display:inline-block;margin:8px}
.parameter{display:flex;justify-content:space-between;align-items:center;
gap:15px;border-bottom:1px solid #334155;padding:8px 0}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:10px;border-bottom:1px solid #334155}
.scroll{overflow:auto}
pre{white-space:pre-wrap;overflow-wrap:anywhere}
a{color:#93c5fd}
</style></head><body><main>
<h1>Trading Strategy Settings</h1>
<p><a href="/">Return to dashboard</a></p>
<section class="warning">
<h2>Desired configuration — not live worker authorization</h2>
<p>Saving here does not place orders, restart bots, change capital reserves,
or enable trading. Workers are not connected to this settings store yet.</p>
<p>Crypto's existing exit patch remains unchanged. Equity's order-reference
repair is separate. This console does not restart its stopped guard.</p>
</section>
<section>
<h2>Current desired settings</h2>
<p id="revision">Loading…</p>
<div class="scroll"><table><thead><tr>
<th>Bot</th><th>Stop %</th><th>Profit start %</th><th>Goal %</th>
<th>Retracement floor %</th><th>Daily loss %</th><th>Max positions</th>
<th>Application status</th>
</tr></thead><tbody id="profiles"></tbody></table></div>
</section>
<section>
<h2>Edit one, multiple, or all bots</h2>
<div>
<label><input type="checkbox" class="bot" value="crypto"> Crypto</label>
<label><input type="checkbox" class="bot" value="equity"> US Equity</label>
<label><input type="checkbox" class="bot" value="zerodha"> Zerodha</label>
<button id="all" type="button">Select all</button>
<button id="none" type="button">Clear selection</button>
</div>
<p>Check only the parameters you want to change. Changes apply to every
selected bot. Unchecked parameters remain unchanged.</p>
<div id="parameters"></div>
<p>Stop percentages exclude fees. Profit percentages are net of fees.
The profit floor means exit on a return to that level after exceeding it;
it is not a mandatory profit cap. The goal is an objective, not a guarantee.</p>
<button id="save" type="button">Save desired settings — not live</button>
<button id="reload" type="button">Reload</button>
<pre id="message" role="status"></pre>
</section>
<section><h2>Change history</h2><pre id="history"></pre></section>
</main>
<script nonce="__NONCE__">
const csrf=__TOKEN_JSON__;
let revision=null, schema={};
const keys=["stop_loss_pct","profit_start_pct","profit_goal_pct",
"profit_floor_pct","daily_loss_limit_pct","max_positions"];
const defaults={stop_loss_pct:1,profit_start_pct:1.5,profit_goal_pct:2,
profit_floor_pct:4,daily_loss_limit_pct:2,max_positions:10};
const el=id=>document.getElementById(id);
async function request(path,options={}){
 const response=await fetch(path,{...options,cache:"no-store",
 credentials:"same-origin"});
 const body=await response.json();
 if(!response.ok)throw Error(body.error||"Request failed");
 return body;
}
async function load(){
 const data=await request("/api/v1/strategy-settings");
 revision=data.revision;schema=data.schema;
 el("revision").textContent="Revision "+revision+" · "+data.updated_at;
 el("profiles").replaceChildren();
 for(const [bot,profile] of Object.entries(data.profiles)){
  const row=document.createElement("tr");
  for(const value of [bot,...keys.map(key=>profile[key]),
      "SAVED — NOT CONNECTED"]){
   const cell=document.createElement("td");cell.textContent=value;
   row.append(cell);
  }
  el("profiles").append(row);
 }
 if(!el("parameters").children.length){
  for(const key of keys){
   const rule=schema[key],row=document.createElement("div");
   row.className="parameter";
   const label=document.createElement("label");
   const use=document.createElement("input");
   use.type="checkbox";use.id="use-"+key;
   label.append(use,document.createTextNode(" "+rule.label));
   const value=document.createElement("input");
   value.type="number";value.id="value-"+key;
   value.min=rule.min;value.max=rule.max;
   value.step=rule.integer?"1":"0.0001";value.value=defaults[key];
   row.append(label,value);el("parameters").append(row);
  }
 }
 const history=await request("/api/v1/strategy-settings/history");
 el("history").textContent=JSON.stringify(history.history,null,2);
}
el("all").onclick=()=>document.querySelectorAll(".bot").forEach(x=>x.checked=true);
el("none").onclick=()=>document.querySelectorAll(".bot").forEach(x=>x.checked=false);
el("reload").onclick=()=>load().catch(e=>el("message").textContent=e.message);
el("save").onclick=async()=>{
 try{
  const targets=[...document.querySelectorAll(".bot:checked")].map(x=>x.value);
  const updates={};
  for(const key of keys){
   if(el("use-"+key).checked){
    const raw=el("value-"+key).value;
    if(raw.trim()==="")throw Error("Enter a value for "+key);
    const value=Number(raw);
    if(!Number.isFinite(value))throw Error("Invalid value for "+key);
    updates[key]=value;
   }
  }
  if(!targets.length)throw Error("Select at least one bot.");
  if(!Object.keys(updates).length)throw Error("Select at least one parameter.");
  if(!confirm("Save DESIRED settings for "+targets.join(", ")+
    "? These changes will NOT activate trading or change live workers."))return;
  el("save").disabled=true;
  const result=await request("/api/v1/strategy-settings",{
   method:"POST",
   headers:{"Content-Type":"application/json","X-SI-CSRF":csrf},
   body:JSON.stringify({expected_revision:revision,targets,updates})
  });
  el("message").textContent=JSON.stringify(result,null,2);
  await load();
 }catch(error){el("message").textContent=error.message}
 finally{el("save").disabled=false}
};
load().catch(error=>el("message").textContent=error.message);
</script></body></html>"""
