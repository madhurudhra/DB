
def load_secure_env():
    try:
        from cryptography.fernet import Fernet
        import os
        enc_path = '/opt/stock-bot/.env.enc'
        key_path = '/opt/stock-bot/.secret.key'
        if os.path.exists(enc_path) and os.path.exists(key_path):
            with open(key_path, 'rb') as kf: key = kf.read().strip()
            with open(enc_path, 'rb') as ef: enc_data = ef.read()
            cipher = Fernet(key)
            decrypted = cipher.decrypt(enc_data).decode('utf-8')
            for line in decrypted.splitlines():
                if line.strip() and not line.startswith('#') and '=' in line:
                    k, v = line.strip().split('=', 1)
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")
            return True
    except Exception:
        pass
    return False
load_secure_env()

import json
import sys
sys.path.insert(0, "/opt/market_intelligence_engine")
from market_intelligence import MarketIntelligenceEngine
intel_engine = MarketIntelligenceEngine(market_type="us_stocks")
import os
from datetime import datetime
from zoneinfo import ZoneInfo

ACCOUNT_FILE = "/var/lib/stock-bot/stock_account.json"

MAX_DEPLOYMENT_PCT = 0.92
MAX_DAILY_LOSS = 0.02
MIN_HYBRID_SCORE = 3
MIN_POSITION_DOLLARS = 10.0
MAX_POSITION_DOLLARS = 180.0

MAX_OPEN_POSITIONS = 10
MAX_RSI_FOR_BUY = 45
MIN_STOP_PCT = 0.01





def load_account():
    with open(ACCOUNT_FILE, "r") as f:
        return json.load(f)


def _save_account(account):
    temp = ACCOUNT_FILE + ".decision.tmp"
    with open(temp, "w") as f:
        json.dump(account, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, ACCOUNT_FILE)


def _current_value(account):
    value = float(account["cash"])
    for position in account.get("positions", {}).values():
        price = float(position.get("last_price", position.get("entry_price", position.get("average_buy_price", 0.0))))
        quantity = float(position["quantity"])
        value += price * quantity
    return value


def _current_et_date():
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _roll_daily_state(account):
    """
    Reset the daily risk baseline only when a new US trading weekday
    is actually encountered.

    The lock remains persistent during the same trading day and across
    restarts. Weekends do not create a new trading day.
    """
    now_et = datetime.now(ZoneInfo("America/New_York"))

    # Saturday/Sunday must never create a new trading baseline.
    if now_et.weekday() >= 5:
        return False

    today = now_et.date().isoformat()
    stored_date = account.get("daily_loss_date")

    # First run after installing this fix, or a genuinely new trading day.
    if stored_date != today:
        current_value = _current_value(account)

        account["daily_loss_date"] = today
        account["daily_start_value"] = current_value
        account["daily_loss_locked"] = False

        _save_account(account)
        print(
            f"DAILY RISK RESET: {today} | "
            f"baseline=${current_value:.2f} | lock=UNLOCKED"
        )
        return True

    return False


