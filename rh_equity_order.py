import asyncio, json, os, sqlite3, sys, uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from rh_equity_review import call, LAST4
from rh_mcp_auth import mask
from app.core.write_policy import can_submit_order
NS = uuid.UUID('[REDACTED_LITERAL]')
DB = os.getenv('DATABASE_PATH', '/var/lib/super-intelligence/super-intelligence.db')
MAX_USD = 5.0

def regular_hours():
    n = datetime.now(ZoneInfo('America/New_York'))
    if n.weekday() >= 5:
        return False
    m = n.hour * 60 + n.minute
    return 9 * 60 + 30 <= m < 16 * 60

def recon_healthy(conn):
    row = conn.execute("SELECT COUNT(*) FROM order_intents WHERE bot_name='equity' AND status IN ('SUBMITTING','UNKNOWN')").fetchone()
    return row[0] == 0

async def main():
    symbol = sys.argv[1].upper()
    usd = float(sys.argv[2])
    bucket = sys.argv[3]
    if not 0 < usd <= MAX_USD:
        sys.exit('Amount outside the allowed cap')
    accts = json.loads(await call('get_accounts', {}))['data']['accounts']
    ok = [a for a in accts if a['account_number'].endswith(LAST4) and a.get('agentic_allowed')]
    if len(ok) != 1:
        sys.exit('Expected exactly one agentic account ending ' + LAST4)
    acct = ok[0]['account_number']
    args = {'account_number': acct, 'symbol': symbol, 'side': 'buy', 'type': 'market', 'dollar_amount': '%.2f' % usd}
    rev = json.loads(await call('review_equity_order', args))['data']
    print('REVIEW alerts:', rev.get('order_checks'), '| approval_required:', rev.get('customer_approval_required'))
    if rev.get('order_checks'):
        sys.exit('Blocked by broker alert. Nothing placed.')
    if not regular_hours():
        sys.exit('Outside regular market hours (9:30-16:00 ET, Mon-Fri). Nothing placed.')
    ref = str(uuid.uuid5(NS, 'equity|%s|BUY|%.2f|%s' % (symbol, usd, bucket)))
    conn = sqlite3.connect(DB, timeout=10)
    try:
        cur = conn.execute('INSERT INTO order_intents (idempotency_key,bot_name,broker,account_ref,symbol,side,quantity,order_type,status,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?)', (ref, 'equity', 'robinhood_equity', '*' + acct[-4:], symbol, 'BUY', '%.2f' % usd, 'market', 'CREATED', json.dumps({'quantity_is': 'dollar_amount_usd'})))
        conn.commit()
    except sqlite3.IntegrityError:
        sys.exit('Duplicate intent; refusing to resend.')
    iid = cur.lastrowid

    def status(s):
        conn.execute('UPDATE order_intents SET status=? WHERE id=?', (s, iid))
        conn.commit()
    allowed, reason = can_submit_order('equity', risk_passed=True, market_calendar_passed=True, account_verified=True, reconciliation_healthy=recon_healthy(conn), durable_order_intent_saved=True)
    if not allowed:
        status('BLOCKED')
        sys.exit('Blocked by policy: ' + reason)
    status('SUBMITTING')
    args['ref_id'] = ref
    try:
        out = await call('place_equity_order', args)
        d = json.loads(out)
        d = d.get('data', d)
        if d.get('error'):
            raise RuntimeError('broker returned error')
        bid = d.get('order_id') or d.get('id') or (d.get('order') or {}).get('id')
        conn.execute("INSERT INTO broker_orders (intent_id,broker,broker_order_id,status,submitted_at,response_json) VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)", (iid, 'robinhood_equity', str(bid) if bid else None, 'submitted', out))
        status('SUBMITTED')
        print('SUBMITTED. Verify in the Robinhood app. Response:', mask(out)[:1500])
    except Exception as exc:
        status('UNKNOWN')
        print('UNKNOWN outcome (%s). CHECK THE ROBINHOOD APP before doing anything.' % exc)
    finally:
        conn.close()
if __name__ == '__main__':
    asyncio.run(main())
