"""Read-only Robinhood v2 evidence collection. Never writes funding/trade state."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, localcontext
from urllib.parse import urlsplit, parse_qsl, urlencode, urljoin
import time
BASE = 'https://trading.robinhood.com'
ROOT = '/api/v2/crypto/'
ALLOWED = {ROOT + 'trading/accounts/', ROOT + 'trading/trading_pairs/', ROOT + 'marketdata/best_bid_ask/', ROOT + 'trading/estimated_price/', ROOT + 'trading/holdings/', ROOT + 'trading/orders/'}

class EvidenceError(Exception):
    pass

def check(condition, code):
    if not condition:
        raise EvidenceError(code)

def number(value, positive=False):
    check(value is not None and (not isinstance(value, bool)), 'MISSING_OR_INVALID_NUMBER')
    try:
        n = Decimal(str(value))
    except Exception:
        raise EvidenceError('MISSING_OR_INVALID_NUMBER') from None
    check(n.is_finite() and n >= 0 and (not positive or n > 0), 'MISSING_OR_INVALID_NUMBER')
    check(n <= Decimal('1000000000000') and n.as_tuple().exponent >= -18, 'UNSUPPORTED_NUMERIC_RANGE')
    return n

def validate_url(url):
    parts = urlsplit(url)
    check(parts.scheme == 'https' and parts.netloc == 'trading.robinhood.com' and (not parts.fragment) and (parts.path in ALLOWED), 'HTTP_DESTINATION_NOT_ALLOWED')
    return parts

def install_get_guard(session_class, http_statuses, maximum=30):
    original = session_class.request
    counter = [0]

    def guarded(self, method, url, *args, **kwargs):
        check(method.upper() == 'GET', 'NON_GET_BLOCKED')
        validate_url(url)
        counter[0] += 1
        check(counter[0] <= maximum, 'HTTP_REQUEST_BUDGET_EXCEEDED')
        kwargs.update(allow_redirects=False, verify=True, timeout=(5, 15))
        response = original(self, method, url, *args, **kwargs)
        http_statuses.append(int(response.status_code))
        return response
    session_class.request = guarded
    return original

class Reader:

    def __init__(self, client):
        self.client = client
        self.receipts = []
        self.calls = 0

    def get(self, path, params=None):
        parts = urlsplit(path)
        check(not parts.scheme and (not parts.netloc) and (not parts.fragment) and (parts.path in ALLOWED), 'API_PATH_NOT_ALLOWED')
        self.calls += 1
        check(self.calls <= 30, 'API_REQUEST_BUDGET_EXCEEDED')
        value = self.client._request('GET', path, params=params)
        check(isinstance(value, dict), 'BROKER_READ_FAILED')
        self.receipts.append({'path': path, 'params': params, 'received_at': datetime.now(timezone.utc).isoformat(), 'response': value})
        return value

    def pages(self, path, params=None):
        values = []
        seen = set()
        current = path
        query = dict(params or {})
        for _ in range(20):
            identity = current + '?' + urlencode(query, doseq=True)
            check(identity not in seen, 'PAGINATION_LOOP')
            seen.add(identity)
            page = self.get(current, query)
            rows = page.get('results')
            check(isinstance(rows, list) and all((isinstance(r, dict) for r in rows)), 'INVALID_RESULTS_ARRAY')
            values.extend(rows)
            next_link = page.get('next')
            if next_link in (None, ''):
                return values
            check(isinstance(next_link, str), 'INVALID_NEXT_LINK')
            parts = validate_url(urljoin(BASE + path, next_link))
            check(parts.path == path, 'PAGINATION_PATH_CHANGED')
            pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=30)
            check(len({k for k, v in pairs}) == len(pairs), 'DUPLICATE_PAGINATION_PARAMETER')
            query = dict(pairs)
            original = dict(params or {})
            check(set(query) <= set(original) | {'cursor', 'limit'}, 'PAGINATION_FILTER_CHANGED')
            for key, value in original.items():
                check(query.get(key, str(value)) == str(value), 'PAGINATION_FILTER_CHANGED')
                query[key] = value
            if params and 'account_number' in params:
                check(query.get('account_number', params['account_number']) == params['account_number'], 'PAGINATION_ACCOUNT_CHANGED')
                query['account_number'] = params['account_number']
            current = parts.path
        raise EvidenceError('PAGINATION_PAGE_LIMIT_EXCEEDED')

def unique(rows, key):
    values = [r.get(key) for r in rows]
    check(all((isinstance(v, str) and v for v in values)) and len(values) == len(set(values)), 'MISSING_OR_DUPLICATE_IDENTITIES')

def collect(reader, configured_account=None, symbol='BTC-USD', hypothetical_usd='5'):
    with localcontext() as context:
        context.prec = 80
        check(symbol == 'BTC-USD', 'THIS_PROBE_IS_BTC_SCHEMA_CHECK_ONLY')
        accounts = reader.pages(ROOT + 'trading/accounts/')
        unique(accounts, 'account_number')
        if configured_account:
            matches = [a for a in accounts if a['account_number'] == configured_account]
            check(len(matches) == 1, 'CONFIGURED_ACCOUNT_NOT_FOUND')
            account = matches[0]
            selection = 'CONFIGURED_ACCOUNT'
        else:
            check(len(accounts) == 1, 'EXPLICIT_ACCOUNT_SELECTION_REQUIRED')
            account = accounts[0]
            selection = 'SINGLE_ACCOUNT_DIAGNOSTIC_ONLY'
        check(account.get('status') == 'active' and account.get('is_api_tradable') is True, 'ACCOUNT_NOT_API_TRADABLE')
        check(account.get('buying_power_currency') == 'USD', 'ACCOUNT_CURRENCY_NOT_USD')
        buying_power = number(account.get('buying_power'))
        account_id = account['account_number']
        pairs = reader.pages(ROOT + 'trading/trading_pairs/', {'symbol': symbol})
        unique(pairs, 'symbol')
        matches = [p for p in pairs if p.get('symbol') == symbol]
        check(len(matches) == 1, 'PAIR_NOT_FOUND')
        pair = matches[0]
        check(pair.get('is_api_tradable') is True and pair.get('quote_code') == 'USD' and (pair.get('asset_code') == 'BTC'), 'PAIR_NOT_TRADABLE')
        increment = number(pair.get('asset_increment'), True)
        number(pair.get('quote_increment'), True)
        maximum = number(pair.get('max_order_size'), True)
        minimum = number(pair.get('min_order_amount'))
        quote = reader.get(ROOT + 'marketdata/best_bid_ask/', {'symbol': symbol})
        matches = [q for q in quote.get('results', []) if isinstance(q, dict) and q.get('symbol') == symbol]
        check(len(matches) == 1, 'QUOTE_NOT_UNIQUE')
        quote = matches[0]
        bid = number(quote.get('bid'), True)
        ask = number(quote.get('ask'), True)
        quote_crossed = bid > ask
        quantity = (number(hypothetical_usd, True) / ask / increment).to_integral_value(rounding=ROUND_FLOOR) * increment
        max_units = (maximum / increment).to_integral_value(rounding=ROUND_CEILING) - 1
        quantity = min(quantity, max_units * increment)
        check(quantity > 0 and quantity < maximum and (quantity * ask >= minimum), 'HYPOTHETICAL_SIZE_OUTSIDE_BROKER_LIMITS')
        estimate = reader.get(ROOT + 'trading/estimated_price/', {'symbol': symbol, 'side': 'ask', 'quantity': format(quantity, 'f')})
        matches = [e for e in estimate.get('results', []) if isinstance(e, dict) and e.get('side') == 'ask' and (number(e.get('quantity')) == quantity)]
        check(len(matches) == 1, 'ESTIMATE_NOT_UNIQUE')
        estimate = matches[0]
        stamp = datetime.fromisoformat(estimate['timestamp'].replace('Z', '+00:00'))
        check(stamp.tzinfo is not None and 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 30, 'ESTIMATE_STALE_OR_FUTURE')
        fee_ratio = number(estimate.get('fee_ratio'))
        check(fee_ratio < 1, 'INVALID_FEE_RATIO')
        fee = number(estimate.get('est_fee'))
        estimated_cost = number(estimate.get('est_total_cost'), True)
        check(estimated_cost >= fee, 'ESTIMATED_COST_INVALID')
        holdings = reader.pages(ROOT + 'trading/holdings/', {'account_number': account_id})
        unique(holdings, 'asset_code')
        for row in holdings:
            check(row.get('account_number') == account_id, 'HOLDING_ACCOUNT_MISMATCH')
            check(number(row.get('quantity_available_for_trading')) <= number(row.get('total_quantity')), 'HOLDING_QUANTITY_INVALID')
        orders = reader.pages(ROOT + 'trading/orders/', {'account_number': account_id})
        unique(orders, 'id')
        missing_fee_count = 0
        pending = 0
        for row in orders:
            check(row.get('account_number') == account_id, 'ORDER_ACCOUNT_MISMATCH')
            check(row.get('state') in {'open', 'pending', 'filled', 'failed', 'canceled'}, 'UNKNOWN_ORDER_STATE')
            check(row.get('side') in {'buy', 'sell'}, 'UNKNOWN_ORDER_SIDE')
            filled = number(row.get('filled_asset_quantity'))
            if filled > 0:
                number(row.get('average_price'), True)
            if row.get('fee_charged') is None:
                missing_fee_count += 1
            else:
                number(row['fee_charged'])
            pending += row['state'] in {'open', 'pending'}
        return {'status': 'LIVE_READ_EVIDENCE_COLLECTED_WITH_WARNINGS' if quote_crossed else 'LIVE_READ_EVIDENCE_COLLECTED', 'checked_at': datetime.now(timezone.utc).isoformat(), 'api_version': 'v2', 'account_ending': account_id[-4:], 'account_selection': selection, 'account_matches_saved_configuration': bool(configured_account), 'account_active_and_api_tradable': True, 'broker_buying_power': str(buying_power), 'broker_buying_power_is_not_bot_funding': True, 'symbol': symbol, 'bid': str(bid), 'ask': str(ask), 'hypothetical_quantity': str(quantity), 'fee_ratio_observed': str(fee_ratio), 'estimated_fee': str(fee), 'estimated_total_cost': str(estimated_cost), 'holding_count': len(holdings), 'order_count_for_query': len(orders), 'open_or_pending_order_count': pending, 'orders_with_unknown_fee': missing_fee_count, 'pages_complete_for_queries': True, 'historical_coverage_since_funding_verified': False, 'funded_bot_capital': None, 'capital_baseline_published': False, 'reconciliation_pass_published': False, 'order_submitted': False, 'automatic_trading_activated': False, 'quote_crossed': quote_crossed, 'quote_sanity_passed': not quote_crossed, 'quote_validation_status': 'QUARANTINED_CROSSED_QUOTE' if quote_crossed else 'PASSED_BASIC_SANITY', 'order_preparation_allowed': False}
