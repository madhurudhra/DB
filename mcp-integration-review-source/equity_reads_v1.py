"""Account-scoped Equity reads and fill imports. No order submission."""
import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

BROKER = "robinhood_equity"

def stamp():
    return datetime.now(timezone.utc).isoformat()

def number(value, positive=False):
    if isinstance(value, bool) or value is None:
        raise ValueError("Missing numeric broker value")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError("Invalid numeric broker value")
    return result

def scrub(value):
    denied = {"access_token", "refresh_token", "api_key", "apikey", "password", "authorization", "private_key", "private_key_b64", "public_token", "request_token", "secret", "enctoken"}
    if isinstance(value, dict):
        return {k: scrub(v) for k,v in value.items() if str(k).lower() not in denied}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value

def check_response(value):
    if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
        raise ValueError("Broker response envelope missing")
    if value.get("error") or value.get("errors") or value["data"].get("error") or value["data"].get("errors"):
        raise ValueError("Broker returned an error")
    return value

async def fetch(config, revision, *, call=None, last4=None):
    if call is None:
        from rh_equity_review import call, LAST4
        last4 = LAST4
    async def invoke(tool, arguments):
        raw = await asyncio.wait_for(call(tool, arguments), timeout=25)
        return check_response(json.loads(raw))
    requested = stamp()
    response = await invoke("get_accounts", {})
    accounts = response["data"].get("accounts")
    if not isinstance(accounts, list):
        raise ValueError("Account list missing")
    configured = config.get("account_ref")
    if configured is not None and (not isinstance(configured,str) or not configured or configured != configured.strip()):
        raise ValueError("Configured Equity account is invalid")
    if configured:
        candidates = [a for a in accounts if isinstance(a,dict) and a.get("account_number") == configured and a.get("agentic_allowed") is True]
    else:
        if not isinstance(last4,str) or len(last4)!=4 or not last4.isdigit():
            raise ValueError("Existing read-helper account selector missing")
        candidates = [a for a in accounts if isinstance(a,dict) and isinstance(a.get("account_number"),str) and a["account_number"].endswith(last4) and a.get("agentic_allowed") is True]
    if len(candidates) != 1:
        raise ValueError("Equity account selection is not unique")
    account = candidates[0]["account_number"]
    parts = {"accounts": {"status":"BROKER_RESPONSE_RECEIVED", "requested_at":requested,
              "received_at":stamp(), "source_timestamp":None,
              "data":scrub({"data":{"accounts":[candidates[0]]}})}}
    for name, tool, field in [("holdings","get_equity_positions","positions"),("orders","get_equity_orders","orders")]:
        started = stamp()
        try:
            value = await invoke(tool,{"account_number":account})
            if not isinstance(value["data"].get(field),list):
                raise ValueError("Expected Equity " + field + " list")
            parts[name] = {"status":"BROKER_RESPONSE_RECEIVED", "requested_at":started,
                "received_at":stamp(), "source_timestamp":None, "data":scrub(value)}
        except Exception as exc:
            parts[name] = {"status":"UNAVAILABLE", "requested_at":started, "received_at":stamp(),
                "source_timestamp":None,"data":None,"error_type":type(exc).__name__}
    return {"source":"existing_robinhood_mcp", "coverage":"EQUITY_POSITIONS_AND_ORDERS_READ",
        "parts":parts, "account_ref":account, "configuration_revision":revision,
        "configured_account_match":bool(configured and configured==account),
        "selection_basis":"CONFIGURED_ACCOUNT" if configured else "EXISTING_READ_HELPER_LAST4_READ_ONLY",
        "history_completeness_verified":False, "automatic_execution_enabled_by_this_adapter":False}

def configuration(db):
    row=db.execute("SELECT revision,config_json FROM independent_bot_configurations WHERE bot='equity'").fetchone()
    if row is None:
        raise ValueError("Saved Equity configuration missing")
    value=json.loads(row["config_json"])
    if value.get("currency")!="USD":
        raise ValueError("Equity configuration must be USD")
    return value, row["revision"]

def load_snapshot():
    from app.core.config import get_settings
    p=Path(get_settings().database_path).resolve()
    with closing(sqlite3.connect(p.as_uri()+"?mode=ro",uri=True,timeout=5)) as db:
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        config,revision=configuration(db)
    return asyncio.run(fetch(config,revision))

def equity_dashboard_payload():
    return load_snapshot()

def rows(snapshot,name,field):
    part=snapshot["parts"][name]
    if part.get("status")!="BROKER_RESPONSE_RECEIVED":
        raise ValueError("Equity "+name+" read failed")
    value=part["data"]["data"][field]
    if not isinstance(value,list):
        raise ValueError("Equity list missing")
    return value

