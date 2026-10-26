import copy
from datetime import datetime, timezone, timedelta
import importlib.util
from pathlib import Path
import unittest
spec = importlib.util.spec_from_file_location('crypto_live_evidence', Path(__file__).resolve().parents[1] / 'crypto_live_evidence_v2.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class Client:

    def __init__(self):
        self.calls = []
        r = m.ROOT
        self.responses = {r + 'trading/accounts/': {'results': [{'account_number': 'TEST-1234', 'status': 'active', 'is_api_tradable': True, 'buying_power_currency': 'USD', 'buying_power': '100'}]}, r + 'trading/trading_pairs/': {'results': [{'symbol': 'BTC-USD', 'asset_code': 'BTC', 'quote_code': 'USD', 'is_api_tradable': True, 'asset_increment': '0.001', 'quote_increment': '0.01', 'max_order_size': '100', 'min_order_amount': '1'}]}, r + 'marketdata/best_bid_ask/': {'results': [{'symbol': 'BTC-USD', 'bid': '99', 'ask': '100'}]}, r + 'trading/estimated_price/': {'results': [{'side': 'ask', 'quantity': '0.05', 'timestamp': datetime.now(timezone.utc).isoformat(), 'fee_ratio': '0.01', 'est_fee': '0.05', 'est_total_cost': '5.05'}]}, r + 'trading/holdings/': {'results': [{'account_number': 'TEST-1234', 'asset_code': 'BTC', 'total_quantity': '1', 'quantity_available_for_trading': '0.5'}]}, r + 'trading/orders/': {'results': [{'id': 'ORDER-1', 'account_number': 'TEST-1234', 'side': 'buy', 'state': 'open', 'filled_asset_quantity': '0', 'average_price': None, 'fee_charged': '0'}]}}

    def _request(self, method, path, params=None):
        self.calls.append((method, path, params))
        return copy.deepcopy(self.responses[path])

class LiveEvidenceTests(unittest.TestCase):

    def setUp(self):
        self.client = Client()

    def collect(self, account='TEST-1234'):
        return m.collect(m.Reader(self.client), account)

    def test_valid_reads_only(self):
        data = self.collect()
        self.assertEqual(data['status'], 'LIVE_READ_EVIDENCE_COLLECTED')
        self.assertTrue(all((c[0] == 'GET' for c in self.client.calls)))
        self.assertEqual(len(self.client.calls), 6)

    def test_never_claims_funding_or_activation(self):
        data = self.collect()
        self.assertIsNone(data['funded_bot_capital'])
        self.assertFalse(data['capital_baseline_published'])
        self.assertFalse(data['order_submitted'])
        self.assertFalse(data['automatic_trading_activated'])

    def test_wrong_configured_account(self):
        with self.assertRaises(m.EvidenceError):
            self.collect('WRONG')

    def test_unconfigured_single_account_is_diagnostic(self):
        data = self.collect(None)
        self.assertFalse(data['account_matches_saved_configuration'])
        self.assertEqual(data['account_selection'], 'SINGLE_ACCOUNT_DIAGNOSTIC_ONLY')

    def test_multiple_accounts_require_selection(self):
        row = copy.deepcopy(self.client.responses[m.ROOT + 'trading/accounts/']['results'][0])
        row['account_number'] = 'OTHER'
        self.client.responses[m.ROOT + 'trading/accounts/']['results'].append(row)
        with self.assertRaises(m.EvidenceError):
            self.collect(None)

    def test_api_tradability_required(self):
        self.client.responses[m.ROOT + 'trading/accounts/']['results'][0]['is_api_tradable'] = False
        with self.assertRaises(m.EvidenceError):
            self.collect()

    def test_pair_not_tradable(self):
        self.client.responses[m.ROOT + 'trading/trading_pairs/']['results'][0]['is_api_tradable'] = False
        with self.assertRaises(m.EvidenceError):
            self.collect()

    def test_crossed_quote(self):
        self.client.responses[m.ROOT + 'marketdata/best_bid_ask/']['results'][0]['bid'] = '101'
        data = self.collect()
        self.assertEqual(data['status'], 'LIVE_READ_EVIDENCE_COLLECTED_WITH_WARNINGS')
        self.assertTrue(data['quote_crossed'])
        self.assertFalse(data['quote_sanity_passed'])
        self.assertEqual(data['quote_validation_status'], 'QUARANTINED_CROSSED_QUOTE')
        self.assertEqual(data['bid'], '101')
        self.assertEqual(data['ask'], '100')
        self.assertEqual(data['holding_count'], 1)
        self.assertEqual(data['order_count_for_query'], 1)
        self.assertFalse(data['order_preparation_allowed'])
        self.assertFalse(data['capital_baseline_published'])
        self.assertFalse(data['order_submitted'])
        self.assertFalse(data['automatic_trading_activated'])

    def test_stale_estimate(self):
        self.client.responses[m.ROOT + 'trading/estimated_price/']['results'][0]['timestamp'] = (datetime.now(timezone.utc) - timedelta(seconds=40)).isoformat()
        with self.assertRaises(m.EvidenceError):
            self.collect()

    def test_holdings_account_mismatch(self):
        self.client.responses[m.ROOT + 'trading/holdings/']['results'][0]['account_number'] = 'OTHER'
        with self.assertRaises(m.EvidenceError):
            self.collect()

    def test_order_account_mismatch(self):
        self.client.responses[m.ROOT + 'trading/orders/']['results'][0]['account_number'] = 'OTHER'
        with self.assertRaises(m.EvidenceError):
            self.collect()

    def test_unknown_fees_not_assumed_zero(self):
        self.client.responses[m.ROOT + 'trading/orders/']['results'][0]['fee_charged'] = None
        self.assertEqual(self.collect()['orders_with_unknown_fee'], 1)

    def test_pagination_cross_host_blocked(self):
        self.client.responses[m.ROOT + 'trading/accounts/']['next'] = 'https://example.com/api/v2/crypto/trading/accounts/?cursor=x'
        with self.assertRaises(m.EvidenceError):
            self.collect()
        self.assertEqual(len(self.client.calls), 1)

    def test_pagination_cross_path_blocked(self):
        self.client.responses[m.ROOT + 'trading/accounts/']['next'] = m.BASE + m.ROOT + 'trading/orders/?cursor=x'
        with self.assertRaises(m.EvidenceError):
            self.collect()

    def test_pagination_changed_filter_blocked(self):
        self.client.responses[m.ROOT + 'trading/trading_pairs/']['next'] = m.BASE + m.ROOT + 'trading/trading_pairs/?symbol=OTHER&cursor=x'
        with self.assertRaises(m.EvidenceError):
            self.collect()

    def test_nan_rejected(self):
        with self.assertRaises(m.EvidenceError):
            m.number('NaN')

    def test_guard_blocks_post_before_transport(self):
        calls = []

        class Session:

            def request(self, *args, **kwargs):
                calls.append((args, kwargs))
                return type('R', (), {'status_code': 200})()
        statuses = []
        m.install_get_guard(Session, statuses)
        with self.assertRaises(m.EvidenceError):
            Session().request('POST', m.BASE + m.ROOT + 'trading/orders/')
        self.assertEqual(calls, [])

    def test_guard_blocks_other_host(self):

        class Session:

            def request(self, *a, **k):
                raise AssertionError('should not run')
        m.install_get_guard(Session, [])
        with self.assertRaises(m.EvidenceError):
            Session().request('GET', 'https://example.com/api/v2/crypto/trading/accounts/')

    def test_guard_enforces_tls_and_no_redirect(self):
        seen = []

        class Session:

            def request(self, *a, **k):
                seen.append(k)
                return type('R', (), {'status_code': 200})()
        statuses = []
        m.install_get_guard(Session, statuses)
        Session().request('GET', m.BASE + m.ROOT + 'trading/accounts/', verify=False, allow_redirects=True)
        self.assertTrue(seen[0]['verify'])
        self.assertFalse(seen[0]['allow_redirects'])
        self.assertEqual(statuses, [200])

    def test_guard_request_budget(self):

        class Session:

            def request(self, *a, **k):
                return type('R', (), {'status_code': 200})()
        m.install_get_guard(Session, [], maximum=1)
        Session().request('GET', m.BASE + m.ROOT + 'trading/accounts/')
        with self.assertRaises(m.EvidenceError):
            Session().request('GET', m.BASE + m.ROOT + 'trading/accounts/')
if __name__ == '__main__':
    unittest.main(verbosity=2)
