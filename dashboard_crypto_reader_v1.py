"""Account-scoped, GET-only Crypto reader for the dashboard."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qsl, urlparse
from app.brokers.robinhood.crypto import RobinhoodCrypto
from app.core.config import get_settings

class DashboardCryptoReader:
    MAX_PAGES = 20
    MAX_RECORDS = 10000

    @staticmethod
    def _read_config():
        path = Path(get_settings().database_path).resolve()
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.execute('PRAGMA query_only=ON')
            row = db.execute('SELECT revision,config_json FROM independent_bot_configurations WHERE bot=?', ('crypto',)).fetchone()
        if row is None:
            raise ValueError('Crypto configuration missing')
        return tuple(row)

    def __init__(self):
        self._configuration = self._read_config()
        config = json.loads(self._configuration[1])
        self._account = config.get('account_ref')
        if not isinstance(self._account, str) or not self._account or self._account != self._account.strip():
            raise ValueError('Valid configured Crypto account required')
        self._client = RobinhoodCrypto()
        self.session = self._client.session
        self.session.max_redirects = 0
        self._verified = False

    def _assert_config(self):
        if self._read_config() != self._configuration:
            raise ValueError('Crypto configuration changed during read')

    def _collect(self, endpoint, scoped=False):
        allowed = {'/api/v2/crypto/trading/accounts/', '/api/v2/crypto/trading/holdings/', '/api/v2/crypto/trading/orders/'}
        if endpoint not in allowed:
            raise ValueError('Unsupported dashboard endpoint')
        self._assert_config()
        params = [('account_number', self._account)] if scoped else []
        records = []
        visited = set()
        for _ in range(self.MAX_PAGES):
            key = (endpoint, tuple(params))
            if key in visited:
                raise ValueError('Pagination loop')
            visited.add(key)
            response = self._client.request('GET', endpoint, params=params)
            if not isinstance(response, dict) or not isinstance(response.get('results'), list) or any((not isinstance(item, dict) for item in response['results'])):
                raise ValueError('Invalid broker response')
            batch = response['results']
            if scoped and any((item.get('account_number') != self._account for item in batch)):
                raise ValueError('Broker account scope mismatch')
            records.extend(batch)
            if len(records) > self.MAX_RECORDS:
                raise ValueError('Dashboard record limit reached')
            next_link = response.get('next')
            if not next_link:
                self._assert_config()
                return {'results': records, 'next': None, 'previous': None}
            if not isinstance(next_link, str):
                raise ValueError('Invalid pagination link')
            parsed = urlparse(next_link)
            if parsed.scheme or parsed.netloc:
                if parsed.scheme != 'https' or parsed.hostname != 'trading.robinhood.com' or parsed.port not in (None, 443) or parsed.username or parsed.password:
                    raise ValueError('Untrusted pagination origin')
            if parsed.path != endpoint or parsed.fragment:
                raise ValueError('Pagination endpoint changed')
            params = parse_qsl(parsed.query, keep_blank_values=True)
            refs = [value for key, value in params if key == 'account_number']
            if scoped:
                if refs and refs != [self._account]:
                    raise ValueError('Pagination account scope changed')
                if not refs:
                    params.append(('account_number', self._account))
            elif refs:
                raise ValueError('Unexpected account-list filter')
        raise ValueError('Pagination limit reached')

    def accounts(self):
        self._verified = False
        records = self._collect('/api/v2/crypto/trading/accounts/')['results']
        selected = [item for item in records if item.get('account_number') == self._account]
        if len(selected) != 1:
            raise ValueError('Configured account missing or ambiguous')
        if selected[0].get('is_api_tradable') is not True:
            raise ValueError('Configured account not API-tradable')
        self._verified = True
        return selected[0]

    def _scoped(self, resource):
        if not self._verified:
            self.accounts()
        return self._collect(f'/api/v2/crypto/trading/{resource}/', scoped=True)

    def holdings(self):
        return self._scoped('holdings')

    def orders(self):
        return self._scoped('orders')

    def pairs(self):
        self._assert_config()
        result = self._client.request('GET', '/api/v1/crypto/trading/trading_pairs/')
        self._assert_config()
        return result
