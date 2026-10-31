
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

CRYPTO_DB = Path("/var/lib/si-crypto-r1/runtime.sqlite3")
CRYPTO_CONFIG = Path("/etc/si-crypto-r1/config.json")

def now():
    return datetime.now(timezone.utc).isoformat()

def dec(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Nonfinite recorded number.")
    return number

def age_seconds(stamp):
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return round(
            (datetime.now(timezone.utc)-parsed).total_seconds(), 1
        )
    except (AttributeError, TypeError, ValueError):
        return None

def amount(quantity, price):
    if quantity is None or price is None:
        return None
    return str(dec(quantity)*dec(price))

def reconstruct(rows):
    quantity = Decimal(0)
    basis = Decimal(0)
    gross = Decimal(0)

    for row in rows:
        filled = dec(row["filled"])
        if filled < 0:
            raise ValueError("Negative recorded fill.")
        if filled == 0:
            continue
        average = dec(row["average"])
        fee = dec(row["fee"])
        if average <= 0 or fee < 0:
            raise ValueError("Invalid recorded fill price/fee.")

        if row["side"] == "buy":
            quantity += filled
            gross += filled*average
            basis += filled*average+fee
        elif row["side"] == "sell":
            if filled > quantity:
                raise ValueError("Recorded sells exceed managed purchases.")
            fraction = (quantity-filled)/quantity
            gross *= fraction
            basis *= fraction
            quantity -= filled
        else:
            raise ValueError("Unknown recorded side.")

    return quantity, basis, gross

def crypto_snapshot():
    config = json.loads(CRYPTO_CONFIG.read_text())
    account = "****"+str(config["account_number"])[-4:]

    with sqlite3.connect(
        "file:"+str(CRYPTO_DB)+"?mode=ro", uri=True, timeout=5
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        rows = db.execute(
            "SELECT * FROM orders ORDER BY seq LIMIT 100001"
        ).fetchall()
        if len(rows) > 100000:
            raise ValueError("Ledger exceeds reconstruction limit.")
        values = {
            row["k"]: json.loads(row["v"])
            for row in db.execute(
                "SELECT k,v FROM kv WHERE k IN ('heartbeat','status')"
            )
        }

    quantity, basis, gross = reconstruct(rows)
    heartbeat = values.get("heartbeat")
    age = age_seconds(heartbeat)
    fresh = age is not None and 0 <= age <= 30
    status = values.get("status") or {}

    if not isinstance(status, dict):
        raise ValueError("Invalid worker status.")
    if status.get("account") not in (None, account):
        raise ValueError("Worker account mismatch.")

    orders = []
    for row in reversed(rows[-200:]):
        created = datetime.fromtimestamp(
            float(row["created"]), timezone.utc
        ).isoformat()
        filled = dec(row["filled"])
        orders.append({
            "created_at": created,
            "symbol": "BTC-USD",
            "side": row["side"],
            "state": row["state"],
            "requested_quantity": row["qty"],
            "filled_quantity": row["filled"],
            "average_fill_price": row["average"] if filled > 0 else None,
            "fees": row["fee"] if filled > 0 else None,
            "filled_gross_amount": (
                amount(row["filled"], row["average"])
                if filled > 0 else None
            ),
            "reference": row["bid"] or row["cid"],
        })

    positions = []
    if quantity > 0:
        marked_value = None
        if (
            fresh
            and status.get("managed_quantity") is not None
            and dec(status["managed_quantity"]) == quantity
        ):
            marked_value = status.get("estimated_net_position_value")

        positions.append({
            "symbol": "BTC-USD",
            "quantity": str(quantity),
            "average_purchase_price": str(gross/quantity),
            "cost_basis": str(basis),
            "estimated_net_value": marked_value,
            "scope": "Bot-managed fills only",
        })

    return {
        "account": account,
        "source": "native-crypto-worker-order-ledger",
        "scope": "BOT_MANAGED_ONLY_NOT_ALL_BROKER_HOLDINGS",
        "heartbeat_fresh": fresh,
        "heartbeat_age_seconds": age,
        "positions_available": True,
        "positions": positions,
        "order_rows_total": len(rows),
        "history_display_limit": 200,
        "orders": orders,
        "filled_orders": [
            order for order in orders
            if dec(order["filled_quantity"]) > 0
        ],
        "closed_trade_pnl_available": False,
    }

def equity_snapshot():
    from dashboard_integration_v2 import equity_snapshot as read_equity
    snapshot = read_equity()
    if "positions" not in snapshot or "orders" not in snapshot:
        return {
            "status": snapshot.get("status", "SNAPSHOT_UNAVAILABLE"),
            "positions_available": False,
            "orders_available": False,
        }

    positions = [{
        "symbol": row.get("symbol"),
        "quantity": row.get("quantity"),
        "average_purchase_price": row.get("average_buy_price"),
        "cost_basis": None,
        "estimated_net_value": None,
        "scope": "Broker equity snapshot",
    } for row in snapshot["positions"]]

    orders = []
    for row in snapshot["orders"]:
        filled = row.get("cumulative_quantity")
        average = row.get("average_price")
        orders.append({
            "created_at": row.get("created_at"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "state": row.get("state"),
            "requested_quantity": None,
            "filled_quantity": filled,
            "average_fill_price": average,
            "fees": row.get("fees"),
            "filled_gross_amount": amount(filled, average),
            "reference": row.get("id"),
        })

    return {
        "account": snapshot.get("account"),
        "source": snapshot.get("source"),
        "status": snapshot.get("status"),
        "snapshot_at": snapshot.get("snapshot_at"),
        "age_seconds": snapshot.get("age_seconds"),
        "positions_available": True,
        "positions": positions,
        "order_rows_total": snapshot.get("order_rows_total"),
        "history_display_limit": snapshot.get("display_row_limit"),
        "orders": orders,
        "filled_orders": [
            row for row in orders
            if row["filled_quantity"] is not None
            and dec(row["filled_quantity"]) > 0
        ],
        "closed_trade_pnl_available": False,
    }

def snapshot():
    result = {"checked_at": now(), "bots": {}}
    for bot, reader in (
        ("crypto", crypto_snapshot),
        ("equity", equity_snapshot),
    ):
        try:
            result["bots"][bot] = reader()
        except Exception as error:
            result["bots"][bot] = {
                "status": "READ_UNAVAILABLE",
                "error_type": type(error).__name__,
                "positions_available": False,
                "orders_available": False,
            }

    result["bots"]["zerodha"] = {
        "status": "RUNTIME_ADAPTER_NOT_CONNECTED",
        "positions_available": False,
        "orders_available": False,
    }
    return result

def handle(handler):
    from strategy_settings_v1 import secure_request, send
    if urlsplit(handler.path).path != "/api/v1/account-history-v1":
        return False
    if handler.command != "GET":
        send(handler, 405, {"error": "METHOD_NOT_ALLOWED"})
    elif not secure_request(handler):
        send(handler, 403, {"error": "LOCAL_SAME_ORIGIN_ACCESS_REQUIRED"})
    else:
        try:
            send(handler, 200, snapshot())
        except Exception as error:
            send(handler, 503, {
                "error": "HISTORY_UNAVAILABLE",
                "error_type": type(error).__name__,
            })
    return True
