import base64
import json
import os
import time
from urllib.parse import urlencode
import requests
from nacl.signing import SigningKey
BASE_URL = 'https://trading.robinhood.com'

class RobinhoodCrypto:

    def __init__(self):
        self.api_key = os.getenv('ROBINHOOD_CRYPTO_API_KEY', '')
        encoded = os.getenv('ROBINHOOD_CRYPTO_PRIVATE_KEY_B64', '')
        if not self.api_key or not encoded:
            raise RuntimeError('Missing Robinhood Crypto API credentials')
        key = base64.b64decode(encoded, validate=True)
        if len(key) not in (32, 64):
            raise RuntimeError('Invalid Ed25519 private key length')
        self.signer = SigningKey(key[:32])
        self.session = requests.Session()

    def request(self, method, path, params=None, payload=None):
        query = '?' + urlencode(params, doseq=True) if params else ''
        signed_path = path + query
        body = json.dumps(payload, separators=(',', ':')) if payload is not None else ''
        timestamp = str(int(time.time()))
        message = (self.api_key + timestamp + signed_path + method.upper() + body).encode()
        signature = base64.b64encode(self.signer.sign(message).signature).decode()
        response = self.session.request(method.upper(), BASE_URL + signed_path, headers={'x-api-key': self.api_key, 'x-timestamp': timestamp, 'x-signature': signature, 'Content-Type': 'application/json; charset=utf-8'}, data=body if body else None, timeout=(5, 20))
        response.raise_for_status()
        return response.json() if response.content else {}

    def accounts(self):
        return self.request('GET', '/api/v1/crypto/trading/accounts/')

    def holdings(self):
        return self.request('GET', '/api/v1/crypto/trading/holdings/')

    def pairs(self):
        return self.request('GET', '/api/v1/crypto/trading/trading_pairs/')

    def orders(self):
        return self.request('GET', '/api/v1/crypto/trading/orders/')

    def quote(self, symbol):
        return self.request('GET', '/api/v1/crypto/marketdata/best_bid_ask/', {'symbol': symbol})

    def submit_order(self, payload):
        """Submit a validated Robinhood Crypto v1 order."""
        import uuid
        if os.getenv('ROBINHOOD_CRYPTO_WRITE_ENABLED', 'false').strip().lower() != 'true':
            raise RuntimeError('Crypto order submission is disabled')
        if not isinstance(payload, dict):
            raise ValueError('payload must be a JSON object')
        required = {'client_order_id', 'side', 'type', 'symbol'}
        missing = required - payload.keys()
        if missing:
            raise ValueError('Missing required fields: ' + ', '.join(sorted(missing)))
        try:
            uuid.UUID(str(payload['client_order_id']))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError('client_order_id must be a UUID') from exc
        symbol = str(payload['symbol']).upper()
        if not symbol.endswith('-USD') or symbol != payload['symbol']:
            raise ValueError('symbol must be uppercase, e.g. BTC-USD')
        side = str(payload['side']).lower()
        order_type = str(payload['type']).lower()
        if side not in {'buy', 'sell'}:
            raise ValueError('side must be buy or sell')
        if order_type not in {'market', 'limit', 'stop_loss', 'stop_limit'}:
            raise ValueError('Unsupported crypto order type')
        config_key = f'{order_type}_order_config'
        config = payload.get(config_key)
        if not isinstance(config, dict):
            raise ValueError(f'{config_key} is required')
        size_fields = {'asset_quantity', 'quote_amount'} & config.keys()
        if len(size_fields) != 1:
            raise ValueError('Provide exactly one of asset_quantity or quote_amount')
        from decimal import Decimal, InvalidOperation
        try:
            size = Decimal(str(config[next(iter(size_fields))]))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError('Invalid order size') from exc
        if not size.is_finite() or size <= 0:
            raise ValueError('Order size must be finite and positive')
        if order_type in {'limit', 'stop_limit'}:
            if 'limit_price' not in config:
                raise ValueError('limit_price is required')
        if order_type in {'stop_loss', 'stop_limit'}:
            if 'stop_price' not in config:
                raise ValueError('stop_price is required')
        clean = {'client_order_id': str(payload['client_order_id']), 'side': side, 'type': order_type, 'symbol': symbol, config_key: config}
        return self.request('POST', '/api/v1/crypto/trading/orders/', payload=clean)
