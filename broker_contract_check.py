import asyncio
import json

def structure(value, depth=0):
    if depth > 7:
        return type(value).__name__
    if isinstance(value, dict):
        return {
            str(key): structure(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        samples = []
        seen = set()
        for item in value[:10]:
            shape = structure(item, depth + 1)
            signature = json.dumps(shape, sort_keys=True)
            if signature not in seen:
                seen.add(signature)
                samples.append(shape)
        return {
            "container": "list",
            "empty": not bool(value),
            "sample_structures": samples,
        }
    return "null" if value is None else type(value).__name__

def display(label, value):
    print("\n===== " + label + " =====", flush=True)
    print(json.dumps(structure(value), indent=2), flush=True)

def check(label, operation):
    try:
        display(label, operation())
    except Exception as exc:
        print(
            "\n===== " + label + " =====\nERROR_TYPE: "
            + type(exc).__name__,
            flush=True,
        )

async def main():
    from rh_equity_review import call, LAST4

    try:
        response = json.loads(await call("get_accounts", {}))
        accounts = response["data"]["accounts"]
        selected = [
            account for account in accounts
            if account.get("account_number", "").endswith(LAST4)
            and account.get("agentic_allowed") is True
        ]
        if len(selected) != 1:
            raise RuntimeError("Account selection is not unique")
        account = selected[0]

        requests = [
            (
                "MCP equity positions",
                "get_equity_positions",
                {"account_number": account["account_number"]},
            ),
            (
                "MCP equity orders",
                "get_equity_orders",
                {"account_number": account["account_number"]},
            ),
            (
                "MCP crypto positions",
                "get_crypto_positions",
                {"rhs_account_number": account["rhs_account_number"]},
            ),
            (
                "MCP crypto orders",
                "get_crypto_orders",
                {"rhs_account_number": account["rhs_account_number"]},
            ),
        ]
        for label, tool, arguments in requests:
            try:
                display(label, json.loads(await call(tool, arguments)))
            except Exception as exc:
                print(
                    "\n===== " + label + " =====\nERROR_TYPE: "
                    + type(exc).__name__,
                    flush=True,
                )
    except Exception as exc:
        print("MCP_CHECK_FAILED:", type(exc).__name__, flush=True)

    try:
        from app.brokers.robinhood.crypto import RobinhoodCrypto
        crypto = RobinhoodCrypto()
        for label, operation in [
            ("Direct crypto accounts", crypto.accounts),
            ("Direct crypto holdings", crypto.holdings),
            ("Direct crypto orders", crypto.orders),
        ]:
            check(label, operation)
    except Exception as exc:
        print("DIRECT_CRYPTO_INIT_FAILED:", type(exc).__name__, flush=True)

    try:
        from app.brokers.zerodha.kite import ZerodhaKite
        kite = ZerodhaKite()
        for label, operation in [
            ("Zerodha holdings", kite.holdings),
            ("Zerodha positions", kite.positions),
            ("Zerodha margins", kite.margins),
            ("Zerodha orders", kite.orders),
            ("Zerodha trades", kite.trades),
        ]:
            check(label, operation)
    except Exception as exc:
        print("ZERODHA_INIT_FAILED:", type(exc).__name__, flush=True)

    print(
        "\nREAD_ONLY_CONTRACT_CHECK_COMPLETE. "
        "No orders or settings changed.",
        flush=True,
    )

asyncio.run(main())
