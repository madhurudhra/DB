import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/opt/si-equity-r1")
import broker_monitor as monitor

auth = monitor.auth
TOOL = "get_earnings_results"
SYMBOLS = ("SPCX", "IONQ")
DESTINATION = Path("/var/lib/si-equity-r1/earnings_latest.json")
monitor.ALLOWED.add(TOOL)

def arguments_for(schema, symbol):
    if not isinstance(schema, dict):
        raise RuntimeError("INPUT_SCHEMA_MISSING")
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    symbols = properties.get("symbols", {})
    symbol_field = properties.get("symbol", {})
    kinds = symbols.get("type", [])
    if isinstance(kinds, str):
        kinds = [kinds]

    if (
        "array" in kinds
        and symbols.get("items", {}).get("type") == "string"
    ):
        arguments = {"symbols": [symbol]}
    elif symbol_field.get("type") == "string":
        arguments = {"symbol": symbol}
    else:
        raise RuntimeError("TICKER_ARGUMENT_REQUIRES_EXPLICIT_ADAPTER")

    unresolved = [name for name in required if name not in arguments]
    if unresolved:
        raise RuntimeError(
            "ADDITIONAL_REQUIRED_ARGUMENTS: " + ", ".join(unresolved)
        )
    return arguments

def save(report):
    fd, temporary = tempfile.mkstemp(
        dir=DESTINATION.parent, prefix=".earnings-"
    )
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(report, output, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, DESTINATION)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

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
        redirect_handler=monitor.refuse_login,
        callback_handler=monitor.refuse_login,
    )

    report = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_EARNINGS",
        "tool": TOOL,
        "orders_submitted": False,
        "entry_authorized": False,
        "earnings_contributions": {symbol: None for symbol in SYMBOLS},
        "responses": {},
    }

    async with auth.httpx2.AsyncClient(
        auth=oauth,
        timeout=auth.httpx2.Timeout(30.0, read=120.0),
    ) as client:
        async with auth.streamable_http_client(
            auth.SERVER, http_client=client
        ) as streams:
            async with auth.ClientSession(
                streams[0], streams[1]
            ) as session:
                await session.initialize()
                tools = []
                cursor = None
                seen = set()

                for _ in range(100):
                    page = (
                        await session.list_tools()
                        if cursor is None
                        else await session.list_tools(cursor=cursor)
                    )
                    tools.extend(page.tools)
                    cursor = getattr(page, "nextCursor", None)
                    if not cursor:
                        break
                    if cursor in seen:
                        raise RuntimeError("REPEATED_TOOL_CURSOR")
                    seen.add(cursor)
                else:
                    raise RuntimeError("TOOL_PAGINATION_LIMIT")

                matches = [tool for tool in tools if tool.name == TOOL]
                if len(matches) != 1:
                    raise RuntimeError("EARNINGS_TOOL_NOT_UNIQUE")

                definition = matches[0].model_dump(
                    mode="json", by_alias=True
                )
                contract = {
                    key: definition.get(key)
                    for key in (
                        "name", "description", "inputSchema", "outputSchema"
                    )
                }
                report["contract"] = contract

                print("=== TICKER-SPECIFIC EARNINGS CONTRACT ===")
                print(json.dumps(contract, indent=2, allow_nan=False))

                requests = {
                    symbol: arguments_for(contract["inputSchema"], symbol)
                    for symbol in SYMBOLS
                }
                for symbol in SYMBOLS:
                    data = await monitor.read_tool(
                        session, TOOL, requests[symbol]
                    )
                    result = {
                        "arguments": requests[symbol],
                        "received_at": datetime.now(timezone.utc).isoformat(),
                        "data": data,
                    }
                    report["responses"][symbol] = result
                    print("\n=== EARNINGS RESPONSE:", symbol, "===")
                    print(json.dumps(result, indent=2, allow_nan=False))

    report["state"] = "EARNINGS_DATA_COLLECTED_NOT_YET_SCORED"
    save(report)
    print("\nSaved:", DESTINATION)
    print("No orders submitted. No services or capital settings changed.")
    print("Earnings contributions remain unknown until the data is interpreted.")

if __name__ == "__main__":
    os.umask(0o077)
    try:
        asyncio.run(main())
    except Exception as error:
        print("COLLECTION STOPPED:", type(error).__name__)
        if isinstance(error, RuntimeError):
            print(str(error))
        print("This attempt did not replace the saved report.")
        print("No order authorization was granted.")
        sys.exit(1)
