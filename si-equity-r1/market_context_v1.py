import argparse
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import entry_signals_v1 as technical

VERSION = "equity-context-proposal-v1"
NY = ZoneInfo("America/New_York")
BENCHMARKS = ("SPY", "QQQ", "IWM")
MAX_CONTEXT_AGE = 1200

# PROPOSED definitions where the master did not specify exact conditions:
# RISK_ON: at least 2/3 benchmark prices above their completed-session
#          50-day averages AND VIX below 25. Otherwise RISK_OFF.
# Recovery: RSI crosses above 30 and the latest completed close rises.
# Strong adverse decline: 5-session return <= -5%, OR 20-session <= -10%.
#
# Market context may be up to 20 minutes old. It is NOT an executable quote.
# Daily performance uses completed sessions, excluding today's daily bar.
# News/earnings are unknown until a verified adapter supplies them.

def daily_data(symbol):
    end = int(time.time())
    query = urlencode({
        "period1": end - 90*24*60*60,
        "period2": end,
        "interval": "1d",
        "events": "splits",
    })
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + quote(symbol, safe="") + "?" + query
    )
    request = Request(url, headers={
        "User-Agent": "SI-Equity-Context/1.0",
        "Accept": "application/json",
    })
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)

    chart = payload.get("chart")
    if not isinstance(chart, dict) or chart.get("error"):
        raise technical.DataRejected("DAILY_PROVIDER_ERROR")
    results = chart.get("result")
    if not isinstance(results, list) or len(results) != 1:
        raise technical.DataRejected("DAILY_RESULT_NOT_UNIQUE")
    result = results[0]
    meta = result.get("meta", {})
    if str(meta.get("symbol", "")).upper() != symbol:
        raise technical.DataRejected("DAILY_SYMBOL_MISMATCH")

    # Do not interpret unadjusted price jumps across reported splits as returns.
    if result.get("events", {}).get("splits"):
        raise technical.DataRejected("SPLIT_REQUIRES_ADJUSTMENT_REVIEW")

    timestamps = result.get("timestamp")
    quote_rows = result.get("indicators", {}).get("quote")
    if (
        not isinstance(timestamps, list)
        or not isinstance(quote_rows, list)
        or len(quote_rows) != 1
    ):
        raise technical.DataRejected("DAILY_HISTORY_MISSING")
    closes = quote_rows[0].get("close")
    if not isinstance(closes, list) or len(closes) != len(timestamps):
        raise technical.DataRejected("DAILY_ARRAY_MISMATCH")

    today = datetime.now(NY).date()
    history = []
    seen = set()
    for raw_time, raw_close in zip(timestamps, closes):
        stamp = technical.numeric(raw_time, "DAILY_TIMESTAMP")
        date = datetime.fromtimestamp(stamp, NY).date()
        if date >= today:
            continue
        if date in seen:
            raise technical.DataRejected("DUPLICATE_DAILY_SESSION")
        seen.add(date)
        close = technical.numeric(raw_close, "DAILY_CLOSE")
        if close <= 0:
            raise technical.DataRejected("NONPOSITIVE_DAILY_CLOSE")
        history.append((date, close))
    history.sort()
    if len(history) < 21:
        raise technical.DataRejected("INSUFFICIENT_20_SESSION_HISTORY")
    if (today - history[-1][0]).days > 4:
        raise technical.DataRejected("STALE_DAILY_HISTORY")

    current = technical.numeric(
        meta.get("regularMarketPrice"), "CONTEXT_MARKET_PRICE"
    )
    current_time = technical.numeric(
        meta.get("regularMarketTime"), "CONTEXT_MARKET_TIME"
    )
    age = end - current_time
    if current <= 0 or not -5 <= age <= MAX_CONTEXT_AGE:
        raise technical.DataRejected("STALE_OR_INVALID_CONTEXT_PRICE")

    values = [item[1] for item in history]
    return {
        "symbol": symbol,
        "current_context_price": current,
        "context_timestamp": datetime.fromtimestamp(
            current_time, timezone.utc
        ).isoformat(),
        "context_age_seconds": age,
        "completed_sessions": len(values),
        "last_completed_session": history[-1][0].isoformat(),
        "return_5_sessions_pct": (values[-1]/values[-6]-1)*100,
        "return_20_sessions_pct": (values[-1]/values[-21]-1)*100,
        "ma50_completed_sessions": (
            sum(values[-50:])/50 if len(values) >= 50 else None
        ),
    }

