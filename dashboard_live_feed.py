import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path.home()))
sys.path.insert(0, str(Path.home() / "si-bots"))

import si_mcp_auth as auth
from equity_adapter_check import read_tool, READ_ONLY, refuse_login

TARGETS = {"crypto": "702274507", "equity": "554395004"}
OUT = Path.home() / "si-bots/dashboard_live_feed.json"

def now():
    return datetime.now(timezone.utc).isoformat()

def norm(s):
    return s.replace("_", "").lower()

def field(obj, key):
    return next((v for k, v in obj.items() if norm(k) == key), None)

def pick(obj, keys):
    return {k: obj[k] for k in keys if k in obj}

def publish(data):
    temp = OUT.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8") as f:
        os.chmod(temp, 0o600)
        json.dump(data, f, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, OUT)

def previous():
    try:
        return json.loads(OUT.read_text())
    except Exception:
        return {"bots": {}}

async def collect():
    snapshot = previous()
    snapshot["attempted_at"] = now()

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
        redirect_handler=refuse_login,
        callback_handler=refuse_login,
    )

    async with auth.httpx2.AsyncClient(
        auth=oauth, timeout=auth.httpx2.Timeout(30.0, read=120.0)
    ) as client:
        async with auth.streamable_http_client(
            auth.SERVER, http_client=client
        ) as streams:
            async with auth.ClientSession(
                streams[0], streams[1]
            ) as session:
                await session.initialize()

                tools, cursor, seen = {}, None, set()
                while True:
                    if cursor is None:
                        listing = await session.list_tools()
                    else:
                        import inspect
                        if "cursor" in inspect.signature(
                            session.list_tools
                        ).parameters:
                            listing = await session.list_tools(cursor=cursor)
                        else:
                            from mcp.types import PaginatedRequestParams
                            listing = await session.list_tools(
                                params=PaginatedRequestParams(cursor=cursor)
                            )
                    envelope = listing.model_dump(mode="json", by_alias=True)
                    for tool in envelope.get("tools", []):
                        tools[norm(tool["name"])] = tool
                    cursor = envelope.get(
                        "nextCursor", envelope.get("next_cursor")
                    )
                    if not cursor:
                        break
                    if cursor in seen:
                        raise RuntimeError("Repeated tool cursor")
                    seen.add(cursor)

                async def call(name, args):
                    tool = tools.get(name)
                    if not tool:
                        raise RuntimeError("Missing read tool: " + name)
                    READ_ONLY.add(tool["name"])
                    return await read_tool(session, tool["name"], args)

                def arguments(name, account):
                    tool = tools.get(name)
                    if not tool:
                        raise RuntimeError("Missing tool: " + name)
                    schema = tool.get(
                        "inputSchema", tool.get("input_schema")
                    )
                    if not isinstance(schema, dict):
                        raise RuntimeError("Missing schema")
                    args = {}
                    for key in schema.get("properties", {}):
                        if norm(key) in {
                            "accountnumber", "rhsaccountnumber"
                        }:
                            val = field(account, norm(key))
                            if val is None:
                                raise RuntimeError("Missing account identity")
                            args[key] = val
                    if set(schema.get("required", [])) - set(args):
                        raise RuntimeError("Unsupported required arguments")
                    return args

                async def pages(name, account, key):
                    args = arguments(name, account)
                    result, seen = [], set()
                    for _ in range(100):
                        data = await call(name, args)
                        rows = data.get(key)
                        if not isinstance(rows, list):
                            raise RuntimeError("Invalid paginated response")
                        result.extend(rows)
                        cursor = data.get("next")
                        if not cursor:
                            return result
                        if cursor in seen:
                            raise RuntimeError("Repeated data cursor")
                        seen.add(cursor)
                        args = {**args, "cursor": cursor}
                    raise RuntimeError("History incomplete: pagination limit")

                account_data = await call("getaccounts", {})
                accounts = account_data.get("accounts")
                if not isinstance(accounts, list):
                    raise RuntimeError("Invalid accounts response")

                for bot, required in TARGETS.items():
                    old = snapshot.setdefault("bots", {}).get(bot, {})
                    try:
                        matches = [
                            a for a in accounts
                            if isinstance(a, dict) and any(
                                str(field(a, k) or "") == required
                                for k in (
                                    "accountnumber", "rhsaccountnumber"
                                )
                            )
                        ]
                        if len(matches) != 1:
                            raise RuntimeError("Exact account not resolved")
                        account = matches[0]
                        if field(account, "state") != "active":
                            raise RuntimeError("Account not active")

                        portfolio = await call(
                            "getportfolio",
                            arguments("getportfolio", account)
                        )
                        crypto = bot == "crypto"
                        positions = await pages(
                            "getcryptopositions" if crypto
                            else "getequitypositions",
                            account, "results" if crypto else "positions"
                        )
                        orders = await pages(
                            "getcryptoorders" if crypto else "getequityorders",
                            account, "results" if crypto else "orders"
                        )

                        safe_positions = [
                            pick(p, (
                                "currency", "symbol", "quantity",
                                "quantity_transferable",
                                "shares_available_for_sells",
                                "average_buy_price", "cost_bases"
                            ))
                            for p in positions
                        ]
                        safe_orders = [
                            pick(o, (
                                "id", "symbol", "currency_code", "side",
                                "state", "cumulative_quantity",
                                "average_price", "fee", "fees",
                                "created_at", "last_transaction_at",
                                "net_rounded_executed_notional"
                            ))
                            for o in orders
                        ]
                        snapshot["bots"][bot] = {
                            "status": "READ_OK",
                            "account": "****" + required[-4:],
                            "fetched_at": now(),
                            "attempted_at": snapshot["attempted_at"],
                            "portfolio": pick(portfolio, (
                                "total_value", "equity_value", "crypto_value",
                                "cash", "currency", "buying_power",
                                "crypto_buying_power"
                            )),
                            "positions": safe_positions,
                            "orders": safe_orders,
                        }
                    except Exception as exc:
                        snapshot["bots"][bot] = {
                            **old,
                            "status": "READ_FAILED",
                            "attempted_at": now(),
                            "error": type(exc).__name__,
                        }
                    publish(snapshot)

async def main():
    while True:
        try:
            await asyncio.wait_for(collect(), timeout=240)
        except Exception as exc:
            data = previous()
            data["attempted_at"] = now()
            for bot in TARGETS:
                old = data.setdefault("bots", {}).get(bot, {})
                data["bots"][bot] = {
                    **old,
                    "status": "READ_FAILED",
                    "attempted_at": now(),
                    "error": type(exc).__name__,
                }
            publish(data)
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