def extract_fills(orders):
    fills=[]
    seen=set()
    for order in orders:
        if not isinstance(order,dict):
            raise ValueError("Invalid order row")
        executions=order.get("executions")
        if not isinstance(executions,list):
            raise ValueError("Order executions list missing")
        cumulative=number(order.get("cumulative_quantity"))
        if cumulative==0 and not executions:
            continue
        symbol=order.get("symbol")
        side=order.get("side")
        order_id=order.get("id")
        if not isinstance(symbol,str) or not symbol or side not in ("buy","sell") or not isinstance(order_id,str) or not order_id:
            raise ValueError("Order identity missing")
        order_rows=[]
        for execution in executions:
            fill_id=execution.get("id")
            if not isinstance(fill_id,str) or not fill_id or fill_id in seen:
                raise ValueError("Missing or duplicate execution identity")
            seen.add(fill_id)
            quantity=number(execution.get("quantity"),positive=True)
            price=number(execution.get("price"),positive=True)
            fee=number(execution["fees"]) if execution.get("fees") is not None else None
            when=datetime.fromisoformat(execution["timestamp"].replace("Z","+00:00"))
            if when.tzinfo is None:
                raise ValueError("Execution timezone missing")
            order_rows.append({"id":fill_id,"order_id":order_id,"symbol":symbol.upper(),"side":side.upper(),
                "quantity":str(quantity),"price":str(price),"fee":str(fee) if fee is not None else None,
                "filled_at":when.astimezone(timezone.utc).isoformat(),
                "raw":json.dumps({"order":order,"execution":execution},separators=(",",":"))})
        if sum((Decimal(x["quantity"]) for x in order_rows),Decimal(0))!=cumulative:
            raise ValueError("Executions do not match cumulative filled quantity; incomplete history")
        if order.get("fees") is not None and all(x["fee"] is not None for x in order_rows):
            if number(order["fees"])!=sum((Decimal(x["fee"]) for x in order_rows),Decimal(0)):
                raise ValueError("Execution fees disagree with order fee total")
        fills.extend(order_rows)
    return fills

def import_snapshot(db,snapshot):
    """Atomic, idempotent ledger import. Never writes capital PASS or submits orders."""
    if db.in_transaction:
        raise ValueError("Importer requires its own transaction")
    if snapshot.get("configured_account_match") is not True:
        raise ValueError("Exact configured Equity account required before ledger import")
    account=snapshot["account_ref"]
    positions=rows(snapshot,"holdings","positions")
    fills=extract_fills(rows(snapshot,"orders","orders"))
    db.execute("BEGIN IMMEDIATE")
    try:
        cfg,revision=configuration(db)
        if cfg.get("account_ref")!=account or revision!=snapshot["configuration_revision"]:
            raise ValueError("Equity account/configuration changed during read")
        inserted=resolved=0
        for f in fills:
            old=db.execute("SELECT * FROM fills WHERE broker=? AND account_ref=? AND broker_fill_id=?",(BROKER,account,f["id"])).fetchone()
            if old:
                if old["broker_order_id"]!=f["order_id"] or old["symbol"]!=f["symbol"] or old["side"]!=f["side"] or Decimal(old["quantity"])!=Decimal(f["quantity"]) or Decimal(old["price"])!=Decimal(f["price"]) or old["currency"]!="USD" or datetime.fromisoformat(old["filled_at"].replace("Z","+00:00"))!=datetime.fromisoformat(f["filled_at"]):
                    raise ValueError("Conflicting existing execution; manual review required")
                if old["fee"] is not None and f["fee"] is not None and Decimal(old["fee"])!=Decimal(f["fee"]):
                    raise ValueError("Conflicting existing execution fee")
                if old["fee"] is None and f["fee"] is not None:
                    db.execute("UPDATE fills SET fee=?,raw_json=? WHERE id=?",(f["fee"],f["raw"],old["id"]))
                    resolved+=1
            else:
                db.execute("INSERT INTO fills (broker,account_ref,broker_fill_id,broker_order_id,symbol,side,quantity,price,fee,currency,filled_at,raw_json) VALUES (?,?,?,?,?,?,?,?,?,'USD',?,?)",(BROKER,account,f["id"],f["order_id"],f["symbol"],f["side"],f["quantity"],f["price"],f["fee"],f["filled_at"],f["raw"]))
                inserted+=1
        observed=stamp()
        for position in positions:
            symbol=position.get("symbol")
            if not isinstance(symbol,str) or not symbol:
                raise ValueError("Position symbol missing")
            quantity=number(position.get("quantity"))
            average=number(position["average_buy_price"]) if position.get("average_buy_price") is not None else None
            db.execute("INSERT INTO position_snapshots (broker,account_ref,symbol,quantity,average_cost,market_value,currency,observed_at,raw_json) VALUES (?,?,?,?,?,NULL,'USD',?,?)",(BROKER,account,symbol.upper(),str(quantity),str(average) if average is not None else None,observed,json.dumps(position)))
        report={"status":"READ_IMPORT_ONLY","account_ending":account[-4:],"inserted_fills":inserted,
            "fees_resolved":resolved,"position_rows":len(positions),
            "unknown_fee_executions_in_response":sum(f["fee"] is None for f in fills),
            "history_completeness_verified":False,"capital_baseline_verified":False,
            "manual_requests_reconciled":False,"automatic_execution_enabled":False}
        db.execute("INSERT INTO reconciliation_runs (broker,account_ref,finished_at,status,discrepancies_json) VALUES (?,?,?,'READ_IMPORT_ONLY',?)",(BROKER,account,observed,json.dumps(report)))
        db.execute("INSERT INTO audit_events (event_type,bot_name,details_json) VALUES ('EQUITY_READ_IMPORT','equity',?)",(json.dumps(report),))
        db.commit()
        return report
    except BaseException:
        db.rollback()
        raise

def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--import-fills",action="store_true")
    args=parser.parse_args()
    snapshot=load_snapshot()
    if args.import_fills:
        from app.core.config import get_settings
        with closing(sqlite3.connect(get_settings().database_path,timeout=10)) as db:
            db.row_factory=sqlite3.Row
            print(json.dumps(import_snapshot(db,snapshot),indent=2))
    else:
        print(json.dumps({"status":"READ_ONLY","account_ending":snapshot["account_ref"][-4:],
            "configured_account_match":snapshot["configured_account_match"],
            "positions_read_status":snapshot["parts"]["holdings"]["status"],
            "orders_read_status":snapshot["parts"]["orders"]["status"],
            "automatic_execution_enabled":False},indent=2))

if __name__=="__main__":
    main()

