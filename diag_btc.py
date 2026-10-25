import uuid, requests
from app.brokers.robinhood.crypto import RobinhoodCrypto
a = RobinhoodCrypto()
print('RECENT ORDERS:')
for o in (a.orders().get('results') or [])[:3]:
    print(' ', o.get('id'), o.get('symbol'), o.get('side'), o.get('state'), o.get('created_at'))
payload = {'client_order_id': str(uuid.uuid4()), 'side': 'buy', 'type': 'market', 'symbol': 'BTC-USD', 'market_order_config': {'quote_amount': '1'}}
try:
    print('UNEXPECTED SUCCESS:', a.request('POST', '/api/v1/crypto/trading/orders/', payload=payload))
except requests.HTTPError as e:
    print('STATUS:', e.response.status_code)
    print('BODY:', e.response.text)
