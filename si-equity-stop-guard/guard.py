import asyncio
import fcntl
import inspect
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/opt/si-equity-r1")
import broker_monitor as monitor

auth = monitor.auth
monitor.ALLOWED.add("get_equity_quotes")

ROOT = Path("/var/lib/si-equity-stop-guard")
EXPECTED = {"SPCX": Decimal("0.053969"), "IONQ": Decimal("0.061680")}
TERMINAL = monitor.TERMINAL
VERSION = "equity-stop-only-v2-broker-id"

def dec(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise RuntimeError("NONFINITE_NUMBER")
    return result

def now():
    return datetime.now(timezone.utc).isoformat()

def log(symbol, state, **fields):
    print(json.dumps({
        "at": now(), "symbol": symbol, "state": state,
        "version": VERSION, **fields,
    }, allow_nan=False), flush=True)

def regular_window():
    current = datetime.now(ZoneInfo("America/New_York"))
    minute = current.hour * 60 + current.minute
    return current.weekday() < 5 and 571 <= minute < 959

def database():
    db = sqlite3.connect(ROOT / "guard.sqlite3", timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS managed(
            symbol TEXT PRIMARY KEY,
            initial_qty TEXT NOT NULL,
            average_ex_fees TEXT NOT NULL,
            external_executions TEXT NOT NULL,
            adopted_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS intents(
            ref TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            requested_qty TEXT NOT NULL,
            state TEXT NOT NULL,
            filled TEXT NOT NULL DEFAULT '0',
            created_at TEXT NOT NULL
        );
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(intents)")}
    additions = {
        "broker_id": "TEXT",
        "broker_ref": "TEXT",
        "average_price": "TEXT NOT NULL DEFAULT '0'",
        "fees": "TEXT NOT NULL DEFAULT '0'",
    }
    for name, definition in additions.items():
        if name not in columns:
            db.execute("ALTER TABLE intents ADD COLUMN " + name + " " + definition)
    db.commit()
    return db

def unpack(result):
    envelope = result.model_dump(mode="json", by_alias=True)
    if envelope.get("isError") or envelope.get("is_error"):
        raise RuntimeError("BROKER_TOOL_ERROR")
    payload = envelope.get(
        "structuredContent", envelope.get("structured_content")
    )
    if payload is None:
        payload = json.loads("\n".join(
            item["text"] for item in envelope.get("content", [])
            if item.get("type") == "text" and "text" in item
        ))
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise RuntimeError("BROKER_RESPONSE_SHAPE")
    return payload["data"]

async def order_call(session, name, arguments):
    if name not in {"review_equity_order", "place_equity_order"}:
        raise RuntimeError("WRITE_TOOL_REFUSED")
    if (
        arguments.get("side") != "sell"
        or arguments.get("type") != "market"
        or arguments.get("market_hours") != "regular_hours"
        or arguments.get("symbol") not in EXPECTED
        or dec(arguments.get("quantity", "0")) <= 0
    ):
        raise RuntimeError("ORDER_SCOPE_REFUSED")
    if not regular_window():
        raise RuntimeError("OUTSIDE_SUBMISSION_WINDOW")
    params = inspect.signature(session.call_tool).parameters
    if "arguments" in params:
        result = await session.call_tool(name, arguments=arguments)
    elif "params" in params:
        from mcp.types import CallToolRequestParams
        result = await session.call_tool(
            params=CallToolRequestParams(name=name, arguments=arguments)
        )
    else:
        raise RuntimeError("UNSUPPORTED_MCP_SIGNATURE")
    return unpack(result)

def fill_book(orders, symbol):
    executions = {}
    for order in orders:
        if order.get("symbol") != symbol:
            continue
        cumulative = dec(order["cumulative_quantity"])
        rows = order.get("executions") or []
        if sum((dec(row["quantity"]) for row in rows), Decimal(0)) != cumulative:
            raise RuntimeError("INCOMPLETE_EXECUTION_HISTORY")
        for row in rows:
            identity = row["id"]
            item = (
                row["timestamp"], identity, order["side"],
                str(dec(row["quantity"])), str(dec(row["price"]))
            )
            if identity in executions and executions[identity] != item:
                raise RuntimeError("CONFLICTING_EXECUTION_ID")
            executions[identity] = item

    quantity = Decimal(0)
    gross = Decimal(0)
    for stamp, identity, side, raw_qty, raw_price in sorted(
        executions.values(),
        key=lambda item: (
            datetime.fromisoformat(item[0].replace("Z", "+00:00")),
            item[1],
        )
    ):
        qty, price = dec(raw_qty), dec(raw_price)
        if qty <= 0 or price <= 0:
            raise RuntimeError("INVALID_EXECUTION")
        if side == "buy":
            quantity += qty
            gross += qty * price
        elif side == "sell":
            if qty > quantity:
                raise RuntimeError("MISSING_PRIOR_BUY_HISTORY")
            gross *= (quantity - qty) / quantity
            quantity -= qty
        else:
            raise RuntimeError("UNKNOWN_EXECUTION_SIDE")
    return quantity, gross


def foreign_execution_ids(orders, symbol, own_refs, own_ids):
    return sorted({
        execution["id"]
        for order in orders
        if order.get("symbol") == symbol
        and order.get("ref_id") not in own_refs
        and order.get("id") not in own_ids
        for execution in order.get("executions") or []
    })

async def quote_bid(session, symbol):
    data = await monitor.read_tool(
        session, "get_equity_quotes", {"symbols": [symbol]}
    )
    matches = [
        item["quote"] for item in data.get("results") or []
        if isinstance(item, dict) and isinstance(item.get("quote"), dict)
        and item["quote"].get("symbol") == symbol
    ]
    if len(matches) != 1:
        raise RuntimeError("QUOTE_NOT_UNIQUE")
    quote = matches[0]
    bid = dec(quote["bid_price"])
    stamp = datetime.fromisoformat(quote["venue_bid_time"].replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise RuntimeError("QUOTE_TIMEZONE_MISSING")
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if bid <= 0 or not -2 <= age <= 10:
        raise RuntimeError("STALE_OR_INVALID_BID")
    return bid


# BROKER_ID_RECONCILIATION_V2

def matching_orders_v2(orders, row):
    broker_id = row["broker_id"]
    if broker_id:
        return [
            order for order in orders
            if order.get("id") == broker_id
        ]
    refs = {
        value for value in (row["ref"], row["broker_ref"])
        if value
    }
    return [
        order for order in orders
        if order.get("ref_id") in refs
    ]

async def handle_symbol(db, session, account, positions, orders, symbol):
    own = db.execute(
        "SELECT * FROM intents WHERE symbol=? ORDER BY created_at", (symbol,)
    ).fetchall()
    own_refs = {
        value for row in own
        for value in (row["ref"], row["broker_ref"])
        if value
    }
    own_ids = {row["broker_id"] for row in own if row["broker_id"]}
    pending = False
    sold = Decimal(0)

    for row in own:
        matches = matching_orders_v2(orders, row)
        if len(matches) != 1:
            raise RuntimeError("ORDER_OUTCOME_UNRESOLVED_NO_RESUBMISSION")
        order = matches[0]
        if order.get("symbol") != symbol or order.get("side") != "sell":
            raise RuntimeError("ORDER_IDENTITY_MISMATCH")
        filled = dec(order["cumulative_quantity"])
        if not dec(row["filled"]) <= filled <= dec(row["requested_qty"]):
            raise RuntimeError("INVALID_RECONCILED_FILL")
        state = order["state"]
        average = dec(order.get("average_price") or "0")
        fees = dec(order["fees"])
        if fees < 0 or (filled > 0 and average <= 0):
            raise RuntimeError("INVALID_BROKER_FILL_PRICE_OR_FEES")
        broker_id = order.get("id")
        if not broker_id:
            raise RuntimeError("BROKER_ORDER_ID_MISSING")
        broker_ref = order.get("ref_id")
        db.execute(
            "UPDATE intents SET state=?,filled=?,broker_id=?,broker_ref=?,"
            "average_price=?,fees=? WHERE ref=?",
            (
                state, str(filled), broker_id, broker_ref,
                str(average), str(fees), row["ref"],
            ),
        )
        own_ids.add(broker_id)
        if broker_ref:
            own_refs.add(broker_ref)
        sold += filled
        pending = pending or state not in TERMINAL
    db.commit()

    if pending:
        log(symbol, "ORDER_PENDING_NO_DUPLICATE")
        return

    matches = [position for position in positions if position.get("symbol") == symbol]
    if len(matches) > 1:
        raise RuntimeError("POSITION_NOT_UNIQUE")
    position = matches[0] if matches else None
    current = dec(position["quantity"]) if position else Decimal(0)

    saved = db.execute(
        "SELECT * FROM managed WHERE symbol=?", (symbol,)
    ).fetchone()
    if saved is None:
        if own or current != EXPECTED[symbol]:
            raise RuntimeError("AUTHORIZED_INITIAL_QUANTITY_CHANGED")
        reconstructed_qty, gross = fill_book(orders, symbol)
        if reconstructed_qty != current or gross <= 0:
            raise RuntimeError("ENTRY_BASIS_NOT_RECONCILED")
        average = gross / current
        ids = foreign_execution_ids(orders, symbol, own_refs, own_ids)
        db.execute(
            "INSERT INTO managed VALUES(?,?,?,?,?)",
            (symbol, str(current), str(average), json.dumps(ids), now()),
        )
        db.commit()
        saved = db.execute(
            "SELECT * FROM managed WHERE symbol=?", (symbol,)
        ).fetchone()
        log(symbol, "POSITION_ADOPTED", quantity=str(current),
            average_ex_fees=str(average))

    if foreign_execution_ids(orders, symbol, own_refs, own_ids) != json.loads(
        saved["external_executions"]
    ):
        raise RuntimeError("EXTERNAL_TRADING_DETECTED")

    expected_remaining = dec(saved["initial_qty"]) - sold
    if expected_remaining < 0 or current != expected_remaining:
        raise RuntimeError("HOLDINGS_NOT_RECONCILED")
    if current == 0:
        log(symbol, "MANAGED_POSITION_CLOSED")
        return

    external_open = [
        order for order in orders
        if order.get("symbol") == symbol
        and order.get("state") not in TERMINAL
        and order.get("ref_id") not in own_refs
        and order.get("id") not in own_ids
    ]
    if external_open:
        raise RuntimeError("EXTERNAL_ORDER_ACTIVE")
    if dec(position["shares_available_for_sells"]) < current:
        raise RuntimeError("SHARES_NOT_FULLY_SELLABLE")

    average = dec(saved["average_ex_fees"])
    stop = average * Decimal("0.99")
    if not regular_window():
        log(symbol, "OUTSIDE_SUBMISSION_WINDOW", stop_price=str(stop))
        return

    bid = await quote_bid(session, symbol)
    if bid > stop:
        log(symbol, "MONITORING", bid=str(bid), stop_price=str(stop))
        return

    args = {
        "account_number": account,
        "symbol": symbol,
        "side": "sell",
        "type": "market",
        "quantity": str(current),
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
    }
    review = await order_call(session, "review_equity_order", args)
    if (
        review.get("symbol") != symbol
        or review.get("side") != "sell"
        or review.get("type") != "market"
    ):
        raise RuntimeError("REVIEW_IDENTITY_MISMATCH")
    if not isinstance(review.get("order_checks"), dict) or review["order_checks"]:
        raise RuntimeError("BROKER_REVIEW_ALERT")
    if review.get("customer_approval_required") is not False:
        raise RuntimeError("UNATTENDED_ORDER_NOT_APPROVED")

    # Recheck the trigger after review; do not trade from the older quote.
    bid = await quote_bid(session, symbol)
    if bid > stop or not regular_window():
        log(symbol, "TRIGGER_NO_LONGER_VALID")
        return

    # Durable intent BEFORE submission. Unknown outcomes are never resent.
    reference = str(uuid.uuid4())
    db.execute(
        "INSERT INTO intents(ref,symbol,requested_qty,state,created_at) "
        "VALUES(?,?,?,?,?)",
        (reference, symbol, str(current), "SUBMITTING", now()),
    )
    db.commit()
    args["ref_id"] = reference
    try:
        result = await order_call(session, "place_equity_order", args)
        order = result.get("order")
        state = "ACKNOWLEDGED" if isinstance(order, dict) and order.get("id") else "UNKNOWN"
        broker_id = None
        broker_ref = None
        if isinstance(order, dict) and order.get("id"):
            if order.get("symbol") != symbol or order.get("side") != "sell":
                raise RuntimeError("SUBMISSION_ORDER_IDENTITY_MISMATCH")
            broker_id = order["id"]
            broker_ref = order.get("ref_id")
        db.execute(
            "UPDATE intents SET state=?,broker_id=?,broker_ref=? WHERE ref=?",
            (state, broker_id, broker_ref, reference),
        )
        db.commit()
        log(symbol, "SELL_SUBMISSION_RETURNED",
            reference=reference, outcome=state,
            fill_verified=False)
    except Exception:
        db.execute("UPDATE intents SET state='UNKNOWN' WHERE ref=?", (reference,))
        db.commit()
        raise RuntimeError("UNKNOWN_SUBMISSION_DO_NOT_RETRY") from None

async def cycle(db):
    oauth = auth.OAuthClientProvider(
        server_url=auth.SERVER,
        client_metadata=auth.OAuthClientMetadata(
            client_name="super-intelligence-vps",
            redirect_uris=[auth.REDIRECT],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=auth.FileStorage(),
        redirect_handler=monitor.refuse_login,
        callback_handler=monitor.refuse_login,
    )
    async with auth.httpx2.AsyncClient(
        auth=oauth, timeout=auth.httpx2.Timeout(30.0, read=120.0)
    ) as client:
        async with auth.streamable_http_client(
            auth.SERVER, http_client=client
        ) as streams:
            async with auth.ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                data = await monitor.read_tool(session, "get_accounts", {})
                matches = [
                    item for item in data.get("accounts") or []
                    if isinstance(item, dict)
                    and item.get("agentic_allowed") is True
                    and str(item.get("account_number", "")).endswith("5004")
                ]
                if len(matches) != 1:
                    raise RuntimeError("ACCOUNT_BINDING_FAILED")
                account = matches[0]
                if (
                    account.get("state") != "active"
                    or account.get("deactivated") is not False
                    or account.get("permanently_deactivated") is not False
                ):
                    raise RuntimeError("ACCOUNT_NOT_ACTIVE")
                number = account["account_number"]
                for symbol in EXPECTED:
                    try:
                        # Refresh before each symbol rather than reuse the prior symbol's state.
                        positions = await monitor.pages(
                            session, "get_equity_positions", number, "positions"
                        )
                        orders = await monitor.pages(
                            session, "get_equity_orders", number, "orders"
                        )
                        await handle_symbol(
                            db, session, number, positions, orders, symbol
                        )
                    except RuntimeError as error:
                        log(symbol, "PROTECTION_BLOCKED", reason=str(error))

async def main():
    db = database()
    while True:
        try:
            await asyncio.wait_for(cycle(db), timeout=180)
        except Exception as error:
            log("ALL", "BROKER_CYCLE_FAILED", error_type=type(error).__name__)
        await asyncio.sleep(10)

if __name__ == "__main__":
    os.umask(0o077)
    with open(ROOT / "worker.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(main())
