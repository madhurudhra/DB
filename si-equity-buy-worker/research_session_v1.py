"""Off-hours research snapshots. No order execution or universe publishing."""
import ast
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/var/lib/si-equity-buy-worker")
RUNS = BASE / "research-runs"
CONFIG = Path("/etc/si-equity-buy-worker/config.json")
SCANNER = Path("/opt/si-equity-buy-worker/broker_link_offhours_v1.py")
NY = ZoneInfo("America/New_York")

def stamp():
    return datetime.now(timezone.utc).isoformat()

def allowed():
    now = datetime.now(NY)
    return now.hour >= 20 or now.hour < 7 or (
        now.hour == 7 and now.minute < 59
    )

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def write_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".session-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def write_bytes(path, raw):
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())

def prepare_review(catalog, source_hash):
    records = catalog.get("records")
    if (
        not isinstance(records, list)
        or not records
        or catalog.get("record_count") != len(records)
        or any(not isinstance(row, dict) for row in records)
    ):
        raise RuntimeError("INVALID_CATALOG")

    excluded = re.compile(
        r"\bwarrants?\b|\brights?\b|\bunits?\b|\bpreferred\b|"
        r"\bpreference\b|\bdepositary shares?\b|\bnotes?\b|"
        r"\bdebentures?\b|\bbonds?\b|\bbeneficial interest\b|"
        r"\bclosed[- ]end\b|\bETNs?\b|\bNextShares\b", re.I
    )
    common = re.compile(
        r"\bcommon stock\b|\bcommon shares?\b|"
        r"\bordinary shares?\b|\bAmerican Depositary Shares?\b|"
        r"\bAmerican Depositary Receipts?\b|\bADRs?\b", re.I
    )
    symbol_pattern = re.compile(r"[A-Z][A-Z0-9.-]{0,9}")
    duplicates = Counter(
        r.get("listing_symbol") for r in records
        if isinstance(r.get("listing_symbol"), str)
    )
    review, held = [], []
    counts = Counter()

    for row in records:
        symbol, name = row.get("listing_symbol"), row.get("security_name")
        reason, inferred = None, None
        if not isinstance(symbol, str) or not symbol_pattern.fullmatch(symbol):
            reason = "SYMBOL_REQUIRES_EXPLICIT_MAPPING"
        elif duplicates[symbol] != 1:
            reason = "DUPLICATE_LISTING_SYMBOL"
        elif not isinstance(name, str) or not name.strip():
            reason = "SECURITY_DESCRIPTION_MISSING"
        elif excluded.search(name):
            reason = "OUT_OF_SCOPE_OR_COMPLEX_SECURITY_DESCRIPTION"
        elif row.get("financial_status") not in (None, "", "N"):
            reason = "NON_NORMAL_FINANCIAL_STATUS"
        elif row.get("is_etf") is True:
            inferred = "ETF_FLAGGED_BY_CATALOG"
        elif row.get("is_etf") is False and common.search(name):
            inferred = "COMMON_STOCK_OR_ADR_DESCRIPTION_MATCH"
        else:
            reason = "SECURITY_TYPE_UNRESOLVED"

        if reason:
            counts[reason] += 1
            held.append({
                "listing_symbol": symbol,
                "security_name": name,
                "reason": reason,
            })
        else:
            counts["REQUIRES_BROKER_AND_RISK_REVIEW"] += 1
            review.append({
                "listing_symbol": symbol,
                "security_name": name,
                "listing_exchange": row.get("listing_exchange"),
                "inferred_type": inferred,
                "classification_verified": False,
                "broker_symbol": None,
                "instrument_id": None,
                "broker_tradability_verified": False,
                "news_risk_verified": False,
                "earnings_risk_verified": False,
                "entry_authorized": False,
            })

    return {
        "state": "PRELIMINARY_SCREEN_ONLY_NOT_AUTHORIZED_UNIVERSE",
        "generated_at": stamp(),
        "source_fetched_at": catalog.get("fetched_at"),
        "source_sha256": source_hash,
        "source_freshness_verified": False,
        "source_record_count": len(records),
        "review_record_count": len(review),
        "held_record_count": len(held),
        "reason_counts": dict(counts),
        "classification_method": "CONSERVATIVE_DESCRIPTION_HEURISTICS",
        "classification_verified": False,
        "broker_tradability_verified": False,
        "news_risk_verified": False,
        "earnings_risk_verified": False,
        "entry_authorized": False,
        "records_for_review": review,
        "held_records": held,
    }

