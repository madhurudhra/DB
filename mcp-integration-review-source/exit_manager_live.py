import logging

logger = logging.getLogger(__name__)

def check_live_position(broker, symbol, strategy_name="ZERODHA_LIVE_NET_6_PROFIT_1_LOSS_V1"):
    try:
        position = broker.get_open_position(symbol)
        if not position:
            return False, f"{symbol} EXIT BLOCKED: position not found"
        
        qty = float(position.get("quantity", 0) or 0)
        shares_available = float(position.get("shares_available_for_sells", qty) or 0)
        avg_price = float(position.get("average_buy_price", 0) or 0)
        curr_price = float(broker.get_last_price(symbol) or 0)
    except Exception as e:
        return False, f"{symbol} EXIT BLOCKED: error fetching position data ({e})"

    if shares_available <= 0:
        return False, f"{symbol} HOLD (Order already pending/queued for exit)"

    if qty <= 0 or curr_price <= 0:
        return False, f"{symbol} EXIT BLOCKED: invalid or non-positive position values"

    if avg_price <= 0:
        avg_price = curr_price

    stop_price = avg_price * 0.99
    target_price = avg_price * 1.06
    pnl_pct = ((curr_price / avg_price) - 1) * 100

    exit_due = False
    reason = "HOLD"

    if curr_price <= stop_price:
        exit_due = True
        reason = f"EXIT_STOP_LOSS_{symbol}"
    elif curr_price >= target_price:
        exit_due = True
        reason = f"EXIT_PROFIT_TARGET_{symbol}"

    return exit_due, f"{symbol} {reason} [{strategy_name}]: price=Rs.{curr_price:.2f} P&L={pnl_pct:+.2f}% cost=Rs.{avg_price:.2f} stop=Rs.{stop_price:.2f} target=Rs.{target_price:.2f}"