def market_regime():
    observations = {}
    for index, symbol in enumerate((*BENCHMARKS, "^VIX")):
        if index:
            time.sleep(2)
        observations[symbol] = daily_data(symbol)

    if any(
        observations[symbol]["ma50_completed_sessions"] is None
        for symbol in BENCHMARKS
    ):
        raise technical.DataRejected("INSUFFICIENT_BENCHMARK_HISTORY")

    above = sum(
        observations[symbol]["current_context_price"]
        > observations[symbol]["ma50_completed_sessions"]
        for symbol in BENCHMARKS
    )
    vix = observations["^VIX"]["current_context_price"]
    regime = "RISK_ON" if above >= 2 and vix < 25 else "RISK_OFF"
    return {
        "rule_version": VERSION,
        "regime": regime,
        "regime_contribution": 1 if regime == "RISK_ON" else -1,
        "benchmarks_above_ma50": above,
        "vix_context_value": vix,
        "max_context_age_seconds": MAX_CONTEXT_AGE,
        "observations": observations,
    }

def candidate_context(symbol, market):
    scan = technical.analyze(symbol)
    time.sleep(2)
    performance = daily_data(symbol)

    recovery = (
        scan["previous_rsi14"] <= 30 < scan["rsi14"]
        and scan["price"] > scan["previous_close"]
    )
    adverse = (
        performance["return_5_sessions_pct"] <= -5
        or performance["return_20_sessions_pct"] <= -10
    )
    contributions = {
        "market_regime": market["regime_contribution"],
        "five_session_return_above_2_pct": (
            1 if performance["return_5_sessions_pct"] > 2 else 0
        ),
        "twenty_session_return_above_5_pct": (
            1 if performance["return_20_sessions_pct"] > 5 else 0
        ),
        "recovery": 1 if recovery else 0,
        "volume_ratio_at_least_1_5": 1 if scan["volume_ratio"] >= 1.5 else 0,
        "strong_adverse_decline": -2 if adverse else 0,
        "news_sentiment": None,
        "near_earnings": None,
    }
    return {
        "symbol": symbol,
        "strategy_version": VERSION,
        "state": "PARTIAL_INTELLIGENCE_READY",
        "technical": scan,
        "market_regime": market["regime"],
        "performance": performance,
        "intelligence_contributions": contributions,
        "known_contribution_subtotal": sum(
            value for value in contributions.values() if value is not None
        ),
        "intelligence_score": None,
        "hybrid_score": None,
        "classification": "AWAITING_NEWS_EARNINGS_AND_RISK_AUTHORIZATION",
        "entry_authorized": False,
        "orders_submitted": False,
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="+")
    args = parser.parse_args()
    symbols = list(dict.fromkeys(value.upper() for value in args.symbols))
    if any(not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", value) for value in symbols):
        raise SystemExit("Invalid ticker format.")

    try:
        market = market_regime()
        print(json.dumps({"market_context": market}, indent=2, allow_nan=False))
    except Exception as error:
        print(json.dumps({
            "state": "MARKET_CONTEXT_UNAVAILABLE",
            "error_type": type(error).__name__,
            "reason": (
                str(error) if isinstance(error, technical.DataRejected)
                else "PROVIDER_OR_PARSING_FAILURE"
            ),
            "entry_authorized": False,
            "orders_submitted": False,
        }, indent=2))
        return

    for symbol in symbols:
        time.sleep(2)
        try:
            result = candidate_context(symbol, market)
        except Exception as error:
            result = {
                "symbol": symbol,
                "state": "CONTEXT_REJECTED",
                "error_type": type(error).__name__,
                "reason": (
                    str(error) if isinstance(error, technical.DataRejected)
                    else "PROVIDER_OR_PARSING_FAILURE"
                ),
                "entry_authorized": False,
                "orders_submitted": False,
            }
        print(json.dumps(result, indent=2, allow_nan=False))

if __name__ == "__main__":
    main()
