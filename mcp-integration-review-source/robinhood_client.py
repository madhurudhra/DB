import os
import time
import json
import base64
import ssl
import uuid
import requests

from typing import Any, Dict, Optional
from dotenv import load_dotenv
from nacl.signing import SigningKey
from requests.adapters import HTTPAdapter
from urllib3.util import ssl_ as urllib3_ssl
from urllib.parse import urlencode, urlsplit
import re
from buy_control_store import submission_guard


load_dotenv(override=True)


class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = urllib3_ssl.create_urllib3_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = context
        return super().init_poolmanager(*args, **kwargs)


class RobinhoodCryptoClient:
    def __init__(self):
        self.api_key = (
            os.getenv("RH_CRYPTO_API_KEY")
            or os.getenv("API_KEY")
        )

        private_b64 = (
            os.getenv("RH_CRYPTO_PRIVATE_KEY")
            or os.getenv("BASE64_PRIVATE_KEY")
        )

        if not self.api_key or not private_b64:
            raise ValueError(
                "Missing RH_CRYPTO_API_KEY / RH_CRYPTO_PRIVATE_KEY "
                "in environment or .env"
            )

        private_seed = base64.b64decode(private_b64)
        self.private_key = SigningKey(private_seed)

        self.base_url = "https://trading.robinhood.com"

        self.session = requests.Session()
        self.session.mount("https://", TLSAdapter())

    def _timestamp(self) -> int:
        return int(time.time())

    def _sign(
        self,
        method: str,
        path: str,
        body: str = "",
    ) -> Dict[str, str]:
        timestamp = self._timestamp()
        message = f"{self.api_key}{timestamp}{path}{method}{body}"

        signed = self.private_key.sign(message.encode("utf-8"))
        signature = base64.b64encode(
            signed.signature
        ).decode("utf-8")

        return {
            "x-api-key": self.api_key,
            "x-signature": signature,
            "x-timestamp": str(timestamp),
        }

    def _request(self, method: str, path: str, body: str = "", params: Optional[Dict] = None) -> Any:
        method = method.upper()
        if method not in ("GET", "POST"):
            raise ValueError("Unsupported method")
        full_path = path
        if params:
            full_path += ("&" if "?" in path else "?") + urlencode(params, doseq=True)
        # BUY_BOUNDARY_HARDENING_V2: direct client calls must obey the same policy.
        order_creation = False
        order_side = None
        if method == "POST":
            if os.getenv("RH_ALLOW_LIVE_ORDERS", "").lower() != "true":
                raise RuntimeError("Live crypto POST is not authorized")
            route = urlsplit(path).path
            order_creation = route == "/api/v2/crypto/trading/orders/"
            cancellation = re.fullmatch(r"/api/v2/crypto/trading/orders/[A-Za-z0-9_-]+/cancel/", route)
            if not order_creation and not cancellation:
                raise ValueError("Unsupported crypto POST route")
            if order_creation:
                try:
                    order_payload = json.loads(body)
                except (TypeError, ValueError):
                    raise ValueError("Invalid crypto order JSON") from None
                if not isinstance(order_payload, dict) or order_payload.get("side") not in ("buy", "sell"):
                    raise ValueError("Explicit buy or sell side required")
                order_side = order_payload["side"].upper()
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            headers = self._sign(method, full_path, body)
            headers["Accept"] = "application/json"
            try:
                if method == "GET":
                    resp = self.session.get(self.base_url + full_path, headers=headers, timeout=(5, 15), allow_redirects=False)
                else:
                    headers["Content-Type"] = "application/json"
                    if order_creation:
                        with submission_guard('crypto', order_side):
                            resp = self.session.post(self.base_url + full_path, headers=headers, data=body.encode("utf-8") if body else None, timeout=(5, 15), allow_redirects=False)
                    else:
                        # Cancellation is not a new BUY; retain existing live authorization.
                        resp = self.session.post(self.base_url + full_path, headers=headers, data=body.encode("utf-8") if body else None, timeout=(5, 15), allow_redirects=False)
                status = resp.status_code
                if method == "GET" and status in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                    delay = 2 ** (attempt + 1)
                    if status == 429:
                        value = resp.headers.get("Retry-After", "")
                        if value.isdigit():
                            delay = max(delay, min(30, int(value)))
                    print("API GET RETRY", "status", status, "attempt", attempt + 1, flush=True)
                    resp.close()
                    time.sleep(delay)
                    continue
                if not 200 <= status < 300:
                    print("API REQUEST FAILED", method, "status", status, flush=True)
                    resp.close()
                    return None
                try:
                    return resp.json() if resp.content else {}
                finally:
                    resp.close()
            except (requests.Timeout, requests.ConnectionError) as exc:
                if isinstance(exc, requests.exceptions.SSLError):
                    print("API TLS VALIDATION FAILED", flush=True)
                    return None
                if method == "GET" and attempt + 1 < attempts:
                    print("API GET RETRY", type(exc).__name__, "attempt", attempt + 1, flush=True)
                    time.sleep(2 ** (attempt + 1))
                    continue
                print("API REQUEST FAILED", method, type(exc).__name__, flush=True)
                return None
            except requests.RequestException as exc:
                print("API REQUEST FAILED", method, type(exc).__name__, flush=True)
                return None
        return None

    def get_accounts(self) -> Any:
        return self._request(
            "GET",
            "/api/v2/crypto/trading/accounts/",
        )

    def get_account_number(self) -> Optional[str]:
        data = self.get_accounts()

        if not data or not isinstance(data, dict):
            return None

        if "account_number" in data:
            return data["account_number"]

        results = data.get("results")

        if isinstance(results, list) and results:
            first_account = results[0]

            if isinstance(first_account, dict):
                return first_account.get("account_number")

        return None

    def get_holdings(
        self,
        account_number: str,
        *asset_codes: str,
    ) -> Any:
        params = {"account_number": account_number}

        if asset_codes:
            params["asset_code"] = list(asset_codes)

        return self._request(
            "GET",
            "/api/v2/crypto/trading/holdings/",
            params=params,
        )

    def get_orders(
        self,
        account_number: str,
        **filters,
    ) -> Any:
        params = {
            "account_number": account_number,
            **filters,
        }

        return self._request(
            "GET",
            "/api/v2/crypto/trading/orders/",
            params=params,
        )

    def place_order(
        self,
        account_number: str,
        side: str,
        order_type: str,
        symbol: str,
        order_config: Dict[str, Any],
        client_order_id: Optional[str] = None,
    ) -> Any:
        # Safety guard: live orders are disabled unless explicitly enabled.
        if os.getenv("RH_ALLOW_LIVE_ORDERS", "").lower() != "true":
            raise RuntimeError(
                "Live order placement is disabled. "
                "This client is connected to Robinhood's trading API, "
                "not a local paper-trading simulator."
            )

        side = side.lower()
        order_type = order_type.lower()
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if order_type not in ("market", "limit", "stop_loss", "stop_limit"):
            raise ValueError("Unsupported order type")

        if not account_number:
            raise ValueError("account_number is required")

        if not client_order_id:
            client_order_id = str(uuid.uuid4())

        body_dict = {
            "client_order_id": client_order_id,
            "side": side,
            "type": order_type,
            "symbol": symbol.upper(),
            f"{order_type}_order_config": order_config,
        }

        body = json.dumps(
            body_dict,
            separators=(",", ":"),
        )

        params = {"account_number": account_number}

        return self._request(
            "POST",
            "/api/v2/crypto/trading/orders/",
            body=body,
            params=params,
        )

    def cancel_order(self, order_id: str) -> Any:
        if os.getenv("RH_ALLOW_LIVE_ORDERS", "").lower() != "true":
            raise RuntimeError(
                "Live order cancellation is disabled."
            )

        return self._request(
            "POST",
            f"/api/v2/crypto/trading/orders/{order_id}/cancel/",
        )

    def get_best_bid_ask(self, *symbols: str) -> Any:
        params = {"symbol": list(symbols)} if symbols else None

        return self._request(
            "GET",
            "/api/v2/crypto/marketdata/best_bid_ask/",
            params=params,
        )

    def get_trading_pairs(self, *symbols: str) -> Any:
        params = {"symbol": list(symbols)} if symbols else None

        return self._request(
            "GET",
            "/api/v2/crypto/trading/trading_pairs/",
            params=params,
        )


if __name__ == "__main__":
    client = RobinhoodCryptoClient()

    account = client.get_account_number()
    print(
        "Account authentication:",
        "PASS" if account else "FAIL",
    )

    quotes = client.get_best_bid_ask("BTC-USD")
    print(
        "Query-string authentication:",
        "PASS" if quotes is not None else "FAIL",
    )
    print("Quote response:", quotes)

# VERIFIED_COMPATIBILITY_PATCH_V1
