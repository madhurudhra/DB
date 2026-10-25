import json
import os
from datetime import datetime, timezone

def run_check(name, factory, methods):
    report = {
        "bot": name,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "BLOCKED",
        "checks": {},
    }
    try:
        client = factory()
        for label, method in methods:
            method(client)
            report["checks"][label] = "PASS"
        report["status"] = "READ_CONNECTIVITY_PASS"
    except Exception as exc:
        # Avoid printing secrets, request headers, or stack traces.
        report["error_type"] = type(exc).__name__
        report["reason"] = str(exc)[:200]
    return report

def main():
    from app.brokers.robinhood.crypto import RobinhoodCrypto
    from app.brokers.zerodha.kite import ZerodhaKite

    results = [
        run_check("US-CRYPTO", RobinhoodCrypto, [
            ("accounts", lambda c: c.accounts()),
            ("holdings", lambda c: c.holdings()),
            ("pairs", lambda c: c.pairs()),
            ("orders", lambda c: c.orders()),
        ]),
        run_check("IN-ZERODHA", ZerodhaKite, [
            ("profile", lambda c: c.profile()),
            ("holdings", lambda c: c.holdings()),
            ("positions", lambda c: c.positions()),
            ("orders", lambda c: c.orders()),
        ]),
    ]
    print(json.dumps(results, indent=2))
    return 0 if all(x["status"] == "READ_CONNECTIVITY_PASS" for x in results) else 2

if __name__ == "__main__":
    raise SystemExit(main())
