"""Explicitly confirmed manual orders. Not an automatic trading engine."""
import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from rh_equity_review import call
DB = os.getenv('DATABASE_PATH', '/var/lib/super-intelligence/super-intelligence.db')

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))

def positive(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError('Amounts and prices must be finite and positive')
    return number

def contains_error(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('error', 'errors', 'validation_errors') and item:
                return True
            if contains_error(item):
                return True
    elif isinstance(value, list):
        return any((contains_error(item) for item in value))
    return False

async def mcp(tool, arguments):
    raw = await call(tool, arguments)
    result = json.loads(raw)
    if contains_error(result):
        raise RuntimeError('Broker response contains an error. No automatic retry.')
    return result

def connection():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=10000')
    db.execute('\n        CREATE TABLE IF NOT EXISTS manual_execution_requests (\n            request_id TEXT PRIMARY KEY,\n            bot TEXT NOT NULL,\n            payload_json TEXT NOT NULL,\n            status TEXT NOT NULL,\n            created_at TEXT NOT NULL,\n            updated_at TEXT NOT NULL,\n            response_json TEXT,\n            error_type TEXT\n        )\n    ')
    db.commit()
    return db

def register(request_id, bot, order):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT bot,payload_json,status FROM manual_execution_requests WHERE request_id=?', (request_id,)).fetchone()
        if existing:
            raise RuntimeError('Request ID already recorded with status ' + existing['status'] + '. Reconcile it; do not generate a replacement blindly.')
        unresolved = db.execute("SELECT COUNT(*) FROM manual_execution_requests WHERE bot=? AND status IN ('SUBMITTING','UNKNOWN')", (bot,)).fetchone()[0]
        if unresolved:
            raise RuntimeError('This bot has an unresolved manual submission. Reconcile before another order.')
        now = datetime.now(timezone.utc).isoformat()
        db.execute('INSERT INTO manual_execution_requests (request_id,bot,payload_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?)', (request_id, bot, canonical(order), 'SUBMITTING', now, now))

def finish(request_id, status, response=None, error_type=None):
    with connection() as db:
        db.execute('UPDATE manual_execution_requests SET status=?,updated_at=?,response_json=?,error_type=? WHERE request_id=?', (status, datetime.now(timezone.utc).isoformat(), json.dumps(response) if response is not None else None, error_type, request_id))

async def validate_robinhood(bot, order):
    tools = {'crypto': ('preview_crypto_order', 'place_crypto_order'), 'equity': ('review_equity_order', 'place_equity_order')}
    preview_tool, place_tool = tools[bot]
    if 'ref_id' in order:
        raise ValueError('Do not put ref_id in the order JSON')
    if order.get('side') not in ('buy', 'sell'):
        raise ValueError('side must be buy or sell')
    if not order.get('symbol') or not order.get('type'):
        raise ValueError('Explicit symbol and order type are required')
    if ('quantity' in order) == ('dollar_amount' in order):
        raise ValueError('Provide exactly one of quantity or dollar_amount')
    for key in ('quantity', 'dollar_amount', 'limit_price', 'stop_price'):
        if key in order:
            if not isinstance(order[key], str):
                raise ValueError(key + ' must be a decimal string')
            positive(order[key])
    accounts_response = await mcp('get_accounts', {})
    accounts = accounts_response['data']['accounts']
    account_key = 'rhs_account_number' if bot == 'crypto' else 'account_number'
    selected = order.get(account_key)
    if not isinstance(selected, str) or not selected:
        raise ValueError('Explicit ' + account_key + ' is required')
    matching = [account for account in accounts if account.get(account_key) == selected]
    if len(matching) != 1:
        raise ValueError('Account selection is not uniquely verified')
    account = matching[0]
    if account.get('agentic_allowed') is not True:
        raise ValueError('Selected account is not agentic-enabled')
    approval = await mcp('get_trade_approval_setting', {'account_number': account['account_number']})
    preview = await mcp(preview_tool, order)
    return (preview, approval, place_tool)

def validate_zerodha(order):
    from app.brokers.zerodha.kite import ZerodhaKite
    required = {'exchange', 'tradingsymbol', 'transaction_type', 'quantity', 'product', 'order_type'}
    if not required.issubset(order):
        raise ValueError('Missing required Zerodha order fields')
    if order['exchange'] not in ('NSE', 'BSE'):
        raise ValueError('Only NSE/BSE supported')
    if order['transaction_type'] not in ('BUY', 'SELL'):
        raise ValueError('transaction_type must be BUY or SELL')
    if order['product'] not in ('CNC', 'MIS'):
        raise ValueError('product must be CNC or MIS')
    quantity = order['quantity']
    if type(quantity) is not int or quantity <= 0:
        raise ValueError('Zerodha quantity must be a positive JSON integer')
    if order['order_type'] not in ('MARKET', 'LIMIT', 'SL', 'SL-M'):
        raise ValueError('Unsupported Zerodha order type')
    if order['order_type'] in ('LIMIT', 'SL'):
        positive(order.get('price', 0))
    if order['order_type'] in ('SL', 'SL-M'):
        positive(order.get('trigger_price', 0))
    adapter = ZerodhaKite()
    profile = adapter.profile()
    if not isinstance(profile, dict) or not profile.get('user_id'):
        raise ValueError('Zerodha identity could not be verified')
    instrument = order['exchange'] + ':' + order['tradingsymbol']
    quotes = adapter.quote(instrument)
    if instrument not in quotes:
        raise ValueError('Instrument quote unavailable')
    return (adapter, {'validation_type': 'LOCAL_ONLY_NOT_BROKER_ORDER_PREVIEW', 'account_user_id': profile['user_id'], 'order': order, 'quote': quotes[instrument], 'warning': 'Buying power, sellable quantity, exchange-session eligibility and the complete broker risk checks are not certified by this local validation.'})

def show_status(request_id):
    with connection() as db:
        row = db.execute('SELECT * FROM manual_execution_requests WHERE request_id=?', (request_id,)).fetchone()
    if row is None:
        raise ValueError('Request ID not found')
    print(json.dumps(dict(row), indent=2))

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bot', choices=('crypto', 'equity', 'zerodha'))
    parser.add_argument('--order-file')
    parser.add_argument('--request-id')
    parser.add_argument('--submit', action='store_true')
    parser.add_argument('--ack-manual-budget-override', action='store_true')
    parser.add_argument('--status')
    args = parser.parse_args()
    if args.status:
        show_status(args.status)
        return
    if not args.bot or not args.order_file:
        parser.error('--bot and --order-file are required')
    with open(args.order_file) as file:
        order = json.load(file)
    if not isinstance(order, dict):
        raise ValueError('Order file must contain one JSON object')
    if args.bot == 'zerodha':
        adapter, preview = validate_zerodha(order)
        approval = None
        place_tool = None
    else:
        preview, approval, place_tool = await validate_robinhood(args.bot, order)
    print('ORDER PARAMETERS:')
    print(json.dumps(order, indent=2))
    print('PREVIEW / VALIDATION — DISPLAYED WITHOUT MASKING:')
    print(json.dumps(preview, indent=2, ensure_ascii=False))
    if approval is not None:
        print('BROKER APPROVAL SETTING:')
        print(json.dumps(approval, indent=2, ensure_ascii=False))
    if not args.submit:
        print('PREVIEW ONLY. No order submitted.')
        return
    if not args.ack_manual_budget_override:
        raise RuntimeError('BLOCKED: account-wide allocation enforcement is not implemented. Submission requires --ack-manual-budget-override.')
    if os.getenv('MANUAL_ORDER_EXECUTION_ENABLED', '').lower() != 'true':
        raise RuntimeError('MANUAL_ORDER_EXECUTION_ENABLED is not true')
    if not args.request_id:
        raise ValueError('Submission requires an explicit UUID --request-id')
    request_id = str(uuid.UUID(args.request_id))
    if not sys.stdin.isatty():
        raise RuntimeError('Submission requires an interactive terminal')
    print('WARNING: manual budget override bypasses the configured allocation/reserve checks. This is NOT automatic trading.')
    print('Existing broker alerts above must be reviewed. Order acceptance does not mean it is filled.')
    fingerprint = hashlib.sha256(canonical({'bot': args.bot, 'order': order}).encode()).hexdigest()[:12]
    phrase = 'SUBMIT ' + fingerprint
    print('To confirm these exact parameters, type: ' + phrase)
    started = asyncio.get_running_loop().time()
    import select
    import time
    answer = ''
    deadline = time.monotonic() + 60
    pending = b''
    print('Blank lines are ignored. Type the exact SUBMIT phrase, or CANCEL to stop.', flush=True)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('Confirmation expired. No order submitted.')
        print('> ', end='', flush=True)
        ready, _, _ = select.select([sys.stdin.fileno()], [], [], remaining)
        if not ready:
            raise RuntimeError('Confirmation expired. No order submitted.')
        chunk = os.read(sys.stdin.fileno(), 4096)
        if not chunk:
            raise RuntimeError('Terminal input closed. No order submitted.')
        pending += chunk
        complete = False
        while b'\n' in pending:
            line, pending = pending.split(b'\n', 1)
            candidate = line.decode('utf-8', errors='replace').strip()
            if not candidate:
                print('Empty line ignored; still awaiting confirmation.')
                continue
            answer = candidate
            complete = True
            break
        if complete:
            break
    if answer != phrase:
        print('CANCELED. No order submitted.')
        return
    if asyncio.get_running_loop().time() - started > 60:
        raise RuntimeError('Confirmation expired; obtain a fresh preview')
    register(request_id, args.bot, order)
    try:
        if args.bot == 'zerodha':
            result = {'order_id': adapter.submit_order(**order), 'execution_status': 'NOT_YET_RECONCILED'}
        else:
            payload = dict(order)
            payload['ref_id'] = request_id
            result = await mcp(place_tool, payload)
        finish(request_id, 'RESPONSE_RECEIVED_UNRECONCILED', result)
        print('BROKER RESPONSE — NOT A FILL CONFIRMATION:')
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print('Request ID:', request_id)
        print('Verify order/approval status in the broker application.')
    except BaseException as exc:
        try:
            finish(request_id, 'UNKNOWN', error_type=type(exc).__name__)
        finally:
            print('UNKNOWN SUBMISSION OUTCOME. Check the broker before retrying. Do not create a replacement request blindly.', file=sys.stderr)
        raise
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        print('STOPPED: ' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        sys.exit(1)
