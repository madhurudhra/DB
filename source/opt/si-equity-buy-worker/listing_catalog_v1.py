"""Listing discovery only; not an authorized trading universe."""
import csv
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import (
    Request, build_opener, HTTPRedirectHandler, ProxyHandler
)
from zoneinfo import ZoneInfo

ROOT = Path("/var/lib/si-equity-buy-worker")
FILES = {
    "nasdaq": (
        "https://www.nasdaqtrader.com/dynamic/SymDir/"
        "nasdaqlisted.txt"
    ),
    "other": (
        "https://www.nasdaqtrader.com/dynamic/SymDir/"
        "otherlisted.txt"
    ),
}

def save(path, data):
    fd, name = tempfile.mkstemp(prefix=".listing-", dir=ROOT)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("DIRECTORY_REDIRECT_REFUSED")

def parse(raw, kind, today=None):
    if kind not in FILES:
        raise ValueError("UNKNOWN_DIRECTORY")

    text = raw.decode("utf-8-sig")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("EMPTY_DIRECTORY")

    footer = lines[-1].split("|", 1)[0]
    match = re.fullmatch(
        r"File Creation Time:\s*(\d{10}:\d{2})", footer
    )
    if not match:
        raise ValueError("DIRECTORY_FOOTER_MISSING")

    timestamp = match.group(1)
    source_date = datetime.strptime(
        timestamp, "%m%d%Y%H:%M"
    ).date()
    reference = today or datetime.now(
        ZoneInfo("America/New_York")
    ).date()
    age = (reference - source_date).days
    if not -1 <= age <= 7:
        raise ValueError("DIRECTORY_DATE_OUTSIDE_ACCEPTED_WINDOW")

    reader = csv.DictReader(
        io.StringIO("\n".join(lines[:-1])), delimiter="|"
    )
    key = "Symbol" if kind == "nasdaq" else "ACT Symbol"
    required = {key, "Security Name", "ETF", "Test Issue"}
    required |= (
        {"Financial Status", "Market Category"}
        if kind == "nasdaq"
        else {"Exchange", "CQS Symbol", "NASDAQ Symbol"}
    )
    if not required <= set(reader.fieldnames or []):
        raise ValueError("DIRECTORY_SCHEMA_MISMATCH")

    records = []
    seen = set()
    excluded = 0

    for row in reader:
        if (
            None in row
            or any(row.get(field) is None for field in required)
        ):
            raise ValueError("MALFORMED_DIRECTORY_ROW")
        if (
            row["Test Issue"] not in {"Y", "N"}
            or row["ETF"] not in {"Y", "N"}
        ):
            raise ValueError("INVALID_DIRECTORY_FLAGS")
        if row["Test Issue"] == "Y":
            excluded += 1
            continue

        exchange = (
            "NASDAQ" if kind == "nasdaq"
            else {
                "N": "NYSE",
                "A": "NYSE_AMERICAN",
                "P": "NYSE_ARCA",
            }.get(row["Exchange"])
        )
        if exchange is None:
            excluded += 1
            continue

        symbol = row[key].strip()
        name = row["Security Name"].strip()
        if not symbol or len(symbol) > 20 or not name:
            raise ValueError("INVALID_DIRECTORY_IDENTITY")

        identity = (exchange, symbol)
        if identity in seen:
            raise ValueError("DUPLICATE_DIRECTORY_IDENTITY")
        seen.add(identity)

        records.append({
            "listing_symbol": symbol,
            "security_name": name,
            "listing_exchange": exchange,
            "is_etf": row["ETF"] == "Y",
            "financial_status": row.get("Financial Status") or None,
            "cqs_symbol": row.get("CQS Symbol"),
            "nasdaq_symbol": row.get("NASDAQ Symbol"),
            "broker_symbol": None,
            "broker_tradability_verified": False,
        })

    if not records:
        raise ValueError("NO_IN_SCOPE_DIRECTORY_RECORDS")

    return records, {
        "file_creation_timestamp_raw": timestamp,
        "source_date_age_days": age,
        "row_count": len(records),
        "excluded_test_or_out_of_scope_rows": excluded,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }

def main():
    attempted = datetime.now(timezone.utc).isoformat()
    records = []
    sources = {}

    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        for kind, url in FILES.items():
            request = Request(url, headers={
                "User-Agent": "SuperIntelligence-ListingCollector/1.0"
            })
            with opener.open(request, timeout=30) as response:
                raw = response.read(5_000_001)
            if len(raw) > 5_000_000:
                raise ValueError("DIRECTORY_TOO_LARGE")

            rows, metadata = parse(raw, kind)
            records.extend(rows)
            sources[kind] = {"url": url, **metadata}

        identities = {
            (row["listing_exchange"], row["listing_symbol"])
            for row in records
        }
        if len(identities) != len(records):
            raise ValueError("DUPLICATE_COMBINED_IDENTITY")

        report = {
            "state": "LISTING_CATALOG_COLLECTED_NOT_SCREENED",
            "fetched_at": attempted,
            "sources": sources,
            "record_count": len(records),
            "records": records,
            "contains_only_stocks_and_etfs": False,
            "broker_tradability_verified": False,
            "news_risk_verified": False,
            "entry_authorized": False,
        }
        save(ROOT / "listing-catalog.json", report)
        status = {
            key: value for key, value in report.items()
            if key != "records"
        }
        save(ROOT / "listing-catalog-status.json", status)
        print(json.dumps(status, indent=2))
        return 0

    except Exception as error:
        status = {
            "state": "CATALOG_REFRESH_FAILED_PREVIOUS_CATALOG_NOT_CURRENT",
            "attempted_at": attempted,
            "error_type": type(error).__name__,
            "reason": (
                str(error)[:160] if isinstance(error, ValueError)
                else "UPSTREAM_OR_IO_FAILURE"
            ),
            "entry_authorized": False,
        }
        save(ROOT / "listing-catalog-status.json", status)
        print(json.dumps(status, indent=2))
        return 1

if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
