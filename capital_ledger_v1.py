"""Capital reservation infrastructure. Does not reconcile or execute orders.
Only an authoritative future reconciler may populate capital_verified_states.
No HTTP funding, reservation, release, or settlement route is provided.
"""
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
BROKERS = {'crypto': 'robinhood_crypto', 'equity': 'robinhood_equity', 'zerodha': 'zerodha'}
SCHEMA = ('CREATE TABLE IF NOT EXISTS capital_verified_states (\n        bot TEXT NOT NULL, account_ref TEXT NOT NULL,\n        config_revision INTEGER NOT NULL,\n        reconciliation_id INTEGER NOT NULL,\n        funded_capital TEXT NOT NULL,\n        eligible_cash TEXT NOT NULL,\n        position_cost TEXT NOT NULL,\n        realized_net_profit TEXT NOT NULL,\n        baseline_pending_buy_commitment TEXT NOT NULL,\n        as_of TEXT NOT NULL,\n        PRIMARY KEY(bot,account_ref),\n        FOREIGN KEY(reconciliation_id) REFERENCES reconciliation_runs(id)\n    )', "CREATE TABLE IF NOT EXISTS capital_buy_reservations (\n        request_id TEXT PRIMARY KEY,\n        bot TEXT NOT NULL, account_ref TEXT NOT NULL,\n        config_revision INTEGER NOT NULL,\n        amount_total TEXT NOT NULL,\n        state TEXT NOT NULL CHECK(state IN ('HELD','UNKNOWN','CLOSED')),\n        created_at TEXT NOT NULL\n    )")

def initialize(db):
    for statement in SCHEMA:
        db.execute(statement)

def decimal_value(value, signed=False):
    if value is None or isinstance(value, bool):
        raise ValueError('Invalid capital value')
    result = Decimal(str(value))
    if not result.is_finite() or (not signed and result < 0):
        raise ValueError('Invalid capital value')
    if abs(result) > Decimal('1000000000000') or result.as_tuple().exponent < -12:
        raise ValueError('Capital value exceeds supported precision/range')
    return result

