import asyncio
import json
import os
import sqlite3
import uuid
from decimal import Decimal
from pathlib import Path

import equity_adapter_check as adapter

auth = adapter.auth
DB = Path.home() / "si-bots/test_orders.sqlite3"
KEY = "mcp-btc-1usd-buy-v1"
os.umask(0o077)

ORDER = {
    "symbol": "BTC-USD",
    "side": "buy",
    "type": "market",
    "dollar_amount": "1.00",
    "time_in_force": "gtc",
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

    adapter.READ_ONLY.update({
        "get_crypto_positions",
        "preview_crypto_order",
    })

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
                    and str(a.get("rhc_account_number", "")).endswith("3478")
                ]
                if len(matches) != 1:
                    raise RuntimeError("MCP Crypto ****3478 not uniquely resolved.")
                account = matches[0]
                if (
                    account.get("state") != "active"
                    or account.get("deactivated") is not False
                    or account.get("permanently_deactivated") is not False
                ):
                    raise RuntimeError("Linked account not verified active.")

                brokerage = account["account_number"]
                rhs = account["rhs_account_number"]
                crypto = account["rhc_account_number"]
                if not all(isinstance(v, str) and v for v in (brokerage, rhs, crypto)):
                    raise RuntimeError("Required account identifier missing.")

                portfolio = await adapter.read_tool(
                    session, "get_portfolio",
                    {"account_number": brokerage},
                )
                crypto_power = portfolio.get("crypto_buying_power") or {}
                if portfolio.get("currency") != "USD":
                    raise RuntimeError("USD portfolio not verified.")
                bp = Decimal(crypto_power["buying_power"])
                if (
                    not bp.is_finite()
                    or bp <= 0
                    or bp * Decimal("0.90") < Decimal("1.01")
                ):
                    raise RuntimeError("Crypto reserve/collar check failed.")

                positions = []
                cursor = None
                seen = set()
                while True:
                    args = {"rhs_account_number": rhs}
                    if cursor:
                        args["cursor"] = cursor
                    page = await adapter.read_tool(
                        session, "get_crypto_positions", args
                    )
                    positions.extend(
                        p for p in (page.get("results") or [])
                        if p and (p.get("currency") or {}).get("code") == "BTC"
                    )
                    cursor = page.get("next")
                    if not cursor:
                        break
                    if cursor in seen:
                        raise RuntimeError("Repeated positions cursor.")
                    seen.add(cursor)

                params = {"rhs_account_number": rhs, **ORDER}
                encoded = json.dumps(params, sort_keys=True)

                with sqlite3.connect(DB) as db:
                    db.execute("PRAGMA synchronous=FULL")
                    db.execute("""
                        CREATE TABLE IF NOT EXISTS crypto_test_plan (
                            plan_key TEXT PRIMARY KEY,
                            ref_id TEXT NOT NULL UNIQUE,
                            parameters TEXT NOT NULL,
                            crypto_account TEXT NOT NULL,
                            state TEXT NOT NULL,
                            baseline TEXT NOT NULL,
                            preview TEXT
                        )
                    """)
                    db.execute(
                        "INSERT OR IGNORE INTO crypto_test_plan "
                        "(plan_key, ref_id, parameters, crypto_account, "
                        "state, baseline) VALUES (?, ?, ?, ?, ?, ?)",
                        (KEY, str(uuid.uuid4()), encoded, crypto,
                         "PREPARED", json.dumps(positions)),
                    )
                    row = db.execute(
                        "SELECT ref_id, parameters, crypto_account, state, baseline "
                        "FROM crypto_test_plan WHERE plan_key=?", (KEY,)
                    ).fetchone()

                if row[1] != encoded or row[2] != crypto or row[3] != "PREPARED":
                    raise RuntimeError("Plan differs or was already advanced.")

                preview = await adapter.read_tool(
                    session, "preview_crypto_order", params
                )
                if preview.get("crypto_account_number") != crypto:
                    raise RuntimeError(
                        "Preview account missing or different; stopped."
                    )
                order = preview.get("order") or {}
                if (
                    order.get("side") != "buy"
                    or order.get("type") != "market"
                    or not order.get("quantity")
                ):
                    raise RuntimeError("Unexpected preview order.")

                with sqlite3.connect(DB) as db:
                    db.execute("PRAGMA synchronous=FULL")
                    changed = db.execute(
                        "UPDATE crypto_test_plan SET preview=? "
                        "WHERE plan_key=? AND state='PREPARED' "
                        "AND parameters=? AND crypto_account=?",
                        (json.dumps(preview), KEY, encoded, crypto),
                    ).rowcount
                    if changed != 1:
                        raise RuntimeError("Plan changed during preview.")

                output = json.dumps({
                    "account": "****3478",
                    "stable_buy_reference": row[0],
                    "request": ORDER,
                    "crypto_buying_power_USD": str(bp),
                    "reserve_USD": str(bp * Decimal("0.10")),
                    "saved_BTC_baseline": json.loads(row[4]),
                    "broker_preview": preview,
                    "orders_submitted": False,
                }, indent=2)

                for value in sorted(
                    {brokerage, rhs, crypto}, key=len, reverse=True
                ):
                    output = output.replace(value, "****" + value[-4:])
                print(output)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        adapter.report(error)
        raise SystemExit(1)
