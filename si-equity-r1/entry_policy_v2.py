"""Fail-closed Equity entry-policy boundary.

No broker calls, database writes, permission changes, or automatic execution.
Authorization is valid only for the supplied snapshot. A submission adapter
must reserve capital atomically and revalidate immediately before submission.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

VERSION = "equity-entry-policy-v2"

REQUIRED_CHECKS = (
    "approved_entry_signal",
    "news_risk",
    "earnings_risk",
    "instrument_eligibility",
    "market_regime",
    "buy_restrictions",
    "daily_loss",
    "exit_coverage",
)


def number(value):
    if isinstance(value, bool) or not isinstance(
        value, (str, int, float, Decimal)
    ):
        raise ValueError("INVALID_NUMBER")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("INVALID_NUMBER") from None
    if not result.is_finite():
        raise ValueError("NONFINITE_NUMBER")
    return result


def fresh(stamp, now, max_age):
    if not isinstance(stamp, str):
        return False
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return False
        age = (now - parsed).total_seconds()
        return 0 <= age <= max_age
    except (ValueError, TypeError, OverflowError):
        return False


def evaluate(snapshot):
    blockers = []
    result = {
        "policy_version": VERSION,
        "entry_authorized": False,
        "orders_submitted": False,
        "capital_reserved": False,
        "blockers": blockers,
    }

    if not isinstance(snapshot, dict):
        blockers.append("INVALID_SNAPSHOT")
        return result

    now = datetime.now(timezone.utc)
    symbol = snapshot.get("symbol")
    result["symbol"] = symbol

    if (
        not isinstance(symbol, str)
        or not symbol
        or len(symbol) > 10
        or not symbol[0].isalpha()
        or not symbol.isascii()
        or symbol != symbol.upper()
        or any(not (c.isalnum() or c in ".-") for c in symbol)
    ):
        blockers.append("INVALID_SYMBOL")

    if not fresh(snapshot.get("checked_at"), now, 10):
        blockers.append("STALE_OR_INVALID_SNAPSHOT")

    controls = snapshot.get("controls")
    if not isinstance(controls, dict) or any(
        controls.get(key) is not True
        for key in ("bot", "auto_trade", "buy", "sell")
    ):
        blockers.append("REQUIRED_PERMISSIONS_NOT_ENABLED")

    if snapshot.get("account_verified") is not True:
        blockers.append("ACCOUNT_NOT_VERIFIED")

    if snapshot.get("unresolved_order") is not False:
        blockers.append("ORDER_OUTCOME_NOT_CLEAR")

    revision = snapshot.get("desired_revision")
    if (
        type(revision) is not int
        or revision < 1
        or type(snapshot.get("applied_revision")) is not int
        or snapshot["applied_revision"] != revision
    ):
        blockers.append("SETTINGS_NOT_APPLIED")

    checks = snapshot.get("checks")
    if not isinstance(checks, dict):
        checks = {}

    for name in REQUIRED_CHECKS:
        check = checks.get(name)
        if (
            not isinstance(check, dict)
            or check.get("passed") is not True
            or check.get("symbol") != symbol
            or check.get("revision") != revision
            or not fresh(check.get("checked_at"), now, 10)
        ):
            blockers.append("GATE_NOT_VERIFIED:" + name)

    try:
        count = snapshot["open_and_reserved_positions"]
        limit = snapshot["max_positions"]
        if (
            type(count) is not int
            or type(limit) is not int
            or count < 0
            or not 1 <= limit <= 100
            or count >= limit
        ):
            blockers.append("POSITION_LIMIT_NOT_VERIFIED")
    except KeyError:
        blockers.append("POSITION_LIMIT_MISSING")

    try:
        required = number(snapshot["maximum_order_cost_with_fees"])
        bot_available = number(snapshot["bot_available_after_reservations"])
        broker_available = number(
            snapshot["broker_available_after_reservations"]
        )
        per_order_limit = number(snapshot["approved_per_order_limit"])
        if (
            required <= 0
            or min(bot_available, broker_available, per_order_limit) < 0
            or required > min(
                bot_available, broker_available, per_order_limit
            )
        ):
            blockers.append("CAPITAL_OR_ORDER_LIMIT_FAILED")
    except (KeyError, ValueError):
        blockers.append("CAPITAL_NOT_VERIFIED")

    result["entry_authorized"] = not blockers
    result["next_step"] = (
        "ATOMIC_RESERVATION_AND_PRETRADE_REVALIDATION"
        if not blockers else "BLOCKED"
    )
    return result
