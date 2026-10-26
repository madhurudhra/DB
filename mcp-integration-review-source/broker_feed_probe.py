#!/usr/bin/env python3
import json
import os
import subprocess

WORKER = '\nimport contextlib, io, json, os, sys\nfrom datetime import datetime, timezone\nresult = {"broker": BROKER, "http_statuses": []}\nstep = "imports"\ntry:\n    import requests\n    original = requests.sessions.Session.request\n    def guarded(self, method, url, *args, **kwargs):\n        if method.upper() != "GET":\n            raise RuntimeError("NON_GET_BLOCKED")\n        kwargs["allow_redirects"] = False\n        response = original(self, method, url, *args, **kwargs)\n        result["http_statuses"].append(response.status_code)\n        return response\n    requests.sessions.Session.request = guarded\n    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):\n        if BROKER == "crypto":\n            step = "import_crypto_client"\n            root = "/home/ubuntu/crypto-bot/paper-trend-500"\n            os.chdir(root)\n            sys.path.insert(0, root)\n            from robinhood_client import RobinhoodCryptoClient\n            step = "load_crypto_credentials"\n            client = RobinhoodCryptoClient()\n            step = "fetch_crypto_accounts"\n            accounts = client.get_accounts()\n            if not isinstance(accounts, dict):\n                raise RuntimeError("INVALID_ACCOUNTS")\n            rows = accounts.get("results")\n            if rows is None and accounts.get("account_number"):\n                rows = [accounts]\n            if not isinstance(rows, list) or len(rows) != 1:\n                step = "select_exactly_one_crypto_account"\n                raise RuntimeError("ACCOUNT_SELECTION_REQUIRED")\n            account = rows[0]\n            if not isinstance(account, dict) or not account.get("account_number"):\n                raise RuntimeError("ACCOUNT_NUMBER_MISSING")\n            step = "fetch_crypto_holdings"\n            holdings = client.get_holdings(account["account_number"])\n            if not isinstance(holdings, dict) or not isinstance(holdings.get("results"), list):\n                raise RuntimeError("INVALID_HOLDINGS")\n            result["holding_count_first_page"] = len(holdings["results"])\n            result["more_pages"] = bool(holdings.get("next"))\n        else:\n            step = "import_kite_dependencies"\n            from dotenv import dotenv_values\n            from kiteconnect import KiteConnect\n            step = "load_kite_credentials"\n            env = dotenv_values("/opt/zerodha-bot/.env", interpolate=False)\n            keys = {(env.get(k) or "").strip() for k in ("KITE_API_KEY", "ZERODHA_API_KEY")} - {""}\n            result["api_key_aliases_consistent"] = len(keys) == 1\n            token = (env.get("KITE_ACCESS_TOKEN") or "").strip()\n            result["access_token_present"] = bool(token)\n            if len(keys) != 1 or not token:\n                raise RuntimeError("CREDENTIAL_CONFIGURATION")\n            kite = KiteConnect(api_key=keys.pop(), timeout=10)\n            kite.set_access_token(token)\n            for name in ("margins", "positions", "holdings"):\n                step = "fetch_kite_" + name\n                data = getattr(kite, name)()\n                if name == "margins" and not isinstance(data, dict):\n                    raise RuntimeError("INVALID_MARGINS")\n                if name == "positions" and (not isinstance(data, dict) or not isinstance(data.get("net"), list)):\n                    raise RuntimeError("INVALID_POSITIONS")\n                if name == "holdings" and not isinstance(data, list):\n                    raise RuntimeError("INVALID_HOLDINGS")\n    result["ok"] = True\nexcept Exception as exc:\n    result["ok"] = False\n    result["error_type"] = type(exc).__name__\n    if isinstance(exc, ModuleNotFoundError):\n        result["missing_module"] = exc.name\n    code = getattr(exc, "code", None)\n    if isinstance(code, int):\n        result["broker_http_code"] = code\n    safe = {"NON_GET_BLOCKED", "INVALID_ACCOUNTS", "ACCOUNT_SELECTION_REQUIRED", "ACCOUNT_NUMBER_MISSING", "INVALID_HOLDINGS", "CREDENTIAL_CONFIGURATION", "INVALID_MARGINS", "INVALID_POSITIONS"}\n    if str(exc) in safe:\n        result["reason"] = str(exc)\nresult["last_step"] = step\nresult["checked_at"] = datetime.now(timezone.utc).isoformat()\nprint(json.dumps(result, indent=2))\n'

def main():
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo python3 broker_feed_probe.py")
    probes = [
        ("crypto", ["sudo", "-H", "-u", "ubuntu", "/home/ubuntu/crypto-bot/paper-trend-500/venv/bin/python", "-"]),
        ("zerodha", ["/opt/zerodha-bot/venv/bin/python", "-"]),
    ]
    for broker, command in probes:
        print("\n=== " + broker.upper() + " ===", flush=True)
        try:
            answer = subprocess.run(command, input="BROKER = " + repr(broker) + "\n" + WORKER, text=True, capture_output=True, timeout=100)
            if answer.returncode == 0:
                try:
                    print(json.dumps(json.loads(answer.stdout), indent=2))
                except ValueError:
                    print("Probe output invalid; raw output withheld.")
            else:
                print(json.dumps({"ok": False, "worker_exit_code": answer.returncode, "reason": "Worker did not complete; raw output withheld."}))
        except subprocess.TimeoutExpired:
            print(json.dumps({"ok": False, "reason": "TIMEOUT"}))
        except OSError as exc:
            print(json.dumps({"ok": False, "error_type": type(exc).__name__}))

if __name__ == "__main__":
    main()
