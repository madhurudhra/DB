import requests
from app.brokers.robinhood.crypto import RobinhoodCrypto
a = RobinhoodCrypto()
OID = '[REDACTED_LITERAL]'
print('LIST (first 5):')
for o in (a.orders().get('results') or [])[:5]:
    print(' ', o.get('id'), o.get('state'), o.get('created_at'))
try:
    o = a.request('GET', '/api/v1/crypto/trading/orders/%s/' % OID)
    print('BY ID:', {k: o.get(k) for k in ('id', 'symbol', 'side', 'state', 'filled_asset_quantity', 'average_price', 'created_at')})
except requests.HTTPError as e:
    print('BY ID FAILED:', e.response.status_code, e.response.text)
