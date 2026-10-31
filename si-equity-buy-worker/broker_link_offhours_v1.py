"""Off-hours broker review only. Never authorizes or submits orders."""
import asyncio
import fcntl
import hashlib
import importlib.util
import json
import os
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path("/var/lib/si-equity-buy-worker")
CONFIG = Path("/etc/si-equity-buy-worker/config.json")
OUTPUT = ROOT / "broker-link-review.json"
ARCHIVE = ROOT / "research-history" / "broker-link"
NY = ZoneInfo("America/New_York")
MAX_RUN_SECONDS = 300
REQUEST_TIMEOUT = 45

def stamp():
    return datetime.now(timezone.utc).isoformat()

def window_open():
    now = datetime.now(NY)
    if now.hour >= 20:
        return True
    # Leave a one-minute buffer before the 08:00 cutoff.
    return now.hour < 7 or (now.hour == 7 and now.minute < 59)

def config():
    cfg = json.loads(CONFIG.read_text())
    if (
        cfg.get("live_enabled") is not False
        or cfg.get("programmatic_execution_authorized") is not False
    ):
        raise RuntimeError("EXECUTION_FLAGS_MUST_REMAIN_FALSE")
    return cfg

def atomic_save(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".research-", dir=path.parent)
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

def archive_checkpoint(label):
    if not OUTPUT.exists():
        return
    ARCHIVE.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = OUTPUT.read_bytes()
    json.loads(raw)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = ARCHIVE / f"{name}-{label}.json"
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())

