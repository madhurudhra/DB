"""LIVE_ONLY_RUNNER_V1: production runner; incomplete automation is blocked."""
import json, logging, os, sqlite3, time, uuid
from decimal import Decimal, InvalidOperation
from app.core.config import get_settings
from app.core.write_policy import can_submit_order
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('runner')
NS = uuid.UUID('[REDACTED_LITERAL]')
INTERVAL = int(os.getenv('RUNNER_INTERVAL_SECONDS', '60'))
MAX_ORDER_USD = Decimal(os.getenv('RUNNER_MAX_ORDER_USD', '10'))
MAX_ORDERS_PER_DAY = int(os.getenv('RUNNER_MAX_ORDERS_PER_DAY', '3'))
COUNTED = ('SUBMITTING', 'SUBMITTED', 'UNKNOWN')
SYMBOL = 'BTC-USD'
SMA_SAMPLES = int(os.getenv('STRATEGY_SMA_SAMPLES', '120'))
DIP_PCT = Decimal(os.getenv('STRATEGY_DIP_PCT', '1.0'))
TRADE_USD = os.getenv('STRATEGY_TRADE_USD', '5')

def _price(adapter, symbol):
    r = adapter.quote(symbol)
    row = r['results'][0] if isinstance(r, dict) and r.get('results') else r
    if row.get('price'):
        return Decimal(str(row['price']))
    bid = Decimal(str(row['bid_inclusive_of_sell_spread']))
    ask = Decimal(str(row['ask_inclusive_of_buy_spread']))
    return (bid + ask) / 2

def strategy_crypto() -> list[dict]:
    """Dip-buy on BTC-USD: price <= SMA of recent samples minus DIP_PCT percent."""
    conn = db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS price_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, price TEXT NOT NULL, sampled_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))")
        from app.brokers.robinhood.crypto import RobinhoodCrypto
        sampling_adapter = RobinhoodCrypto()
        try:
            price = _price(sampling_adapter, SYMBOL)
        finally:
            sampling_adapter.session.close()
        conn.execute('INSERT INTO price_samples (symbol, price) VALUES (?, ?)', (SYMBOL, str(price)))
        conn.execute('DELETE FROM price_samples WHERE symbol=? AND id NOT IN (SELECT id FROM price_samples WHERE symbol=? ORDER BY id DESC LIMIT 5000)', (SYMBOL, SYMBOL))
        conn.commit()
        rows = conn.execute('SELECT price FROM price_samples WHERE symbol=? ORDER BY id DESC LIMIT ?', (SYMBOL, SMA_SAMPLES)).fetchall()
        if len(rows) < SMA_SAMPLES:
            log.info('strategy warming up: %d/%d samples, price=%s', len(rows), SMA_SAMPLES, price)
            return []
        sma = sum((Decimal(r[0]) for r in rows)) / len(rows)
        threshold = sma * (1 - DIP_PCT / 100)
        log.info('strategy price=%s sma=%.2f buy_below=%.2f', price, sma, threshold)
        if price <= threshold:
            return [{'symbol': SYMBOL, 'side': 'BUY', 'notional_usd': TRADE_USD, 'bucket': time.strftime('%Y-%m-%dT%H', time.gmtime())}]
        return []
    except Exception:
        log.exception('strategy_crypto failed')
        return []
    finally:
        conn.close()

