import asyncio
import fcntl
import inspect
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, '/opt/si-equity-r1')
import broker_monitor as monitor
sys.path.insert(0, str(Path(__file__).resolve().parent))
auth = monitor.auth
monitor.ALLOWED.add('get_equity_quotes')
ROOT = Path('/var/lib/si-equity-stop-guard')
EXPECTED = {'SPCX': Decimal('0.053969'), 'IONQ': Decimal('0.061680')}
TERMINAL = monitor.TERMINAL
VERSION = 'equity-stop-only-v2-broker-id'

def dec(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise RuntimeError('NONFINITE_NUMBER')
    return result

def now():
    return datetime.now(timezone.utc).isoformat()

def log(symbol, state, **fields):
    print(json.dumps({'at': now(), 'symbol': symbol, 'state': state, 'version': VERSION, **fields}, allow_nan=False), flush=True)

def regular_window():
    current = datetime.now(ZoneInfo('America/New_York'))
    minute = current.hour * 60 + current.minute
    return current.weekday() < 5 and 571 <= minute < 959

def database():
    db = sqlite3.connect(ROOT / 'guard.sqlite3', timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA synchronous=FULL')
    db.executescript("\n        CREATE TABLE IF NOT EXISTS managed(\n            symbol TEXT PRIMARY KEY,\n            initial_qty TEXT NOT NULL,\n            average_ex_fees TEXT NOT NULL,\n            external_executions TEXT NOT NULL,\n            adopted_at TEXT NOT NULL\n        );\n        CREATE TABLE IF NOT EXISTS intents(\n            ref TEXT PRIMARY KEY,\n            symbol TEXT NOT NULL,\n            requested_qty TEXT NOT NULL,\n            state TEXT NOT NULL,\n            filled TEXT NOT NULL DEFAULT '0',\n            created_at TEXT NOT NULL\n        );\n    ")
    columns = {row[1] for row in db.execute('PRAGMA table_info(intents)')}
    additions = {'broker_id': 'TEXT', 'broker_ref': 'TEXT', 'average_price': "TEXT NOT NULL DEFAULT '0'", 'fees': "TEXT NOT NULL DEFAULT '0'"}
    for name, definition in additions.items():
        if name not in columns:
            db.execute('ALTER TABLE intents ADD COLUMN ' + name + ' ' + definition)
    db.commit()
    return db

def unpack(result):
    envelope = result.model_dump(mode='json', by_alias=True)
    if envelope.get('isError') or envelope.get('is_error'):
        raise RuntimeError('BROKER_TOOL_ERROR')
    payload = envelope.get('structuredContent', envelope.get('structured_content'))
    if payload is None:
        payload = json.loads('\n'.join((item['text'] for item in envelope.get('content', []) if item.get('type') == 'text' and 'text' in item)))
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
        raise RuntimeError('BROKER_RESPONSE_SHAPE')
    return payload['data']

class LocalSellPermissionBlocked(RuntimeError):
    pass

async def order_call(session, name, arguments):
    import equity_commands_v1 as equity_commands
    try:
        equity_commands.require_sell_permission()
    except RuntimeError as error:
        if str(error) != 'EQUITY_SELL_BLOCKED_BY_WORKER_CONTROLS':
            raise
        raise LocalSellPermissionBlocked(str(error)) from error
    if name not in {'review_equity_order', 'place_equity_order'}:
        raise RuntimeError('WRITE_TOOL_REFUSED')
    if arguments.get('side') != 'sell' or arguments.get('type') != 'market' or arguments.get('market_hours') != 'regular_hours' or (arguments.get('symbol') not in EXPECTED) or (dec(arguments.get('quantity', '0')) <= 0):
        raise RuntimeError('ORDER_SCOPE_REFUSED')
    if not regular_window():
        raise RuntimeError('OUTSIDE_SUBMISSION_WINDOW')
    params = inspect.signature(session.call_tool).parameters
    if 'arguments' in params:
        result = await session.call_tool(name, arguments=arguments)
    elif 'params' in params:
        from mcp.types import CallToolRequestParams
        result = await session.call_tool(params=CallToolRequestParams(name=name, arguments=arguments))
    else:
        raise RuntimeError('UNSUPPORTED_MCP_SIGNATURE')
    return unpack(result)

def fill_book(orders, symbol):
    executions = {}
    for order in orders:
        if order.get('symbol') != symbol:
            continue
        cumulative = dec(order['cumulative_quantity'])
        rows = order.get('executions') or []
        if sum((dec(row['quantity']) for row in rows), Decimal(0)) != cumulative:
            raise RuntimeError('INCOMPLETE_EXECUTION_HISTORY')
        for row in rows:
            identity = row['id']
            item = (row['timestamp'], identity, order['side'], str(dec(row['quantity'])), str(dec(row['price'])))
            if identity in executions and executions[identity] != item:
                raise RuntimeError('CONFLICTING_EXECUTION_ID')
            executions[identity] = item
    quantity = Decimal(0)
    gross = Decimal(0)
    for stamp, identity, side, raw_qty, raw_price in sorted(executions.values(), key=lambda item: (datetime.fromisoformat(item[0].replace('Z', '+00:00')), item[1])):
        qty, price = (dec(raw_qty), dec(raw_price))
        if qty <= 0 or price <= 0:
            raise RuntimeError('INVALID_EXECUTION')
        if side == 'buy':
            quantity += qty
            gross += qty * price
        elif side == 'sell':
            if qty > quantity:
                raise RuntimeError('MISSING_PRIOR_BUY_HISTORY')
            gross *= (quantity - qty) / quantity
            quantity -= qty
        else:
            raise RuntimeError('UNKNOWN_EXECUTION_SIDE')
    return (quantity, gross)

def foreign_execution_ids(orders, symbol, own_refs, own_ids):
    return sorted({execution['id'] for order in orders if order.get('symbol') == symbol and order.get('ref_id') not in own_refs and (order.get('id') not in own_ids) for execution in order.get('executions') or []})

async def quote_bid(session, symbol):
    data = await monitor.read_tool(session, 'get_equity_quotes', {'symbols': [symbol]})
    matches = [item['quote'] for item in data.get('results') or [] if isinstance(item, dict) and isinstance(item.get('quote'), dict) and (item['quote'].get('symbol') == symbol)]
    if len(matches) != 1:
        raise RuntimeError('QUOTE_NOT_UNIQUE')
    quote = matches[0]
    bid = dec(quote['bid_price'])
    stamp = datetime.fromisoformat(quote['venue_bid_time'].replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise RuntimeError('QUOTE_TIMEZONE_MISSING')
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if bid <= 0 or not -2 <= age <= 10:
        raise RuntimeError('STALE_OR_INVALID_BID')
    return bid

def matching_orders_v2(orders, row):
    broker_id = row['broker_id']
    if broker_id:
        return [order for order in orders if order.get('id') == broker_id]
    refs = {value for value in (row['ref'], row['broker_ref']) if value}
    return [order for order in orders if order.get('ref_id') in refs]
ACTIVE_POLICY = None

def review_load_milestones(db, symbol, saved, policy):
    import hashlib
    profit_keys = ('profit_start_pct', 'profit_goal_pct', 'profit_floor_pct')
    identity = json.dumps([symbol, saved['adopted_at'], saved['initial_qty'], saved['average_ex_fees']], separators=(',', ':'))
    db.execute('\n        CREATE TABLE IF NOT EXISTS profit_milestones_review_v4(\n            position_key TEXT PRIMARY KEY,\n            policy_hash TEXT NOT NULL,\n            profit_policy TEXT NOT NULL,\n            state TEXT NOT NULL\n        )\n    ')
    row = db.execute('SELECT policy_hash,profit_policy,state FROM profit_milestones_review_v4 WHERE position_key=?', (identity,)).fetchone()
    if row is None:
        pinned = {key: policy[key] for key in profit_keys}
        fingerprint = hashlib.sha256(json.dumps(pinned, sort_keys=True).encode('utf-8')).hexdigest()
        initial = {'protection_armed': False, 'floor_armed': False}
        db.execute('INSERT INTO profit_milestones_review_v4 VALUES(?,?,?,?)', (identity, fingerprint, json.dumps(pinned, sort_keys=True), json.dumps(initial, sort_keys=True)))
        db.commit()
        state = initial
    else:
        pinned = json.loads(row['profit_policy'])
        if not isinstance(pinned, dict) or set(pinned) != set(profit_keys):
            raise RuntimeError('INVALID_PINNED_PROFIT_POLICY')
        fingerprint = hashlib.sha256(json.dumps(pinned, sort_keys=True).encode('utf-8')).hexdigest()
        if fingerprint != row['policy_hash']:
            raise RuntimeError('PINNED_POLICY_HASH_MISMATCH')
        state = json.loads(row['state'])
    policy.update(pinned)
    return (identity, fingerprint, state)

def review_save_milestones(db, identity, fingerprint, state):
    cursor = db.execute('UPDATE profit_milestones_review_v4 SET state=? WHERE position_key=? AND policy_hash=?', (json.dumps(state, sort_keys=True), identity, fingerprint))
    if cursor.rowcount != 1:
        raise RuntimeError('MILESTONE_POSITION_OR_POLICY_MISMATCH')
    db.commit()

async def _v4_review_exit_decision(db, session, symbol, saved, policy):
    entry = dec(saved['average_ex_fees'])
    stop_pct = dec(policy['stop_loss_pct']) if policy is not None else ACTIVE_STOP_PCT
    loss_price = entry * (Decimal('1') - stop_pct / Decimal('100'))

    def loss_result(price):
        return {'action': 'EXIT_LOSS_STOP', 'bid': str(price), 'trigger_price': str(loss_price)}

    def degraded_result(price, error):
        log(symbol, 'PROFIT_PROTECTION_UNAVAILABLE', error_type=type(error).__name__, loss_stop_still_evaluated=True)
        return {'action': 'HOLD_PROFIT_STATE_UNAVAILABLE', 'bid': str(price), 'profit_protection_available': False}
    bid = await quote_bid(session, symbol)
    if bid <= loss_price:
        return loss_result(bid)
    if policy is None:
        return {'action': 'HOLD', 'bid': str(bid)}
    try:
        from profit_policy_draft import evaluate
        identity, fingerprint, previous = review_load_milestones(db, symbol, saved, policy)
    except Exception as error:
        return degraded_result(bid, error)

    def assess(price, score=None, fresh=False):
        nonlocal previous
        result = evaluate(entry=entry, bid=price, settings=policy, previous=previous, score=score, score_fresh=fresh)
        review_save_milestones(db, identity, fingerprint, result['state'])
        previous = result['state']
        return result
    try:
        result = assess(bid)
    except Exception as error:
        return degraded_result(bid, error)
    protective = {'EXIT_ENTRY_PRICE', 'EXIT_PROFIT_FLOOR'}
    if result['action'] in protective:
        return {**result, 'bid': str(bid)}
    goal = entry * (Decimal('1') + dec(policy['profit_goal_pct']) / Decimal('100'))
    if bid < goal:
        return {**result, 'bid': str(bid)}
    score, fresh = (None, False)
    try:
        from rally_reader_draft import fetch
        observation = await asyncio.wait_for(fetch(symbol), timeout=9)
        score = observation.get('score')
        fresh = observation.get('fresh') is True
    except Exception:
        pass
    bid = await quote_bid(session, symbol)
    if bid <= loss_price:
        return loss_result(bid)
    try:
        result = assess(bid, score, fresh)
    except Exception as error:
        return degraded_result(bid, error)
    return {**result, 'bid': str(bid)}

async def _v4_handle_symbol(db, session, account, positions, orders, symbol):
    own = db.execute("SELECT * FROM intents WHERE symbol=? AND state != 'LOCAL_PERMISSION_BLOCKED' ORDER BY created_at", (symbol,)).fetchall()
    own_refs = {value for row in own for value in (row['ref'], row['broker_ref']) if value}
    own_ids = {row['broker_id'] for row in own if row['broker_id']}
    pending = False
    sold = Decimal(0)
    for row in own:
        matches = matching_orders_v2(orders, row)
        if len(matches) != 1:
            raise RuntimeError('ORDER_OUTCOME_UNRESOLVED_NO_RESUBMISSION')
        order = matches[0]
        if order.get('symbol') != symbol or order.get('side') != 'sell':
            raise RuntimeError('ORDER_IDENTITY_MISMATCH')
        filled = dec(order['cumulative_quantity'])
        if not dec(row['filled']) <= filled <= dec(row['requested_qty']):
            raise RuntimeError('INVALID_RECONCILED_FILL')
        state = order['state']
        average = dec(order.get('average_price') or '0')
        fees = dec(order['fees'])
        if fees < 0 or (filled > 0 and average <= 0):
            raise RuntimeError('INVALID_BROKER_FILL_PRICE_OR_FEES')
        broker_id = order.get('id')
        if not broker_id:
            raise RuntimeError('BROKER_ORDER_ID_MISSING')
        broker_ref = order.get('ref_id')
        db.execute('UPDATE intents SET state=?,filled=?,broker_id=?,broker_ref=?,average_price=?,fees=? WHERE ref=?', (state, str(filled), broker_id, broker_ref, str(average), str(fees), row['ref']))
        own_ids.add(broker_id)
        if broker_ref:
            own_refs.add(broker_ref)
        sold += filled
        pending = pending or state not in TERMINAL
    db.commit()
    if pending:
        log(symbol, 'ORDER_PENDING_NO_DUPLICATE')
        return
    matches = [position for position in positions if position.get('symbol') == symbol]
    if len(matches) > 1:
        raise RuntimeError('POSITION_NOT_UNIQUE')
    position = matches[0] if matches else None
    current = dec(position['quantity']) if position else Decimal(0)
    saved = db.execute('SELECT * FROM managed WHERE symbol=?', (symbol,)).fetchone()
    if saved is None:
        if own or current != EXPECTED[symbol]:
            raise RuntimeError('AUTHORIZED_INITIAL_QUANTITY_CHANGED')
        reconstructed_qty, gross = fill_book(orders, symbol)
        if reconstructed_qty != current or gross <= 0:
            raise RuntimeError('ENTRY_BASIS_NOT_RECONCILED')
        average = gross / current
        ids = foreign_execution_ids(orders, symbol, own_refs, own_ids)
        db.execute('INSERT INTO managed VALUES(?,?,?,?,?)', (symbol, str(current), str(average), json.dumps(ids), now()))
        db.commit()
        saved = db.execute('SELECT * FROM managed WHERE symbol=?', (symbol,)).fetchone()
        log(symbol, 'POSITION_ADOPTED', quantity=str(current), average_ex_fees=str(average))
    if foreign_execution_ids(orders, symbol, own_refs, own_ids) != json.loads(saved['external_executions']):
        raise RuntimeError('EXTERNAL_TRADING_DETECTED')
    expected_remaining = dec(saved['initial_qty']) - sold
    if expected_remaining < 0 or current != expected_remaining:
        raise RuntimeError('HOLDINGS_NOT_RECONCILED')
    if current == 0:
        log(symbol, 'MANAGED_POSITION_CLOSED')
        return
    external_open = [order for order in orders if order.get('symbol') == symbol and order.get('state') not in TERMINAL and (order.get('ref_id') not in own_refs) and (order.get('id') not in own_ids)]
    if external_open:
        raise RuntimeError('EXTERNAL_ORDER_ACTIVE')
    if dec(position['shares_available_for_sells']) < current:
        raise RuntimeError('SHARES_NOT_FULLY_SELLABLE')
    policy_snapshot = dict(ACTIVE_POLICY) if ACTIVE_POLICY is not None else None
    if not regular_window():
        log(symbol, 'OUTSIDE_SUBMISSION_WINDOW')
        return
    decision = await review_exit_decision(db, session, symbol, saved, policy_snapshot)
    if not decision['action'].startswith('EXIT_'):
        log(symbol, 'MONITORING', decision=decision)
        return
    log(symbol, 'EXIT_CANDIDATE', decision=decision)
    args = {'account_number': account, 'symbol': symbol, 'side': 'sell', 'type': 'market', 'quantity': str(current), 'time_in_force': 'gfd', 'market_hours': 'regular_hours'}
    review = await order_call(session, 'review_equity_order', args)
    if review.get('symbol') != symbol or review.get('side') != 'sell' or review.get('type') != 'market':
        raise RuntimeError('REVIEW_IDENTITY_MISMATCH')
    if not isinstance(review.get('order_checks'), dict) or review['order_checks']:
        raise RuntimeError('BROKER_REVIEW_ALERT')
    if review.get('customer_approval_required') is not False:
        raise RuntimeError('UNATTENDED_ORDER_NOT_APPROVED')
    if not regular_window():
        log(symbol, 'TRIGGER_NO_LONGER_VALID')
        return
    decision = await review_exit_decision(db, session, symbol, saved, policy_snapshot)
    if not decision['action'].startswith('EXIT_') or not regular_window():
        log(symbol, 'TRIGGER_NO_LONGER_VALID')
        return
    reference = str(uuid.uuid4())
    db.execute('INSERT INTO intents(ref,symbol,requested_qty,state,created_at) VALUES(?,?,?,?,?)', (reference, symbol, str(current), 'SUBMITTING', now()))
    db.commit()
    args['ref_id'] = reference
    try:
        result = await order_call(session, 'place_equity_order', args)
        order = result.get('order')
        state = 'ACKNOWLEDGED' if isinstance(order, dict) and order.get('id') else 'UNKNOWN'
        broker_id = None
        broker_ref = None
        if isinstance(order, dict) and order.get('id'):
            if order.get('symbol') != symbol or order.get('side') != 'sell':
                raise RuntimeError('SUBMISSION_ORDER_IDENTITY_MISMATCH')
            broker_id = order['id']
            broker_ref = order.get('ref_id')
        db.execute('UPDATE intents SET state=?,broker_id=?,broker_ref=? WHERE ref=?', (state, broker_id, broker_ref, reference))
        db.commit()
        log(symbol, 'SELL_SUBMISSION_RETURNED', reference=reference, outcome=state, fill_verified=False)
    except LocalSellPermissionBlocked:
        db.execute("UPDATE intents SET state='LOCAL_PERMISSION_BLOCKED' WHERE ref=?", (reference,))
        db.commit()
        log(symbol, 'SELL_BLOCKED_LOCALLY', reference=reference, broker_submission_attempted=False)
        return
    except Exception:
        db.execute("UPDATE intents SET state='UNKNOWN' WHERE ref=?", (reference,))
        db.commit()
        raise RuntimeError('UNKNOWN_SUBMISSION_DO_NOT_RETRY') from None
ACTIVE_STOP_PCT = Decimal('1')
ACTIVE_STOP_REVISION = None

async def refresh_shared_stop():
    global ACTIVE_STOP_PCT, ACTIVE_STOP_REVISION, ACTIVE_POLICY
    REPORT_SETTINGS['last_refresh_attempt_at'] = now()
    try:
        revision, policy = await asyncio.to_thread(read_desired_settings_observation)
        stop = dec(policy['stop_loss_pct'])
        if not Decimal('0.1') <= stop <= Decimal('20'):
            raise ValueError('INVALID_SHARED_STOP')
        ACTIVE_POLICY = dict(policy)
        ACTIVE_STOP_PCT = stop
        ACTIVE_STOP_REVISION = revision
        REPORT_SETTINGS.update(last_successful_load_at=now(), active_revision=revision, active_policy=dict(policy), last_observed_desired_revision=revision, last_observed_desired_policy=dict(policy), last_refresh_error=None)
        log('ALL', 'REVIEW_POLICY_LOADED', settings_revision=revision, full_strategy_revision_applied=False)
    except Exception as error:
        REPORT_SETTINGS['last_refresh_error'] = type(error).__name__
        log('ALL', 'POLICY_REFRESH_FAILED_RETAINING_ACTIVE', error_type=type(error).__name__, retained_stop_loss_pct=str(ACTIVE_STOP_PCT), retained_stop_revision=ACTIVE_STOP_REVISION)

async def _v4_cycle(db):
    await refresh_shared_stop()
    oauth = auth.OAuthClientProvider(server_url=auth.SERVER, client_metadata=auth.OAuthClientMetadata(client_name='super-intelligence-vps', redirect_uris=[auth.REDIRECT], grant_types=['authorization_code', 'refresh_token'], response_types=['code'], token_endpoint_auth_method='none'), storage=auth.FileStorage(), redirect_handler=monitor.refuse_login, callback_handler=monitor.refuse_login)
    async with auth.httpx2.AsyncClient(auth=oauth, timeout=auth.httpx2.Timeout(30.0, read=120.0)) as client:
        async with auth.streamable_http_client(auth.SERVER, http_client=client) as streams:
            async with auth.ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                data = await monitor.read_tool(session, 'get_accounts', {})
                matches = [item for item in data.get('accounts') or [] if isinstance(item, dict) and item.get('agentic_allowed') is True and str(item.get('account_number', '')).endswith('5004')]
                if len(matches) != 1:
                    raise RuntimeError('ACCOUNT_BINDING_FAILED')
                account = matches[0]
                if account.get('state') != 'active' or account.get('deactivated') is not False or account.get('permanently_deactivated') is not False:
                    raise RuntimeError('ACCOUNT_NOT_ACTIVE')
                number = account['account_number']
                for symbol in EXPECTED:
                    try:
                        positions = await monitor.pages(session, 'get_equity_positions', number, 'positions')
                        orders = await monitor.pages(session, 'get_equity_orders', number, 'orders')
                        await handle_symbol(db, session, number, positions, orders, symbol)
                    except RuntimeError as error:
                        log(symbol, 'PROTECTION_BLOCKED', reason=str(error))

def read_desired_settings_observation():
    from urllib.request import Request, ProxyHandler, build_opener
    request = Request('http://127.0.0.1:8000/api/v1/strategy-settings', headers={'Accept': 'application/json', 'Cache-Control': 'no-cache'}, method='GET')
    opener = build_opener(ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        if response.status != 200:
            raise RuntimeError('SETTINGS_HTTP_ERROR')
        raw = response.read(262145)
        if len(raw) > 262144:
            raise RuntimeError('SETTINGS_RESPONSE_TOO_LARGE')
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('INVALID_SETTINGS_RESPONSE')
    revision = data.get('revision')
    if type(revision) is not int or revision < 1:
        raise ValueError('INVALID_SETTINGS_REVISION')
    profiles = data.get('profiles')
    if not isinstance(profiles, dict):
        raise ValueError('SETTINGS_PROFILES_MISSING')
    equity = profiles.get('equity')
    if not isinstance(equity, dict):
        raise ValueError('EQUITY_PROFILE_MISSING')
    value = equity.get('stop_loss_pct')
    if type(value) not in (int, float):
        raise ValueError('INVALID_STOP_LOSS_TYPE')
    stop = dec(value)
    if not Decimal('0.1') <= stop <= Decimal('20'):
        raise ValueError('INVALID_STOP_LOSS_RANGE')
    policy = {}
    for key in ('stop_loss_pct', 'profit_start_pct', 'profit_goal_pct', 'profit_floor_pct'):
        value = equity.get(key)
        if type(value) not in (int, float):
            raise ValueError('INVALID_POLICY_TYPE')
        policy[key] = str(dec(value))
    from profit_policy_draft import evaluate
    evaluate(entry=100, bid=100, settings=policy)
    return (revision, policy)

def publish_settings_observation():
    import tempfile
    loaded = ACTIVE_STOP_REVISION is not None
    payload = _report_payload()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=ROOT, prefix='.settings-observation-', delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, ROOT / 'settings-observation.json')
        temporary = None
    except Exception as error:
        log('ALL', 'SETTINGS_STATUS_WRITE_FAILED', error_type=type(error).__name__)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass

async def observe_desired_settings():
    while True:
        publish_settings_observation()
        await asyncio.sleep(5)

async def main():
    import equity_commands_v1 as equity_commands
    equity_commands.initialize_worker()
    db = database()
    settings_observer = asyncio.create_task(observe_desired_settings())
    while True:
        try:
            equity_commands.apply_pending()
            await asyncio.wait_for(cycle(db), timeout=180)
        except Exception as error:
            log('ALL', 'BROKER_CYCLE_FAILED', error_type=type(error).__name__)
        await asyncio.sleep(10)
from copy import deepcopy
PROFIT_KEYS = ('profit_start_pct', 'profit_goal_pct', 'profit_floor_pct')

def build_report(*, heartbeat_at, settings, cycle, positions):
    """Build status from explicit observations; never infer successful work."""
    settings = deepcopy(settings)
    cycle = deepcopy(cycle)
    positions = deepcopy(positions)
    loaded = settings.get('last_successful_load_at') is not None and settings.get('active_revision') is not None
    cycle_complete = cycle.get('last_attempt_id') is not None and cycle.get('last_attempt_id') == cycle.get('last_completed_id')
    symbols = {}
    for symbol, item in positions.items():
        evaluated = item.get('evaluation_cycle_id') is not None and item.get('evaluation_cycle_id') == cycle.get('last_attempt_id') and (item.get('evaluation_at') is not None)
        decision = item.get('decision') if evaluated else None
        if not isinstance(decision, dict):
            decision = {}
        action = decision.get('action')
        degraded = []
        if not evaluated:
            degraded.append('NO_EVALUATION_IN_LATEST_CYCLE')
        if item.get('blocked_reason'):
            degraded.append('SYMBOL_BLOCKED')
        if action == 'HOLD_PROFIT_STATE_UNAVAILABLE':
            degraded.append('PROFIT_STATE_UNAVAILABLE')
        if action == 'EXIT_SCORE_UNAVAILABLE':
            degraded.append('RALLY_SCORE_UNAVAILABLE_FALLBACK_EXIT')
        pinned = item.get('pinned_profit_policy')
        if not isinstance(pinned, dict) or set(pinned) != set(PROFIT_KEYS):
            pinned = None
            degraded.append('PINNED_PROFIT_POLICY_UNAVAILABLE')
        milestones = item.get('persisted_milestones')
        valid_milestones = isinstance(milestones, dict) and set(milestones) == {'protection_armed', 'floor_armed'} and all((type(value) is bool for value in milestones.values())) and (not (milestones['floor_armed'] and (not milestones['protection_armed'])))
        if not valid_milestones:
            milestones = None
            degraded.append('PERSISTED_MILESTONES_UNAVAILABLE')
        symbols[symbol] = {'effective_profit_policy': pinned, 'profit_policy_source': 'POSITION_PINNED' if pinned is not None else 'UNKNOWN', 'decision_stop_loss_pct': item.get('decision_stop_loss_pct'), 'decision_settings_revision': item.get('decision_settings_revision'), 'persisted_milestones': milestones, 'milestones_observed_at': item.get('milestones_observed_at'), 'evaluated_in_latest_cycle': evaluated, 'last_evaluation_at': item.get('evaluation_at'), 'latest_cycle_action': action, 'blocked_reason': item.get('blocked_reason'), 'degraded_reasons': degraded, 'broker_execution_verified': False}
    return {'schema': 'equity-review-report-v5', 'heartbeat_at': heartbeat_at, 'settings': settings, 'settings_loaded_successfully_at_least_once': loaded, 'cycle': cycle, 'latest_cycle_completed': cycle_complete, 'positions': symbols, 'full_strategy_revision_applied': False, 'full_protection_verified': False, 'broker_execution_verified': False, 'not_connected': ['daily_loss_limit_pct', 'max_positions', 'automatic_equity_buying']}
from copy import deepcopy
REPORT_SETTINGS = {}
REPORT_CYCLE = {}
REPORT_POSITIONS = {}

def _report_position(db, symbol, saved, policy, decision):
    item = REPORT_POSITIONS.setdefault(symbol, {})
    item.update(evaluation_cycle_id=REPORT_CYCLE.get('last_attempt_id'), evaluation_at=now(), decision=dict(decision), blocked_reason=None, decision_stop_loss_pct=str(policy['stop_loss_pct'] if policy is not None else ACTIVE_STOP_PCT), decision_settings_revision=ACTIVE_STOP_REVISION, pinned_profit_policy=None, persisted_milestones=None, milestones_observed_at=None)
    identity = json.dumps([symbol, saved['adopted_at'], saved['initial_qty'], saved['average_ex_fees']], separators=(',', ':'))
    try:
        import hashlib
        row = db.execute('SELECT policy_hash,profit_policy,state FROM profit_milestones_review_v4 WHERE position_key=?', (identity,)).fetchone()
        if row is not None:
            pinned = json.loads(row['profit_policy'])
            fingerprint = hashlib.sha256(json.dumps(pinned, sort_keys=True).encode('utf-8')).hexdigest()
            if fingerprint != row['policy_hash']:
                raise ValueError('REPORT_PINNED_POLICY_HASH_MISMATCH')
            item.update(pinned_profit_policy=pinned, persisted_milestones=json.loads(row['state']), milestones_observed_at=now())
        item.pop('reporting_error', None)
    except Exception as error:
        item['reporting_error'] = type(error).__name__

async def review_exit_decision(db, session, symbol, saved, policy):
    result = await _v4_review_exit_decision(db, session, symbol, saved, policy)
    try:
        _report_position(db, symbol, saved, policy, result)
    except Exception as error:
        log(symbol, 'REPORT_POSITION_FAILED', error_type=type(error).__name__)
    return result

async def handle_symbol(db, session, account, positions, orders, symbol):
    item = REPORT_POSITIONS.setdefault(symbol, {})
    item['blocked_reason'] = None
    item['attempt_cycle_id'] = REPORT_CYCLE.get('last_attempt_id')
    try:
        return await _v4_handle_symbol(db, session, account, positions, orders, symbol)
    except Exception as error:
        item['blocked_reason'] = str(error)
        raise

async def cycle(db):
    identifier = REPORT_CYCLE.get('last_attempt_id', 0) + 1
    REPORT_CYCLE.update(last_attempt_id=identifier, last_started_at=now(), last_error=None)
    try:
        result = await _v4_cycle(db)
    except BaseException as error:
        REPORT_CYCLE['last_error'] = type(error).__name__
        raise
    REPORT_CYCLE.update(last_completed_id=identifier, last_completed_at=now())
    return result

def _report_payload():
    payload = build_report(heartbeat_at=now(), settings=REPORT_SETTINGS, cycle=REPORT_CYCLE, positions=REPORT_POSITIONS)
    for symbol in EXPECTED:
        if symbol not in payload['positions']:
            payload['positions'][symbol] = {'evaluated_in_latest_cycle': False, 'degraded_reasons': ['NO_POSITION_OBSERVATION'], 'broker_execution_verified': False}
    for symbol, item in payload['positions'].items():
        reporting_error = REPORT_POSITIONS.get(symbol, {}).get('reporting_error')
        if reporting_error:
            item['reporting_error'] = reporting_error
            item['degraded_reasons'].append('POSITION_REPORT_READ_FAILED')
    loaded = ACTIVE_STOP_REVISION is not None
    payload.update(checked_at=payload['heartbeat_at'], pid=os.getpid(), state='PARTIALLY_APPLIED' if loaded else 'FIXED_FALLBACK', source='/api/v1/strategy-settings', stop_loaded_revision=ACTIVE_STOP_REVISION, decision_stop_loss_pct=str(ACTIVE_STOP_PCT), decision_source='SHARED_SETTINGS_LAST_SUCCESSFUL_LOAD' if loaded else 'INITIAL_FIXED_FALLBACK', applied_parameters=['stop_loss_pct'] if loaded else [], per_position_parameters=list(PROFIT_KEYS), not_applied_parameters=['daily_loss_limit_pct', 'max_positions'], worker_applied_revision=None, full_strategy_revision_applied=False, worker_enforcement='PER_POSITION_STATUS_SEE_POSITIONS', observation_only=not loaded, current_desired_revision_match_verified=False, broker_execution_verified=False)
    return payload
if __name__ == '__main__':
    os.umask(63)
    with open(ROOT / 'worker.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(main())
