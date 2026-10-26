import sqlite3
from pathlib import Path

from app.core.config import get_settings


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS bot_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_name TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    price TEXT,
    currency TEXT,
    data_json TEXT NOT NULL DEFAULT '{}',
    source_verified INTEGER NOT NULL DEFAULT 0
        CHECK (source_verified IN (0, 1))
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (
        decision IN ('BUY', 'SELL', 'HOLD', 'BLOCK')
    ),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    strategy_version TEXT NOT NULL,
    rationale_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS risk_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER REFERENCES decisions(id),
    bot_name TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    checked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    rules_version TEXT NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS order_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    bot_name TEXT NOT NULL,
    broker TEXT NOT NULL,
    account_ref TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity TEXT NOT NULL,
    order_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CREATED',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    decision_id INTEGER REFERENCES decisions(id),
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS broker_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id INTEGER NOT NULL REFERENCES order_intents(id),
    broker TEXT NOT NULL,
    broker_order_id TEXT,
    status TEXT NOT NULL,
    submitted_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    response_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (broker, broker_order_id)
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker TEXT NOT NULL,
    account_ref TEXT,
    broker_fill_id TEXT NOT NULL,
    broker_order_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity TEXT NOT NULL,
    price TEXT NOT NULL,
    fee TEXT,
    currency TEXT,
    filled_at TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (broker, account_ref, broker_fill_id)
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker TEXT NOT NULL,
    account_ref TEXT,
    symbol TEXT NOT NULL,
    quantity TEXT NOT NULL,
    average_cost TEXT,
    market_value TEXT,
    currency TEXT,
    observed_at TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker TEXT NOT NULL,
    account_ref TEXT,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT,
    status TEXT NOT NULL,
    discrepancies_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    bot_name TEXT,
    correlation_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;

CREATE INDEX IF NOT EXISTS idx_decisions_bot_time
    ON decisions(bot_name, created_at);

CREATE INDEX IF NOT EXISTS idx_orders_status
    ON order_intents(status, created_at);

CREATE INDEX IF NOT EXISTS idx_fills_symbol_time
    ON fills(symbol, filled_at);

CREATE INDEX IF NOT EXISTS idx_positions_account_time
    ON position_snapshots(broker, account_ref, observed_at);
"""


def connect_database(path: str | Path | None = None) -> sqlite3.Connection:
    if path is None:
        path = get_settings().database_path

    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(db_path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def initialize_database(path: str | Path | None = None) -> None:
    with connect_database(path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
        connection.execute(
            """
            INSERT INTO audit_events (event_type, details_json)
            VALUES ('DATABASE_INITIALIZED', '{"schema_version":1}')
            """
        )