def db():
    conn = sqlite3.connect(get_settings().database_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def orders_today(conn, bot):
    """Count submitted orders even after their local status becomes FILLED."""
    return conn.execute("\n        SELECT COUNT(*)\n        FROM order_intents AS oi\n        WHERE oi.bot_name=?\n          AND oi.created_at >= strftime('%Y-%m-%dT00:00:00Z','now')\n          AND (\n              oi.status IN ('SUBMITTING','SUBMITTED','UNKNOWN','FILLED')\n              OR EXISTS (\n                  SELECT 1 FROM broker_orders AS bo\n                  WHERE bo.intent_id=oi.id AND bo.broker=oi.broker\n              )\n          )\n        ", (bot,)).fetchone()[0]

def reconciliation_healthy(conn, bot):
    n = conn.execute("SELECT COUNT(*) FROM order_intents WHERE bot_name=? AND status IN ('SUBMITTING','UNKNOWN')", (bot,)).fetchone()[0]
    return n == 0

def asset_qty_for_notional(adapter, symbol, notional):
    from decimal import ROUND_DOWN
    target = Decimal(str(notional))
    if not target.is_finite() or target <= 0:
        raise ValueError('Order notional must be finite and positive')
    response = adapter.pairs()
    if not isinstance(response, dict):
        raise ValueError('Trading-pair response is invalid')
    matches = [pair for pair in response.get('results', []) if isinstance(pair, dict) and pair.get('symbol') == symbol]
    if len(matches) != 1:
        raise ValueError('Trading pair is missing or not unique')
    pair = matches[0]
    increment = Decimal(str(pair.get('asset_increment')))
    minimum = Decimal(str(pair.get('min_order_size')))
    if not increment.is_finite() or increment <= 0:
        raise ValueError('Verified positive asset increment required')
    if not minimum.is_finite() or minimum < 0:
        raise ValueError('Verified nonnegative minimum size required')
    quote = adapter.quote(symbol)
    if not isinstance(quote, dict):
        raise ValueError('Quote response is invalid')
    if 'results' in quote:
        quote_rows = quote['results']
        if not isinstance(quote_rows, list) or len(quote_rows) != 1:
            raise ValueError('Expected exactly one quote')
        row = quote_rows[0]
    else:
        row = quote
    if not isinstance(row, dict):
        raise ValueError('Quote row is invalid')
    if row.get('symbol') not in (None, symbol):
        raise ValueError('Quote symbol mismatch')
    ask = Decimal(str(row.get('ask_inclusive_of_buy_spread')))
    if not ask.is_finite() or ask <= 0:
        raise ValueError('Verified positive ask required')
    quantity = (target / ask / increment).to_integral_value(rounding=ROUND_DOWN) * increment
    if quantity <= 0 or quantity < minimum:
        raise ValueError('Budget cannot satisfy the broker minimum size')
    if quantity * ask > target:
        raise ValueError('Rounded quantity exceeds quoted notional budget')
    return (quantity, ask)

def handle_crypto(d, settings):
    if not MAX_ORDER_USD.is_finite() or MAX_ORDER_USD <= 0:
        log.error('Invalid RUNNER_MAX_ORDER_USD; automatic order rejected')
        return
    conn = db()
    try:
        configuration_row = conn.execute('SELECT config_json FROM independent_bot_configurations WHERE bot=?', ('crypto',)).fetchone()
        if configuration_row is None:
            log.error('Crypto configuration missing; decision blocked')
            return
        saved_configuration = json.loads(configuration_row['config_json'])
        configured_account = saved_configuration.get('account_ref')
        if not isinstance(configured_account, str) or not configured_account or configured_account != configured_account.strip() or (saved_configuration.get('currency') != 'USD'):
            log.error('Valid configured USD Crypto account required; decision blocked')
            return
        symbol, side = (d['symbol'], d['side'].upper())
        try:
            notional = Decimal(str(d['notional_usd']))
        except InvalidOperation:
            log.error('bad notional in decision %s', d)
            return
        if side not in ('BUY', 'SELL') or not notional.is_finite() or notional <= 0 or (notional > MAX_ORDER_USD):
            log.warning('decision rejected by caps/validation: %s', d)
            return
        if orders_today(conn, 'crypto') >= MAX_ORDERS_PER_DAY:
            log.warning('daily order cap reached')
            return
        from app.core.crypto_buy_budget_v1 import require_verified_crypto_capital
        try:
            require_verified_crypto_capital(conn, account_ref=configured_account)
        except Exception as capital_error:
            log.warning('Automatic Crypto decision blocked by capital readiness (%s)', type(capital_error).__name__)
            return
        idem = str(uuid.uuid5(NS, f'crypto|{symbol}|{side}|{notional}|{d.get('bucket', '')}'))
        meta = json.dumps({'decision': d, 'execution_mode': 'live', 'quantity_is': 'quote_amount_usd'})
        try:
            cur = conn.execute('INSERT INTO order_intents (idempotency_key,bot_name,broker,account_ref,symbol,side,quantity,order_type,status,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?)', (idem, 'crypto', 'robinhood_crypto', configured_account, symbol, side, str(notional), 'market', 'CREATED', meta))
            conn.commit()
        except sqlite3.IntegrityError:
            log.info('duplicate intent skipped: %s', idem)
            return
        intent_id = cur.lastrowid

        def set_status(status):
            conn.execute('UPDATE order_intents SET status=? WHERE id=?', (status, intent_id))
            conn.commit()
        if not settings.live_trading_enabled:
            log.warning('LIVE_TRADING_ENABLED is false; blocked')
            set_status('BLOCKED')
            return
        from app.brokers.robinhood.crypto import RobinhoodCrypto
        try:
            adapter = RobinhoodCrypto()
            account_ok = bool(adapter.accounts())
        except Exception as exc:
            log.error('account verification failed: %s', exc)
            set_status('BLOCKED')
            return
        ok, reason = can_submit_order('crypto', risk_passed=True, market_calendar_passed=True, account_verified=account_ok, reconciliation_healthy=reconciliation_healthy(conn, 'crypto'), durable_order_intent_saved=True, side=side, symbol=symbol, account_ref=configured_account)
        if not ok:
            log.warning('blocked: %s', reason)
            set_status('BLOCKED')
            return
        try:
            qty, ask = asset_qty_for_notional(adapter, symbol, notional)
        except Exception as exc:
            log.error('quantity calc failed: %s', exc)
            set_status('BLOCKED')
            return
        log.info('order qty=%s at ask=%s (target $%s)', qty, ask, notional)
        set_status('SUBMITTING')
        payload = {'client_order_id': idem, 'side': side.lower(), 'type': 'market', 'symbol': symbol, 'market_order_config': {'asset_quantity': format(qty, 'f')}}
        try:
            resp = adapter.submit_order(payload)
        except Exception as exc:
            log.error('submit error, marking UNKNOWN: %s', exc)
            set_status('UNKNOWN')
            return
        conn.execute("INSERT INTO broker_orders (intent_id,broker,broker_order_id,status,submitted_at,response_json) VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)", (intent_id, 'robinhood_crypto', str(resp.get('id')), str(resp.get('state', 'submitted')), json.dumps(resp)))
        set_status('SUBMITTED')
        log.info('SUBMITTED %s %s %s broker_id=%s', side, notional, symbol, resp.get('id'))
    finally:
        conn.close()

def run_once():
    s = get_settings()
    log.info('heartbeat mode=live crypto=%s equity=%s zerodha=%s', s.crypto_bot_enabled, s.equity_bot_enabled, s.zerodha_bot_enabled)
    if s.crypto_bot_enabled:
        for d in strategy_crypto():
            handle_crypto(d, s)
    if s.equity_bot_enabled:
        log.warning('equity: no verified broker adapter; skipped')
    if s.zerodha_bot_enabled:
        log.warning('zerodha: daily token refresh and strategy not implemented; skipped')
if __name__ == '__main__':
    while True:
        try:
            run_once()
        except Exception:
            log.exception('run_once failed')
        time.sleep(INTERVAL)
