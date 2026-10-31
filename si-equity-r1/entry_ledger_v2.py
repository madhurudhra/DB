"""Staged Equity entry ledger. Importing performs no I/O.

No broker calls. No automatic release of reservations.
The adapter must serialize budget calculation, reservation, and submission.
A missing submission response is not permission to submit again.
"""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

DB = Path("/var/lib/si-equity-stop-guard/entry-ledger-v2.sqlite3")


def now():
    return datetime.now(timezone.utc).isoformat()


def amount(value):
    if isinstance(value, bool):
        raise ValueError("INVALID_AMOUNT")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("INVALID_AMOUNT") from None
    if not result.is_finite() or result < 0:
        raise ValueError("INVALID_AMOUNT")
    return result


def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous=FULL")
    return db


def initialize():
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS entry_intents(
                request_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                reserved_cost TEXT NOT NULL,
                state TEXT NOT NULL,
                broker_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def reserve(request_id, order, budget_before_ledger_reservations):
    """Budget must already exclude deployed capital and other commitments.

    This function subtracts this ledger's reservations exactly once.
    It does not verify the supplied budget against the broker or allocation.
    """
    request_id = str(UUID(request_id))
    if not isinstance(order, dict):
        raise ValueError("INVALID_ORDER")
    if set(order) != {
        "account", "symbol", "quantity", "maximum_cost", "revision"
    }:
        raise ValueError("INVALID_ORDER_FIELDS")
    if any(
        not isinstance(order[key], str) or not order[key]
        for key in ("account", "symbol")
    ):
        raise ValueError("MISSING_ORDER_IDENTITY")
    if amount(order["quantity"]) <= 0:
        raise ValueError("INVALID_QUANTITY")
    cost = amount(order["maximum_cost"])
    if cost <= 0:
        raise ValueError("INVALID_COST")
    if type(order["revision"]) is not int or order["revision"] < 1:
        raise ValueError("INVALID_REVISION")

    budget = amount(budget_before_ledger_reservations)
    payload = json.dumps(
        order, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        saved = db.execute(
            "SELECT * FROM entry_intents WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if saved:
            if saved["payload"] != payload:
                raise ValueError("REQUEST_ID_CONTENT_CONFLICT")
            db.commit()
            return {**dict(saved), "new_reservation": False}

        rows = db.execute(
            "SELECT payload,reserved_cost FROM entry_intents"
        ).fetchall()
        committed = sum(
            (
                amount(row["reserved_cost"])
                for row in rows
                if json.loads(row["payload"])["account"] == order["account"]
            ),
            Decimal(0),
        )
        if committed + cost > budget:
            raise RuntimeError("INSUFFICIENT_UNRESERVED_BUDGET")

        stamp = now()
        db.execute(
            "INSERT INTO entry_intents VALUES(?,?,?,'RESERVED',NULL,?,?)",
            (request_id, payload, str(cost), stamp, stamp),
        )
        db.commit()
        return {
            "request_id": request_id,
            "state": "RESERVED",
            "reserved_cost": str(cost),
            "new_reservation": True,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def mark_submitting(request_id):
    """Persist BEFORE the broker call. A second attempt is refused."""
    request_id = str(UUID(request_id))
    with connect() as db:
        cursor = db.execute(
            "UPDATE entry_intents SET state='SUBMITTING',updated_at=? "
            "WHERE request_id=? AND state='RESERVED'",
            (now(), request_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("SUBMISSION_ALREADY_STARTED_OR_NOT_RESERVED")


def record_broker_id(request_id, broker_id):
    """An acknowledgement is not a verified fill."""
    request_id = str(UUID(request_id))
    if not isinstance(broker_id, str) or not broker_id:
        raise ValueError("BROKER_ID_REQUIRED")
    with connect() as db:
        row = db.execute(
            "SELECT state,broker_id FROM entry_intents WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("INTENT_NOT_FOUND")
        if row["state"] == "ACKNOWLEDGED" and row["broker_id"] == broker_id:
            return
        if row["state"] != "SUBMITTING" or row["broker_id"] is not None:
            raise RuntimeError("BROKER_ACK_CONFLICT")
        db.execute(
            "UPDATE entry_intents SET state='ACKNOWLEDGED',"
            "broker_id=?,updated_at=? WHERE request_id=?",
            (broker_id, now(), request_id),
        )
