from decimal import Decimal
from urllib.parse import urlparse, parse_qs
from app.brokers.robinhood.crypto import RobinhoodCrypto
a = RobinhoodCrypto()
buy = sell = Decimal(0)
n = 0
path = '/api/v1/crypto/trading/orders/'
params = None
while path and n < 2000:
    r = a.request('GET', path, params)
    for o in r.get('results') or []:
        n += 1
        if o.get('symbol') == 'BTC-USD' and o.get('state') == 'filled':
            q = Decimal(str(o.get('filled_asset_quantity') or 0))
            if o.get('side') == 'buy':
                buy += q
            else:
                sell += q
    nxt = r.get('next')
    if nxt:
        u = urlparse(nxt)
        path = u.path
        params = parse_qs(u.query)
    else:
        path = None
print('orders scanned:', n)
print('filled BTC bought:', buy, ' sold:', sell, ' net:', buy - sell)
print('holdings now:      0.00006062')
print('net + our order:  ', buy - sell + Decimal('0.00001231'))
