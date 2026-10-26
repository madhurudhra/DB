from dataclasses import dataclass


@dataclass(frozen=True)
class TradingReadiness:
    broker_authenticated: bool = False
    market_data_healthy: bool = False
    risk_checks_passed: bool = False
    reconciliation_passed: bool = False
    duplicate_order_protection_ready: bool = False


@dataclass(frozen=True)
class TradePermission:
    allowed: bool
    reasons: tuple[str, ...]


def evaluate_trade_permission(
    *,
    trading_mode: str,
    live_trading_enabled: bool,
    bot_enabled: bool,
    readiness: TradingReadiness,
) -> TradePermission:
    """Fail closed. This evaluates permission; it never submits an order."""
    reasons: list[str] = []

    if trading_mode != "live":
        reasons.append("Trading mode is not live")

    if not live_trading_enabled:
        reasons.append("Live trading master switch is disabled")

    if not bot_enabled:
        reasons.append("This bot is disabled")

    required_checks = {
        "Broker authentication is not verified": readiness.broker_authenticated,
        "Market data health is not verified": readiness.market_data_healthy,
        "Risk checks have not passed": readiness.risk_checks_passed,
        "Broker/account reconciliation has not passed": readiness.reconciliation_passed,
        "Duplicate-order protection is not ready": readiness.duplicate_order_protection_ready,
    }

    reasons.extend(message for message, passed in required_checks.items() if not passed)

    return TradePermission(allowed=not reasons, reasons=tuple(reasons))
