from fastapi import APIRouter
from datetime import datetime, timezone
import time

from app.core.config import get_settings
from app.core.database import connect_database
from app.core.readiness import BOT_NAMES, evaluate_readiness
from app.brokers.healthcheck import run_check

router = APIRouter()
_cache = {"checked_at": 0.0, "reports": {}}
_CACHE_SECONDS = 60


def broker_connectivity():
    # Cache read-only checks briefly to avoid repeated broker API calls.
    now = time.monotonic()
    if now - _cache["checked_at"] < _CACHE_SECONDS and _cache["reports"]:
        return _cache["reports"]

    from app.brokers.robinhood.crypto import RobinhoodCrypto
    from app.brokers.zerodha.kite import ZerodhaKite

    reports = {
        "US-CRYPTO": run_check("US-CRYPTO", RobinhoodCrypto, [
            ("accounts", lambda c: c.accounts()),
            ("holdings", lambda c: c.holdings()),
            ("pairs", lambda c: c.pairs()),
            ("orders", lambda c: c.orders()),
        ]),
        "IN-ZERODHA": run_check("IN-ZERODHA", ZerodhaKite, [
            ("profile", lambda c: c.profile()),
            ("holdings", lambda c: c.holdings()),
            ("positions", lambda c: c.positions()),
            ("orders", lambda c: c.orders()),
        ]),
        "US-EQUITY": {
            "bot": "US-EQUITY",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "NOT_IMPLEMENTED",
            "checks": {},
            "reason": "Robinhood equity adapter is not implemented",
        },
    }
    _cache["reports"] = reports
    _cache["checked_at"] = now
    return reports


@router.get("/health")
def health():
    return {"status": "ok", "component": "super-intelligence"}


@router.get("/api/status")
def status():
    settings = get_settings()

    with connect_database() as db:
        table_count = db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        audit_count = db.execute(
            "SELECT COUNT(*) FROM audit_events"
        ).fetchone()[0]

    connectivity = broker_connectivity()
    enabled_by_bot = {
        "US-CRYPTO": settings.crypto_bot_enabled,
        "US-EQUITY": settings.equity_bot_enabled,
        "IN-ZERODHA": settings.zerodha_bot_enabled,
    }

    reports = []
    for name in BOT_NAMES:
        connected = (
            connectivity.get(name, {}).get("status") == "READ_CONNECTIVITY_PASS"
        )
        report = evaluate_readiness(
            bot_name=name,
            bot_enabled=enabled_by_bot[name],
            live_trading_enabled=settings.live_trading_enabled,
            broker_authenticated=connected,
            # These remain false until their real checks are implemented.
            market_data_healthy=False,
            risk_checks_passed=False,
            reconciliation_passed=False,
            duplicate_order_protection_ready=False,
        )
        reports.append({
            "name": report.bot_name,
            "status": report.status,
            "checks": report.checks,
            "blockers": list(report.blockers),
        })

    return {
        "environment": settings.app_env,
        "trading_mode": settings.trading_mode,
        "live_trading_enabled": settings.live_trading_enabled,
        "database": {
            "reachable": True,
            "table_count": table_count,
            "audit_event_count": audit_count,
        },
        "broker_connectivity": connectivity,
        "bots": reports,
    }