def age(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp timezone required')
    return (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()

def snapshot(db, bot):
    if bot not in BROKERS:
        raise ValueError('Unknown bot')
    row = db.execute('SELECT revision,config_json FROM independent_bot_configurations WHERE bot=?', (bot,)).fetchone()
    if row is None:
        raise ValueError('Saved configuration missing')
    config = json.loads(row['config_json'])
    account = config.get('account_ref')
    currency = 'INR' if bot == 'zerodha' else 'USD'
    if config.get('currency') != currency:
        raise ValueError('Configuration currency mismatch')
    reserve = decimal_value(config.get('cash_reserve_pct'))
    if reserve >= 100:
        raise ValueError('Invalid reserve percentage')
    allocation = config.get('allocated_capital')
    allocation = decimal_value(allocation) if allocation is not None else None
    if allocation is None and bot != 'zerodha':
        raise ValueError('Fixed bot allocation required')
    report = {'schema_version': 1, 'bot': bot, 'currency': currency, 'configuration_revision': row['revision'], 'account_ending': str(account)[-4:] if account else None, 'configured_allocation': str(allocation) if allocation is not None else None, 'reserve_pct': str(reserve), 'funded_capital': None, 'verified_position_cost': None, 'realized_net_profit': None, 'verified_pending_buy_commitment': None, 'local_held_reservations': None, 'available_automatic_budget': None, 'as_of': None, 'status': 'AWAITING_VERIFIED_CAPITAL_BASELINE', 'reservation_checks_ready': False, 'automatic_trading_enabled': False, 'execution_integration': 'NOT_CONNECTED_TO_RUNNER'}
    if not account:
        report['status'] = 'ACCOUNT_REFERENCE_REQUIRED'
        return report
    state = db.execute('SELECT * FROM capital_verified_states WHERE bot=? AND account_ref=?', (bot, account)).fetchone()
    if state is None:
        return report
    report['as_of'] = state['as_of']
    if state['config_revision'] != row['revision']:
        report['status'] = 'CAPITAL_BASELINE_CONFIGURATION_MISMATCH'
        return report
    reconciliation = db.execute('SELECT broker,account_ref,status,finished_at FROM reconciliation_runs WHERE id=?', (state['reconciliation_id'],)).fetchone()
    if reconciliation is None or reconciliation['broker'] != BROKERS[bot] or reconciliation['account_ref'] != account or (reconciliation['status'] != 'PASS'):
        report['status'] = 'PASSING_ACCOUNT_RECONCILIATION_REQUIRED'
        return report
    if not reconciliation['finished_at'] or not 0 <= age(reconciliation['finished_at']) <= 60 or (not 0 <= age(state['as_of']) <= 60):
        report['status'] = 'CAPITAL_BASELINE_STALE'
        return report
    funded = decimal_value(state['funded_capital'])
    cash = decimal_value(state['eligible_cash'])
    exposure = decimal_value(state['position_cost'])
    profit = decimal_value(state['realized_net_profit'], signed=True)
    pending = decimal_value(state['baseline_pending_buy_commitment'])
    held_rows = db.execute("SELECT amount_total FROM capital_buy_reservations WHERE bot=? AND account_ref=? AND state IN ('HELD','UNKNOWN')", (bot, account)).fetchall()
    held = sum((decimal_value(item['amount_total']) for item in held_rows), Decimal('0'))
    assigned = funded if allocation is None else min(allocation, funded)
    adjustment = profit if bot == 'crypto' else min(profit, Decimal('0')) if bot == 'equity' else Decimal('0')
    capital = max(Decimal('0'), assigned + adjustment)
    ceiling = capital * (100 - reserve) / 100
    remaining = max(Decimal('0'), ceiling - exposure - pending - held)
    available = max(Decimal('0'), min(remaining, cash - held))
    report.update({'status': 'RESERVATION_INPUTS_READY', 'funded_capital': str(funded), 'verified_position_cost': str(exposure), 'realized_net_profit': str(profit), 'deployment_ceiling': str(ceiling), 'verified_pending_buy_commitment': str(pending), 'local_held_reservations': str(held), 'available_automatic_budget': str(available), 'reservation_checks_ready': True})
    return report

def reserve_buy(db, *, bot, account_ref, request_id, amount_total):
    """Internal only. Caller must hold BEGIN IMMEDIATE and commit atomically.
    amount_total must include fees/slippage allowance. Not permission to submit.
    No automatic expiry/release: unresolved holds must remain reserved.
    """
    if not db.in_transaction:
        raise RuntimeError('BEGIN IMMEDIATE transaction required')
    db.execute('UPDATE capital_buy_reservations SET state=state WHERE 0')
    request_id = str(uuid.UUID(str(request_id)))
    amount = decimal_value(amount_total)
    if amount <= 0:
        raise ValueError('Positive reservation required')
    row = db.execute('SELECT revision,config_json FROM independent_bot_configurations WHERE bot=?', (bot,)).fetchone()
    if row is None or json.loads(row['config_json']).get('account_ref') != account_ref:
        raise ValueError('Configured account mismatch')
    existing = db.execute('SELECT * FROM capital_buy_reservations WHERE request_id=?', (request_id,)).fetchone()
    if existing:
        if existing['bot'] != bot or existing['account_ref'] != account_ref or decimal_value(existing['amount_total']) != amount:
            raise ValueError('Reservation request ID reused with different content')
        return {'request_id': request_id, 'state': existing['state'], 'replayed': True, 'order_submission_allowed': False}
    report = snapshot(db, bot)
    if report['reservation_checks_ready'] is not True:
        raise RuntimeError(report['status'])
    if amount > decimal_value(report['available_automatic_budget']):
        raise ValueError('Insufficient automatic budget')
    created = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT INTO capital_buy_reservations (request_id,bot,account_ref,config_revision,amount_total,state,created_at) VALUES (?,?,?,?,?,'HELD',?)", (request_id, bot, account_ref, row['revision'], str(amount), created))
    db.execute('INSERT INTO audit_events (event_type,bot_name,correlation_id,details_json) VALUES (?,?,?,?)', ('CAPITAL_BUY_RESERVED', bot, request_id, json.dumps({'amount_total': str(amount), 'config_revision': row['revision']})))
    return {'request_id': request_id, 'state': 'HELD', 'replayed': False, 'order_submission_allowed': False}
