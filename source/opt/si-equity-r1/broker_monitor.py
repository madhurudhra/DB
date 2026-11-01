import argparse
import asyncio
import inspect
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/home/botuser")
import si_mcp_auth as auth

ROOT = Path("/var/lib/si-equity-r1")
ALLOWED = {
    "get_accounts", "get_portfolio",
    "get_equity_positions", "get_equity_orders",
}
TERMINAL = {
    "filled", "cancelled", "rejected", "failed",
    "voided", "partially_filled_rest_cancelled",
}

def now():
    return datetime.now(timezone.utc).isoformat()

def database():
    db = sqlite3.connect(ROOT / "monitor.sqlite3", timeout=10)
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots(
            id INTEGER PRIMARY KEY,
            at TEXT NOT NULL,
            summary TEXT NOT NULL,
            broker_data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS health(
            id INTEGER PRIMARY KEY CHECK(id=1),
            at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
    """)
    db.commit()
    return db

def save_health(db, payload):
    db.execute(
        "INSERT INTO health VALUES(1,?,?) "
        "ON CONFLICT(id) DO UPDATE SET at=excluded.at,payload=excluded.payload",
        (now(), json.dumps(payload, allow_nan=False)),
    )
    db.commit()

async def refuse_login(*args, **kwargs):
    raise RuntimeError("INTERACTIVE_AUTHENTICATION_REQUIRED")

async def read_tool(session, name, arguments):
    if name not in ALLOWED:
        raise RuntimeError("NON_READ_ONLY_TOOL_REFUSED")
    params = inspect.signature(session.call_tool).parameters
    if "arguments" in params:
        result = await session.call_tool(name, arguments=arguments)
    elif "params" in params:
        from mcp.types import CallToolRequestParams
        result = await session.call_tool(
            params=CallToolRequestParams(name=name, arguments=arguments)
        )
    else:
        raise RuntimeError("UNSUPPORTED_MCP_CALL_SIGNATURE")

    envelope = result.model_dump(mode="json", by_alias=True)
    if envelope.get("isError") or envelope.get("is_error"):
        raise RuntimeError("BROKER_TOOL_ERROR")

    payload = envelope.get(
        "structuredContent", envelope.get("structured_content")
    )
    if payload is None:
        texts = [
            item["text"] for item in envelope.get("content", [])
            if item.get("type") == "text" and "text" in item
        ]
        payload = json.loads("\n".join(texts))

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise RuntimeError("UNEXPECTED_BROKER_RESPONSE")
    return payload["data"]

async def pages(session, name, account, field):
    collected = []
    seen = set()
    cursor = None
    for _ in range(100):
        args = {"account_number": account}
        if cursor:
            args["cursor"] = cursor
        data = await read_tool(session, name, args)
        rows = data.get(field)
        if rows is not None and not isinstance(rows, list):
            raise RuntimeError("INVALID_PAGINATED_RESPONSE")
        for row in rows or []:
            if row is not None:
                if not isinstance(row, dict):
                    raise RuntimeError("INVALID_BROKER_ROW")
                collected.append(row)
        cursor = data.get("next")
        if not cursor:
            return collected
        if not isinstance(cursor, str) or cursor in seen:
            raise RuntimeError("INVALID_OR_REPEATED_CURSOR")
        seen.add(cursor)
    raise RuntimeError("PAGINATION_LIMIT")

async def snapshot(config):
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
                accounts = data.get("accounts")
                if accounts is not None and not isinstance(accounts, list):
                    raise RuntimeError("INVALID_ACCOUNTS_RESPONSE")
                matches = [
                    item for item in accounts or []
                    if isinstance(item, dict)
                    and item.get("agentic_allowed") is True
                    and str(item.get("account_number", "")).endswith(
                        config["account_last4"]
                    )
                ]
                if len(matches) != 1:
                    raise RuntimeError("ACCOUNT_BINDING_NOT_VERIFIED")
                account = matches[0]
                if (
                    account.get("state") != "active"
                    or account.get("deactivated") is not False
                    or account.get("permanently_deactivated") is not False
                ):
                    raise RuntimeError("ACCOUNT_NOT_ACTIVE")

                number = account["account_number"]
                portfolio = await read_tool(
                    session, "get_portfolio", {"account_number": number}
                )
                positions = await pages(
                    session, "get_equity_positions", number, "positions"
                )
                orders = await pages(
                    session, "get_equity_orders", number, "orders"
                )
                return portfolio, positions, orders

async def run():
    config = json.loads((ROOT / "config.json").read_text())
    if (
        config.get("mode") != "READ_ONLY_BROKER_MONITOR"
        or config.get("entries_enabled") is not False
    ):
        raise RuntimeError("READ_ONLY_CONFIGURATION_REQUIRED")

    db = database()
    try:
        while True:
            save_health(db, {
                "state": "COLLECTING",
                "mode": "READ_ONLY",
                "entries_enabled": False,
                "exit_protection_implemented": False,
            })
            try:
                portfolio, positions, orders = await asyncio.wait_for(
                    snapshot(config), timeout=180
                )
                buying_power = portfolio.get("buying_power")
                summary = {
                    "state": "BROKER_READ_VERIFIED",
                    "checked_at": now(),
                    "mode": "READ_ONLY",
                    "account": "****" + config["account_last4"],
                    "currency": portfolio.get("currency"),
                    "buying_power": buying_power,
                    "capital_snapshot": config["capital_snapshot"],
                    "capital_snapshot_is_live_authorization": False,
                    "broker_position_rows": len(positions),
                    "broker_order_rows": len(orders),
                    "nonterminal_order_rows": sum(
                        item.get("state") not in TERMINAL for item in orders
                    ),
                    "entries_enabled": False,
                    "positions_adopted": False,
                    "order_submission_implemented": False,
                    "exit_protection_implemented": False,
                    "master_strategy_implemented": False,
                }
                db.execute(
                    "INSERT INTO snapshots(at,summary,broker_data) VALUES(?,?,?)",
                    (
                        now(), json.dumps(summary, allow_nan=False),
                        json.dumps({
                            "portfolio": portfolio,
                            "positions": positions,
                            "orders": orders,
                        }, allow_nan=False),
                    ),
                )
                db.execute(
                    "DELETE FROM snapshots WHERE id NOT IN "
                    "(SELECT id FROM snapshots ORDER BY id DESC LIMIT 100)"
                )
                db.commit()
                save_health(db, summary)
                print(json.dumps(summary, allow_nan=False), flush=True)
            except Exception as error:
                failure = {
                    "state": "BROKER_READ_FAILED",
                    "checked_at": now(),
                    "error_type": type(error).__name__,
                    "entries_enabled": False,
                    "order_submission_implemented": False,
                    "exit_protection_implemented": False,
                }
                save_health(db, failure)
                print(json.dumps(failure), flush=True)
            await asyncio.sleep(30)
    finally:
        db.close()

def status():
    path = ROOT / "monitor.sqlite3"
    if not path.is_file():
        print(json.dumps({"state": "NO_MONITOR_DATABASE"}))
        return
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT at,payload FROM health WHERE id=1").fetchone()
        latest = db.execute(
            "SELECT at,summary FROM snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
    print(json.dumps({
        "current_health": (
            {"at": row[0], "payload": json.loads(row[1])} if row else None
        ),
        "last_successful_snapshot": (
            {"at": latest[0], "payload": json.loads(latest[1])}
            if latest else None
        ),
    }, indent=2))

if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["run", "status"])
    args = parser.parse_args()
    if args.action == "run":
        asyncio.run(run())
    else:
        status()
