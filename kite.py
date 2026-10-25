import os
from kiteconnect import KiteConnect

class ZerodhaKite:

    def __init__(self):
        api_key = os.getenv('ZERODHA_API_KEY', '')
        access_token = os.getenv('ZERODHA_ACCESS_TOKEN', '')
        if not api_key or not access_token:
            raise RuntimeError('Missing Zerodha API key or access token')
        self.client = KiteConnect(api_key=api_key)
        self.client.set_access_token(access_token)

    def profile(self):
        return self.client.profile()

    def holdings(self):
        return self.client.holdings()

    def positions(self):
        return self.client.positions()

    def margins(self):
        return self.client.margins()

    def orders(self):
        return self.client.orders()

    def trades(self):
        return self.client.trades()

    def quote(self, *instruments):
        if not instruments:
            raise ValueError('Provide exchange:symbol instruments')
        return self.client.quote(*instruments)

    def submit_order(self, **kwargs):
        """Submit a validated Zerodha Kite regular order."""
        if os.getenv('ZERODHA_WRITE_ENABLED', 'false').strip().lower() != 'true':
            raise RuntimeError('Zerodha order submission is disabled')
        required = {'exchange', 'tradingsymbol', 'transaction_type', 'quantity', 'product', 'order_type'}
        missing = required - kwargs.keys()
        if missing:
            raise ValueError('Missing required fields: ' + ', '.join(sorted(missing)))
        exchange = str(kwargs['exchange']).upper()
        side = str(kwargs['transaction_type']).upper()
        order_type = str(kwargs['order_type']).upper()
        product = str(kwargs['product']).upper()
        if exchange not in {'NSE', 'BSE'}:
            raise ValueError('Only NSE and BSE equity exchanges are allowed')
        if side not in {'BUY', 'SELL'}:
            raise ValueError('transaction_type must be BUY or SELL')
        if order_type not in {'MARKET', 'LIMIT', 'SL', 'SL-M'}:
            raise ValueError('Unsupported order_type')
        if product not in {'CNC', 'MIS'}:
            raise ValueError('product must be CNC or MIS')
        try:
            quantity = int(kwargs['quantity'])
        except (ValueError, TypeError) as exc:
            raise ValueError('quantity must be a positive integer') from exc
        if quantity <= 0:
            raise ValueError('quantity must be positive')
        if order_type == 'LIMIT':
            try:
                price = float(kwargs.get('price', 0))
            except (ValueError, TypeError) as exc:
                raise ValueError('LIMIT orders require a valid price') from exc
            if not price > 0:
                raise ValueError('LIMIT orders require price > 0')
        order = dict(kwargs)
        order['exchange'] = exchange
        order['transaction_type'] = side
        order['order_type'] = order_type
        order['product'] = product
        order['quantity'] = quantity
        order.setdefault('variety', self.client.VARIETY_REGULAR)
        order.setdefault('validity', self.client.VALIDITY_DAY)
        return self.client.place_order(**order)
