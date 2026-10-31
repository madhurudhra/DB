
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

EQUITY_DB = Path("/var/lib/si-equity-r1/monitor.sqlite3")
EQUITY_CONFIG = Path("/var/lib/si-equity-r1/config.json")
NATIVE_CONFIG = Path("/etc/si-crypto-r1/config.json")
OLD_FEED = Path("/home/botuser/si-bots/dashboard_live_feed.json")

def now():
    return datetime.now(timezone.utc).isoformat()

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

def safe_rows(rows, keys):
    if not isinstance(rows, list):
        raise ValueError("Expected broker rows.")
    return [
        {key: item[key] for key in keys if key in item}
        for item in rows[:200] if isinstance(item, dict)
    ]

def equity_snapshot():
    config = json.loads(EQUITY_CONFIG.read_text())
    suffix = str(config["account_last4"])
    if len(suffix) != 4 or not suffix.isdigit():
        raise ValueError("Invalid equity account binding.")

    with sqlite3.connect(
        "file:" + str(EQUITY_DB) + "?mode=ro",
        uri=True, timeout=5,
    ) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        health = db.execute(
            "SELECT at,payload FROM health WHERE id=1"
        ).fetchone()
        latest = db.execute(
            "SELECT at,summary,broker_data FROM snapshots "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

    if latest is None:
        return {"status": "NO_BROKER_SNAPSHOT", "controls_connected": False}

    at, summary_text, broker_text = latest
    summary = json.loads(summary_text)
    broker = json.loads(broker_text)

    if summary.get("account") != "****" + suffix:
        raise ValueError("Equity snapshot account binding mismatch.")

    age = age_seconds(at)
    fresh = age is not None and 0 <= age <= 180
    portfolio = broker.get("portfolio")
    if not isinstance(portfolio, dict):
        raise ValueError("Invalid portfolio shape.")

    positions = broker.get("positions")
    orders = broker.get("orders")
    if not isinstance(positions, list) or not isinstance(orders, list):
        raise ValueError("Invalid broker row collections.")

    return {
        "status": "SNAPSHOT_FRESH" if fresh else "SNAPSHOT_STALE",
        "snapshot_at": at,
        "age_seconds": age,
        "account": "****" + suffix,
        "source": "equity-monitor-broker-snapshot",
        "controls_connected": False,
        "automatic_entries_implemented": False,
        "monitor_health": json.loads(health[1]).get("state")
            if health else None,
        "portfolio": {
            key: portfolio[key] for key in (
                "currency", "buying_power", "cash", "total_value",
                "equity_value", "crypto_value",
            ) if key in portfolio
        },
        "position_rows_total": len(positions),
        "order_rows_total": len(orders),
        "display_row_limit": 200,
        "positions": safe_rows(positions, (
            "symbol", "quantity", "shares_available_for_sells",
            "average_buy_price",
        )),
        "orders": safe_rows(orders, (
            "id", "symbol", "side", "state",
            "cumulative_quantity", "average_price", "fees",
            "created_at", "last_transaction_at",
        )),
    }

def filter_legacy_feed(payload, native_suffix):
    if not isinstance(payload, dict):
        raise ValueError("Invalid collector payload.")
    result = dict(payload)
    bots = payload.get("bots")
    if not isinstance(bots, dict):
        raise ValueError("Invalid collector bot mapping.")
    bots = dict(bots)
    crypto = bots.get("crypto")
    if not isinstance(crypto, dict):
        bots.pop("crypto", None)
        warning = "NATIVE_CRYPTO_ACCOUNT_DATA_NOT_AVAILABLE"
    elif (
        not isinstance(native_suffix, str)
        or len(native_suffix) != 4
        or not native_suffix.isdigit()
        or crypto.get("account") != "****" + native_suffix
    ):
        bots.pop("crypto", None)
        warning = "CRYPTO_ACCOUNT_MISMATCH_DATA_WITHHELD"
    else:
        warning = None
    result["bots"] = bots
    result["crypto_account_binding_warning"] = warning
    result["account_suffix_check_is_not_full_identity_verification"] = True
    return result

def integration_snapshot():
    from strategy_settings_v1 import TOKEN
    import worker_controls_v1
    from crypto_worker_view import snapshot as runtime_snapshot

    result = {
        "checked_at": now(),
        "csrf": TOKEN,
        "crypto": None,
        "equity": None,
        "zerodha": {
            "status": "RUNTIME_ADAPTER_NOT_CONNECTED",
            "controls_connected": False,
        },
        "manual_orders_connected": False,
    }

    try:
        control = worker_controls_v1.snapshot()
        runtime = runtime_snapshot()
        control["runtime"] = {
            key: runtime.get(key) for key in (
                "account", "process", "heartbeat_fresh",
                "display_state", "configuration",
            )
        }
        result["crypto"] = control
    except Exception as error:
        result["crypto"] = {
            "status": "RUNTIME_UNAVAILABLE",
            "error_type": type(error).__name__,
        }

    try:
        result["equity"] = equity_snapshot()
    except Exception as error:
        result["equity"] = {
            "status": "BROKER_SNAPSHOT_UNAVAILABLE",
            "error_type": type(error).__name__,
            "controls_connected": False,
        }

    return result

def handle(handler):
    from strategy_settings_v1 import secure_request, send

    path = urlsplit(handler.path).path
    if path not in {"/api/v1/integration-v2", "/api/v1/live"}:
        return False

    if handler.command != "GET":
        send(handler, 405, {"error": "METHOD_NOT_ALLOWED"})
        return True

    if not secure_request(handler):
        send(handler, 403, {
            "error": "LOCAL_SAME_ORIGIN_ACCESS_REQUIRED",
            "message": "Use the existing localhost SSH tunnel.",
        })
        return True

    try:
        if path == "/api/v1/integration-v2":
            payload = integration_snapshot()
        else:
            native = json.loads(NATIVE_CONFIG.read_text())
            suffix = str(native["account_number"])[-4:]
            payload = filter_legacy_feed(
                json.loads(OLD_FEED.read_text()), suffix
            )
        send(handler, 200, payload)
    except Exception as error:
        send(handler, 503, {
            "error": "INTEGRATION_DATA_UNAVAILABLE",
            "error_type": type(error).__name__,
        })
    return True
