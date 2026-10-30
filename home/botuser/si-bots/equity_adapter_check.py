import asyncio
import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home()))
import si_mcp_auth as auth

READ_ONLY = {
    "get_accounts",
    "get_portfolio",
    "get_equity_positions",
    "get_equity_tradability",
}

async def refuse_login(*args, **kwargs):
    raise RuntimeError("Interactive authentication required; stopped.")

async def read_tool(session, name, arguments):
    if name not in READ_ONLY:
        raise RuntimeError("Tool is not in the read-only allowlist.")

    parameters = inspect.signature(session.call_tool).parameters
    if "arguments" in parameters:
        result = await session.call_tool(name, arguments=arguments)
    elif "params" in parameters:
        from mcp.types import CallToolRequestParams
        result = await session.call_tool(
            params=CallToolRequestParams(name=name, arguments=arguments)
        )
    else:
        raise RuntimeError("Unsupported call_tool signature; stopped.")

    envelope = result.model_dump(mode="json", by_alias=True)
    if envelope.get("isError") or envelope.get("is_error"):
        raise RuntimeError(f"{name}: broker returned a tool error.")

    structured = envelope.get(
        "structuredContent", envelope.get("structured_content")
    )
    if isinstance(structured, dict):
        payload = structured
    else:
        texts = [
            item["text"] for item in envelope.get("content", [])
            if item.get("type") == "text" and "text" in item
        ]
        payload = json.loads("\n".join(texts))

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise RuntimeError(f"{name}: unexpected response shape; stopped.")
    return payload["data"]

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
        redirect_handler=refuse_login,
        callback_handler=refuse_login,
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
                data = await read_tool(session, "get_accounts", {})
                matches = [
                    a for a in (data.get("accounts") or [])
                    if a and a.get("agentic_allowed") is True
                    and str(a.get("account_number", "")).endswith("5004")
                ]
                if len(matches) != 1:
                    raise RuntimeError("Expected tradable account not resolved.")
                account = matches[0]
                if (
                    account.get("state") != "active"
                    or account.get("deactivated") is not False
                    or account.get("permanently_deactivated") is not False
                ):
                    raise RuntimeError("Account is not verified active.")

                number = account["account_number"]
                portfolio = await read_tool(
                    session, "get_portfolio", {"account_number": number}
                )
                eligibility = await read_tool(
                    session, "get_equity_tradability",
                    {"account_number": number, "symbols": ["IONQ"]},
                )

                positions = []
                cursor = None
                seen = set()
                while True:
                    args = {"account_number": number}
                    if cursor:
                        args["cursor"] = cursor
                    page = await read_tool(session, "get_equity_positions", args)
                    positions.extend(
                        p for p in (page.get("positions") or [])
                        if p and p.get("symbol") == "IONQ"
                    )
                    cursor = page.get("next")
                    if not cursor:
                        break
                    if cursor in seen:
                        raise RuntimeError("Repeated positions cursor.")
                    seen.add(cursor)

                print(json.dumps({
                    "mode": "READ_ONLY",
                    "account": "****5004",
                    "account_type": account.get("brokerage_account_type"),
                    "trading_type": account.get("type"),
                    "currency": portfolio.get("currency"),
                    "buying_power": portfolio.get("buying_power"),
                    "IONQ_tradability": eligibility,
                    "existing_IONQ_positions": positions,
                    "orders_submitted": False,
                }, indent=2))

def report(error):
    children = getattr(error, "exceptions", ())
    if children:
        for child in children:
            report(child)
    else:
        print("STOPPED:", type(error).__name__)
        if isinstance(error, RuntimeError):
            print(str(error))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        report(error)
        sys.exit(1)
