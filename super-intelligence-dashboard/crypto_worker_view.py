
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

DB = Path("/var/lib/si-crypto-r1/runtime.sqlite3")
CONF = Path("/etc/si-crypto-r1/config.json")

def snapshot():
    with sqlite3.connect(
        "file:" + str(DB) + "?mode=ro",
        uri=True, timeout=2,
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        values = {
            r["k"]: json.loads(r["v"])
            for r in db.execute(
                "SELECT k,v FROM kv WHERE k IN "
                "('status','heartbeat','entries_enabled')"
            )
        }
        events = [
            {
                "id": r["id"], "at": r["at"],
                "kind": r["kind"], "data": json.loads(r["data"]),
            }
            for r in db.execute(
                "SELECT id,at,kind,data FROM events "
                "ORDER BY id DESC LIMIT 8"
            )
        ]

    process = subprocess.run(
        ["systemctl", "is-active", "si-crypto-r1.service"],
        capture_output=True, text=True, timeout=2,
    ).stdout.strip() or "unknown"

    heartbeat = values.get("heartbeat")
    age = None
    try:
        stamp = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Missing timezone")
        age = round(
            (datetime.now(timezone.utc)-stamp).total_seconds(), 1
        )
    except (AttributeError, TypeError, ValueError):
        pass

    fresh = age is not None and 0 <= age <= 30
    status = values.get("status") or {}
    enabled = values.get("entries_enabled")
    state = status.get("state", "UNKNOWN")

    if process != "active":
        display = "WORKER_NOT_ACTIVE"
    elif not fresh:
        display = "STALE_OR_UNKNOWN"
    elif status.get("entries_enabled") != enabled:
        display = "STATE_UPDATE_IN_PROGRESS"
    else:
        display = state

    config = json.loads(CONF.read_text())
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "worker": "crypto-r1",
        "symbol": "BTC-USD",
        "account": "****" + str(config["account_number"])[-4:],
        "process": process,
        "heartbeat": heartbeat,
        "heartbeat_age_seconds": age,
        "heartbeat_fresh": fresh,
        "display_state": display,
        "applied_entries_enabled": enabled,
        "status": status,
        "configuration": {
            key: config.get(key) for key in (
                "capital_usd", "entry_budget_usd",
                "stop_loss_fraction", "fee_allowance_per_side",
            )
        },
        "recent_events": events,
        "controls": "READ_ONLY",
        "order_execution_verified_by_this_endpoint": False,
    }
