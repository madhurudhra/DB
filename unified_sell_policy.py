"""Backend-only exit decisions. No broker calls, file writes, or UI changes."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

BOTS = frozenset({"crypto", "us_trading", "zerodha"})
STOP_RETURN = Decimal("-0.01")
PROFIT_RETURN = Decimal("0.06")

@dataclass(frozen=True)
class ExitDecision:
    bot: str
    state: str
    reason: str
    net_return: Decimal
    threshold_due: bool
    submit_allowed: bool
    blockers: tuple[str, ...]

def _decimal(value, name):
    if isinstance(value, bool) or value is None:
        raise ValueError(name + " must be a finite monetary value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(name + " must be a finite monetary value") from None
    if not result.is_finite():
        raise ValueError(name + " must be finite")
    return result

def evaluate_exit(
    bot, *, entry_cost_including_fees, estimated_net_sale_credit,
    sell_enabled, reconciled, pending_order, reference_verified,
):
    """Evaluate one matched, positive long-position quantity in one currency.

    The caller must supply entry cost and sale credit for the SAME quantity.
    Sale credit must already deduct estimated exit costs. An explicitly approved,
    persisted activation reference may be used, but must be labelled by the caller.
    All control flags must come from verified runtime state, not UI defaults.
    This function authorizes no broker interaction and holds no execution lock.
    """
    if bot not in BOTS:
        raise ValueError("Unknown bot")
    flags = {
        "sell_enabled": sell_enabled,
        "reconciled": reconciled,
        "pending_order": pending_order,
        "reference_verified": reference_verified,
    }
    if any(type(value) is not bool for value in flags.values()):
        raise ValueError("All control flags must be explicit Boolean values")

    cost = _decimal(entry_cost_including_fees, "entry cost")
    credit = _decimal(estimated_net_sale_credit, "sale credit")
    if cost <= 0 or credit < 0:
        raise ValueError("Entry cost must be positive; sale credit cannot be negative")

    net_return = (credit - cost) / cost
    if credit <= cost * (Decimal("1") + STOP_RETURN):
        reason = "STOP_LOSS_1PCT_NET"
    elif credit >= cost * (Decimal("1") + PROFIT_RETURN):
        reason = "TAKE_PROFIT_6PCT_NET"
    else:
        reason = "WITHIN_THRESHOLDS"
    due = reason != "WITHIN_THRESHOLDS"

    blockers = tuple(
        label for condition, label in (
            (not sell_enabled, "SELL_OFF"),
            (not reconciled, "UNRECONCILED"),
            (pending_order, "PENDING_ORDER"),
            (not reference_verified, "REFERENCE_UNVERIFIED"),
        ) if condition
    )
    allowed = due and not blockers
    state = "BLOCKED" if blockers else "EXIT_DUE" if due else "HOLD"
    return ExitDecision(bot, state, reason, net_return, due, allowed, blockers)
