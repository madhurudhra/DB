from dataclasses import dataclass


BOT_NAMES = ("US-CRYPTO", "US-EQUITY", "IN-ZERODHA")


@dataclass(frozen=True)
class ReadinessReport:
    bot_name: str
    status: str
    checks: dict[str, bool]
    blockers: tuple[str, ...]


def evaluate_readiness(
    *,
    bot_name: str,
    bot_enabled: bool,
    live_trading_enabled: bool,
    broker_authenticated: bool = False,
    market_data_healthy: bool = False,
    risk_checks_passed: bool = False,
    reconciliation_passed: bool = False,
    duplicate_order_protection_ready: bool = False,
) -> ReadinessReport:
    checks = {
        "bot_enabled": bot_enabled,
        "live_trading_enabled": live_trading_enabled,
        "broker_authenticated": broker_authenticated,
        "market_data_healthy": market_data_healthy,
        "risk_checks_passed": risk_checks_passed,
        "reconciliation_passed": reconciliation_passed,
        "duplicate_order_protection_ready": duplicate_order_protection_ready,
    }

    blockers = tuple(name for name, passed in checks.items() if not passed)

    return ReadinessReport(
        bot_name=bot_name,
        status="READY" if not blockers else "BLOCKED",
        checks=checks,
        blockers=blockers,
    )


def initial_reports() -> list[ReadinessReport]:
    """Initial state: no broker or live-trading readiness is assumed."""
    return [
        evaluate_readiness(
            bot_name=name,
            bot_enabled=False,
            live_trading_enabled=False,
        )
        for name in BOT_NAMES
    ]
