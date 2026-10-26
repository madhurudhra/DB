import concurrent.futures
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "capital_baseline_publisher_v1.py"
)

if not MODULE_PATH.is_file():
    raise RuntimeError(
        f"Publisher implementation is missing: {MODULE_PATH}"
    )

spec = importlib.util.spec_from_file_location(
    "capital_publisher_under_test",
    MODULE_PATH,
)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


SCHEMA = """
CREATE TABLE independent_bot_configurations (
    bot TEXT PRIMARY KEY,
    revision INTEGER,
    config_json TEXT
);

CREATE TABLE reconciliation_runs (
    id INTEGER PRIMARY KEY,
    broker TEXT,
    account_ref TEXT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    discrepancies_json TEXT
);

CREATE TABLE capital_verified_states (
    bot TEXT NOT NULL,
    account_ref TEXT NOT NULL,
    config_revision INTEGER NOT NULL,
    reconciliation_id INTEGER NOT NULL,
    funded_capital TEXT NOT NULL,
    eligible_cash TEXT NOT NULL,
    position_cost TEXT NOT NULL,
    realized_net_profit TEXT NOT NULL,
    baseline_pending_buy_commitment TEXT NOT NULL,
    as_of TEXT NOT NULL,
    PRIMARY KEY (bot, account_ref),
    FOREIGN KEY (reconciliation_id)
        REFERENCES reconciliation_runs(id)
);

CREATE TABLE capital_buy_reservations (
    request_id TEXT PRIMARY KEY,
    bot TEXT,
    account_ref TEXT,
    state TEXT
);

CREATE TABLE order_intents (
    id INTEGER PRIMARY KEY,
    broker TEXT,
    account_ref TEXT,
    status TEXT
);

CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY,
    event_type TEXT,
    bot_name TEXT,
    correlation_id TEXT,
    details_json TEXT
);

CREATE TRIGGER audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;

CREATE TRIGGER audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;
"""


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.now = datetime.now(timezone.utc)

        # Synthetic test data only. Never published to a live database.
        self.proof = {
            "schema_version": 1,
            "bot": "crypto",
            "broker": "robinhood_crypto",
            "account_ref": "TEST-5576",
            "config_revision": 2,
            "reconciliation_id": 1,
            "currency": "USD",
            "as_of": (
                self.now - timedelta(seconds=2)
            ).isoformat(),
            "funded_capital": "150.00",
            "eligible_cash": "100.00",
            "position_cost": "40.00",
            "realized_net_profit": "-2.00",
            "baseline_pending_buy_commitment": "0",
            "checks": {
                key: True for key in publisher.CHECKS
            },
            "unresolved_counts": {
                key: 0 for key in publisher.UNRESOLVED
            },
            "source_evidence_sha256": hashlib.sha256(
                b"SYNTHETIC UNIT TEST NOT BROKER EVIDENCE"
            ).hexdigest(),
        }

        with self.db() as db:
            db.executescript(SCHEMA)

            db.execute(
                """
                INSERT INTO independent_bot_configurations
                VALUES (?, 2, ?)
                """,
                (
                    "crypto",
                    json.dumps({
                        "account_ref": "TEST-5576",
                        "currency": "USD",
                        "allocated_capital": "150",
                        "cash_reserve_pct": "10",
                    }),
                ),
            )

            db.execute(
                """
                INSERT INTO reconciliation_runs
                VALUES (
                    1,
                    'robinhood_crypto',
                    'TEST-5576',
                    ?,
                    ?,
                    'PASS',
                    '[]'
                )
                """,
                (
                    (
                        self.now - timedelta(seconds=3)
                    ).isoformat(),
                    (
                        self.now - timedelta(seconds=1)
                    ).isoformat(),
                ),
            )

    def tearDown(self):
        self.temp.cleanup()

    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def evidence(self, proof=None):
        data = self.proof if proof is None else proof

        with self.db() as db:
            db.execute(
                """
                INSERT INTO audit_events (
                    event_type,
                    bot_name,
                    correlation_id,
                    details_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    publisher.RESULT_EVENT,
                    "crypto",
                    "1",
                    json.dumps(data),
                ),
            )

    def publish(self):
        return publisher.publish_capital_baseline(
            self.path,
            bot="crypto",
            reconciliation_id=1,
        )

    def assert_blocked(self, expected_code=None):
        with self.assertRaises(
            publisher.PublicationBlocked
        ) as caught:
            self.publish()

        if expected_code is not None:
            self.assertEqual(
                str(caught.exception),
                expected_code,
            )

        with self.db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM capital_verified_states"
            ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_publish(self):
        self.evidence()

        result = self.publish()
        self.assertTrue(result["baseline_updated"])

        with self.db() as db:
            row = db.execute(
                "SELECT * FROM capital_verified_states"
            ).fetchone()

            self.assertEqual(row["funded_capital"], "150.00")
            self.assertEqual(row["config_revision"], 2)

            count = db.execute(
                """
                SELECT COUNT(*)
                FROM audit_events
                WHERE event_type=?
                """,
                (publisher.PUBLISH_EVENT,),
            ).fetchone()[0]

            self.assertEqual(count, 1)

    def test_idempotent_no_timestamp_refresh(self):
        self.evidence()

        first = self.publish()
        second = self.publish()

        self.assertEqual(
            second["status"],
            "ALREADY_PUBLISHED",
        )
        self.assertFalse(second["baseline_updated"])
        self.assertEqual(
            first["as_of"],
            second["as_of"],
        )

    def test_two_publishers_one_publication(self):
        self.evidence()

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=2
        ) as pool:
            results = list(
                pool.map(
                    lambda unused: self.publish(),
                    range(2),
                )
            )

        updated = sum(
            result["baseline_updated"]
            for result in results
        )
        self.assertEqual(updated, 1)

    def test_missing_evidence(self):
        self.assert_blocked(
            "ONE_TRUSTED_RECONCILIATION_RESULT_REQUIRED"
        )

    def test_duplicate_evidence(self):
        self.evidence()
        self.evidence()

        self.assert_blocked(
            "ONE_TRUSTED_RECONCILIATION_RESULT_REQUIRED"
        )

    def test_missing_fee_check(self):
        self.proof["checks"]["fees_verified"] = False
        self.evidence()

        self.assert_blocked(
            "RECONCILIATION_CHECK_FAILED"
        )

    def test_unresolved_fees(self):
        self.proof["unresolved_counts"]["fees"] = 5
        self.evidence()

        self.assert_blocked(
            "UNRESOLVED_RECONCILIATION_ITEMS"
        )

    def test_boolean_unresolved_not_accepted_as_zero(self):
        self.proof["unresolved_counts"]["fees"] = False
        self.evidence()

        self.assert_blocked(
            "UNRESOLVED_RECONCILIATION_ITEMS"
        )

    def test_account_mismatch(self):
        self.proof["account_ref"] = "OTHER"
        self.evidence()

        self.assert_blocked(
            "RESULT_ACCOUNT_MISMATCH"
        )

    def test_changed_config(self):
        self.evidence()

        with self.db() as db:
            db.execute(
                """
                UPDATE independent_bot_configurations
                SET revision=3
                """
            )

        self.assert_blocked(
            "CONFIGURATION_REVISION_CHANGED"
        )

    def test_wrong_currency(self):
        self.proof["currency"] = "INR"
        self.evidence()

        self.assert_blocked(
            "RESULT_CURRENCY_MISMATCH"
        )

    def test_nan(self):
        self.proof["funded_capital"] = "NaN"
        self.evidence()

        self.assert_blocked(
            "INVALID_CAPITAL_VALUE"
        )

    def test_float_rejected(self):
        self.proof["funded_capital"] = 150.0
        self.evidence()

        self.assert_blocked(
            "MONEY_MUST_BE_DECIMAL_STRINGS"
        )

    def test_missing_amount_not_zero(self):
        del self.proof["eligible_cash"]
        self.evidence()

        self.assert_blocked(
            "MONEY_MUST_BE_DECIMAL_STRINGS"
        )

    def test_negative_funding_rejected(self):
        self.proof["funded_capital"] = "-1"
        self.evidence()

        self.assert_blocked(
            "NEGATIVE_CAPITAL_VALUE"
        )

    def test_negative_profit_allowed(self):
        self.evidence()

        self.assertTrue(
            self.publish()["baseline_updated"]
        )

    def test_verified_zero_not_missing(self):
        for key in publisher.MONEY:
            self.proof[key] = "0"

        self.evidence()

        self.assertTrue(
            self.publish()["baseline_updated"]
        )

        with self.db() as db:
            value = db.execute(
                """
                SELECT funded_capital
                FROM capital_verified_states
                """
            ).fetchone()[0]

            self.assertEqual(value, "0")

    def test_stale(self):
        self.proof["as_of"] = (
            self.now - timedelta(seconds=120)
        ).isoformat()
        self.evidence()

        with self.db() as db:
            db.execute(
                """
                UPDATE reconciliation_runs
                SET started_at=?
                """,
                (
                    (
                        self.now - timedelta(seconds=130)
                    ).isoformat(),
                ),
            )

        self.assert_blocked(
            "RECONCILIATION_RESULT_STALE"
        )

    def test_future(self):
        self.proof["as_of"] = (
            self.now + timedelta(seconds=1)
        ).isoformat()
        self.evidence()

        self.assert_blocked(
            "RECONCILIATION_TIME_ORDER_INVALID"
        )

    def test_newer_failed_run_blocks_old_pass(self):
        self.evidence()

        with self.db() as db:
            db.execute(
                """
                INSERT INTO reconciliation_runs (
                    id, broker, account_ref, status
                )
                VALUES (
                    2,
                    'robinhood_crypto',
                    'TEST-5576',
                    'FAIL'
                )
                """
            )

        self.assert_blocked(
            "LATEST_PASSING_RECONCILIATION_REQUIRED"
        )

    def test_discrepancies_block(self):
        self.evidence()

        with self.db() as db:
            db.execute(
                """
                UPDATE reconciliation_runs
                SET discrepancies_json=?
                """,
                (json.dumps(["mismatch"]),),
            )

        self.assert_blocked(
            "RECONCILIATION_DISCREPANCIES_PRESENT"
        )

    def test_held_buy_blocks(self):
        self.evidence()

        with self.db() as db:
            db.execute(
                """
                INSERT INTO capital_buy_reservations
                VALUES ('R','crypto','TEST-5576','HELD')
                """
            )

        self.assert_blocked(
            "LOCAL_RESERVATIONS_REQUIRE_ATOMIC_SETTLEMENT"
        )

    def test_unknown_buy_blocks(self):
        self.evidence()

        with self.db() as db:
            db.execute(
                """
                INSERT INTO capital_buy_reservations
                VALUES ('R','crypto','TEST-5576','UNKNOWN')
                """
            )

        self.assert_blocked(
            "LOCAL_RESERVATIONS_REQUIRE_ATOMIC_SETTLEMENT"
        )

    def test_held_sell_blocks(self):
        self.evidence()

        with self.db() as db:
            db.execute(
                """
                CREATE TABLE automatic_sell_reservations_v2 (
                    bot TEXT,
                    account_ref TEXT,
                    state TEXT
                )
                """
            )
            db.execute(
                """
                INSERT INTO automatic_sell_reservations_v2
                VALUES ('crypto','TEST-5576','HELD')
                """
            )

        self.assert_blocked(
            "LOCAL_RESERVATIONS_REQUIRE_ATOMIC_SETTLEMENT"
        )

    def test_pending_order_blocks(self):
        self.evidence()

        with self.db() as db:
            db.execute(
                """
                INSERT INTO order_intents
                VALUES (
                    1,
                    'robinhood_crypto',
                    'TEST-5576',
                    'SUBMITTED'
                )
                """
            )

        self.assert_blocked(
            "LOCAL_ORDERS_REQUIRE_RECONCILIATION"
        )

    def test_unscoped_unknown_order_blocks(self):
        self.evidence()

        with self.db() as db:
            db.execute(
                """
                INSERT INTO order_intents
                VALUES (
                    1,
                    'robinhood_crypto',
                    NULL,
                    'UNKNOWN'
                )
                """
            )

        self.assert_blocked(
            "LOCAL_ORDERS_REQUIRE_RECONCILIATION"
        )

    def test_audit_failure_rolls_back_state(self):
        self.evidence()

        with self.db() as db:
            db.execute(
                """
                CREATE TRIGGER fail_publish
                BEFORE INSERT ON audit_events
                WHEN NEW.event_type=
                    'CAPITAL_BASELINE_PUBLISHED_V1'
                BEGIN
                    SELECT RAISE(ABORT, 'test');
                END;
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.publish()

        with self.db() as db:
            count = db.execute(
                """
                SELECT COUNT(*)
                FROM capital_verified_states
                """
            ).fetchone()[0]

            self.assertEqual(count, 0)

    def test_replay_does_not_release_new_holds(self):
        self.evidence()
        self.publish()

        with self.db() as db:
            db.execute(
                """
                INSERT INTO capital_buy_reservations
                VALUES ('R','crypto','TEST-5576','HELD')
                """
            )

        self.assertEqual(
            self.publish()["status"],
            "ALREADY_PUBLISHED",
        )

        with self.db() as db:
            state = db.execute(
                """
                SELECT state
                FROM capital_buy_reservations
                """
            ).fetchone()[0]

            self.assertEqual(state, "HELD")

    def test_existing_row_tamper_detected_on_replay(self):
        self.evidence()
        self.publish()

        with self.db() as db:
            db.execute(
                """
                UPDATE capital_verified_states
                SET eligible_cash='999'
                """
            )

        with self.assertRaises(
            publisher.PublicationBlocked
        ):
            self.publish()

    def test_missing_database_not_created(self):
        missing = (
            Path(self.temp.name) / "missing.sqlite3"
        )

        with self.assertRaises(sqlite3.OperationalError):
            publisher.publish_capital_baseline(
                missing,
                bot="crypto",
                reconciliation_id=1,
            )

        self.assertFalse(missing.exists())

    def test_atomic_settlement_and_publication(self):
        self.evidence()

        with self.db() as setup:
            setup.execute(
                """
                INSERT INTO capital_buy_reservations
                VALUES ('R','crypto','TEST-5576','HELD')
                """
            )

        db = self.db()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE capital_buy_reservations
                SET state='CLOSED'
                """
            )

            result = (
                publisher.publish_capital_baseline_in_transaction(
                    db,
                    bot="crypto",
                    reconciliation_id=1,
                )
            )

            self.assertTrue(result["baseline_updated"])
            self.assertTrue(db.in_transaction)

            # Another connection must still see the old committed state.
            with self.db() as observer:
                count = observer.execute(
                    """
                    SELECT COUNT(*)
                    FROM capital_verified_states
                    """
                ).fetchone()[0]
                state = observer.execute(
                    """
                    SELECT state
                    FROM capital_buy_reservations
                    """
                ).fetchone()[0]

                self.assertEqual(count, 0)
                self.assertEqual(state, "HELD")

            db.commit()

            with self.db() as observer:
                count = observer.execute(
                    """
                    SELECT COUNT(*)
                    FROM capital_verified_states
                    """
                ).fetchone()[0]
                state = observer.execute(
                    """
                    SELECT state
                    FROM capital_buy_reservations
                    """
                ).fetchone()[0]

                self.assertEqual(count, 1)
                self.assertEqual(state, "CLOSED")
        finally:
            db.close()

    def test_failed_publication_rolls_back_callers_hold_closure(self):
        self.proof["unresolved_counts"]["fees"] = 5
        self.evidence()

        with self.db() as setup:
            setup.execute(
                """
                INSERT INTO capital_buy_reservations
                VALUES ('R','crypto','TEST-5576','HELD')
                """
            )

        db = self.db()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE capital_buy_reservations
                SET state='CLOSED'
                """
            )

            with self.assertRaises(
                publisher.PublicationBlocked
            ):
                publisher.publish_capital_baseline_in_transaction(
                    db,
                    bot="crypto",
                    reconciliation_id=1,
                )

            self.assertFalse(db.in_transaction)

            with self.db() as observer:
                state = observer.execute(
                    """
                    SELECT state
                    FROM capital_buy_reservations
                    """
                ).fetchone()[0]

                self.assertEqual(state, "HELD")
        finally:
            db.close()

    def test_in_transaction_api_requires_transaction(self):
        db = self.db()
        try:
            with self.assertRaises(
                publisher.PublicationBlocked
            ):
                publisher.publish_capital_baseline_in_transaction(
                    db,
                    bot="crypto",
                    reconciliation_id=1,
                )
        finally:
            db.close()

    def test_in_transaction_api_rejects_wrong_row_factory(self):
        self.evidence()
        db = self.db()

        try:
            db.row_factory = None
            db.execute("BEGIN IMMEDIATE")

            with self.assertRaises(
                publisher.PublicationBlocked
            ):
                publisher.publish_capital_baseline_in_transaction(
                    db,
                    bot="crypto",
                    reconciliation_id=1,
                )

            self.assertFalse(db.in_transaction)
        finally:
            db.close()


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        PublisherTests
    )

    count = suite.countTestCases()
    if count != 34:
        raise SystemExit(
            f"Expected 34 tests, found {count}. "
            "The file may not have been pasted completely."
        )

    result = unittest.TextTestRunner(
        verbosity=2
    ).run(suite)

    raise SystemExit(
        0 if result.wasSuccessful() else 1
    )
