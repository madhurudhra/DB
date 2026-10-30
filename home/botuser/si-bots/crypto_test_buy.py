import asyncio
import inspect
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import equity_adapter_check as adapter

auth = adapter.auth
DB = Path.home() / "si-bots/test_orders.sqlite3"
KEY = "mcp-btc-1usd-buy-v1"
REF = "0d9eed92-18b0-42af-ac34-1265d8705ac7"
os.umask(0o077)

async def submit(session, arguments):
    signature = inspect.signature(session.call_tool).parameters
    if "arguments" in signature:
        result = await session.call_tool(
            "place_crypto_order", arguments=arguments
        )
    elif "params" in signature:
        from mcp.types import CallToolRequestParams
        result = await session.call_tool(
            params=CallToolRequestParams(
                name="place_crypto_order", arguments=arguments
            )
        )
    else:
        raise RuntimeError("Unsupported call_tool signature.")
    return result.model_dump(mode="json", by_alias=True)

async def main():
    if not DB.is_file():
        raise RuntimeError("Saved order database missing.")

    with sqlite3.connect(DB) as db:
        db.execute("PRAGMA synchronous=FULL")
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(crypto_test_plan)")
        }
        if "submission_response" not in columns:
            db.execute(
                "ALTER TABLE crypto_test_plan "
                "ADD COLUMN submission_response TEXT"
            )
        row = db.execute(
            "SELECT ref_id, parameters, crypto_account, state, preview "
            "FROM crypto_test_plan WHERE plan_key=?", (KEY,)
        ).fetchone()

    if not row or row[0] != REF or row[3] != "PREPARED" or not row[4]:
        raise RuntimeError(
            "Plan missing, changed, or already attempted. Do not resubmit."
        )

    params = json.loads(row[1])
    preview = json.loads(row[4])
    expected = {
        "symbol": "BTC-USD",
        "side": "buy",
        "type": "market",
        "dollar_amount": "1.00",
        "time_in_force": "gtc",
    }
    if set(params) != set(expected) | {"rhs_account_number"}:
        raise RuntimeError("Unexpected saved parameter fields.")
    if any(params.get(k) != v for k, v in expected.items()):
        raise RuntimeError("Saved request differs from approved BUY.")
    if preview.get("crypto_account_number") != row[2]:
        raise RuntimeError("Saved preview account mismatch.")

    order = preview.get("order") or {}
    stamp = datetime.fromisoformat(order["created_at"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if not 0 <= age <= 900:
        raise RuntimeError("Preview older than 15 minutes; stopped.")
    total = Decimal(order["net_rounded_estimated_notional"])
    fee = Decimal(preview["estimated_fee"])
    if (
        not total.is_finite() or total <= 0 or total > Decimal("1.00")
        or not fee.is_finite() or fee < 0 or fee > Decimal("0.01")
        or order.get("side") != "buy"
        or order.get("type") != "market"
        or order.get("currency_code") != "BTC"
        or preview.get("customer_approval_required") is not False
    ):
        raise RuntimeError("Preview differs from the reviewed terms.")

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

    async with auth.httpx2.AsyncClient(
        auth=oauth,
        timeout=auth.httpx2.Timeout(30.0, read=120.0),
    ) as client:
        async with auth.streamable_http_client(
            auth.SERVER, http_client=client
        ) as streams:
            async with auth.ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                accounts = await adapter.read_tool(session, "get_accounts", {})
                matches = [
                    a for a in (accounts.get("accounts") or [])
                    if a
                    and a.get("rhs_account_number") == params["rhs_account_number"]
                    and a.get("rhc_account_number") == row[2]
                    and str(a.get("account_number", "")).endswith("5004")
                    and str(a.get("rhc_account_number", "")).endswith("3478")
                    and a.get("agentic_allowed") is True
                    and a.get("state") == "active"
                    and a.get("deactivated") is False
                    and a.get("permanently_deactivated") is False
                ]
                if len(matches) != 1:
                    raise RuntimeError("Approved account not uniquely active.")
                account = matches[0]
                portfolio = await adapter.read_tool(
                    session, "get_portfolio",
                    {"account_number": account["account_number"]},
                )
                bp = Decimal(
                    portfolio["crypto_buying_power"]["buying_power"]
                )
                if (
                    portfolio.get("currency") != "USD"
                    or not bp.is_finite()
                    or bp * Decimal("0.90") < Decimal("1.01")
                ):
                    raise RuntimeError("Crypto buying-power/reserve check failed.")

                args = {**params, "ref_id": REF}
                tool = None
                cursor = None
                seen = set()
                while True:
                    page = (
                        await session.list_tools(cursor=cursor)
                        if cursor else await session.list_tools()
                    )
                    tool = next(
                        (t for t in page.tools if t.name == "place_crypto_order"),
                        None,
                    )
                    if tool:
                        break
                    cursor = page.nextCursor
                    if not cursor:
                        break
                    if cursor in seen:
                        raise RuntimeError("Repeated tool-list cursor.")
                    seen.add(cursor)
                if not tool:
                    raise RuntimeError("Placement tool unavailable.")
                schema = tool.input_schema
                if (
                    not set(args).issubset(schema.get("properties", {}))
                    or not set(schema.get("required", [])).issubset(args)
                ):
                    raise RuntimeError("Live placement schema mismatch.")

                with sqlite3.connect(DB) as db:
                    db.execute("PRAGMA synchronous=FULL")
                    changed = db.execute(
                        "UPDATE crypto_test_plan SET state='SUBMITTING' "
                        "WHERE plan_key=? AND state='PREPARED' AND ref_id=? "
                        "AND parameters=? AND preview=?",
                        (KEY, REF, row[1], row[4]),
                    ).rowcount
                    if changed != 1:
                        raise RuntimeError("Plan changed; submission blocked.")

                print("Submitting approved BUY once; reference:", REF, flush=True)
                envelope = await submit(session, args)

                with sqlite3.connect(DB) as db:
                    db.execute("PRAGMA synchronous=FULL")
                    db.execute(
                        "UPDATE crypto_test_plan "
                        "SET state='RESPONSE_RECEIVED', submission_response=? "
                        "WHERE plan_key=? AND ref_id=?",
                        (json.dumps(envelope), KEY, REF),
                    )

                text = json.dumps(envelope, indent=2)
                identifiers = {
                    account["account_number"],
                    account["rhs_account_number"],
                    account["rhc_account_number"],
                }
                for value in sorted(identifiers, key=len, reverse=True):
                    text = text.replace(value, "****" + value[-4:])
                print(text)
                print("Broker response saved. No SELL submitted.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        adapter.report(error)
        print(
            "If submission began, its outcome may be unknown. "
            "Do not retry or create a new reference; reconcile first."
        )
        raise SystemExit(1)
