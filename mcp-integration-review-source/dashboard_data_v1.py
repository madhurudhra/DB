"""Authenticated dashboard reads. Never submits or cancels orders."""

import asyncio
import copy
import json
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.trading_controls import authorize
from app.core.config import get_settings

Bot = Literal["equity", "crypto", "zerodha"]

router = APIRouter(
    prefix="/api/dashboard/v1",
    dependencies=[Depends(authorize)],
)

_cache = {}
_locks = {
    name: threading.Lock()
    for name in ("equity", "crypto", "zerodha")
}
CACHE_SECONDS = 30


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def private(response):
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"


def scrub(value):
    denied = {
        "access_token", "refresh_token", "api_key", "apikey",
        "password", "authorization", "private_key",
        "private_key_b64", "secret", "enctoken",
        "public_token", "request_token",
    }
    if isinstance(value, dict):
        return {
            str(key): scrub(item)
            for key, item in value.items()
            if str(key).lower() not in denied
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def saved_state(bot):
    try:
        path = Path(get_settings().database_path).resolve()

        with closing(sqlite3.connect(
            path.as_uri() + "?mode=ro",
            uri=True,
            timeout=5,
        )) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")

            config_row = db.execute(
                """
                SELECT revision, config_json, updated_at
                FROM independent_bot_configurations
                WHERE bot=?
                """,
                (bot,),
            ).fetchone()

            control_row = db.execute(
                """
                SELECT revision, state_json
                FROM dashboard_control_state
                WHERE singleton=1
                """
            ).fetchone()

            if config_row is None or control_row is None:
                raise ValueError("Saved state missing")

            configuration = json.loads(config_row["config_json"])
            controls = json.loads(control_row["state_json"])[bot]

            return {
                "status": "SAVED_STATE_ONLY",
                "configuration": configuration,
                "configuration_revision": config_row["revision"],
                "configuration_updated_at": config_row["updated_at"],
                "control_revision": control_row["revision"],
                "controls": {
                    "requested": controls,
                    "effective": None,
                    "enforcement_connected": False,
                },
                "remaining_automatic_budget": None,
                "budget_status": "EXECUTION_LEDGER_NOT_IMPLEMENTED",
            }

    except Exception as exc:
        return {
            "status": "SAVED_STATE_UNAVAILABLE",
            "error_type": type(exc).__name__,
            "configuration": None,
            "controls": None,
        }


def read_part(function):
    requested_at = timestamp()

    try:
        value = function()

        if value is None:
            raise ValueError("Empty broker response")

        if isinstance(value, dict):
            if value.get("error") or value.get("errors"):
                raise ValueError("Broker returned error")

        return {
            "status": "BROKER_RESPONSE_RECEIVED",
            "requested_at": requested_at,
            "received_at": timestamp(),
            "source_timestamp": None,
            "data": scrub(value),
        }

    except Exception as exc:
        return {
            "status": "UNAVAILABLE",
            "requested_at": requested_at,
            "received_at": timestamp(),
            "source_timestamp": None,
            "error_type": type(exc).__name__,
            "data": None,
        }


def broker_data(bot):
    client = None

    try:
        if bot == 'equity':
            from app.core.equity_reads_v1 import equity_dashboard_payload
            return equity_dashboard_payload()

        if bot == "crypto":
            from app.brokers.robinhood.dashboard_crypto_reader_v1 import DashboardCryptoReader
            client = DashboardCryptoReader()
            methods = ("accounts", "holdings", "orders", "pairs")
        else:
            from app.brokers.zerodha.kite import ZerodhaKite
            client = ZerodhaKite()
            methods = (
                "profile", "margins", "holdings",
                "positions", "orders", "trades",
            )

        return {
            "source": "existing_" + bot + "_adapter",
            "coverage": "RAW_BROKER_RESPONSES",
            "parts": {
                name: read_part(getattr(client, name))
                for name in methods
            },
        }

    except Exception as exc:
        return {
            "source": bot,
            "coverage": "UNAVAILABLE",
            "parts": {},
            "error_type": type(exc).__name__,
        }

    finally:
        session = getattr(client, "session", None)
        if session is not None:
            session.close()


@router.get("/layout")
def layout(response: Response):
    private(response)

    return {
        "schema_version": 1,
        "default_page": "equity",
        "pages": [
            {
                "bot": "equity",
                "title": "Equity-Trading",
                "ticker_rows": ["NYSE_NASDAQ_BULL", "NYSE_NASDAQ_BEAR"],
            },
            {
                "bot": "crypto",
                "title": "Crypto",
                "ticker_rows": ["CRYPTO_BULL", "CRYPTO_BEAR"],
            },
            {
                "bot": "zerodha",
                "title": "IN-Zerodha",
                "ticker_rows": [
                    "NSE_BULL", "NSE_BEAR",
                    "BSE_BULL", "BSE_BEAR",
                ],
            },
        ],
        "colors": {
            "bull": "#39ff88",
            "bear": "#ffaaaa",
        },
        "deduplication": "exchange_and_instrument_id",
        "manual_order_api_available": False,
        "automatic_execution_readiness": "NOT_ESTABLISHED_BY_THIS_API",
    }


@router.get("/bots/{bot}")
def bot_snapshot(bot: Bot, response: Response):
    private(response)
    state = saved_state(bot)

    with _locks[bot]:
        entry = _cache.get(bot)

        if entry is None or time.monotonic() - entry[0] >= CACHE_SECONDS:
            payload = broker_data(bot)
            entry = (time.monotonic(), timestamp(), payload)
            _cache[bot] = entry

        payload = copy.deepcopy(entry[2])
        age = round(max(0, time.monotonic() - entry[0]), 2)

    parts = list(payload.get("parts", {}).values())
    received = sum(
        part["status"] == "BROKER_RESPONSE_RECEIVED"
        for part in parts
    )

    if received == 0:
        status = "UNAVAILABLE"
    elif received != len(parts) or bot == "equity":
        status = "PARTIAL"
    else:
        status = "RAW_READS_COMPLETE"

    return {
        "schema_version": 1,
        "bot": bot,
        "generated_at": timestamp(),
        "status": status,
        "saved_state": state,
        "broker": payload,
        "broker_fetch_completed_at": entry[1],
        "cache_age_seconds": age,
        "cache_ttl_seconds": CACHE_SECONDS,
        "account_selection_verified": False,
        "account_scope": "BROKER_RESPONSE_NOT_BOUND_TO_CONFIGURED_ACCOUNT",
        "closed_trade_ledger": {
            "status": "NOT_IMPLEMENTED",
            "records": None,
        },
        "manual_order_api_available": False,
        "automatic_execution_readiness": "NOT_ESTABLISHED_BY_THIS_API",
        "warning": (
            "Raw reads are not verified account routing, fill confirmation, "
            "or permission to trade. Pagination is not followed."
        ),
    }


@router.get("/bots/{bot}/market-movers")
def market_movers(
    bot: Bot,
    response: Response,
    symbols: list[str] | None = Query(default=None, max_length=50),
):
    private(response)

    if bot != "zerodha":
        return {
            "bot": bot,
            "status": "FEED_ADAPTER_REQUIRED",
            "asOf": None,
            "instruments": None,
            "rows": None,
        }

    selected = list(dict.fromkeys(symbols or []))
    rows = {
        "NSE_BULL": [],
        "NSE_BEAR": [],
        "BSE_BULL": [],
        "BSE_BEAR": [],
    }

    if not selected:
        return {
            "bot": bot,
            "status": "SYMBOL_UNIVERSE_REQUIRED",
            "asOf": None,
            "instruments": None,
            "rows": {key: None for key in rows},
        }

    for identifier in selected:
        exchange, separator, name = identifier.partition(":")

        if (
            exchange not in ("NSE", "BSE")
            or not separator
            or not name
            or len(identifier) > 100
            or any(ord(char) < 32 for char in identifier)
        ):
            raise HTTPException(
                422,
                "Use explicit NSE:SYMBOL or BSE:SYMBOL identifiers",
            )

    try:
        from app.brokers.zerodha.kite import ZerodhaKite
        quotes = ZerodhaKite().quote(*selected)

        if not isinstance(quotes, dict):
            raise ValueError("Invalid quote response")

    except Exception as exc:
        raise HTTPException(
            503,
            {
                "status": "QUOTE_READ_FAILED",
                "error_type": type(exc).__name__,
            },
        ) from None

    instruments = []
    skipped = []

    for identifier in selected:
        try:
            quote = quotes[identifier]
            price = Decimal(str(quote["last_price"]))
            previous = Decimal(str(quote["ohlc"]["close"]))

            if (
                not price.is_finite()
                or price < 0
                or not previous.is_finite()
                or previous <= 0
            ):
                raise ValueError("Invalid price baseline")

            change = price - previous
            percent = change / previous * 100
            exchange, symbol = identifier.split(":", 1)

            item = {
                "instrumentId": str(
                    quote.get("instrument_token", identifier)
                ),
                "symbol": symbol,
                "exchange": exchange,
                "assetClass": "equity",
                "price": float(price),
                "change": float(change),
                "changePct": float(percent),
                "sourceTimestamp": quote.get("timestamp"),
                "analysisLinks": [],
            }

            instruments.append(item)

            if percent != 0:
                direction = "BULL" if percent > 0 else "BEAR"
                rows[exchange + "_" + direction].append(item)

        except (KeyError, TypeError, ValueError, ArithmeticError):
            skipped.append(identifier)

    for key, values in rows.items():
        values.sort(
            key=lambda item: item["changePct"],
            reverse=key.endswith("BULL"),
        )

    return {
        "bot": bot,
        "status": "QUOTES_RECEIVED" if instruments else "UNAVAILABLE",
        "asOf": timestamp(),
        "timestampMeaning": "server_receipt_not_exchange_timestamp",
        "source": "zerodha_kite",
        "changeBasis": "previous_close",
        "delayed": None,
        "delayStatus": "NOT_CERTIFIED",
        "universeScope": "REQUESTED_SYMBOLS_ONLY",
        "instruments": instruments,
        "rows": rows,
        "skipped": skipped,
        "colors": {"bull": "#39ff88", "bear": "#ffaaaa"},
    }
