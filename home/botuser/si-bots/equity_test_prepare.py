import asyncio
import json
import os
import sqlite3
import uuid
from decimal import Decimal
from pathlib import Path

import equity_adapter_check as adapter

auth = adapter.auth
ROOT = Path.home() / "si-bots"
os.umask(0o077)

ORDER = {
    "symbol": "IONQ",
    "side": "buy",
    "type": "market",
    "dollar_amount": "1.00",
    "time_in_force": "gfd",
    "market_hours": "regular_hours",
}

async def main():
    oauth = auth.OAuthClientProvider(
        server_url=auth.SERVER,
        client_metadata=auth.OAuthClientMetadata(
            client_name="super-intelligence-vps",
            redirect_uris=[auth.REDIRECT],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=auth.FileStorage(),
        redirect_handler=adapter.refuse_login,
        callback_handler=adapter.refuse_login,
    )

    adapter.READ_ONLY.add("review_equity_order")
    async with auth.httpx2.AsyncClient(
        auth=oauth,
        timeout=auth.httpx2.Timeout(30.0, read=120.0),
    ) as client:
        async with auth.streamable_http_client(
            auth.SERVER, http_client=client
        ) as streams:
            async with auth.ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await adapter.read_tool(session, "get_accounts", {})
                matches = [
                    a for a in (result.get("accounts") or [])
                    if a and a.get("agentic_allowed") is True
                    and str(a.get("account_number", "")).endswith("5004")
                ]
                if len(matches) != 1:
                    raise RuntimeError("Expected Equity account not resolved.")
                account = matches[0]
                if (
                    account.get("state") != "active"
                    or account.get("deactivated") is not False
                    or account.get("permanently_deactivated") is not False
                ):
                    raise RuntimeError("Equity account is not verified active.")

                number = account["account_number"]
                portfolio = await adapter.read_tool(
                    session, "get_portfolio", {"account_number": number}
                )
                power = portfolio.get("buying_power") or {}
                if power.get("display_currency") != "USD":
                    raise RuntimeError("USD buying power not verified.")
                buying_power = Decimal(power["buying_power"])
                if not buying_power.is_finite() or buying_power <= 0:
                    raise RuntimeError("Invalid buying power.")
                reserve = buying_power * Decimal("0.10")
                if buying_power - reserve < Decimal("1.00"):
                    raise RuntimeError("Insufficient funds after reserve.")

                params = {"account_number": number, **ORDER}
                encoded = json.dumps(params, sort_keys=True)
                with sqlite3.connect(ROOT / "test_orders.sqlite3") as db:
                    db.execute("""
                        CREATE TABLE IF NOT EXISTS equity_test_plan (
                            plan_key TEXT PRIMARY KEY,
                            ref_id TEXT NOT NULL UNIQUE,
                            parameters TEXT NOT NULL,
                            state TEXT NOT NULL,
                            review TEXT
                        )
                    """)
                    db.execute(
                        "INSERT OR IGNORE INTO equity_test_plan "
                        "(plan_key, ref_id, parameters, state) VALUES (?, ?, ?, ?)",
                        ("ionq-1usd-buy-v1", str(uuid.uuid4()), encoded, "PREPARED"),
                    )
                    row = db.execute(
                        "SELECT ref_id, parameters, state FROM equity_test_plan "
                        "WHERE plan_key = ?",
                        ("ionq-1usd-buy-v1",),
                    ).fetchone()

                if row[1] != encoded or row[2] != "PREPARED":
                    raise RuntimeError("Existing plan differs or has advanced; stopped.")

                review = await adapter.read_tool(
                    session, "review_equity_order", params
                )
                with sqlite3.connect(ROOT / "test_orders.sqlite3") as db:
                    db.execute(
                        "UPDATE equity_test_plan SET review = ? "
                        "WHERE plan_key = ? AND state = 'PREPARED'",
                        (json.dumps(review), "ionq-1usd-buy-v1"),
                    )

                print("Account: ****5004")
                print("Stable BUY reference:", row[0])
                print("Buying power USD:", buying_power)
                print("10% reserve USD:", reserve)
                print("BUY request:", json.dumps(ORDER))
                print("BROKER REVIEW:")
                print(json.dumps(review, indent=2))
                print("No order submitted. Existing holdings untouched.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        adapter.report(error)
        raise SystemExit(1)