def validate_inputs(run):
    manifest = json.loads((run / "manifest.json").read_text())
    for name, expected in manifest["input_sha256"].items():
        if digest((run / name).read_bytes()) != expected:
            raise RuntimeError("FROZEN_INPUT_HASH_MISMATCH")
    return manifest

def main():
    if not allowed():
        print(json.dumps({
            "state": "DEFERRED_OUTSIDE_RESEARCH_WINDOW",
            "new_york_time": datetime.now(NY).isoformat(),
            "broker_calls": 0,
            "orders_submitted": False,
        }, indent=2))
        return

    cfg = json.loads(CONFIG.read_text())
    if (
        cfg.get("live_enabled") is not False
        or cfg.get("programmatic_execution_authorized") is not False
    ):
        raise RuntimeError("EXECUTION_FLAGS_MUST_REMAIN_FALSE")

    # Share the original lock with manually launched legacy scans.
    with (BASE / "broker-link-review.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        RUNS.mkdir(mode=0o700, parents=True, exist_ok=True)
        now = datetime.now(NY)
        # A session runs from one evening through the following morning.
        session_date = (
            (now + timedelta(days=1)).date()
            if now.hour >= 20 else now.date()
        )
        run = RUNS / session_date.isoformat()

        if not run.exists():
            staging = Path(tempfile.mkdtemp(prefix=".prepare-", dir=RUNS))
            try:
                raw = (BASE / "listing-catalog.json").read_bytes()
                catalog = json.loads(raw)
                review = prepare_review(catalog, digest(raw))

                write_bytes(staging / "listing-catalog.json", raw)
                write_json(staging / "listing-screen-review.json", review)

                hashes = {
                    "listing-catalog.json": digest(raw),
                    "listing-screen-review.json": digest(
                        (staging / "listing-screen-review.json").read_bytes()
                    ),
                }

                news = BASE / "google-news.json"
                if news.is_file():
                    news_raw = news.read_bytes()
                    json.loads(news_raw)
                    write_bytes(staging / "google-news.json", news_raw)
                    hashes["google-news.json"] = digest(news_raw)

                expected_identity = {
                    "source_sha256": digest(raw),
                    "review_sha256": digest(json.dumps(
                        review["records_for_review"], sort_keys=True
                    ).encode()),
                    "account_fingerprint": digest(
                        cfg["account_number"].encode()
                    ),
                }

                legacy = BASE / "broker-link-review.json"
                reused = False
                if legacy.is_file():
                    legacy_raw = legacy.read_bytes()
                    legacy_data = json.loads(legacy_raw)
                    if legacy_data.get("identity") == expected_identity:
                        write_bytes(
                            staging / "broker-link-review.json", legacy_raw
                        )
                        reused = True

                write_json(staging / "manifest.json", {
                    "session_id": session_date.isoformat(),
                    "created_at": stamp(),
                    "classification_rules_version": "description-heuristics-v1",
                    "input_sha256": hashes,
                    "legacy_checkpoint_reused": reused,
                    "entry_authorized": False,
                })
                os.rename(staging, run)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

        manifest = validate_inputs(run)
        if not allowed():
            print("DEFERRED: session prepared; research window closed.")
            return

        ast.parse(SCANNER.read_text(), filename=str(SCANNER))
        spec = importlib.util.spec_from_file_location(
            "equity_frozen_session_scanner", str(SCANNER)
        )
        scanner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scanner)

        # Redirect research files only; actual configuration stays unchanged.
        scanner.ROOT = run
        scanner.OUTPUT = run / "broker-link-review.json"
        scanner.ARCHIVE = run / "checkpoint-history"

        print(json.dumps({
            "session_id": manifest["session_id"],
            "research_directory": str(run),
            "legacy_checkpoint_reused": manifest["legacy_checkpoint_reused"],
            "entry_authorized": False,
        }, indent=2), flush=True)

        scanner.main()

if __name__ == "__main__":
    try:
        main()
    except BlockingIOError:
        print("DEFERRED: another research scan holds the lock.")
    except KeyboardInterrupt:
        print("Stopped. Research snapshots and saved batches retained.")
        raise SystemExit(130)
    except Exception as error:
        print("RESEARCH_STOPPED:", type(error).__name__, str(error))
        raise SystemExit(1)
