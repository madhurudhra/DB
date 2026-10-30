import base64
import json
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from nacl.signing import SigningKey

BASE = "https://trading.robinhood.com"
STORE = Path.home() / ".config/super-intelligence/rh_native_crypto.json"

class NativeCrypto:
    def __init__(self):
        saved = json.loads(STORE.read_text())
        self.api_key = saved["api_key"]
        self.account_number = str(saved["account_number"])
        if not self.account_number.endswith("5576"):
            raise RuntimeError("Saved native account does not match ****5576.")
        seed = base64.b64decode(saved["private_key_b64"], validate=True)
        if len(seed) not in (32, 64):
            raise RuntimeError("Unexpected private-key length.")
        self.signer = SigningKey(seed[:32])

    def get(self, path):
        if not path.startswith("/api/v1/crypto/"):
            raise RuntimeError("Unexpected endpoint; stopped.")
        timestamp = str(int(time.time()))
        message = (self.api_key + timestamp + path + "GET").encode()
        signature = base64.b64encode(
            self.signer.sign(message).signature
        ).decode()
        request = Request(
            BASE + path,
            headers={
                "x-api-key": self.api_key,
                "x-timestamp": timestamp,
                "x-signature": signature,
                "Accept": "application/json",
            },
            method="GET",
        )
        with urlopen(request, timeout=20) as response:
            return json.load(response)

    def verified_account(self):
        account = self.get("/api/v1/crypto/trading/accounts/")
        if (
            str(account.get("account_number")) != self.account_number
            or account.get("status") != "active"
            or account.get("buying_power_currency") != "USD"
        ):
            raise RuntimeError("Native account verification failed.")
        return account

def main():
    client = NativeCrypto()
    account = client.verified_account()
    checks = {
        "account": {
            "account_number": "****5576",
            "status": account["status"],
            "buying_power": account.get("buying_power"),
            "currency": account["buying_power_currency"],
        },
        "BTC_trading_rules": client.get(
            "/api/v1/crypto/trading/trading_pairs/?symbol=BTC-USD"
        ),
        "BTC_quote": client.get(
            "/api/v1/crypto/marketdata/best_bid_ask/?symbol=BTC-USD"
        ),
        "existing_BTC_holdings": client.get(
            "/api/v1/crypto/trading/holdings/?asset_code=BTC"
        ),
        "requested_buy_budget_USD": "1.00",
        "mode": "READ_ONLY",
        "orders_submitted": False,
    }
    output = json.dumps(checks, indent=2)
    print(output.replace(client.account_number, "****5576"))

if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        print("Native API HTTP status:", error.code)
        print("Stopped. No order submission code was invoked.")
        raise SystemExit(1)
    except Exception as error:
        print("Stopped:", type(error).__name__)
        print("No order submission code was invoked.")
        raise SystemExit(1)
