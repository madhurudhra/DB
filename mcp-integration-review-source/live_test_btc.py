import os, sys, sqlite3
if os.environ.get('CONFIRM_LIVE_BTC_1USD') != 'YES':
    sys.exit('Refusing: confirmation not set')
from app.runner import handle_crypto
from app.core.config import get_settings
s = get_settings()
handle_crypto({'symbol': 'BTC-USD', 'side': 'BUY', 'notional_usd': '1', 'bucket': 'livetest-btc-1usd-2'}, s)
c = sqlite3.connect(s.database_path)
print('INTENT:', c.execute('SELECT id,status,symbol,side,quantity,created_at FROM order_intents ORDER BY id DESC LIMIT 1').fetchall())
print('BROKER:', c.execute('SELECT broker_order_id,status,submitted_at FROM broker_orders ORDER BY id DESC LIMIT 1').fetchall())
