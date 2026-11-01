"""Standalone Equity worker. No live actions on import. See README before use."""
import argparse
import asyncio
import contextlib
import fcntl
import importlib.util
import inspect
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener, HTTPRedirectHandler
from zoneinfo import ZoneInfo

VERSION = "equity-worker-reference-1"
TERMINAL = {"filled", "cancelled", "rejected", "failed", "voided", "partially_filled_rest_cancelled"}

class Blocked(RuntimeError):
    pass

def number(v):
    if isinstance(v, bool):
        raise Blocked("BOOLEAN_NUMBER")
    try:
        n = Decimal(str(v))
    except Exception as e:
        raise Blocked("INVALID_NUMBER") from e
    if not n.is_finite():
        raise Blocked("NONFINITE_NUMBER")
    return n

def stamp():
    return datetime.now(timezone.utc).isoformat()

def age(value):
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise Blocked("TIMESTAMP_TIMEZONE_REQUIRED")
    return (datetime.now(timezone.utc) - d).total_seconds()

def regular_window():
    d = datetime.now(ZoneInfo("America/New_York"))
    return d.weekday() < 5 and 571 <= d.hour * 60 + d.minute < 959

def atomic_json(path, value):
    import tempfile
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".equity-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, allow_nan=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at TEXT NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS intents(
      ref TEXT PRIMARY KEY, symbol TEXT NOT NULL,side TEXT NOT NULL,
      quantity TEXT NOT NULL,reservation TEXT NOT NULL,state TEXT NOT NULL,
      created REAL NOT NULL,broker_id TEXT,approval_id TEXT,
      filled TEXT NOT NULL DEFAULT '0',average TEXT NOT NULL DEFAULT '0',
      fees TEXT NOT NULL DEFAULT '0',error TEXT);
    CREATE TABLE IF NOT EXISTS samples(symbol TEXT NOT NULL,t REAL NOT NULL,price TEXT NOT NULL,PRIMARY KEY(symbol,t));
    CREATE TABLE IF NOT EXISTS milestones(symbol TEXT PRIMARY KEY,policy TEXT NOT NULL,armed INTEGER NOT NULL DEFAULT 0,floor INTEGER NOT NULL DEFAULT 0);
    """)
    db.commit()
    return db

def get(db, key, default=None):
    row = db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default

def put(db, key, value):
    db.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, json.dumps(value, allow_nan=False)))

def event(db, kind, **data):
    payload = {"at": stamp(), "kind": kind, **data}
    db.execute("INSERT INTO events(at,kind,data) VALUES(?,?,?)", (payload["at"], kind, json.dumps(data, allow_nan=False)))
    db.commit()
    print(json.dumps(payload, allow_nan=False), flush=True)

def score(prices):
    if len(prices) != 60 or any(number(p) <= 0 for p in prices):
        raise Blocked("SIXTY_POSITIVE_SAMPLES_REQUIRED")
    p = list(map(number, prices))
    return 40 * (sum(p[-20:])/20 > sum(p)/60) + 30 * (p[-1] > p[-7]) + 30 * (p[-1] > max(p[-21:-1]))

def owned_book(db):
    holdings, realized = {}, Decimal(0)
    for r in db.execute("SELECT * FROM intents ORDER BY created,ref"):
        q, p, fees = number(r["filled"]), number(r["average"]), number(r["fees"])
        if q == 0:
            continue
        if q < 0 or p <= 0 or fees < 0:
            raise Blocked("INVALID_LEDGER_FILL")
        h = holdings.setdefault(r["symbol"], {"qty": Decimal(0), "gross": Decimal(0), "cost": Decimal(0)})
        if r["side"] == "buy":
            h["qty"] += q
            h["gross"] += q*p
            h["cost"] += q*p+fees
        elif r["side"] == "sell":
            if q > h["qty"]:
                raise Blocked("SELL_EXCEEDS_OWNED_POSITION")
            fraction = q/h["qty"]
            realized += q*p-fees-h["cost"]*fraction
            h["gross"] *= 1-fraction
            h["cost"] *= 1-fraction
            h["qty"] -= q
        else:
            raise Blocked("UNKNOWN_ORDER_SIDE")
    return {s:h for s,h in holdings.items() if h["qty"] > 0}, realized

def pending(db):
    return [r for r in db.execute("SELECT * FROM intents ORDER BY created") if r["state"] not in TERMINAL | {"LOCAL_BLOCKED"}]

def buy_capacity(db, allocated, pct, broker_cash):
    allocated, pct, broker_cash = map(number, (allocated,pct,broker_cash))
    if allocated < 0 or not 0 <= pct <= 100 or broker_cash < 0:
        raise Blocked("INVALID_CAPITAL")
    h, realized = owned_book(db)
    cost = sum((v["cost"] for v in h.values()), Decimal(0))
    holds = sum((number(r["reservation"]) for r in pending(db) if r["side"] == "buy"), Decimal(0))
    # Retain the full reservation on partially filled buys until terminal.
    # This deliberately double-counts some commitment rather than over-spending.
    ceiling = allocated*pct/100
    return max(Decimal(0), min(ceiling-cost-holds, allocated+realized-cost-holds, broker_cash))

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Blocked("LOCAL_SETTINGS_REDIRECT_REFUSED")

def read_local(base, path):
    if base != "http://127.0.0.1:8000":
        raise Blocked("UNAPPROVED_SETTINGS_ORIGIN")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    req = Request(base+path, headers={"Accept":"application/json", "Cache-Control":"no-cache"})
    with opener.open(req, timeout=3) as response:
        raw = response.read(262145)
        if response.status != 200 or len(raw) > 262144:
            raise Blocked("SETTINGS_HTTP_ERROR")
        return json.loads(raw)

def policy_snapshot(cfg):
    base = cfg.get("settings_origin", "http://127.0.0.1:8000")
    settings = read_local(base, "/api/v1/strategy-settings")
    allocation = read_local(base, "/api/v1/config")["bot_allocations"]["equity"]
    p = settings["profiles"]["equity"]
    if "equity" in settings.get("capital_utilization_defaulted_bots", ["equity"]):
        raise Blocked("UTILIZATION_NOT_PERSISTED")
    p = {**p, "allocated_capital": allocation["allocated_capital"], "revision":settings["revision"]}
    for key in ("allocated_capital","capital_utilization_pct","stop_loss_pct","profit_start_pct","profit_goal_pct","profit_floor_pct","daily_loss_limit_pct"):
        p[key] = str(number(p[key]))
    if not (0 < number(p["stop_loss_pct"]) < 100 and 0 < number(p["daily_loss_limit_pct"]) <= 100 and 0 <= number(p["capital_utilization_pct"]) <= 100):
        raise Blocked("INVALID_RISK_POLICY")
    if not 0 < number(p["profit_start_pct"]) <= number(p["profit_goal_pct"]) <= number(p["profit_floor_pct"]):
        raise Blocked("INVALID_PROFIT_POLICY")
    if type(p["max_positions"]) is not int or p["max_positions"] < 1:
        raise Blocked("INVALID_POSITION_LIMIT")
    authorization = json.loads(Path(cfg["funding_authorization_file"]).read_text())
    if authorization.get("account_number") != cfg["account_number"] or authorization.get("bot") != "equity":
        raise Blocked("FUNDING_ACCOUNT_MISMATCH")
    if number(authorization["allocated_capital"]) != number(p["allocated_capital"]):
        raise Blocked("ALLOCATION_NOT_MATCHED_TO_FUNDING_AUTHORIZATION")
    return p

def universe(cfg):
    data = json.loads(Path(cfg["universe_file"]).read_text())
    if not -2 <= age(data["updated_at"]) <= cfg.get("universe_max_age_seconds", 300):
        raise Blocked("STALE_UNIVERSE")
    symbols = data["symbols"]
    import re
    if not isinstance(symbols,list) or not symbols or len(symbols) > cfg.get("universe_max_symbols", 100):
        raise Blocked("INVALID_UNIVERSE")
    if any(not isinstance(s,str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}",s) for s in symbols):
        raise Blocked("INVALID_UNIVERSE_SYMBOL")
    return list(dict.fromkeys(symbols))


# SI_STAGED_ENTRY_EVIDENCE_GATE_V2_MARKET_ESTIMATE
def require_entry_evidence(cfg, symbol, quantity, reservation, policy):
    """Research is advisory; actual execution checks remain in Worker."""
    import re

    if cfg.get("state_directory") != "/var/lib/si-equity-buy-worker":
        raise Blocked("UNAPPROVED_STATE_DIRECTORY")
    if not isinstance(symbol, str) or not re.fullmatch(
        r"[A-Z][A-Z0-9.-]{0,9}", symbol
    ):
        raise Blocked("INVALID_ORDER_SYMBOL")
    account = cfg.get("account_number")
    if not isinstance(account, str) or not account or "*" in account:
        raise Blocked("FULL_ACCOUNT_BINDING_REQUIRED")
    if not isinstance(policy, dict):
        raise Blocked("INVALID_RISK_POLICY")
    if type(policy.get("revision")) is not int or policy["revision"] < 1:
        raise Blocked("INVALID_POLICY_REVISION")

    q = number(quantity)
    amount = number(reservation)
    if q <= 0 or q != q.quantize(Decimal("0.000001")):
        raise Blocked("INVALID_ORDER_QUANTITY")
    if amount <= 0:
        raise Blocked("INVALID_RESERVATION")

    return {
        "mode": "RESEARCH_ADVISORY_ONLY",
        "research_verified": False,
        "research_authorizes_order": False,
        "pending_issues": [
            "NEWS_RISK_ASSESSMENT",
            "EARNINGS_RISK_ASSESSMENT",
            "APPROVED_MARKET_REGIME_POLICY",
            "VERIFIED_INSTRUMENT_CLASSIFICATION",
            "CONFIGURABLE_BUY_RESTRICTION_INTEGRATION",
            "SEPARATE_EXIT_COVERAGE_CERTIFICATION",
            "STAGED_SNAPSHOT_POLICY_BOUNDARY_NOT_ENFORCED",
        ],
    }

class MCPBroker:
    """Uses the existing VPS OAuth helper. No credentials embedded."""
    def __init__(self, cfg):
        self.cfg = cfg
    async def __aenter__(self):
        path = Path(self.cfg["broker_monitor_path"])
        spec = importlib.util.spec_from_file_location("equity_worker_monitor", path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        a = m.auth
        self.stack = contextlib.AsyncExitStack()
        await self.stack.__aenter__()
        try:
            oauth = a.OAuthClientProvider(server_url=a.SERVER,client_metadata=a.OAuthClientMetadata(client_name="super-intelligence-vps",redirect_uris=[a.REDIRECT],grant_types=["authorization_code","refresh_token"],response_types=["code"],token_endpoint_auth_method="none"),storage=a.FileStorage(),redirect_handler=m.refuse_login,callback_handler=m.refuse_login)
            client = await self.stack.enter_async_context(a.httpx2.AsyncClient(auth=oauth,timeout=a.httpx2.Timeout(30,read=60)))
            streams = await self.stack.enter_async_context(a.streamable_http_client(a.SERVER,http_client=client))
            self.session = await self.stack.enter_async_context(a.ClientSession(streams[0],streams[1]))
            await self.session.initialize()
            return self
        except BaseException:
            await self.stack.aclose()
            raise
    async def __aexit__(self, *exc):
        await self.stack.aclose()
    async def call(self, name, args):
        allowed = {"get_accounts","get_portfolio","get_equity_positions","get_equity_orders","get_equity_quotes","get_equity_tradability","review_equity_order","place_equity_order"}
        if name not in allowed:
            raise Blocked("BROKER_TOOL_NOT_ALLOWED")
        params = inspect.signature(self.session.call_tool).parameters
        if "arguments" in params:
            result = await self.session.call_tool(name,arguments=args)
        elif "params" in params:
            from mcp.types import CallToolRequestParams
            result = await self.session.call_tool(params=CallToolRequestParams(name=name,arguments=args))
        else:
            raise Blocked("UNSUPPORTED_MCP_SIGNATURE")
        envelope = result.model_dump(mode="json",by_alias=True)
        if envelope.get("isError") or envelope.get("is_error"):
            raise Blocked("BROKER_TOOL_ERROR")
        content = envelope.get("structuredContent",envelope.get("structured_content"))
        if content is None:
            content = json.loads("\n".join(i["text"] for i in envelope.get("content",[]) if i.get("type")=="text"))
        if not isinstance(content,dict) or not isinstance(content.get("data"),dict):
            raise Blocked("INVALID_BROKER_RESPONSE")
        return content["data"]
    async def pages(self, name, key):
        rows, cursor, seen = [], None, set()
        for _ in range(100):
            args = {"account_number":self.cfg["account_number"]}
            if cursor: args["cursor"] = cursor
            data = await self.call(name,args)
            page = data.get(key)
            if page is None: page = []
            if not isinstance(page,list): raise Blocked("INVALID_BROKER_PAGE")
            rows.extend(page)
            cursor = data.get("next")
            if not cursor: return rows
            if cursor in seen: raise Blocked("REPEATED_BROKER_CURSOR")
            seen.add(cursor)
        raise Blocked("BROKER_PAGINATION_LIMIT")

class Worker:
    def __init__(self,db,broker,cfg,controls):
        self.db,self.broker,self.cfg,self.controls = db,broker,cfg,controls
    def permission(self,side):
        try:
            c = self.controls()
            if not isinstance(c,dict): raise ValueError("INVALID_CONTROL_RESPONSE")
        except Exception as e:
            raise Blocked("CONTROL_STATE_UNAVAILABLE") from e
        if not all(c.get(k) is True for k in ("bot","auto_trade",side)):
            raise Blocked("PERMISSION_BLOCKED_"+side.upper())
    async def reconcile(self,orders):
        for row in list(self.db.execute("SELECT * FROM intents")):
            if row["state"] == "LOCAL_BLOCKED": continue
            matches = [o for o in orders if (row["broker_id"] and o.get("id")==row["broker_id"]) or (not row["broker_id"] and o.get("ref_id") in {row["ref"],row["approval_id"]}-{None})]
            if len(matches) != 1:
                if row["state"] in TERMINAL and not matches: continue
                event(self.db,"UNRESOLVED_ORDER_NO_RESUBMISSION",reference=row["ref"],symbol=row["symbol"])
                continue
            o = matches[0]
            if o.get("symbol") != row["symbol"] or o.get("side") != row["side"] or not o.get("id"):
                raise Blocked("ORDER_IDENTITY_MISMATCH")
            fill,avg,fees = number(o["cumulative_quantity"]),number(o.get("average_price") or "0"),number(o["fees"])
            if not number(row["filled"]) <= fill <= number(row["quantity"]) or fees < number(row["fees"]) or (fill > 0 and avg <= 0):
                raise Blocked("INVALID_RECONCILED_FILL")
            state = o["state"]
            if state not in TERMINAL | {"new","queued","unconfirmed","confirmed","partially_filled","pending_cancelled"}:
                raise Blocked("UNKNOWN_ORDER_STATE")
            self.db.execute("UPDATE intents SET state=?,broker_id=?,filled=?,average=?,fees=? WHERE ref=?",(state,o["id"],str(fill),str(avg),str(fees),row["ref"]))
        self.db.commit()
    async def quote(self,symbol):
        d = await self.broker.call("get_equity_quotes",{"symbols":[symbol]})
        matches = [r["quote"] for r in d.get("results") or [] if isinstance(r,dict) and isinstance(r.get("quote"),dict) and r["quote"].get("symbol")==symbol]
        if len(matches)!=1: raise Blocked("QUOTE_NOT_UNIQUE")
        q = matches[0]
        bid,ask = number(q["bid_price"]),number(q["ask_price"])
        if not q.get("has_traded") or q.get("state")!="active" or not 0 < bid <= ask:
            raise Blocked("INVALID_QUOTE")
        if any(not -2 <= age(q[k]) <= 10 for k in ("venue_bid_time","venue_ask_time")):
            raise Blocked("STALE_QUOTE")
        return bid,ask
    def history(self,symbol,bid,ask):
        now = time.time()
        self.db.execute("INSERT INTO samples VALUES(?,?,?)",(symbol,now,str((bid+ask)/2)))
        self.db.execute("DELETE FROM samples WHERE t<?",(now-3600,))
        self.db.commit()
        rows = self.db.execute("SELECT * FROM samples WHERE symbol=? ORDER BY t DESC LIMIT 60",(symbol,)).fetchall()[::-1]
        if len(rows)!=60 or any(not 5 <= rows[i]["t"]-rows[i-1]["t"] <= 25 for i in range(1,60)):
            return None
        return score([r["price"] for r in rows])
    async def submit(self,symbol,side,quantity,reservation,policy):
        self.permission(side)
        if not regular_window(): raise Blocked("OUTSIDE_REGULAR_WINDOW")
        quantity = number(quantity)
        if quantity<=0 or quantity != quantity.quantize(Decimal("0.000001")):
            raise Blocked("INVALID_ORDER_QUANTITY")
        if any(r["symbol"]==symbol for r in pending(self.db)):
            raise Blocked("SYMBOL_ORDER_PENDING")
        if side == "buy":
            require_entry_evidence(
                self.cfg, symbol, quantity, reservation, policy
            )
        args = {"account_number":self.cfg["account_number"],"symbol":symbol,"side":side,"type":"market","quantity":str(quantity),"time_in_force":"gfd","market_hours":"regular_hours"}
        preview = await self.broker.call("review_equity_order",args)
        if any(preview.get(k)!=args[k] for k in ("symbol","side","type")) or number(preview.get("quantity"))!=quantity:
            raise Blocked("REVIEW_IDENTITY_MISMATCH")
        if not isinstance(preview.get("order_checks"),dict) or preview["order_checks"]:
            event(self.db,"BROKER_ALERT_BLOCKED",symbol=symbol,preview=preview)
            raise Blocked("BROKER_REVIEW_ALERT")
        if preview.get("customer_approval_required") is not False:
            event(self.db,"BROKER_APP_APPROVAL_REQUIRED",symbol=symbol,side=side)
            raise Blocked("BROKER_APP_APPROVAL_REQUIRED")
        if self.cfg.get("programmatic_execution_authorized") is not True:
            raise Blocked("PROGRAMMATIC_EXECUTION_NOT_AUTHORIZED")
        if side=="buy":
            latest = await asyncio.to_thread(policy_snapshot,self.cfg)
            if latest != policy:
                raise Blocked("POLICY_CHANGED_DURING_REVIEW")
        self.permission(side)
        if not regular_window(): raise Blocked("OUTSIDE_REGULAR_WINDOW")
        latest_orders=await self.broker.pages("get_equity_orders","orders")
        await self.reconcile(latest_orders)
        owned_ids={r["broker_id"] for r in self.db.execute("SELECT broker_id FROM intents") if r["broker_id"]}
        if any(o.get("symbol")==symbol and o.get("state") not in TERMINAL and o.get("id") not in owned_ids for o in latest_orders):
            raise Blocked("EXTERNAL_ORDER_APPEARED_AFTER_REVIEW")
        if side=="buy" and symbol in self.legacy_symbols():
            raise Blocked("LEGACY_GUARD_OWNS_SYMBOL")
        bid,ask = await self.quote(symbol)
        quote_received=time.monotonic()
        if side=="buy":
            if quantity*ask*(1+number(self.cfg["cost_buffer_pct"])/100) > number(reservation):
                raise Blocked("QUOTE_EXCEEDS_RESERVATION")
            samples = self.db.execute("SELECT * FROM samples WHERE symbol=? ORDER BY t DESC LIMIT 60",(symbol,)).fetchall()[::-1]
            if len(samples)!=60 or time.time()-samples[-1]["t"]>25:
                raise Blocked("ENTRY_HISTORY_STALE_AFTER_REVIEW")
            prices=[number(r["price"]) for r in samples]
            prices[-1]=(bid+ask)/2
            if score(prices)!=100 or (ask-bid)/((ask+bid)/2)>Decimal("0.005"):
                raise Blocked("ENTRY_SIGNAL_CHANGED_AFTER_REVIEW")
            positions=await self.broker.pages("get_equity_positions","positions")
            await self.verify_ownership(positions)
            if pending(self.db): raise Blocked("ORDER_BECAME_PENDING")
            own,_=owned_book(self.db)
            if symbol not in own and len(own)>=policy["max_positions"]:
                raise Blocked("POSITION_LIMIT_AFTER_REVIEW")
            portfolio=await self.broker.call("get_portfolio",{"account_number":self.cfg["account_number"]})
            bp=portfolio["buying_power"]
            cash=max(Decimal(0),min(number(bp["buying_power"]),number(bp["unleveraged_buying_power"]),number(portfolio["cash"])))
            if number(reservation)>buy_capacity(self.db,policy["allocated_capital"],policy["capital_utilization_pct"],cash):
                raise Blocked("CAPACITY_CHANGED_AFTER_REVIEW")
            self.last_quote_received=quote_received
        else:
            holdings,_=owned_book(self.db)
            h=holdings.get(symbol)
            if h is None or h["qty"]!=quantity:
                raise Blocked("OWNED_QUANTITY_CHANGED")
            positions=await self.broker.pages("get_equity_positions","positions")
            await self.verify_ownership(positions)
            matches=[r for r in positions if r.get("symbol")==symbol]
            if len(matches)!=1 or number(matches[0]["shares_available_for_sells"])<quantity:
                raise Blocked("OWNED_SHARES_NOT_SELLABLE")
            if not self.exit_reason(symbol,h,bid,self.current_score(symbol,bid,ask),policy):
                raise Blocked("EXIT_SIGNAL_RECOVERED")
            self.last_quote_received=quote_received
        if side=="buy":
            latest=await asyncio.to_thread(policy_snapshot,self.cfg)
            if latest!=policy: raise Blocked("POLICY_CHANGED_BEFORE_INTENT")
        if side == "buy":
            require_entry_evidence(
                self.cfg, symbol, quantity, reservation, policy
            )
        if time.monotonic()-self.last_quote_received>5: raise Blocked("QUOTE_TOO_OLD_BEFORE_INTENT")
        ref = str(uuid.uuid4())
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if any(r["symbol"]==symbol for r in pending(self.db)):
                raise Blocked("SYMBOL_ORDER_PENDING")
            self.db.execute("INSERT INTO intents(ref,symbol,side,quantity,reservation,state,created) VALUES(?,?,?,?,?,'SUBMITTING',?)",(ref,symbol,side,str(quantity),str(reservation),time.time()))
            if side=="buy":
                pinned={k:policy[k] for k in ("profit_start_pct","profit_goal_pct","profit_floor_pct")}
                self.db.execute("INSERT OR IGNORE INTO milestones(symbol,policy) VALUES(?,?)",(symbol,json.dumps(pinned)))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        args["ref_id"] = ref
        try:
            if side == "buy":
                require_entry_evidence(
                    self.cfg, symbol, quantity, reservation, policy
                )
            self.permission(side)
            if not regular_window() or time.monotonic()-self.last_quote_received>5:
                raise Blocked("STALE_SUBMISSION_WINDOW")
        except Blocked:
            self.db.execute("UPDATE intents SET state='LOCAL_BLOCKED',reservation='0' WHERE ref=?",(ref,))
            self.db.commit()
            raise
        try:
            result = await self.broker.call("place_equity_order",args)
            order = result.get("order")
            app = result.get("approval")
            if isinstance(app,dict) and app.get("approval_id"):
                self.db.execute("UPDATE intents SET state='PENDING_APPROVAL',approval_id=? WHERE ref=?",(app["approval_id"],ref))
            elif isinstance(order,dict) and order.get("id") and order.get("symbol")==symbol and order.get("side")==side:
                self.db.execute("UPDATE intents SET state='ACKNOWLEDGED',broker_id=? WHERE ref=?",(order["id"],ref))
            else:
                raise Blocked("UNRECOGNIZED_SUBMISSION_RESPONSE")
            self.db.commit()
            event(self.db,"ORDER_SUBMISSION_RETURNED",reference=ref,symbol=symbol,fill_verified=False)
        except BaseException as e:
            self.db.execute("UPDATE intents SET state='UNKNOWN',error=? WHERE ref=?",(type(e).__name__,ref))
            self.db.commit()
            raise
        return ref
    async def cycle(self,policy,symbols):
        self.candidate_symbols=symbols
        accounts = (await self.broker.call("get_accounts",{})).get("accounts") or []
        matches = [a for a in accounts if a.get("account_number")==self.cfg["account_number"]]
        if len(matches)!=1 or matches[0].get("agentic_allowed") is not True or matches[0].get("state")!="active" or matches[0].get("deactivated") is not False or matches[0].get("permanently_deactivated") is not False:
            raise Blocked("EXACT_ACCOUNT_NOT_AUTHORIZED")
        orders = await self.broker.pages("get_equity_orders","orders")
        await self.reconcile(orders)
        positions = await self.broker.pages("get_equity_positions","positions")
        holdings,realized = owned_book(self.db)
        await self.verify_ownership(positions)
        baseline=get(self.db,"external_baseline",{})
        for row in list(self.db.execute("SELECT symbol FROM milestones")):
            if row["symbol"] not in holdings:
                self.db.execute("DELETE FROM milestones WHERE symbol=?",(row["symbol"],))
        self.db.commit()
        quotes,scores,failures = {},{},{}
        for s in sorted(set(symbols)|set(holdings)):
            try:
                quotes[s] = await self.quote(s)
                scores[s] = self.history(s,*quotes[s])
            except Exception as e:
                failures[s] = str(e)
        # Own positions only: preexisting holdings remain outside this worker.
        for s,h in holdings.items():
            if s not in quotes: continue
            try:
                if any(r["symbol"]==s for r in pending(self.db)): continue
                own_ids={r["broker_id"] for r in self.db.execute("SELECT broker_id FROM intents") if r["broker_id"]}
                if any(o.get("symbol")==s and o.get("state") not in TERMINAL and o.get("id") not in own_ids for o in orders):
                    raise Blocked("EXTERNAL_ORDER_ACTIVE")
                reason=self.exit_reason(s,h,quotes[s][0],scores.get(s),policy)
                if reason:
                    self.permission("sell")
                    event(self.db,"EXIT_CANDIDATE",symbol=s,reason=reason)
                    await self.submit(s,"sell",h["qty"],Decimal(0),policy)
            except Exception as e:
                event(self.db,"EXIT_BLOCKED",symbol=s,reason=str(e))
        if pending(self.db): return {"state":"ORDER_PENDING"}
        if any(s in failures for s in holdings):
            raise Blocked("OWNED_POSITION_QUOTE_UNAVAILABLE")
        if not regular_window(): return {"state":"OUTSIDE_REGULAR_WINDOW"}
        marked = sum((h["qty"]*quotes[s][0] for s,h in holdings.items()),Decimal(0))
        invested = sum((h["cost"] for h in holdings.values()),Decimal(0))
        equity = number(policy["allocated_capital"])+realized+marked-invested
        day = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        daily = get(self.db,"daily_loss")
        if daily is None:
            # First initialization requires a funded, flat managed portfolio.
            if holdings: raise Blocked("DAILY_OPENING_BASELINE_REQUIRED")
            daily={"day":day,"baseline":str(equity),"allocation":policy["allocated_capital"],"locked":False}
        elif daily["day"] != day:
            # Baseline definition: first verified regular-session cycle each NY day.
            daily={"day":day,"baseline":str(equity),"allocation":policy["allocated_capital"],"locked":False}
        adjustment = number(policy["allocated_capital"])-number(daily["allocation"])
        daily["baseline"] = str(number(daily["baseline"])+adjustment)
        daily["allocation"] = policy["allocated_capital"]
        daily["locked"] = daily["locked"] or number(daily["baseline"])<=0 or (number(daily["baseline"])-equity)/number(daily["baseline"])*100 >= number(policy["daily_loss_limit_pct"])
        put(self.db,"daily_loss",daily)
        self.db.commit()
        if daily["locked"]: raise Blocked("DAILY_LOSS_LOCK")
        if not self.cfg.get("_policy_fresh",True) or not self.cfg.get("_universe_fresh",True):
            raise Blocked("NEW_ENTRIES_BLOCKED_STALE_CONFIGURATION_OR_UNIVERSE")
        self.permission("buy")
        if not regular_window(): return {"state":"OUTSIDE_REGULAR_WINDOW"}
        portfolio = await self.broker.call("get_portfolio",{"account_number":self.cfg["account_number"]})
        bp = portfolio["buying_power"]
        broker_cash = max(Decimal(0),min(number(bp["buying_power"]),number(bp["unleveraged_buying_power"]),number(portfolio["cash"])))
        capacity = buy_capacity(self.db,policy["allocated_capital"],policy["capital_utilization_pct"],broker_cash)
        eligible = [s for s in symbols if s in quotes and scores.get(s)==100 and (quotes[s][1]-quotes[s][0])/((quotes[s][1]+quotes[s][0])/2)<=Decimal("0.005")]
        # Deterministic ties, not a claim of predictive superiority.
        for s in sorted(eligible):
            if s not in holdings and len(holdings)>=policy["max_positions"]: continue
            if s in self.legacy_symbols():
                event(self.db,"BUY_BLOCKED_LEGACY_GUARD_OWNS_SYMBOL",symbol=s)
                continue
            last = self.db.execute("SELECT MAX(created) FROM intents WHERE symbol=?",(s,)).fetchone()[0]
            if last and time.time()-last<600: continue
            if any(o.get("symbol")==s and o.get("state") not in TERMINAL for o in orders): continue
            if capacity<number(self.cfg["minimum_order_usd"]): break
            data = await self.broker.call("get_equity_tradability",{"account_number":self.cfg["account_number"],"symbols":[s]})
            rows = [r for r in data.get("results") or [] if r.get("symbol")==s]
            if len(rows)!=1: continue
            r=rows[0]
            # Match restriction categories, not trading-permission classifications.
            account_category=matches[0].get("brokerage_account_type")
            if not isinstance(account_category,str) or not account_category: continue
            typed=[t for t in r.get("account_type_tradabilities") or [] if t.get("account_type")==account_category]
            if r.get("tradeable") is not True or r.get("state")!="active" or r.get("fractional_tradability")!="tradable" or (len(typed)!=1 or typed[0].get("account_type_tradability")!="tradable") or "regular_hours" in (r.get("internal_halt_sessions") or []): continue
            q=(capacity/(quotes[s][1]*(1+number(self.cfg["cost_buffer_pct"])/100))).quantize(Decimal("0.000001"),rounding=ROUND_DOWN)
            if q<=0: continue
            reservation=capacity/len(eligible)
            q=(reservation/(quotes[s][1]*(1+number(self.cfg["cost_buffer_pct"])/100))).quantize(Decimal("0.000001"),rounding=ROUND_DOWN)
            if q*quotes[s][1]<number(self.cfg["minimum_order_usd"]): continue
            event(self.db,"BUY_CANDIDATE",symbol=s,quantity=str(q),reservation=str(reservation))
            ref=await self.submit(s,"buy",q,reservation,policy)
            return {"state":"BUY_SUBMITTED_OR_APPROVAL_PENDING","reference":ref,"fill_verified":False}
        return {"state":"SCREENING_NO_QUALIFIED_ORDER","buy_capacity":str(capacity),"symbols_screened":len(symbols)}
    def current_score(self,symbol,bid,ask):
        rows=self.db.execute("SELECT * FROM samples WHERE symbol=? ORDER BY t DESC LIMIT 60",(symbol,)).fetchall()[::-1]
        if len(rows)!=60 or time.time()-rows[-1]["t"]>25:
            return None
        prices=[number(r["price"]) for r in rows]
        prices[-1]=(bid+ask)/2
        return score(prices)
    def legacy_symbols(self):
        path=Path(self.cfg.get("legacy_guard_database","/var/lib/si-equity-stop-guard/guard.sqlite3"))
        if not path.is_file(): return set()
        with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=5) as db:
            return {row[0] for row in db.execute("SELECT symbol FROM managed")}
    async def verify_ownership(self,positions):
        holdings,_=owned_book(self.db)
        actual={}
        for row in positions:
            s=row["symbol"]
            if s in actual: raise Blocked("POSITION_NOT_UNIQUE")
            actual[s]=number(row["quantity"])
        baseline=get(self.db,"external_baseline",{})
        for s,h in holdings.items():
            if s not in baseline:
                raise Blocked("OWNED_POSITION_WITHOUT_BASELINE")
            if any(r["symbol"]==s for r in pending(self.db)):
                continue
            if actual.get(s,Decimal(0))!=number(baseline[s])+h["qty"]:
                raise Blocked("OWNED_HOLDINGS_CHANGED_EXTERNALLY:"+s)
        for s in set(actual)|set(baseline):
            if s not in holdings and not any(r["symbol"]==s for r in pending(self.db)):
                baseline[s]=str(actual.get(s,Decimal(0)))
        for s in self.candidate_symbols if hasattr(self,"candidate_symbols") else []:
            baseline.setdefault(s,"0")
        put(self.db,"external_baseline",baseline)
        self.db.commit()
    def exit_reason(self,s,h,bid,rally,p):
        avg=h["gross"]/h["qty"]
        pinned={k:p[k] for k in ("profit_start_pct","profit_goal_pct","profit_floor_pct")}
        row=self.db.execute("SELECT * FROM milestones WHERE symbol=?",(s,)).fetchone()
        if row:
            pinned=json.loads(row["policy"])
            armed,floor=bool(row["armed"]),bool(row["floor"])
        else:
            armed=floor=False
        armed=armed or bid>=avg*(1+number(pinned["profit_start_pct"])/100)
        floor=floor or bid>avg*(1+number(pinned["profit_floor_pct"])/100)
        self.db.execute("INSERT INTO milestones VALUES(?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET armed=excluded.armed,floor=excluded.floor",(s,json.dumps(pinned),int(armed),int(floor)))
        self.db.commit()
        reason=None
        if bid<=avg*(1-number(p["stop_loss_pct"])/100): reason="LOSS_STOP"
        elif floor and bid<=avg*(1+number(pinned["profit_floor_pct"])/100): reason="PROFIT_FLOOR"
        elif armed and bid<=avg: reason="ENTRY_PRICE"
        elif bid>=avg*(1+number(pinned["profit_goal_pct"])/100) and (rally is None or rally<70): reason="WEAK_OR_UNAVAILABLE_RALLY"
        return reason

async def run(cfg):
    if cfg.get("live_enabled") is not True:
        raise Blocked("LIVE_DISABLED_IN_CONFIGURATION")
    if cfg.get("programmatic_execution_authorized") is not True:
        raise Blocked("PROGRAMMATIC_EXECUTION_NOT_AUTHORIZED")
    root=Path(cfg["state_directory"])
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (root/"worker.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db=connect(root/"worker.sqlite3")
        spec=importlib.util.spec_from_file_location("equity_existing_controls",cfg["controls_module_path"])
        controls=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controls)
        controls.DB=Path(cfg["controls_database"])
        controls.initialize_worker(role="buy_worker")
        def flags():
            return controls.apply_pending()
        while True:
            started=time.monotonic()
            try:
                controls.apply_pending()
                try:
                    p=await asyncio.to_thread(policy_snapshot,cfg)
                    put(db,"last_good_policy",p)
                    db.commit()
                    cfg["_policy_fresh"]=True
                except Exception:
                    cfg["_policy_fresh"]=False
                    p=get(db,"last_good_policy")
                    if p is None: raise Blocked("NO_VALID_POLICY_LOADED")
                try:
                    symbols=await asyncio.to_thread(universe,cfg)
                    cfg["_universe_fresh"]=True
                except Exception:
                    symbols=[]
                    cfg["_universe_fresh"]=False
                async with MCPBroker(cfg) as broker:
                    worker=Worker(db,broker,cfg,flags)
                    result=await asyncio.wait_for(worker.cycle(p,symbols),timeout=180)
                result.update(last_cycle_completed=stamp(),settings_revision=p["revision"])
            except Exception as e:
                result={"state":"BLOCKED","reason":str(e),"error_type":type(e).__name__}
            result.update(
                heartbeat_at=stamp(),
                pid=os.getpid(),
                version=VERSION,
                buy_executor_implemented=True,
                broker_execution_verified=False,
                research_gate_mode="ADVISORY_ONLY",
                research_verified=False,
                pending_issues=[
                    "NEWS_RISK_ASSESSMENT",
                    "EARNINGS_RISK_ASSESSMENT",
                    "APPROVED_MARKET_REGIME_POLICY",
                    "VERIFIED_INSTRUMENT_CLASSIFICATION",
                    "CONFIGURABLE_BUY_RESTRICTION_INTEGRATION",
                    "SEPARATE_EXIT_COVERAGE_CERTIFICATION",
                    "STAGED_SNAPSHOT_POLICY_BOUNDARY_NOT_ENFORCED",
                ],
            )
            atomic_json(root/"status.json",result)
            event(db,"CYCLE",**result)
            await asyncio.sleep(max(1,10-(time.monotonic()-started)))

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--config",required=True)
    args=parser.parse_args()
    cfg=json.loads(Path(args.config).read_text())
    os.umask(0o077)
    asyncio.run(run(cfg))

if __name__=="__main__":
    main()
