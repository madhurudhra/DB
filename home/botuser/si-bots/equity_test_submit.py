import asyncio
import json
import os
import sqlite3
from decimal import Decimal
from pathlib import Path

import equity_adapter_check as adapter

auth = adapter.auth
DB = Path.home() / "si-bots/test_orders.sqlite3"
KEY = "ionq-1usd-buy-v1"
REF = "415a9e05-b7e4-43c3-a49d-f37c5fa20bb2"
os.umask(0o077)

def connect_db():
    if not DB.is_file():
        raise RuntimeError("Saved plan database missing; stopped.")
    db = sqlite3.connect(DB, timeout=10)
    db.execute("PRAGMA synchronous=FULL")
    return db

def decode_result(envelope):
    payload = envelope.get(
        "structuredContent", envelope.get("structured_content")
    )
    if not isinstance(payload, dict):
        text = "\n".join(
            item["text"] for item in envelope.get("content", [])
            if item.get("type") == "text" and "text" in item
        )
        payload = json.loads(text)
    return payload

async def main():
    with connect_db() as db:
        row = db.execute(
            "SELECT ref_id, parameters, state, review "
            "FROM equity_test_plan WHERE plan_key=?", (KEY,)
        ).fetchone()

    if not row or row[0] != REF or row[2] != "PREPARED":
        raise RuntimeError("Plan absent, different, or already attempted; stopped.")

    params = json.loads(row[1])
    number = params.get("account_number")
    expected = {
        "account_number": number,
        "symbol": "IONQ", "side": "buy", "type": "market",
        "dollar_amount": "1.00",
        "time_in_force": "gfd", "market_hours": "regular_hours",
    }
    if params != expected or not str(number).endswith("5004"):
        raise RuntimeError("Saved parameters differ from approval; stopped.")

    review = json.loads(row[3] or "{}")
    if (
        review.get("order_checks") != {}
        or review.get("customer_approval_required") is not False
        or any(review.get(k) != params[k]
               for k in ("symbol", "side", "type", "dollar_amount"))
    ):
        raise RuntimeError("Saved review differs from approved review; stopped.")

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
                matched = [
                    a for a in (accounts.get("accounts") or [])
                    if a and a.get("account_number") == number
                ]
                if len(matched) != 1:
                    raise RuntimeError("Exact account not found.")
                account = matched[0]
                if (
                    account.get("agentic_allowed") is not True
                    or account.get("state") != "active"
                    or account.get("deactivated") is not False
                    or account.get("permanently_deactivated") is not False
                ):
                    raise RuntimeError("Account eligibility changed; stopped.")

                portfolio = await adapter.read_tool(
                    session, "get_portfolio", {"account_number": number}
                )
                power = portfolio.get("buying_power") or {}
                bp = Decimal(power["buying_power"])
                if (
                    power.get("display_currency") != "USD"
                    or not bp.is_finite()
                    or bp * Decimal("0.90") < Decimal("1.00")
                ):
                    raise RuntimeError("Reserve or currency check failed.")

                with connect_db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("""
                        CREATE TABLE IF NOT EXISTS equity_test_attempt (
                            ref_id TEXT PRIMARY KEY,
                            response TEXT
                        )
                    """)
                    current = db.execute(
                        "SELECT ref_id, parameters, state, review "
                        "FROM equity_test_plan WHERE plan_key=?", (KEY,)
                    ).fetchone()
                    if current != row:
                        raise RuntimeError("Plan changed concurrently; stopped.")
                    db.execute(
                        "INSERT INTO equity_test_attempt(ref_id) VALUES (?)",
                        (REF,),
                    )
                    db.execute(
                        "UPDATE equity_test_plan SET state='SUBMITTING' "
                        "WHERE plan_key=?", (KEY,)
                    )

                print("Attempt recorded. Submitting approved $1 IONQ BUY.",
                      flush=True)
                try:
                    result = await session.call_tool(
                        "place_equity_order",
                        arguments={**params, "ref_id": REF},
                    )
                    envelope = result.model_dump(mode="json", by_alias=True)
                    with connect_db() as db:
                        db.execute(
                            "UPDATE equity_test_attempt SET response=? "
                            "WHERE ref_id=?", (json.dumps(envelope), REF)
                        )
                        db.execute(
                            "UPDATE equity_test_plan SET state='RESPONSE_SAVED' "
                            "WHERE plan_key=?", (KEY,)
                        )

                    payload = decode_result(envelope)
                    data = payload.get("data", {})
                    order = data.get("order")
                    approval = data.get("approval")

                    if envelope.get("isError") or envelope.get("is_error"):
                        print("BROKER ERROR:")
                        print(json.dumps(payload, indent=2).replace(
                            str(number), "****5004"
                        ))
                    elif order:
                        print("ORDER RETURNED:")
                        print(json.dumps({
                            k: order.get(k) for k in (
                                "id", "ref_id", "symbol", "side", "state",
                                "dollar_based_amount", "cumulative_quantity",
                                "average_price", "fees",
                                "time_in_force", "market_hours",
                            )
                        }, indent=2))
                        print("Submission response is not proof of a fill.")
                    elif approval:
                        print("Awaiting Robinhood-app approval; not yet placed.")
                        print("Approval state:", approval.get("state"))
                    else:
                        print("Unrecognized response saved. Reconcile before action.")
                except BaseException:
                    print(
                        "Attempt may have reached broker. DO NOT resubmit, "
                        "delete the journal, or generate a new reference.",
                        flush=True,
                    )
                    raise

    print("No SELL or Crypto order was requested by this component.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        adapter.report(error)
        raise SystemExit(1)