async def scan():
    cfg = config()
    review = json.loads((ROOT / "listing-screen-review.json").read_text())
    source_hash = hashlib.sha256(
        (ROOT / "listing-catalog.json").read_bytes()
    ).hexdigest()

    if review.get("source_sha256") != source_hash:
        raise RuntimeError("CATALOG_CHANGED_REGENERATE_PRELIMINARY_REPORT")
    if review.get("entry_authorized") is not False:
        raise RuntimeError("INVALID_REVIEW_AUTHORIZATION")

    records = review.get("records_for_review")
    if not isinstance(records, list) or not records:
        raise RuntimeError("REVIEW_RECORDS_MISSING")
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("listing_symbol"), str)
        for row in records
    ):
        raise RuntimeError("INVALID_REVIEW_RECORDS")

    symbols = sorted({row["listing_symbol"] for row in records})
    identity = {
        "source_sha256": source_hash,
        "review_sha256": hashlib.sha256(
            json.dumps(records, sort_keys=True).encode()
        ).hexdigest(),
        "account_fingerprint": hashlib.sha256(
            cfg["account_number"].encode()
        ).hexdigest(),
    }

    if OUTPUT.exists():
        checkpoint = json.loads(OUTPUT.read_text())
        if checkpoint.get("identity") != identity:
            raise RuntimeError("CHECKPOINT_INPUT_CHANGED_EXISTING_DATA_PRESERVED")
        results = checkpoint.get("results_by_symbol")
        if (
            not isinstance(results, dict)
            or not set(results).issubset(set(symbols))
            or any(not isinstance(row, dict) for row in results.values())
        ):
            raise RuntimeError("INVALID_CHECKPOINT")
    else:
        checkpoint = {
            "identity": identity,
            "started_at": stamp(),
            "results_by_symbol": {},
        }
        results = checkpoint["results_by_symbol"]

    archive_checkpoint("before")
    todo = [s for s in symbols if s not in results]
    started = time.monotonic()

    if not todo:
        print("BROKER_REVIEW_ALREADY_COMPLETE_NOT_ENTRY_AUTHORIZED")
        return

    spec = importlib.util.spec_from_file_location(
        "equity_offhours_broker",
        "/opt/si-equity-buy-worker/equity_worker.py",
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    async def read(broker, name, args):
        if not window_open():
            raise RuntimeError("OFFHOURS_WINDOW_CLOSED")
        if name not in {"get_accounts", "get_equity_tradability"}:
            raise RuntimeError("READ_TOOL_NOT_ALLOWED")
        return await asyncio.wait_for(
            broker.call(name, args), timeout=REQUEST_TIMEOUT
        )

    try:
        if not window_open():
            raise RuntimeError("OFFHOURS_WINDOW_CLOSED")
        async with worker.MCPBroker(cfg) as broker:
            accounts = (await read(broker, "get_accounts", {})).get("accounts")
            if not isinstance(accounts, list):
                raise RuntimeError("INVALID_ACCOUNTS_RESPONSE")
            matches = [
                a for a in accounts if isinstance(a, dict)
                and a.get("account_number") == cfg["account_number"]
            ]
            if len(matches) != 1:
                raise RuntimeError("CONFIGURED_ACCOUNT_NOT_UNIQUE")
            account = matches[0]
            if (
                account.get("agentic_allowed") is not True
                or account.get("state") != "active"
                or account.get("deactivated") is not False
                or account.get("permanently_deactivated") is not False
            ):
                raise RuntimeError("CONFIGURED_ACCOUNT_NOT_ELIGIBLE")

            category = account.get("brokerage_account_type")
            if not isinstance(category, str) or not category:
                raise RuntimeError("ACCOUNT_CATEGORY_MISSING")
            previous = checkpoint.get("brokerage_account_type")
            if previous is not None and previous != category:
                raise RuntimeError("ACCOUNT_CATEGORY_CHANGED")
            checkpoint["brokerage_account_type"] = category

            for offset in range(0, len(todo), 10):
                if (
                    not window_open()
                    or time.monotonic() - started >= MAX_RUN_SECONDS
                ):
                    break

                current = config()
                if current["account_number"] != cfg["account_number"]:
                    raise RuntimeError("CONFIGURED_ACCOUNT_CHANGED")

                batch = todo[offset:offset + 10]
                data = await read(broker, "get_equity_tradability", {
                    "account_number": cfg["account_number"],
                    "symbols": batch,
                })
                rows = data.get("results")
                missing = data.get("not_found")
                rows = [] if rows is None else rows
                missing = [] if missing is None else missing
                if not isinstance(rows, list) or not isinstance(missing, list):
                    raise RuntimeError("INVALID_BATCH_RESPONSE")
                if any(
                    not isinstance(r, dict) or r.get("symbol") not in batch
                    for r in rows
                ) or any(s not in batch for s in missing):
                    raise RuntimeError("UNEXPECTED_BATCH_RESULT")

                checked_at = stamp()
                for symbol in batch:
                    found = [r for r in rows if r.get("symbol") == symbol]
                    row = found[0] if len(found) == 1 else {}
                    reasons = []
                    if symbol in missing:
                        reasons.append("BROKER_SYMBOL_NOT_FOUND")
                    if len(found) != 1:
                        reasons.append("RESULT_MISSING_OR_DUPLICATED")
                    else:
                        if row.get("tradeable") is not True:
                            reasons.append("NOT_TRADEABLE")
                        if row.get("state") != "active":
                            reasons.append("NOT_CONFIRMED_ACTIVE")
                        if row.get("fractional_tradability") != "tradable":
                            reasons.append("FRACTIONAL_NOT_TRADABLE")
                        halts = row.get("internal_halt_sessions")
                        if halts is not None and (
                            not isinstance(halts, list)
                            or any(not isinstance(x, str) for x in halts)
                        ):
                            reasons.append("INVALID_HALT_DATA")
                        elif "regular_hours" in (halts or []):
                            reasons.append("REGULAR_SESSION_HALTED")

                        typed = row.get("account_type_tradabilities")
                        if not isinstance(typed, list) or any(
                            not isinstance(x, dict) for x in typed
                        ):
                            reasons.append("ACCOUNT_CATEGORY_STATUS_UNAVAILABLE")
                        else:
                            matched = [
                                x for x in typed
                                if x.get("account_type") == category
                            ]
                            if len(matched) != 1:
                                reasons.append("ACCOUNT_CATEGORY_STATUS_UNAVAILABLE")
                            elif matched[0].get(
                                "account_type_tradability"
                            ) != "tradable":
                                reasons.append("ACCOUNT_CATEGORY_NOT_TRADABLE")

                    results[symbol] = {
                        "checked_at": checked_at,
                        "broker_name": row.get("name"),
                        "tradability_checks_passed": not reasons,
                        "hold_reasons": reasons,
                        "classification_verified": False,
                        "news_risk_verified": False,
                        "earnings_risk_verified": False,
                        "entry_authorized": False,
                    }

                checkpoint.update(
                    state="BROKER_REVIEW_COMPLETE_NOT_ENTRY_AUTHORIZED"
                    if len(results) == len(symbols)
                    else "BROKER_REVIEW_PARTIAL_NOT_ENTRY_AUTHORIZED",
                    updated_at=stamp(),
                    total_symbols=len(symbols),
                    checked_symbols=len(results),
                    entry_authorized=False,
                )
                atomic_save(OUTPUT, checkpoint)
                print(f"Checked {len(results)}/{len(symbols)}", flush=True)
                await asyncio.sleep(1)
    finally:
        archive_checkpoint("after")

    counts = Counter(
        "TRADABILITY_CHECKS_PASSED"
        if r.get("tradability_checks_passed") is True else "HELD"
        for r in results.values()
    )
    print(json.dumps({
        "state": checkpoint.get("state"),
        "checked_symbols": len(results),
        "remaining_symbols": len(symbols) - len(results),
        "summary": dict(counts),
        "entry_authorized": False,
    }, indent=2))

def main():
    # Refuse daytime launches before reading the catalog or contacting MCP.
    if not window_open():
        print(json.dumps({
            "state": "DEFERRED_OUTSIDE_RESEARCH_WINDOW",
            "new_york_time": datetime.now(NY).isoformat(),
            "allowed": "20:00 through 07:59 America/New_York",
            "broker_calls": 0,
            "orders_submitted": False,
        }, indent=2))
        return

    with (ROOT / "broker-link-review.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(scan())

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped. Saved checkpoints and completed archives retained.")
        raise SystemExit(130)
    except Exception as error:
        print("SCAN_STOPPED:", type(error).__name__, str(error))
        raise SystemExit(1)
