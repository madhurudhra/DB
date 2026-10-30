import argparse
import json
import math
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

VERSION = "equity-technical-proposal-v1"
NY = ZoneInfo("America/New_York")

# New proposed technical score, maximum 10:
# MA9 > MA21:             +2
# MA21 > MA50:            +2
# Close > MA200:          +1
# Close > session VWAP:   +1
# MACD histogram > 0:     +1
# RSI14 between 30–65:    +1
# Volume ratio >= 1.5:    +1
# Five-bar momentum > 0:  +1
#
# This is NOT an order-authorization formula.
# No missing intelligence score is replaced with zero.

class DataRejected(RuntimeError):
    pass

def numeric(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataRejected("INVALID_" + name)
    result = float(value)
    if not math.isfinite(result):
        raise DataRejected("NONFINITE_" + name)
    return result

def ema(values, period):
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1-alpha) * result[-1])
    return result

def rsi(values, period=14):
    differences = [b-a for a, b in zip(values, values[1:])]
    gain = sum(max(value, 0) for value in differences[:period]) / period
    loss = sum(max(-value, 0) for value in differences[:period]) / period
    for value in differences[period:]:
        gain = (gain*(period-1) + max(value, 0)) / period
        loss = (loss*(period-1) + max(-value, 0)) / period
    if loss == 0:
        return 100 if gain > 0 else 50
    return 100 - 100/(1 + gain/loss)

def wilder_average(values, period=14):
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = (result*(period-1) + value) / period
    return result

def download(symbol):
    end = int(time.time())
    params = urlencode({
        "period1": end - 10*24*60*60,
        "period2": end,
        "interval": "5m",
        "includePrePost": "false",
    })
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + quote(symbol, safe="") + "?" + params
    )
    request = Request(url, headers={
        "User-Agent": "SI-Equity-Technical-Scanner/1.0",
        "Accept": "application/json",
    })
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)

    chart = payload.get("chart")
    if not isinstance(chart, dict) or chart.get("error"):
        raise DataRejected("PROVIDER_ERROR")
    results = chart.get("result")
    if not isinstance(results, list) or len(results) != 1:
        raise DataRejected("PROVIDER_RESULT_NOT_UNIQUE")

    result = results[0]
    meta = result.get("meta", {})
    if str(meta.get("symbol", "")).upper() != symbol:
        raise DataRejected("PROVIDER_SYMBOL_MISMATCH")
    if meta.get("currency") != "USD":
        raise DataRejected("NON_USD_INSTRUMENT")

    timestamps = result.get("timestamp")
    quotes = result.get("indicators", {}).get("quote")
    if (
        not isinstance(timestamps, list)
        or not isinstance(quotes, list)
        or len(quotes) != 1
    ):
        raise DataRejected("MISSING_CANDLES")

    data = quotes[0]
    names = ("open", "high", "low", "close", "volume")
    if any(
        not isinstance(data.get(name), list)
        or len(data[name]) != len(timestamps)
        for name in names
    ):
        raise DataRejected("CANDLE_ARRAY_MISMATCH")

    bars = []
    seen = set()
    for index, timestamp in enumerate(timestamps):
        stamp = numeric(timestamp, "TIMESTAMP")
        if stamp in seen:
            raise DataRejected("DUPLICATE_TIMESTAMP")
        seen.add(stamp)

        # Use completed candles only. Never fill a missing candle.
        if stamp + 300 > end:
            continue
        bar = {"timestamp": stamp}
        for name in names:
            bar[name] = numeric(data[name][index], name.upper())

        if (
            min(bar[name] for name in ("open", "high", "low", "close")) <= 0
            or bar["volume"] < 0
            or bar["low"] > min(bar["open"], bar["close"])
            or bar["high"] < max(bar["open"], bar["close"])
            or bar["low"] > bar["high"]
        ):
            raise DataRejected("INVALID_OHLCV")
        bars.append(bar)

    bars.sort(key=lambda item: item["timestamp"])
    if len(bars) < 220:
        raise DataRejected("INSUFFICIENT_HISTORY")

    # Proposed technical-data freshness ceiling: 10 minutes after candle end.
    age = end - (bars[-1]["timestamp"] + 300)
    if not 0 <= age <= 600:
        raise DataRejected("STALE_COMPLETED_CANDLE")
    return bars, meta, age

