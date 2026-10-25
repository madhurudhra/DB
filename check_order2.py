import requests
from app.brokers.robinhood.crypto import RobinhoodCrypto
a = RobinhoodCrypto()
OID = '[REDACTED_LITERAL]'
tests = {'today BTC': {'symbol': 'BTC-USD', 'created_at_start': '2026-10-24T23:00:00Z'}, 'by id filter': {'id': OID}, 'state open': {'state': 'open'}, 'state canceled': {'state': 'canceled'}}
for name, params in tests.items():
    try:
        r = a.request('GET', '/api/v1/crypto/trading/orders/', params=params)
        rows = r.get('results') or []
        print('==', name, '->', len(rows), 'rows; next:', bool(r.get('next')))
        for o in rows[:5]:
            print('  ', o.get('id'), o.get('side'), o.get('state'), o.get('created_at'))
    except requests.HTTPError as e:
        print('==', name, 'FAILED', e.response.status_code, e.response.text[:200])
