"""Shared exit-signal policy. No broker calls or order authorization."""

from decimal import Decimal, InvalidOperation

VERSION = "shared-exit-policy-v1"

SCORE_RULES = {
    "crypto": (Decimal("70"), Decimal("100")),
    "equity": (Decimal("6"), Decimal("10")),
}


def number(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not a numeric input")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Numeric input must be finite")
    return result


def decision(action, reason):
    return {
        "policy_version": VERSION,
        "action": action,
        "reason": reason,
        "order_authorized": False,
    }


def evaluate_exit(
    *,
    bot,
    bid,
    average_purchase_price,
    estimated_net_return_fraction,
    rally_score,
):
    """
    Net return is a fraction: 0.05 means 5%, not 5.0.

    Caller must calculate estimated net return after fees and verify:
    fresh quotes, accurate cost basis, protected quantities, account
    identity, open orders, reconciliation, and broker authorization.

    EXIT_SIGNAL is not permission to submit an order.
    """
    if bot not in SCORE_RULES:
        return decision("BLOCKED", "UNKNOWN_BOT")

    try:
        price = number(bid)
        average = number(average_purchase_price)
        if price <= 0 or average <= 0:
            raise ValueError("Prices must be positive")
    except (InvalidOperation, ValueError, TypeError):
        return decision("BLOCKED", "INVALID_PRICE_OR_BASIS")

    if price <= average * Decimal("0.985"):
        return decision("EXIT_SIGNAL", "STOP_LOSS_1_5_PERCENT")

    try:
        net_return = number(estimated_net_return_fraction)
    except (InvalidOperation, ValueError, TypeError):
        return decision("BLOCKED", "INVALID_NET_RETURN")

    if net_return >= Decimal("0.20"):
        return decision("EXIT_SIGNAL", "PROFIT_CAP_20_PERCENT")

    if net_return < Decimal("0.02"):
        return decision("HOLD", "BELOW_PROFIT_EXIT_RANGE")

    threshold, maximum = SCORE_RULES[bot]
    try:
        score = number(rally_score)
        if not Decimal("0") <= score <= maximum:
            raise ValueError("Score outside bot scale")
    except (InvalidOperation, ValueError, TypeError):
        return decision("BLOCKED", "RALLY_SCORE_MISSING_OR_INVALID")

    if score < threshold:
        reason = (
            "WEAK_RALLY_AT_OR_ABOVE_5_PERCENT"
            if net_return >= Decimal("0.05")
            else "WEAK_RALLY_BETWEEN_2_AND_5_PERCENT"
        )
        return decision("EXIT_SIGNAL", reason)

    return decision("HOLD", "STRONG_RALLY_BELOW_20_PERCENT")
