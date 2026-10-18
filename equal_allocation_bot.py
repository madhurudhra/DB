#!/usr/bin/env python3
"""
Equal-allocation Robinhood controller.
Default: GET-only preview.
Live: --live --acknowledge-risk

Separate journal; never uses the legacy paper cash balance.
New implementation, not a profitability or production-readiness guarantee.
"""
import threading
import queue
import argparse
import fcntl
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import pandas as pd
import requests
from robinhood_client import RobinhoodCryptoClient
from buy_control_store import BuyBlocked, require_buy_enabled, submission_guard

import smtplib
import os
from email.message import EmailMessage
from datetime import datetime, timezone
import atexit

import sys
sys.path.insert(0, '/home/ubuntu/bot-capital')
from capital_guard import crypto_cash, stock_cash

def send_alert(subject: str, body: str) -> bool:
    """Best-effort SMTP submission; never raises."""
    try:
        required = (
            "BOT_SMTP_HOST", "BOT_SMTP_USER", "BOT_SMTP_PASS",
            "BOT_MAIL_FROM", "BOT_MAIL_TO",
        )
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            log("EMAIL SKIPPED", "missing settings", ",".join(missing))
            return False

        msg = EmailMessage()
        msg["Subject"] = f"[CryptoBot] {subject}"
        msg["From"] = os.environ["BOT_MAIL_FROM"]
        msg["To"] = os.environ["BOT_MAIL_TO"]
        msg.set_content(
            f"{body}\n\nUTC: {datetime.now(timezone.utc).isoformat()}\n"
            f"Host: {os.uname().nodename}"
        )
        with smtplib.SMTP(
            os.environ["BOT_SMTP_HOST"],
            int(os.getenv("BOT_SMTP_PORT", "587")),
            timeout=15,
        ) as server:
            server.starttls()
            server.login(
                os.environ["BOT_SMTP_USER"],
                os.environ["BOT_SMTP_PASS"],
            )
            refused = server.send_message(msg)

        if refused:
            log("EMAIL INCOMPLETE", subject, "recipient refusal")
            return False
        log("EMAIL ACCEPTED", subject)
        return True
    except Exception as exc:
        log(
            "EMAIL FAILED", subject, type(exc).__name__,
            "smtp_code", getattr(exc, "smtp_code", "n/a"),
        )
        return False


ROOT = Path(__file__).resolve().parent
PREFIX = "/api/v2/crypto/"
JOURNAL = ROOT / "equal_allocation_v3.db"
TERMINAL = {"filled", "canceled", "failed"}
KNOWN_STATES = TERMINAL | {"open", "pending"}
BUDGET_FRACTION = Decimal("0.98")