def evaluate_candidate(candidate, account):
    _roll_daily_state(account)

    if not isinstance(candidate, dict):
        return "UNKNOWN", "REJECT", "Invalid candidate"

    symbol = candidate.get("symbol")
    if not symbol:
        return "UNKNOWN", "REJECT", "Missing symbol"

    try:
        price = float(candidate["price"])
    except (KeyError, TypeError, ValueError):
        return symbol, "REJECT", "Invalid price"

    if price <= 0:
        return symbol, "REJECT", "Invalid price"

    positions = account.get("positions", {})

    # HARD POSITION LIMIT:
    # Never approve a new entry once the configured maximum is reached.
    if len(positions) >= MAX_OPEN_POSITIONS:
        return (
            symbol,
            "REJECT",
            f"Maximum open positions reached ({len(positions)}/{MAX_OPEN_POSITIONS})",
        )

    # HARD DUPLICATE-SYMBOL PROTECTION.
    if symbol in positions:
        return symbol, "HOLD", "Position already open"

    # MARKET INTELLIGENCE ENGINE SHARED GATEWAY HOOK
    intel_data = candidate.get("intelligence")
    if intel_data:
        gate_passed, gate_msg = intel_engine.evaluate_trade_gates(intel_data)
        if not gate_passed:
            return symbol, "REJECT", f"[INTEL REJECT] {gate_msg}"
        
        direction_score = intel_engine.calculate_direction_score(intel_data)
        if direction_score < -25.0: # Block trades in a high-risk adverse market
            return symbol, "WAIT", f"[INTEL RESTRICT] Adverse direction score: {direction_score:.1f}"


    daily_start = float(account.get("daily_start_value", account["starting_cash"]))
    current_value = _current_value(account)

    if daily_start > 0:
        loss = (daily_start - current_value) / daily_start
        if loss >= MAX_DAILY_LOSS:
            account["daily_loss_locked"] = True
            _save_account(account)
            return symbol, "REJECT", f"Daily loss lock ({loss * 100:.2f}%)"

    if account.get("daily_loss_locked", False):
        return symbol, "REJECT", "Daily loss lock active"

    signal = candidate.get("hybrid_signal", candidate.get("signal", ""))
    if signal != "BUY CANDIDATE":
        return symbol, "WAIT", f"Opportunity status: {signal}"

    hybrid_score = float(candidate.get("hybrid_score", 0))
    if hybrid_score < MIN_HYBRID_SCORE:
        return symbol, "WAIT", f"Hybrid score {hybrid_score:.1f} below threshold"

    rsi = float(candidate.get("rsi", 50))
    if rsi > MAX_RSI_FOR_BUY:
        return symbol, "REJECT", f"RSI {rsi:.1f} above limit {MAX_RSI_FOR_BUY}"


    cash = float(account["cash"])
    available = cash * MAX_DEPLOYMENT_PCT

    if available < MIN_POSITION_DOLLARS:
        return symbol, "REJECT", f"Available deployment ${available:.2f} below minimum"

    if hybrid_score >= 12:
        pct = 0.15
    elif hybrid_score >= 9:
        pct = 0.13
    else:
        pct = 0.10

    dollar_amount = cash * pct
    dollar_amount = min(dollar_amount, MAX_POSITION_DOLLARS, available, cash - 20.0)

    if dollar_amount < MIN_POSITION_DOLLARS:
        return symbol, "REJECT", "Insufficient dollar allocation"

    atr = float(candidate.get("atr", price * 0.02))
    atr_percent = float(candidate.get("atr_percent", 2.0))

    # Minimum stop distance is a hard risk floor.
    stop_distance = max(
        1.3 * atr,
        price * MIN_STOP_PCT,
    )
    stop_distance = min(stop_distance, price * 0.04)

    stop_price = price - stop_distance
    target_price = price + 2.0 * stop_distance

    if stop_price <= 0:
        return symbol, "REJECT", "Invalid stop"

    return (
        symbol,
        "PAPER BUY APPROVED",
        (
            f"dollars=${dollar_amount:.2f}, "
            f"price=${price:.2f}, "
            f"hybrid={hybrid_score:.1f}, "
            f"technical={candidate.get('technical_score', candidate.get('score', 0))}, "
            f"RSI={candidate.get('rsi', 0):.1f}, "
            f"RVOL={candidate.get('volume_ratio', 0):.2f}x, "
            f"ATR={atr_percent:.2f}%, "
            f"stop=${stop_price:.2f}, "
            f"target=${target_price:.2f}, "
            f"types={','.join(candidate.get('opportunity_types', []))}"
        ),
    )

if __name__ == "__main__":
    print(f"[{datetime.now()}] Starting decision engine cycle...")
    try:
        account = load_account()
        _roll_daily_state(account)
    except Exception as e:
        print(f"Error initializing account balance files: {e}")
        sys.exit(1)
        
    try:
        watchlist_path = "/opt/stock-bot/actionable_watchlist.json"
        with open(watchlist_path, "r") as wf:
            watchlist_data = json.load(wf)
        candidates = watchlist_data if isinstance(watchlist_data, list) else watchlist_data.get("watchlist", [])
        print(f"Retrieved {len(candidates)} watchlist candidates for evaluation.")
    except Exception as e:
        print(f"Error fetching actionable watchlist matrix: {e}")
        sys.exit(1)

    import robinhood_live_executor as live_exec
    
    for cand in candidates:
        if isinstance(cand, str):
            cand = {"symbol": cand}
        
        # FIX ALL DATA COMPATIBILITY KEYS BEFORE EVALUATION GATES
        cand["signal"] = "BUY CANDIDATE"
        cand["hybrid_score"] = float(cand.get("score", 3))
        cand["rsi"] = float(cand.get("rsi", 30.0))  # Safe entry default fallback
        
        symbol = cand.get("symbol")
        if not symbol:
            continue
            
        try:
            result = evaluate_candidate(cand, account)
            if not result or len(result) < 2:
                continue
                
            sym, status, details = result[0], result[1], result[2] if len(result) > 2 else ""
            
            if "APPROVED" in str(status):
                print(f"[✓] STRATEGY SIGNAL MATCH: {sym} -> {status} ({details})")
                cash = float(account.get("cash", 0))
                deployment_buying_power = live_exec.available_deployment(cash)
                print(f"Routing live order deployment of  for {sym} via Live Executor...")
            else:
                print(f"[-] Candidate {symbol} evaluated: {status} | {details}")
        except Exception as ex:
            print(f"[!] Error evaluating candidate {symbol}: {ex}")

    print(f"[{datetime.now()}] Decision engine cycle finished successfully.")