def analyze(symbol):
    bars, meta, age = download(symbol)
    closes = [bar["close"] for bar in bars]
    price = closes[-1]
    averages = {
        period: sum(closes[-period:]) / period
        for period in (9, 21, 50, 100, 200)
    }

    today = datetime.now(NY).date()
    session = [
        bar for bar in bars
        if datetime.fromtimestamp(bar["timestamp"], NY).date() == today
    ]
    total_volume = sum(bar["volume"] for bar in session)
    if total_volume <= 0:
        raise DataRejected("SESSION_VWAP_UNAVAILABLE")
    vwap = sum(
        ((bar["high"] + bar["low"] + bar["close"]) / 3) * bar["volume"]
        for bar in session
    ) / total_volume

    preceding_volume = sum(bar["volume"] for bar in bars[-21:-1]) / 20
    if preceding_volume <= 0:
        raise DataRejected("VOLUME_REFERENCE_UNAVAILABLE")
    volume_ratio = bars[-1]["volume"] / preceding_volume

    daily_volume = defaultdict(float)
    for bar in bars:
        date = datetime.fromtimestamp(bar["timestamp"], NY).date()
        daily_volume[date] += bar["volume"]
    completed_days = sorted(date for date in daily_volume if date < today)
    # Exclude the first day: the 10-day request can begin mid-session.
    completed_days = completed_days[1:]
    if len(completed_days) < 5:
        raise DataRejected("INSUFFICIENT_COMPLETED_VOLUME_SESSIONS")
    average_volume = sum(daily_volume[date] for date in completed_days) / len(completed_days)

    fast, slow = ema(closes, 12), ema(closes, 26)
    macd = [a-b for a, b in zip(fast, slow)]
    histogram = macd[-1] - ema(macd, 9)[-1]
    rsi14 = rsi(closes)

    ranges = [
        max(
            bars[index]["high"] - bars[index]["low"],
            abs(bars[index]["high"] - bars[index-1]["close"]),
            abs(bars[index]["low"] - bars[index-1]["close"]),
        )
        for index in range(1, len(bars))
    ]
    atr14 = wilder_average(ranges)
    momentum = {
        period: (price / closes[-period-1] - 1) * 100
        for period in (5, 15, 30)
    }

    factors = {
        "ma9_above_ma21": averages[9] > averages[21],
        "ma21_above_ma50": averages[21] > averages[50],
        "price_above_ma200": price > averages[200],
        "price_above_vwap": price > vwap,
        "macd_histogram_positive": histogram > 0,
        "rsi_30_to_65": 30 <= rsi14 <= 65,
        "volume_ratio_at_least_1_5": volume_ratio >= 1.5,
        "momentum_5_positive": momentum[5] > 0,
    }
    weights = {
        "ma9_above_ma21": 2,
        "ma21_above_ma50": 2,
        "price_above_ma200": 1,
        "price_above_vwap": 1,
        "macd_histogram_positive": 1,
        "rsi_30_to_65": 1,
        "volume_ratio_at_least_1_5": 1,
        "momentum_5_positive": 1,
    }
    score = sum(weights[key] for key, passed in factors.items() if passed)

    return {
        "symbol": symbol,
        "strategy_version": VERSION,
        "state": "TECHNICAL_DATA_READY",
        "source": "YAHOO_CHART",
        "instrument_type": meta.get("instrumentType"),
        "candle_interval": "5m",
        "completed_candle_start": datetime.fromtimestamp(
            bars[-1]["timestamp"], timezone.utc
        ).isoformat(),
        "candle_end_age_seconds": age,
        "sample_count": len(bars),
        "price": price,
        "moving_averages": averages,
        "vwap": vwap,
        "rsi14": rsi14,
        "previous_rsi14": rsi(closes[:-1]),
        "previous_close": closes[-2],
        "atr14": atr14,
        "atr_pct": atr14/price*100,
        "macd_histogram": histogram,
        "volume_ratio": volume_ratio,
        "average_daily_volume": average_volume,
        "momentum_pct": momentum,
        "technical_score": score,
        "technical_score_max": 10,
        "factors": factors,
        "technical_rally_proposal": "STRONG" if score >= 6 else "WEAK",
        "price_and_volume_filters_pass": price >= 0.5 and average_volume >= 100000,
        "intelligence_score": None,
        "hybrid_score": None,
        "classification": "AWAITING_INTELLIGENCE_AND_RISK_CHECKS",
        "entry_authorized": False,
        "orders_submitted": False,
        "missing_gates": [
            "Verified news and earnings risk",
            "Market-regime intelligence",
            "Instrument eligibility including leveraged/inverse exclusion",
            "Live capital and reservation authorization",
            "Per-ticker daily-loss authorization",
            "Broker pre-trade review and execution integration",
        ],
    }

def self_test():
    assert rsi(list(range(1, 231))) == 100
    assert rsi(list(range(230, 0, -1))) == 0
    assert rsi([100]*230) == 50
    assert len(ema(list(range(1, 231)), 50)) == 230
    assert ema([100]*230, 50)[-1] == 100
    print(json.dumps({"unit_checks_passed": 5, "broker_calls": 0}))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if not args.symbols:
        parser.error("Provide explicit ticker symbols.")
    symbols = list(dict.fromkeys(symbol.upper() for symbol in args.symbols))
    for symbol in symbols:
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
            raise SystemExit("Invalid ticker format.")
    for index, symbol in enumerate(symbols):
        if index:
            time.sleep(2)
        try:
            result = analyze(symbol)
        except Exception as error:
            result = {
                "symbol": symbol,
                "strategy_version": VERSION,
                "state": "DATA_REJECTED",
                "error_type": type(error).__name__,
                "reason": str(error) if isinstance(error, DataRejected) else "PROVIDER_OR_PARSING_FAILURE",
                "entry_authorized": False,
                "orders_submitted": False,
            }
        print(json.dumps(result, indent=2, allow_nan=False))

if __name__ == "__main__":
    main()
