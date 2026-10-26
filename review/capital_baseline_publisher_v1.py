"""Capital baseline publisher.

No broker calls, configuration changes, reconciliation PASS creation,
reservation release, or trading activation.

Trust boundary:
CAPITAL_RECONCILIATION_RESULT_V1 must be produced by trusted reconciliation
code, never copied from dashboard input or manually fabricated.

A source hash is an integrity reference, not proof of broker verification.
"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


BROKERS = {
    "crypto": "robinhood_crypto",
    "equity": "robinhood_equity",
    "zerodha": "zerodha",
}

RESULT_EVENT = "CAPITAL_RECONCILIATION_RESULT_V1"
PUBLISH_EVENT = "CAPITAL_BASELINE_PUBLISHED_V1"

MONEY = (
    "funded_capital",
    "eligible_cash",
    "position_cost",
    "realized_net_profit",
    "baseline_pending_buy_commitment",
)

CHECKS = {
    "funding_verified",
    "ownership_verified",
    "fees_verified",
    "positions_verified",
    "orders_verified",
    "history_complete_for_bot",
}

UNRESOLVED = {
    "fees",
    "funding",
    "ownership",
    "orders",
    "positions",
}


class PublicationBlocked(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise PublicationBlocked(code)


def decode(text):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=pairs)


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def amount(value, signed=False):
    require(
        isinstance(value, str),
        "MONEY_MUST_BE_DECIMAL_STRINGS",
    )

    try:
        number = Decimal(value)
    except InvalidOperation:
        raise PublicationBlocked("INVALID_CAPITAL_VALUE") from None

    require(number.is_finite(), "INVALID_CAPITAL_VALUE")
    require(signed or number >= 0, "NEGATIVE_CAPITAL_VALUE")
    require(
        abs(number) <= Decimal("1000000000000")
        and number.as_tuple().exponent >= -12,
        "CAPITAL_RANGE_OR_PRECISION_EXCEEDED",
    )

    return format(number if number else Decimal(0), "f")


def timestamp(value):
    require(isinstance(value, str), "TIMESTAMP_REQUIRED")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "TIMESTAMP_TIMEZONE_REQUIRED")
    return parsed.astimezone(timezone.utc)


def _publish_locked(db, bot, reconciliation_id):
    run = db.execute(
        "SELECT * FROM reconciliation_runs WHERE id=?",
        (reconciliation_id,),
    ).fetchone()

    require(
        run is not None and run["broker"] == BROKERS[bot],
        "RECONCILIATION_CONTEXT_MISMATCH",
    )

    records = db.execute(
        """
        SELECT details_json
        FROM audit_events
        WHERE event_type=? AND bot_name=? AND correlation_id=?
        """,
        (RESULT_EVENT, bot, str(reconciliation_id)),
    ).fetchall()

    require(
        len(records) == 1,
        "ONE_TRUSTED_RECONCILIATION_RESULT_REQUIRED",
    )

    proof = decode(records[0]["details_json"])
    require(isinstance(proof, dict), "INVALID_RECONCILIATION_RESULT")

    require(
        type(proof.get("schema_version")) is int
        and proof["schema_version"] == 1,
        "UNSUPPORTED_RESULT_SCHEMA",
    )
    require(
        proof.get("bot") == bot
        and proof.get("broker") == BROKERS[bot],
        "RESULT_BOT_MISMATCH",
    )
    require(
        type(proof.get("reconciliation_id")) is int
        and proof["reconciliation_id"] == reconciliation_id,
        "RESULT_RUN_MISMATCH",
    )

    account = proof.get("account_ref")
    require(
        isinstance(account, str)
        and bool(account)
        and account == account.strip()
        and account == run["account_ref"],
        "RESULT_ACCOUNT_MISMATCH",
    )
    require(
        type(proof.get("config_revision")) is int
        and proof["config_revision"] >= 0,
        "INVALID_CONFIGURATION_REVISION",
    )

    currency = "INR" if bot == "zerodha" else "USD"
    require(
        proof.get("currency") == currency,
        "RESULT_CURRENCY_MISMATCH",
    )

    checks = proof.get("checks")
    require(
        isinstance(checks, dict) and set(checks) == CHECKS,
        "RECONCILIATION_CHECKS_REQUIRED",
    )
    require(
        all(value is True for value in checks.values()),
        "RECONCILIATION_CHECK_FAILED",
    )

    unresolved = proof.get("unresolved_counts")
    require(
        isinstance(unresolved, dict)
        and set(unresolved) == UNRESOLVED,
        "UNRESOLVED_COUNTS_REQUIRED",
    )
    require(
        all(
            type(value) is int and value == 0
            for value in unresolved.values()
        ),
        "UNRESOLVED_RECONCILIATION_ITEMS",
    )

    source_hash = proof.get("source_evidence_sha256")
    require(
        isinstance(source_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", source_hash) is not None,
        "SOURCE_EVIDENCE_DIGEST_REQUIRED",
    )

    values = {
        key: amount(
            proof.get(key),
            signed=(key == "realized_net_profit"),
        )
        for key in MONEY
    }

    observed = timestamp(proof.get("as_of"))
    as_of = observed.isoformat()
    result_hash = hashlib.sha256(
        canonical(proof).encode()
    ).hexdigest()

    correlation = f"capital:{bot}:{reconciliation_id}"

    publications = db.execute(
        """
        SELECT details_json
        FROM audit_events
        WHERE event_type=? AND bot_name=? AND correlation_id=?
        """,
        (PUBLISH_EVENT, bot, correlation),
    ).fetchall()

    require(
        len(publications) <= 1,
        "DUPLICATE_PUBLICATION_AUDIT",
    )

    current = db.execute(
        """
        SELECT *
        FROM capital_verified_states
        WHERE bot=? AND account_ref=?
        """,
        (bot, account),
    ).fetchone()

    # Idempotent replay never refreshes timestamps or releases resources.
    if publications:
        audit = decode(publications[0]["details_json"])
        require(
            audit.get("result_sha256") == result_hash,
            "PUBLISHED_RESULT_CHANGED",
        )
        require(
            current is not None
            and current["reconciliation_id"] >= reconciliation_id,
            "PUBLISHED_STATE_MISSING_OR_REGRESSED",
        )

        if current["reconciliation_id"] == reconciliation_id:
            require(
                current["config_revision"] == proof["config_revision"]
                and timestamp(current["as_of"]) == observed,
                "PUBLISHED_STATE_CHANGED",
            )
            require(
                all(
                    amount(
                        current[key],
                        signed=(key == "realized_net_profit"),
                    ) == values[key]
                    for key in MONEY
                ),
                "PUBLISHED_STATE_CHANGED",
            )

        return {
            "status": "ALREADY_PUBLISHED",
            "baseline_updated": False,
            "superseded":
                current["reconciliation_id"] != reconciliation_id,
            "reconciliation_id": reconciliation_id,
            "as_of": as_of,
            "order_submission_authorized": False,
        }

    config_row = db.execute(
        """
        SELECT revision,config_json
        FROM independent_bot_configurations
        WHERE bot=?
        """,
        (bot,),
    ).fetchone()

    require(config_row is not None, "BOT_CONFIGURATION_MISSING")
    config = decode(config_row["config_json"])

    require(
        config_row["revision"] == proof["config_revision"],
        "CONFIGURATION_REVISION_CHANGED",
    )
    require(
        config.get("account_ref") == account
        and config.get("currency") == currency,
        "SAVED_ACCOUNT_OR_CURRENCY_MISMATCH",
    )

    latest = db.execute(
        """
        SELECT id,status
        FROM reconciliation_runs
        WHERE broker=? AND account_ref=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (BROKERS[bot], account),
    ).fetchone()

    require(
        latest is not None
        and latest["id"] == reconciliation_id
        and latest["status"] == "PASS",
        "LATEST_PASSING_RECONCILIATION_REQUIRED",
    )
    require(
        decode(run["discrepancies_json"]) == [],
        "RECONCILIATION_DISCREPANCIES_PRESENT",
    )

    now = datetime.now(timezone.utc)
    started = timestamp(run["started_at"])
    finished = timestamp(run["finished_at"])

    require(
        started <= observed <= finished <= now,
        "RECONCILIATION_TIME_ORDER_INVALID",
    )
    require(
        0 <= (now - observed).total_seconds() <= 60
        and 0 <= (now - finished).total_seconds() <= 60,
        "RECONCILIATION_RESULT_STALE",
    )

    if current is not None:
        require(
            current["reconciliation_id"] < reconciliation_id
            and timestamp(current["as_of"]) <= observed,
            "BASELINE_REGRESSION_OR_UNAUDITED_OVERWRITE",
        )

    # A reconciler must settle relevant holds before publication, within
    # this same transaction. This publisher never releases holds itself.
    for table in (
        "capital_buy_reservations",
        "automatic_sell_reservations_v2",
    ):
        exists = db.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name=?
            """,
            (table,),
        ).fetchone()

        require(
            exists is not None
            or table == "automatic_sell_reservations_v2",
            "CAPITAL_RESERVATION_SCHEMA_MISSING",
        )

        if exists:
            held = db.execute(
                f"""
                SELECT 1 FROM {table}
                WHERE bot=? AND account_ref=?
                  AND (state IS NULL OR state<>'CLOSED')
                LIMIT 1
                """,
                (bot, account),
            ).fetchone()

            require(
                held is None,
                "LOCAL_RESERVATIONS_REQUIRE_ATOMIC_SETTLEMENT",
            )

    pending = db.execute(
        """
        SELECT 1
        FROM order_intents
        WHERE broker=?
          AND (account_ref=? OR account_ref IS NULL)
          AND status IN ('SUBMITTING','SUBMITTED','UNKNOWN')
        LIMIT 1
        """,
        (BROKERS[bot], account),
    ).fetchone()

    require(
        pending is None,
        "LOCAL_ORDERS_REQUIRE_RECONCILIATION",
    )

    db.execute(
        """
        INSERT INTO capital_verified_states (
            bot, account_ref, config_revision, reconciliation_id,
            funded_capital, eligible_cash, position_cost,
            realized_net_profit, baseline_pending_buy_commitment,
            as_of
        )
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(bot,account_ref) DO UPDATE SET
            config_revision=excluded.config_revision,
            reconciliation_id=excluded.reconciliation_id,
            funded_capital=excluded.funded_capital,
            eligible_cash=excluded.eligible_cash,
            position_cost=excluded.position_cost,
            realized_net_profit=excluded.realized_net_profit,
            baseline_pending_buy_commitment=
                excluded.baseline_pending_buy_commitment,
            as_of=excluded.as_of
        """,
        (
            bot,
            account,
            proof["config_revision"],
            reconciliation_id,
            *(values[key] for key in MONEY),
            as_of,
        ),
    )

    db.execute(
        """
        INSERT INTO audit_events (
            event_type,bot_name,correlation_id,details_json
        )
        VALUES (?,?,?,?)
        """,
        (
            PUBLISH_EVENT,
            bot,
            correlation,
            canonical({
                "reconciliation_id": reconciliation_id,
                "config_revision": proof["config_revision"],
                "result_sha256": result_hash,
                "source_evidence_sha256": source_hash,
                "as_of": as_of,
            }),
        ),
    )

    return {
        "status": "CAPITAL_BASELINE_PUBLISHED",
        "baseline_updated": True,
        "reconciliation_id": reconciliation_id,
        "as_of": as_of,
        "order_submission_authorized": False,
    }


def publish_capital_baseline_in_transaction(
    db, *, bot, reconciliation_id
):
    """Atomic settlement integration.

    Caller starts BEGIN IMMEDIATE and uses sqlite3.Row.
    Success does not commit.
    Any failure rolls back the ENTIRE caller transaction, including
    its preceding settlement/hold changes.
    """
    try:
        require(db.in_transaction, "CALLER_TRANSACTION_REQUIRED")
        require(
            db.row_factory is sqlite3.Row,
            "SQLITE_ROW_FACTORY_REQUIRED",
        )
        require(bot in BROKERS, "UNKNOWN_BOT")
        require(
            type(reconciliation_id) is int
            and reconciliation_id > 0,
            "INVALID_RECONCILIATION_ID",
        )

        # Verify write ownership before inspecting dependent records.
        db.execute(
            "UPDATE capital_verified_states SET as_of=as_of WHERE 0"
        )

        return _publish_locked(db, bot, reconciliation_id)

    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise


def publish_capital_baseline(
    database_path, *, bot, reconciliation_id
):
    """Standalone initial/no-holds publication.

    Existing database only. Does not initialize tables.
    Do not use this separate-connection wrapper for hold settlement.
    """
    db = None
    try:
        db = sqlite3.connect(
            Path(database_path).resolve().as_uri() + "?mode=rw",
            uri=True,
            timeout=10,
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")

        result = publish_capital_baseline_in_transaction(
            db,
            bot=bot,
            reconciliation_id=reconciliation_id,
        )

        db.commit()
        return result

    finally:
        if db is not None:
            if db.in_transaction:
                db.rollback()
            db.close()