class EntryBlocked(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def numeric(value):
    result = Decimal(str(value))
    require(result.is_finite(), "Non-finite numeric value")
    return result


def rounded(quantity, increment):
    require(increment > 0, "Invalid asset increment")
    return (
        quantity / increment
    ).to_integral_value(rounding=ROUND_DOWN) * increment


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def log(*values):
    print(timestamp(), *values, flush=True)



# CG_PACING_V2
_cg_last_request = 0.0

def paced_market_get(url, params=None, timeout=25):
    from email.utils import parsedate_to_datetime
    global _cg_last_request

    require(
        url.startswith("https://api.coingecko.com/api/v3/"),
        "Unexpected market-data URL",
    )

    for attempt in range(3):
        delay = 15 - (time.monotonic() - _cg_last_request)
        if delay > 0:
            time.sleep(delay)

        _cg_last_request = time.monotonic()
        log("COINGECKO REQUEST", "attempt", attempt + 1)
        response = requests.get(url, params=params, timeout=timeout)

        if response.status_code != 429:
            response.raise_for_status()
            return response

        if attempt == 2:
            raise RuntimeError(
                "CoinGecko rate limit persists; scan aborted"
            )

        wait = 60 * (2 ** attempt)
        header = response.headers.get("Retry-After")
        if header:
            try:
                advised = float(header)
            except ValueError:
                try:
                    moment = parsedate_to_datetime(header)
                    if moment.tzinfo is None:
                        moment = moment.replace(tzinfo=timezone.utc)
                    advised = (
                        moment - datetime.now(timezone.utc)
                    ).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    advised = 0
            wait = max(wait, advised)

        require(
            wait <= 300,
            "Provider requests a longer cooldown; stop and wait",
        )
        log("COINGECKO COOLDOWN", "seconds", wait)
        time.sleep(wait)

    raise RuntimeError("Market-data retry limit reached")


class Broker:
    def __init__(self, live):
        self.live = live
        self.client = RobinhoodCryptoClient()
        os.environ["RH_ALLOW_LIVE_ORDERS"] = (
            "true" if live else "false"
        )
        self.number = None
        self.last_request = 0

    def request(self, path, params=None, payload=None):
        require(path.startswith(PREFIX), "Unexpected API path")
        method = "POST" if payload is not None else "GET"

        if method == "POST":
            require(self.live, "POST blocked in preview")
            require(
                path == PREFIX + "trading/orders/",
                "Only new-order POSTs are permitted",
            )

        delay = 0.8 - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()

        body = (
            json.dumps(payload, separators=(",", ":"))
            if payload is not None else ""
        )
        if method == "POST":
            require(isinstance(payload, dict) and payload.get("side") in ("buy", "sell"), "Invalid order side")
            with submission_guard('crypto', payload['side'].upper()):
                result = self.client._request(method, path, body=body, params=params)
        else:
            result = self.client._request(method, path, body=body, params=params)
        require(
            isinstance(result, dict)
            and not result.get("error")
            and not result.get("errors"),
            "API request failed or returned an error",
        )
        return result

    def pages(self, endpoint, params=None):
        path = endpoint
        output = []
        visited = set()

        for _ in range(1000):
            marker = (path, json.dumps(params, sort_keys=True))
            require(marker not in visited, "Pagination loop")
            visited.add(marker)

            response = self.request(path, params=params)
            rows = response.get("results")
            require(
                isinstance(rows, list)
                and all(isinstance(row, dict) for row in rows),
                "Expected paginated results list",
            )
            output.extend(rows)

            following = response.get("next")
            if not following:
                return output

            require(
                isinstance(following, str),
                "Invalid next-page value",
            )
            url = urlsplit(following)
            require(
                url.scheme in ("", "https")
                and url.netloc in ("", "trading.robinhood.com")
                and url.path == endpoint,
                "Unexpected pagination URL",
            )
            path = url.path
            params = parse_qs(url.query, keep_blank_values=True)

            if self.number:
                account = params.get("account_number")
                if account:
                    require(
                        account == [self.number],
                        "Pagination account mismatch",
                    )
        raise RuntimeError("Pagination limit exceeded")

    def account(self):
        accounts = self.pages(PREFIX + "trading/accounts/")

        eligible = [
            account for account in accounts
            if account.get("status") == "active"
            and account.get("is_api_tradable") is True
            and account.get("buying_power_currency") == "USD"
            and account.get("account_number")
        ]

        require(
            len(eligible) == 1,
            "Expected exactly one active API-tradable USD account; "
            f"found {len(eligible)} among {len(accounts)} accounts",
        )
        account = eligible[0]
        number = account["account_number"]

        if self.number:
            require(
                number == self.number,
                "Eligible account changed; account switching blocked",
            )
        self.number = number

        require(
            numeric(account["buying_power"]) >= 0,
            "Negative buying power",
        )
        fee_status = account.get("fee_tier_status")
        require(
            isinstance(fee_status, dict)
            and "fee_ratio" in fee_status,
            "Eligible account is missing its fee ratio",
        )
        require(
            0 <= numeric(fee_status["fee_ratio"]) < 1,
            "Invalid eligible-account fee ratio",
        )
        return account

    def orders(self):
        rows = self.pages(
            PREFIX + "trading/orders/",
            {"account_number": self.number},
        )
        for row in rows:
            require(
                row.get("state") in KNOWN_STATES,
                "Unknown broker order state",
            )
            require(
                row.get("account_number") == self.number,
                "Order account mismatch",
            )
        return rows

    def holdings(self):
        result = {}
        for row in self.pages(
            PREFIX + "trading/holdings/",
            {"account_number": self.number},
        ):
            asset = row["asset_code"]
            require(asset not in result, "Duplicate holding")
            total = numeric(row["total_quantity"])
            available = numeric(row["quantity_available_for_trading"])
            require(
                0 <= available <= total,
                "Invalid holding quantities",
            )
            result[asset] = (total, available)
        return result

    def pairs(self):
        result = {}
        for row in self.pages(PREFIX + "trading/trading_pairs/"):
            if (
                row.get("is_api_tradable") is True
                and row.get("quote_code") == "USD"
            ):
                symbol = row["symbol"]
                require(symbol not in result, "Duplicate trading pair")
                require(
                    symbol == row["asset_code"] + "-USD",
                    "Unexpected pair symbol",
                )
                result[symbol] = row
        return result

    def quote(self, symbol):
        # Raw prices are indications, not fee-inclusive execution totals.
        reason = "No usable quote"

        for attempt in range(3):
            rows = self.request(
                PREFIX + "marketdata/best_bid_ask/",
                {"symbol": symbol},
            ).get("results")

            require(
                isinstance(rows, list)
                and len(rows) == 1
                and rows[0].get("symbol") == symbol,
                "Invalid quote response structure",
            )

            row = rows[0]
            bid = numeric(row["bid"])
            ask = numeric(row["ask"])
            moment = pd.Timestamp(row["timestamp"])
            require(
                moment.tzinfo is not None,
                "Quote timestamp lacks timezone",
            )
            age = (
                pd.Timestamp.now(tz="UTC") - moment
            ).total_seconds()

            if bid <= 0 or ask <= 0:
                reason = "Nonpositive raw quote"
            elif not (-5 <= age <= 30):
                reason = f"Stale/future raw quote: age={age:.2f}s"
            else:
                if bid > ask:
                    log(
                        "RAW CROSSED QUOTE", symbol,
                        "bid", bid, "ask", ask,
                        "quantity-specific totals required for sizing",
                    )
                return bid, ask

            if attempt < 2:
                time.sleep(2)

        raise RuntimeError(f"No usable raw quote for {symbol}: {reason}")

    def estimated_side(self, symbol, quantity, side):
        require(side in ("bid", "ask"), "Invalid estimate side")
        quantity = numeric(quantity)
        require(quantity > 0, "Estimate quantity must be positive")

        rows = self.request(
            PREFIX + "trading/estimated_price/",
            {
                "symbol": symbol,
                "side": side,
                "quantity": str(quantity),
            },
        ).get("results")

        require(
            isinstance(rows, list) and len(rows) == 1,
            "Invalid estimated-price response",
        )
        row = rows[0]
        require(
            row.get("symbol") == symbol
            and row.get("side") == side
            and numeric(row["quantity"]) == quantity,
            "Estimate symbol/side/quantity mismatch",
        )

        moment = pd.Timestamp(row["timestamp"])
        require(
            moment.tzinfo is not None,
            "Estimate timestamp lacks timezone",
        )
        age = (
            pd.Timestamp.now(tz="UTC") - moment
        ).total_seconds()
        require(-5 <= age <= 30, "Stale/future estimate")

        price = numeric(row[side])
        fee = numeric(row["est_fee"])
        ratio = numeric(row["fee_ratio"])
        require(
            price > 0 and fee >= 0 and 0 <= ratio < 1,
            "Invalid estimated price or fee",
        )

        total_key = (
            "est_total_cost" if side == "ask"
            else "est_total_credit"
        )
        total = numeric(row[total_key])
        require(total > 0, "Nonpositive estimated total")

        notional = price * quantity
        expected = (
            notional + fee if side == "ask"
            else notional - fee
        )

        # Allow a cent for provider rounding, not unexplained costs.
        require(
            abs(total - expected) <= numeric("0.01"),
            "Estimated total is inconsistent with price and fee",
        )

        return {
            "price": price,
            "quantity": quantity,
            "fee": fee,
            "fee_ratio": ratio,
            "total": total,
            "timestamp": row["timestamp"],
        }

    def estimate(self, symbol, quantity):
        # Already includes fees. Do not add the fee a second time.
        return self.estimated_side(
            symbol, quantity, "ask"
        )["total"]

    @staticmethod
    def fingerprint(rows):
        return sorted(
            (
                row["id"],
                row["state"],
                str(row.get("filled_asset_quantity")),
                str(row.get("average_price")),
                str(row.get("fee_charged")),
            )
            for row in rows
        )

    def stable_snapshot(self):
        for _ in range(3):
            before = self.orders()
            holdings = self.holdings()
            after = self.orders()
            if self.fingerprint(before) == self.fingerprint(after):
                return after, holdings
            time.sleep(2)
        raise RuntimeError("Account changed during snapshot capture")


class Engine:
    def __init__(self, live):
        self.live = live
        self.broker = Broker(live)
        self.db = None
        self.baseline = {}
        self.cache = {}
        self.last_scan = float("-inf")
        self.email_thread = None
        self.exit_research_queue = queue.Queue()
        self.exit_research_results = {}
        self.exit_research_pending = set()
        self.exit_research_lock = threading.Lock()
        self.exit_research_thread = None
        self.research_io_lock = threading.Lock()
        self.scan_lock = threading.Lock()
        self.scan_thread = None
        self.scan_result = None

    def open_journal(self, history, holdings):
        self.db = sqlite3.connect(JOURNAL)
        os.chmod(JOURNAL, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders(
                cid TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity TEXT NOT NULL,
                candle TEXT NOT NULL,
                distance TEXT NOT NULL,
                state TEXT NOT NULL,
                filled TEXT NOT NULL DEFAULT '0',
                average TEXT NOT NULL DEFAULT '0',
                fee TEXT NOT NULL DEFAULT '0',
                broker_id TEXT,
                response TEXT,
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plans(
                symbol TEXT NOT NULL,
                candle TEXT NOT NULL,
                coin TEXT NOT NULL,
                budget TEXT NOT NULL,
                distance TEXT NOT NULL,
                state TEXT NOT NULL,
                PRIMARY KEY(symbol,candle)
            );
            CREATE TABLE IF NOT EXISTS exit_decisions(
                cid TEXT PRIMARY KEY,
                created TEXT NOT NULL,
                symbol TEXT NOT NULL,
                reason TEXT NOT NULL,
                details TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS capital_reservations(
                cid TEXT PRIMARY KEY,
                amount TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS email_outbox(
                cid TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0
            );
        """)
        metadata = dict(
            self.db.execute("SELECT key,value FROM meta")
        )
        if metadata:
            require(
                metadata.get("version") == "3"
                and metadata.get("account") == self.broker.number,
                "Journal version/account mismatch",
            )
            self.baseline = json.loads(metadata["baseline"])
        else:
            require(
                not any(
                    row["state"] not in TERMINAL for row in history
                ),
                "Cannot capture baseline with open/pending orders",
            )
            require(
                self.db.execute(
                    "SELECT COUNT(*) FROM orders"
                ).fetchone()[0] == 0
                and self.db.execute(
                    "SELECT COUNT(*) FROM plans"
                ).fetchone()[0] == 0,
                "Journal has data but no baseline",
            )
            self.baseline = {
                asset: str(values[0])
                for asset, values in holdings.items()
            }
            with self.db:
                self.db.executemany(
                    "INSERT INTO meta VALUES (?,?)",
                    [
                        ("version", "3"),
                        ("account", self.broker.number),
                        ("baseline", json.dumps(self.baseline)),
                        ("started", timestamp()),
                    ],
                )

    def reconcile(self, history):
        remote = {}
        for order in history:
            cid = order.get("client_order_id")
            if cid:
                require(cid not in remote, "Duplicate client order ID")
                remote[cid] = order

        unknown = []
        for local in list(
            self.db.execute("SELECT * FROM orders ORDER BY rowid")
        ):
            order = remote.get(local["cid"])
            if order is None:
                unknown.append(local["cid"])
                continue

            require(
                order["symbol"] == local["symbol"]
                and order["side"] == local["side"],
                "Broker/local order identity mismatch",
            )
            filled = numeric(order["filled_asset_quantity"])
            average = numeric(order.get("average_price") or 0)
            fee = numeric(order["fee_charged"])
            require(
                numeric(local["filled"]) <= filled
                <= numeric(local["quantity"])
                and fee >= 0
                and (filled == 0 or average > 0),
                "Invalid cumulative fill information",
            )
            with self.db:
                self.db.execute(
                    """UPDATE orders SET state=?,filled=?,average=?,
                       fee=?,broker_id=?,response=? WHERE cid=?""",
                    (
                        order["state"],
                        str(filled),
                        str(average),
                        str(fee),
                        order["id"],
                        json.dumps(order),
                        local["cid"],
                    ),
                )


                if (
                    order["state"] == "filled"
                    and local["state"] != "filled"
                    and filled > 0
                ):
                    subject = (
                        f"{local['side'].upper()} filled {local['symbol']}"
                    )
                    body = (
                        f"Side: {local['side']}\n"
                        f"Symbol: {local['symbol']}\n"
                        f"Filled quantity: {filled}\n"
                        f"Average fill price: {average}\n"
                        f"Broker-reported fee: {fee}\n"
                        f"Broker order ID: {order['id']}\n"
                        f"Client order ID: {local['cid']}\n"
                    )
                    self.db.execute(
                        """INSERT OR IGNORE INTO email_outbox
                           (cid, subject, body) VALUES (?, ?, ?)""",
                        (local["cid"], subject, body),
                    )

        own_ids = {
            row[0] for row in self.db.execute("SELECT cid FROM orders")
        }
        external_open = [
            order for order in history
            if order["state"] not in TERMINAL
            and order.get("client_order_id") not in own_ids
        ]
        return unknown, external_open

    def positions(self):
        result = {}
        for row in self.db.execute(
            "SELECT * FROM orders ORDER BY rowid"
        ):
            quantity = numeric(row["filled"])
            if quantity == 0:
                continue

            position = result.setdefault(
                row["symbol"],
                {
                    "quantity": numeric(0),
                    "cost": numeric(0),
                    "coin": row["coin"],
                    "stop": numeric(0),
                },
            )
            require(
                position["coin"] == row["coin"],
                "Coin identity changed",
            )
            if row["side"] == "buy":
                average = numeric(row["average"])
                fee = numeric(row["fee"])
                require(average > 0 and fee >= 0, "Invalid buy cost")
                position["cost"] += quantity * average + fee
                position["quantity"] += quantity
                position["stop"] = max(
                    position["stop"], numeric(0),
                    numeric(row["average"]) - numeric(row["distance"]),
                )
            else:
                require(
                    quantity <= position["quantity"],
                    "Journal contains an oversell",
                )
                matched_cost = (
                    position["cost"] * quantity / position["quantity"]
                )
                position["cost"] -= matched_cost
                position["quantity"] -= quantity
                if position["quantity"] == 0:
                    position["cost"] = numeric(0)
                    position["stop"] = numeric(0)

        return {
            symbol: position
            for symbol, position in result.items()
            if position["quantity"] > 0
        }

    def ownership_matches(self, holdings, positions):
        expected = {
            asset: numeric(quantity)
            for asset, quantity in self.baseline.items()
        }
        for symbol, position in positions.items():
            asset = symbol.removesuffix("-USD")
            expected[asset] = (
                expected.get(asset, numeric(0))
                + position["quantity"]
            )
        differences = [
            asset
            for asset in set(expected) | set(holdings)
            if holdings.get(asset, (numeric(0), numeric(0)))[0]
            != expected.get(asset, numeric(0))
        ]
        if differences:
            log("OWNERSHIP MISMATCH", differences)
        return not differences

    def signal(self, coin):
        # Allow publication time before adopting a new hourly boundary.
        # Pin the cutoff for this call, including cache lookup and validation.
        request_time = pd.Timestamp.now(tz="UTC")
        hour = (request_time - pd.Timedelta(minutes=5)).floor("h")
        expected_candle = (
            hour - pd.Timedelta(hours=1)
        ).isoformat()
        cached = self.cache.get(coin)
        if (
            cached
            and time.monotonic() - cached[0] < 300
            and cached[1]["candle"] == expected_candle
        ):
            return cached[1]

        response = paced_market_get(
            f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart",
            params={"vs_currency": "usd", "days": "30"},
            timeout=25,
        )
        response.raise_for_status()
        frame = pd.DataFrame(
            response.json()["prices"],
            columns=["time", "price"],
        )
        frame["time"] = pd.to_datetime(
            frame["time"], unit="ms", utc=True
        )
        import math

        # Do not advance the cutoff after the request.
        # A response crossing an hour boundary uses the pinned cutoff above.
        require(
            not frame["time"].duplicated().any(),
            "Duplicate market-data timestamps",
        )

        # Ignore the rolling current-time sample. Use exact UTC boundaries.
        boundary = frame[
            (frame["time"] <= hour)
            & (frame["time"] == frame["time"].dt.floor("h"))
        ].copy()
        boundary["price"] = pd.to_numeric(
            boundary["price"], errors="raise"
        )
        price = (
            boundary.set_index("time")["price"]
            .sort_index()
            .tail(720)
        )

        # Accept the latest available hourly boundary (grace already applied).
        # Allow up to 2 hours behind and tolerate minor gaps.
        last = price.index[-1] if len(price) else None
        require(
            len(price) >= 400
            and not price.isna().any()
            and price.map(math.isfinite).all()
            and (price > 0).all()
            and last is not None
            and (hour - pd.Timedelta(hours=2) <= last <= hour),
            "Missing, invalid, or stale hourly boundary samples",
        )

        # Boundary at 07:00 is the endpoint proxy for the 06:00 hour.
        price.index = price.index - pd.Timedelta(hours=1)

        fast = price.ewm(span=50, adjust=False).mean()
        slow = price.ewm(span=200, adjust=False).mean()
        distance = numeric(price.rolling(14).std().iloc[-1]) * 2
        require(
            0 < distance < numeric(price.iloc[-1]),
            "Invalid stop distance",
        )
        result = {
            "buy": bool(fast.iloc[-1] > slow.iloc[-1]),
            "sell": bool(
                fast.iloc[-1] < slow.iloc[-1]
                and fast.iloc[-2] >= slow.iloc[-2]
            ),
            "candle": price.index[-1].isoformat(),
            "distance": distance,
        }
        self.cache[coin] = (time.monotonic(), result)
        time.sleep(2)
        return result

    @staticmethod
    def maximum_quantity(pair):
        increment = numeric(pair["asset_increment"])
        maximum = numeric(pair["max_order_size"])
        quantity = rounded(maximum, increment)
        # Conservative: stay strictly below the reported maximum.
        if quantity >= maximum:
            quantity -= increment
        require(quantity > 0, "Invalid maximum order quantity")
        return quantity

    def size_buy(self, symbol, budget, pair, account):
        budget = min(numeric(budget), crypto_cash(self.db))
        if budget <= 0:
            return numeric(0), numeric(0)

        _, raw_ask = self.broker.quote(symbol)
        fee_hint = numeric(account["fee_tier_status"]["fee_ratio"])
        require(0 <= fee_hint < 1, "Unexpected account fee ratio")

        increment = numeric(pair["asset_increment"])
        minimum = numeric(pair["min_order_amount"])
        require(minimum >= 0, "Invalid minimum order amount")

        # Raw ask and fee ratio are only an initial sizing hint.
        quantity = rounded(
            min(
                budget / (raw_ask * (1 + fee_hint)),
                self.maximum_quantity(pair),
            ),
            increment,
        )

        for _ in range(6):
            if quantity <= 0:
                return numeric(0), numeric(0)

            estimate = self.broker.estimated_side(
                symbol, quantity, "ask"
            )
            cost = estimate["total"]

            # Check the minimum against quantity-specific pre-fee notional.
            if quantity * estimate["price"] < minimum:
                return numeric(0), numeric(0)

            if cost <= budget:
                log(
                    "BUY SIZE VERIFIED", symbol,
                    "quantity", quantity,
                    "estimated_fee", estimate["fee"],
                    "estimated_total_cost", cost,
                    "allocation", budget,
                )
                return quantity, cost

            reduced = rounded(
                quantity * budget / cost * numeric("0.999"),
                increment,
            )

            # Ensure every over-budget iteration actually reduces quantity.
            if reduced >= quantity:
                reduced = quantity - increment
            quantity = reduced

        raise RuntimeError(
            f"Could not obtain an in-budget estimate for {symbol}"
        )

    def submit(
        self, symbol, coin, side, quantity, candle, distance,
        exit_reason=None, exit_details=None
    ):
        with submission_guard('crypto', side.upper()):
            require(side in ('buy', 'sell'), 'Invalid order side')
            if side == 'buy':
                require_buy_enabled('crypto')
            require(self.live and self.db is not None, "Live journal required")
            require(
                not (side == "buy" and getattr(self, "exits_only", False)),
                "Buy blocked in exits-only mode",
            )
            cid = str(uuid.uuid4())
            capital_cost = None
            if side == "buy":
                estimate = self.broker.estimated_side(symbol, quantity, "ask")
                capital_cost = numeric(estimate["total"])
                require(capital_cost > 0, "Invalid buy capital reservation")
                fresh_account = self.broker.account()
                require(
                    capital_cost <= min(
                        crypto_cash(self.db),
                        numeric(fresh_account["buying_power"]),
                    ),
                    "Buy exceeds crypto-only capital",
                )

            with self.db:
                if capital_cost is not None:
                    plan = self.db.execute(
                        "SELECT state FROM plans WHERE symbol=? AND candle=?", (symbol, candle)
                    ).fetchone()
                    require(plan is not None and plan[0] == "queued", "Buy requires an unsubmitted queued plan")
                    require(not self.db.execute(
                        "SELECT 1 FROM orders WHERE state NOT IN ('filled','canceled','failed') LIMIT 1"
                    ).fetchone(), "Buy blocked by unresolved journal order")
                    require(
                        capital_cost <= crypto_cash(self.db),
                        "Crypto capital changed before reservation",
                    )
                    self.db.execute(
                        "INSERT INTO capital_reservations(cid,amount) VALUES (?,?)",
                        (cid, str(capital_cost)),
                    )
                self.db.execute(
                    """INSERT INTO orders
                       (cid,symbol,coin,side,quantity,candle,distance,
                        state,created)
                       VALUES (?,?,?,?,?,?,?,'uncertain',?)""",
                    (
                        cid, symbol, coin, side, str(quantity),
                        candle, str(distance), timestamp(),
                    ),
                )
                if side == "sell":
                    require(bool(exit_reason), "Sell requires an exit decision reason")
                    self.db.execute(
                        "INSERT INTO exit_decisions(cid,created,symbol,reason,details) VALUES (?,?,?,?,?)",
                        (cid, timestamp(), symbol, exit_reason,
                         json.dumps(exit_details or {}, sort_keys=True)),
                    )
                if side == "buy":
                    self.db.execute(
                        """UPDATE plans SET state='submitted'
                           WHERE symbol=? AND candle=?""",
                        (symbol, candle),
                    )

            if side == "sell":
                log("EXIT_DECISION", symbol, "reason", exit_reason,
                    "client_id", cid, "details", json.dumps(exit_details or {}, sort_keys=True))
            log("SUBMIT", side, symbol, quantity, "client_id", cid)
            response = self.broker.request(
                PREFIX + "trading/orders/",
                params={"account_number": self.broker.number},
                payload={
                    "client_order_id": cid,
                    "side": side,
                    "type": "market",
                    "symbol": symbol,
                    "market_order_config": {
                        "asset_quantity": str(quantity)
                    },
                },
            )
            require(
                response.get("client_order_id") == cid
                and response.get("id"),
                "Unexpected submission response; intention remains uncertain",
            )
            with self.db:
                self.db.execute(
                    """UPDATE orders SET broker_id=?,response=?
                       WHERE cid=?""",
                    (response["id"], json.dumps(response), cid),
                )

    def exit_research_worker(self):
        while True:
            coin = self.exit_research_queue.get()
            try:
                with self.research_io_lock:
                    result = self.signal(coin)
                error = None
            except Exception as exc:
                result = None
                error = type(exc).__name__ + ": " + str(exc)
            with self.exit_research_lock:
                self.exit_research_results[coin] = (time.monotonic(), result, error)
                self.exit_research_pending.discard(coin)
            self.exit_research_queue.task_done()

    def exit_signal(self, coin):
        with self.exit_research_lock:
            cached = self.exit_research_results.get(coin)
            age = time.monotonic() - cached[0] if cached else float("inf")
            if age >= 60 and coin not in self.exit_research_pending:
                self.exit_research_pending.add(coin)
                self.exit_research_queue.put(coin)
            if self.exit_research_thread is None or not self.exit_research_thread.is_alive():
                self.exit_research_thread = threading.Thread(
                    target=self.exit_research_worker, name="exit-research", daemon=True
                )
                self.exit_research_thread.start()
            if cached and cached[1] is not None and age <= 300:
                return cached[1]
            detail = cached[2] if cached and cached[2] else "Research pending or stale"
        raise RuntimeError(detail)

    def monitor_exits(
        self, positions, holdings, history, pairs, writable
    ):
        busy = {
            order["symbol"] for order in history
            if order["state"] not in TERMINAL
        }
        monitoring_error = False

        for symbol, position in positions.items():
            try:
                bid, _ = self.broker.quote(symbol)
                signal = {"sell": False, "candle": timestamp()}
                # Check price protection and net return before research.
                due = bid <= position["stop"]
                exit_reason = (
                    "stop_loss" if bid <= position["stop"]
                    else "sell_signal" if signal["sell"]
                    else None
                )
                net_return = None

                # Existing protective exits do not require a profit estimate.
                if not due and writable and symbol not in busy:
                    pair = pairs.get(symbol)
                    require(pair is not None, "Held pair is not API tradable")
                    sell_quantity = rounded(
                        min(
                            position["quantity"],
                            self.maximum_quantity(pair),
                        ),
                        numeric(pair["asset_increment"]),
                    )
                    require(sell_quantity > 0, "No sellable quantity")

                    # Allocate entry cost proportionally if only part can sell.
                    entry_cost = (
                        position["cost"]
                        * sell_quantity / position["quantity"]
                    )
                    require(entry_cost > 0, "Invalid fee-inclusive entry cost")

                    estimate = self.broker.estimated_side(
                        symbol, sell_quantity, "bid"
                    )
                    net_credit = estimate["total"]
                    net_return = (net_credit - entry_cost) / entry_cost
                    profit_due = net_credit >= entry_cost * numeric("1.06")
                    due = profit_due
                    if profit_due:
                        exit_reason = "take_profit_6pct_net"

                    log(
                        "NET PROFIT CHECK", symbol,
                        "quantity", sell_quantity,
                        "entry_cost_including_fees", entry_cost,
                        "estimated_net_sale_credit", net_credit,
                        "estimated_exit_fee", estimate["fee"],
                        "net_return", net_return,
                        "target_net_return", numeric("0.06"),
                    )
                if not due:
                    try:
                        signal = self.exit_signal(position["coin"])
                        if signal["sell"]:
                            due = True
                            exit_reason = "sell_signal"
                    except Exception as exc:
                        log("EXIT RESEARCH UNAVAILABLE", symbol,
                            type(exc).__name__)
                        monitoring_error = True
                log(
                    "POSITION", symbol,
                    "quantity", position["quantity"],
                    "bid", bid, "stop", position["stop"],
                    "exit_due", due,
                    "exit_reason", exit_reason,
                    "net_return", net_return,
                )
                if not due or not writable or symbol in busy:
                    continue

                pair = pairs.get(symbol)
                require(pair is not None, "Held pair is not API tradable")
                increment = numeric(pair["asset_increment"])
                quantity = rounded(
                    min(
                        position["quantity"],
                        self.maximum_quantity(pair),
                    ),
                    increment,
                )
                require(
                    quantity > 0
                    and quantity * bid
                    >= numeric(pair["min_order_amount"]),
                    "Remaining position is below sell constraints",
                )
                available = holdings.get(
                    pair["asset_code"], (numeric(0), numeric(0))
                )[1]
                require(
                    available >= quantity,
                    "Insufficient available bot-owned quantity",
                )

                self.submit(
                    symbol, position["coin"], "sell",
                    quantity, signal["candle"], numeric(0),
                    exit_reason=exit_reason,
                    exit_details={"bid": str(bid), "price_stop": str(position["stop"]),
                                  "net_return": str(net_return), "quantity": str(quantity)},
                )
                return True

            except Exception as exc:
                log(
                    "EXIT MONITOR ERROR", symbol,
                    type(exc).__name__, str(exc),
                )
                monitoring_error = True

        return monitoring_error

    @staticmethod
    def signal_is_current(signal):
        try:
            candle = datetime.fromisoformat(signal["candle"])
            require(candle.tzinfo is not None, "Signal candle lacks timezone")
            boundary = ((time.time() - 300) // 3600) * 3600 - 3600
            return boundary - 7200 <= candle.timestamp() <= boundary
        except (ValueError, TypeError, KeyError, RuntimeError):
            return False

    def scan_worker(self, pairs):
        candidates = []
        error = None
        try:
            with self.research_io_lock:
                response = paced_market_get(
                    "https://api.coingecko.com/api/v3/coins/markets",
                    params={"vs_currency": "usd", "order": "market_cap_desc",
                            "per_page": 20, "page": 1}, timeout=25,
                )
                response.raise_for_status()
                coins = response.json()
            require(isinstance(coins, list), "Invalid market response")
            seen = set()
            for coin in coins:
                symbol = coin["symbol"].upper() + "-USD"
                require(symbol not in seen, "Ambiguous market symbol")
                seen.add(symbol)
                if symbol not in pairs:
                    continue
                if (numeric(coin.get("market_cap") or 0) < numeric(5000000000)
                        or numeric(coin.get("total_volume") or 0) < numeric(100000000)):
                    continue
                try:
                    with self.research_io_lock:
                        signal = self.signal(coin["id"])
                    observed = time.monotonic()
                    with self.exit_research_lock:
                        self.exit_research_results[coin["id"]] = (observed, signal, None)
                    if signal["buy"] and self.signal_is_current(signal):
                        candidates.append((symbol, coin["id"], signal, observed))
                except Exception as exc:
                    log("SIGNAL FAILED", symbol, type(exc).__name__)
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)
        with self.scan_lock:
            self.scan_result = (candidates, error)

    def scan(self, holdings, positions, pairs, account):
        with self.scan_lock:
            completed = self.scan_result
            self.scan_result = None
            running = self.scan_thread is not None and self.scan_thread.is_alive()
        if completed is None:
            if running or time.monotonic() - self.last_scan < 300:
                return
            self.last_scan = time.monotonic()
            if self.live:
                self.scan_thread = threading.Thread(
                    target=self.scan_worker, args=(dict(pairs),),
                    name="entry-research", daemon=True,
                )
                self.scan_thread.start()
                log("ENTRY RESEARCH", "background scan started; broker and journal stay on main thread")
                return
            self.scan_worker(dict(pairs))
            with self.scan_lock:
                completed = self.scan_result
                self.scan_result = None
        candidates, error = completed
        if error:
            log("ENTRY RESEARCH ERROR", error, "no batch created")
            return
        eligible = []
        for symbol, coin, signal, observed in candidates:
            if symbol not in pairs or time.monotonic() - observed > 300:
                continue
            if not self.signal_is_current(signal):
                continue
            if symbol in positions and positions[symbol]["coin"] != coin:
                log("ENTRY RESEARCH ERROR", symbol, "coin identity mismatch")
                continue
            if self.db is not None and self.db.execute(
                "SELECT 1 FROM plans WHERE symbol=? AND candle=?",
                (symbol, signal["candle"]),
            ).fetchone():
                continue
            eligible.append((symbol, coin, signal))
        budget = min(numeric(account["buying_power"]), crypto_cash(self.db)) * BUDGET_FRACTION
        log("SCAN", "qualifying", [x[0] for x in eligible], "batch_budget", budget)
        if not eligible or budget <= 0:
            return
        allocation = budget / len(eligible)
        if self.live:
            with self.db:
                for symbol, coin, signal in eligible:
                    self.db.execute(
                        "INSERT INTO plans VALUES (?,?,?,?,?,'queued')",
                        (symbol, signal["candle"], coin, str(allocation), str(signal["distance"])),
                    )
                    log("ALLOCATION", symbol, allocation)
        else:
            for symbol, coin, signal in eligible:
                quantity, cost = self.size_buy(symbol, allocation, pairs[symbol], account)
                log("PREVIEW PLAN", symbol, "budget", allocation,
                    "quantity", quantity, "estimated_cost", cost)

    def execute_queue(self, holdings, positions, pairs, account):
        plans = list(self.db.execute(
            "SELECT * FROM plans WHERE state='queued' ORDER BY rowid"
        ))

        for plan in plans:
            symbol = plan["symbol"]
            pair = pairs.get(symbol)
            try:
                signal = self.exit_signal(plan["coin"])
            except RuntimeError:
                log("WAIT", "entry research pending or stale", symbol)
                return
            require(
                symbol not in positions or positions[symbol]["coin"] == plan["coin"],
                "Entry coin identity mismatch",
            )

            skip = (
                pair is None
                or not self.signal_is_current(signal)
                or signal["candle"] != plan["candle"]
                or not signal["buy"]
            )
            # Both baseline and bot-managed holdings may receive eligible additions.
            # Stale signals and duplicate candle plans still block entries.

            if skip:
                with self.db:
                    self.db.execute(
                        """UPDATE plans SET state='skipped'
                           WHERE symbol=? AND candle=?""",
                        (symbol, plan["candle"]),
                    )
                continue

            budget = numeric(plan["budget"])
            if budget > crypto_cash(self.db):
                log("WAIT", "crypto-only capital below allocation", symbol)
                return
            if numeric(account["buying_power"]) < budget:
                log("WAIT", "buying power below equal allocation", symbol)
                return

            quantity, cost = self.size_buy(
                symbol, budget, pair, account
            )
            if quantity <= 0:
                with self.db:
                    self.db.execute(
                        """UPDATE plans SET state='skipped'
                           WHERE symbol=? AND candle=?""",
                        (symbol, plan["candle"]),
                    )
                log("SKIP CONSTRAINTS", symbol)
                continue

            fresh = self.broker.account()
            require(
                min(numeric(fresh["buying_power"]), crypto_cash(self.db)) >= cost,
                "Buying power changed before submission",
            )
            try:
                self.submit(
                    symbol, plan["coin"], "buy", quantity,
                    plan["candle"], numeric(plan["distance"]),
                )
            except EntryBlocked as exc:
                with self.db:
                    self.db.execute("UPDATE plans SET state='skipped' WHERE symbol=? AND candle=?",
                                    (symbol, plan["candle"]))
                log("SKIP ENTRY FRICTION", symbol, str(exc))
                continue
            return

    def start_email_worker(self):
        if self.email_thread is not None and self.email_thread.is_alive():
            return
        self.email_thread = threading.Thread(
            target=self.deliver_pending_emails,
            name="crypto-email",
            daemon=True,
        )
        self.email_thread.start()

    def deliver_pending_emails(self):
        db = None
        try:
            db = sqlite3.connect(JOURNAL, timeout=1)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            pending = list(db.execute(
                """SELECT cid, subject, body FROM email_outbox
                   WHERE sent=0 ORDER BY rowid LIMIT 10"""
            ))
            for row in pending:
                if not send_alert(row["subject"], row["body"]):
                    break
                with db:
                    db.execute(
                        "UPDATE email_outbox SET sent=1 WHERE cid=?",
                        (row["cid"],),
                    )
        except Exception as exc:
            log("EMAIL WORKER FAILED", type(exc).__name__)
        finally:
            if db is not None:
                db.close()

    def cycle(self):
        account = self.broker.account()
        history, holdings = self.broker.stable_snapshot()
        pairs = self.broker.pairs()

        if not self.live:
            require(
                not any(
                    row["state"] not in TERMINAL for row in history
                ),
                "Preview blocked: account has open/pending orders",
            )
            self.scan(holdings, {}, pairs, account)
            return

        if self.db is None:
            self.open_journal(history, holdings)

        unknown, external = self.reconcile(history)
        self.start_email_worker()
        positions = self.positions()
        writable = (
            not unknown
            and not external
            and self.ownership_matches(holdings, positions)
        )

        if not writable:
            log(
                "SUBMISSIONS BLOCKED",
                "unknown orders", len(unknown),
                "external open orders", len(external),
                "check journal and holdings",
            )

        # Monitor exits even while known buy orders remain pending.
        if self.monitor_exits(
            positions, holdings, history, pairs, writable
        ):
            return

        if not writable:
            return

        if getattr(self, "exits_only", False):
            log("EXITS ONLY", "entry queue and new buys blocked")
            return

        if any(row["state"] not in TERMINAL for row in history):
            log("WAIT", "account order pending; no new buys")
            return

        if self.db.execute(
            "SELECT 1 FROM plans WHERE state='queued'"
        ).fetchone():
            try:
                self.execute_queue(holdings, positions, pairs, self.broker.account())
            except BuyBlocked:
                log("BUY BLOCKED", "OFF or control unavailable; exit monitoring continues")
            return

        self.scan(holdings, positions, pairs, account)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--acknowledge-risk", action="store_true")
    parser.add_argument("--exits-only", action="store_true")
    args = parser.parse_args()

    require(
        not args.live or args.acknowledge_risk,
        "Actual orders require --live --acknowledge-risk",
    )

    os.chdir(ROOT)
    lock_path = ROOT / "equal_allocation_v3.lock"

    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(
                lock, fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except BlockingIOError:
            raise SystemExit("Another controller owns the process lock")

        engine = Engine(args.live)
        engine.exits_only = args.exits_only
        if args.exits_only:
            log("RECOVERY MODE", "EXITS ONLY; new buys blocked")
        log("MODE", "LIVE" if args.live else "READ-ONLY PREVIEW")

        while True:
            try:
                engine.cycle()
            except KeyboardInterrupt:
                log(
                    "STOPPED",
                    "Shutdown does not cancel orders or sell holdings",
                )
                send_alert(
                    "Controller stopped",
                    "Controller interrupted; live exit monitoring stopped.\n"
                    "Holdings were not automatically sold.",
                )
                return
            except Exception as exc:
                log(
                    "HALTED", type(exc).__name__, str(exc),
                    "No automatic restart or order resubmission",
                )
                import traceback
                traceback.print_exc()
                send_alert(
                    "CRITICAL: controller halted",
                    "Live exit monitoring has stopped.\n"
                    f"Failure type: {type(exc).__name__}\n"
                    "Check broker holdings and pending orders.\n"
                    "Inspect the terminal traceback. No automatic restart.",
                )
                raise SystemExit(1)

            if not args.live:
                log(
                    "PREVIEW COMPLETE",
                    "No orders submitted; no trading journal created",
                )
                return

            time.sleep(30)


if __name__ == "__main__":
    main()

# PRICING_FIX_V1

# HOURLY_BOUNDARY_FIX_V1

# HALT_TRACEBACK_FIX_V1

# ALLOW_BASELINE_HOLDINGS_LIVE_V1

# HOURLY_BOUNDARY_GRACE_V1

# ALLFIXES_ALERTS_V1
def _on_exit():
    send_alert("Bot stopped", "Process exiting.")
atexit.register(_on_exit)

# NET_LOSS_1PCT_V1

# RECOVERY_EXITS_ONLY_V1
