"""Central write policy for the three trading bots.

WRITE_ENABLED permits submission only when all required safety
checks pass. It does not bypass broker-specific order validation.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
import os


@dataclass(frozen=True)
class BotPolicy:
    name: str
    broker: str
    timezone: str
    start_hour: int | None
    end_hour: int | None
    continuous: bool = False


BOT_POLICIES = {
    "crypto": BotPolicy(
        name="US Crypto",
        broker="robinhood_crypto",
        timezone="UTC",
        start_hour=None,
        end_hour=None,
        continuous=True,
    ),
    "equity": BotPolicy(
        name="US Equity",
        broker="robinhood_equity",
        timezone="America/New_York",
        start_hour=8,
        end_hour=20,
    ),
    "zerodha": BotPolicy(
        name="India Zerodha",
        broker="zerodha",
        timezone="Asia/Kolkata",
        start_hour=8,
        end_hour=17,
    ),
}


def _enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def is_market_window_open(
    bot: str,
    now: datetime | None = None,
) -> bool:
    policy = BOT_POLICIES.get(bot)
    if policy is None:
        return False

    if policy.continuous:
        return True

    current = now or datetime.now(ZoneInfo(policy.timezone))
    current = current.astimezone(ZoneInfo(policy.timezone))

    # Requested window; holidays and exchange sessions must also
    # be checked by the broker-specific market calendar.
    return policy.start_hour <= current.hour < policy.end_hour


def can_submit_order(
    bot: str,
    *,
    risk_passed: bool,
    market_calendar_passed: bool,
    account_verified: bool,
    reconciliation_healthy: bool,
    durable_order_intent_saved: bool,
) -> tuple[bool, str]:
    """Return permission and an explicit reason."""

    # LIVE_ONLY_ENFORCEMENT_GUARD_V1
    return False, "AUTOMATIC_ENFORCEMENT_NOT_IMPLEMENTED"

    policy = BOT_POLICIES.get(bot)
    if policy is None:
        return False, "UNKNOWN_BOT"

    if not _enabled("WRITE_ENABLED"):
        return False, "WRITE_DISABLED"

    bot_env = {
        "crypto": "CRYPTO_BOT_ENABLED",
        "equity": "EQUITY_BOT_ENABLED",
        "zerodha": "ZERODHA_BOT_ENABLED",
    }[bot]

    if not _enabled(bot_env):
        return False, "BOT_DISABLED"

    if not is_market_window_open(bot):
        return False, "OUTSIDE_CONFIGURED_MARKET_WINDOW"

    checks = {
        "RISK_CHECK_FAILED": risk_passed,
        "MARKET_CALENDAR_CHECK_FAILED": market_calendar_passed,
        "BROKER_ACCOUNT_NOT_VERIFIED": account_verified,
        "RECONCILIATION_NOT_HEALTHY": reconciliation_healthy,
        "ORDER_INTENT_NOT_PERSISTED": durable_order_intent_saved,
    }

    for reason, passed in checks.items():
        if not passed:
            return False, reason

    return True, "WRITE_PERMITTED"


# SAVED_AUTOMATIC_POLICY_GATE_V1
_previous_can_submit_order = can_submit_order


def can_submit_order(
    bot,
    *,
    side=None,
    symbol=None,
    account_ref=None,
    **checks,
):
    from app.core.automatic_order_guard import automatic_order_preflight

    allowed, reason = automatic_order_preflight(
        bot,
        side=side,
        symbol=symbol,
        account_ref=account_ref,
    )
    if not allowed:
        return False, reason

    return _previous_can_submit_order(bot, **checks)
