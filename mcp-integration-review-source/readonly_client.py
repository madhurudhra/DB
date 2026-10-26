import base64
import os
import time
from urllib.parse import urlencode

import httpx
from nacl.signing import SigningKey


BASE_URL = "https://trading.robinhood.com"


class RobinhoodCryptoReadOnly:
    """Read-only Robinhood Crypto API client. No order methods are provided."""

    def __init__(self):
        self.api_key = os.environ.get("ROBINHOOD_CRYPTO_API_KEY", "").strip()
        private_key = os.environ.get(
            "ROBINHOOD_CRYPTO_PRIVATE_KEY_B64", ""
        ).strip()

        if not self.api_key or not private_key:
            raise RuntimeError(
                "Robinhood Crypto credentials are not configured."
            )

        try:
            seed = base64.b64decode(private_key, validate=True)
            self.signing_key = SigningKey(seed)
        except Exception as exc:
            raise RuntimeError(
                "Robinhood Crypto private-key format is invalid."
            ) from exc

        self.timeout = httpx.Timeout(15.0, connect=5.0)

    def _get(self, path, params=None):
        query = urlencode(params or [], doseq=True)
        signed_path = f"{path}?{query}" if query else path
        timestamp = str(int(time.time()))
        message = (
            f"{self.api_key}{timestamp}{signed_path}GET"
        ).encode("utf-8")
        signature = self.signing_key.sign(message).signature

        headers = {
            "x-api-key": self.api_key,
            "x-signature": base64.b64encode(signature).decode("ascii"),
            "x-timestamp": timestamp,
            "Content-Type": "application/json; charset=utf-8",
        }

        with httpx.Client(
            base_url=BASE_URL,
            timeout=self.timeout,
            follow_redirects=False,
        ) as client:
            response = client.get(
                signed_path,
                headers=headers,
            )
            response.raise_for_status()
            return response.json()

    def get_accounts(self):
        return self._get("/api/v2/crypto/trading/accounts/")

    def get_btc_quote(self):
        return self._get(
            "/api/v2/crypto/marketdata/best_bid_ask/",
            [("symbol", "BTC-USD")],
        )
