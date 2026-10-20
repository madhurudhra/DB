import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BOTS = ("crypto", "us_trading", "zerodha")


def now():
    return datetime.now(timezone.utc).isoformat()


class SellControlStore:
    def __init__(self, filename):
        self.filename = str(filename)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.filename, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        Path(self.filename).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS sell_controls (
                bot TEXT PRIMARY KEY,
                requested_enabled INTEGER NOT NULL
                    CHECK(requested_enabled IN (0,1)),
                revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sell_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                bot TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                actor TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            """)
            for bot in BOTS:
                db.execute(
                    """INSERT OR IGNORE INTO sell_controls
                       (bot,requested_enabled,revision,updated_at)
                       VALUES(?,1,0,?)""",
                    (bot, now()),
                )

    def state(self, bot):
        if bot not in BOTS:
            raise ValueError("Unknown bot")

        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM sell_controls WHERE bot=?",
                (bot,),
            ).fetchone()

        if row is None:
            raise RuntimeError("SELL controls are not initialized")

        return {
            "bot": bot,
            "requested_enabled": bool(row["requested_enabled"]),
            "revision": row["revision"],
            "updated_at": row["updated_at"],
            "effective_enabled": None,
            "runtime_acknowledged": False,
            "status": "UNVERIFIED",
        }

    def request_change(self, bot, enabled, request_id, actor, confirm):
        if bot not in BOTS:
            raise ValueError("Unknown bot")
        if type(enabled) is not bool:
            raise ValueError("enabled must be Boolean")
        if confirm is not True:
            raise ValueError("Explicit confirmation required")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("Invalid request ID")
        if not isinstance(actor, str) or not 1 <= len(actor) <= 128:
            raise ValueError("Authenticated actor required")

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")

            prior = db.execute(
                "SELECT * FROM sell_events WHERE request_id=?",
                (request_id,),
            ).fetchone()

            if prior is not None:
                if (
                    prior["bot"] != bot
                    or bool(prior["enabled"]) != enabled
                    or prior["actor"] != actor
                ):
                    raise ValueError("Request ID reused for a different action")
                return json.loads(prior["payload"])

            current = db.execute(
                "SELECT * FROM sell_controls WHERE bot=?",
                (bot,),
            ).fetchone()

            if current is None:
                raise RuntimeError("SELL controls are not initialized")

            timestamp = now()
            revision = current["revision"] + 1

            result = {
                "request_id": request_id,
                "bot": bot,
                "previous_requested_enabled":
                    bool(current["requested_enabled"]),
                "requested_enabled": enabled,
                "revision": revision,
                "updated_at": timestamp,
                "actor": actor,
                "saved": True,
                "runtime_acknowledged": False,
                "effective_enabled": None,
                "status": "PENDING_BOT_ACK",
                "scope": "ALL_AUTOMATIC_SELLS",
                "existing_orders_cancelled": False,
                "message": (
                    "SELL enable request saved; bot acknowledgement pending."
                    if enabled else
                    "SELL disable request saved; bot acknowledgement pending. "
                    "Requested scope includes automatic stop-loss, trailing-stop "
                    "and profit exits. Existing broker orders are unchanged."
                ),
            }

            db.execute(
                """UPDATE sell_controls
                   SET requested_enabled=?,revision=?,updated_at=?
                   WHERE bot=?""",
                (int(enabled), revision, timestamp, bot),
            )

            db.execute(
                """INSERT INTO sell_events
                   (request_id,bot,enabled,actor,revision,created_at,payload)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    request_id, bot, int(enabled), actor,
                    revision, timestamp, json.dumps(result),
                ),
            )

        return result
