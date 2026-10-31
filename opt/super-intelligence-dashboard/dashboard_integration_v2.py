import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
EQUITY_DB = Path('/var/lib/si-equity-r1/monitor.sqlite3')
EQUITY_CONFIG = Path('/var/lib/si-equity-r1/config.json')
NATIVE_CONFIG = Path('/etc/si-crypto-r1/config.json')
OLD_FEED = Path('/home/botuser/si-bots/dashboard_live_feed.json')

def now():
    return datetime.now(timezone.utc).isoformat()

def age_seconds(stamp):
    try:
        parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return round((datetime.now(timezone.utc) - parsed).total_seconds(), 1)
    except (AttributeError, TypeError, ValueError):
        return None

def safe_rows(rows, keys):
    if not isinstance(rows, list):
        raise ValueError('Expected broker rows.')
    return [{key: item[key] for key in keys if key in item} for item in rows[:200] if isinstance(item, dict)]

def equity_settings_observation():
    from decimal import Decimal
    result = {'status': 'MISSING', 'fresh': False, 'successful_observation': False, 'worker_applied_revision': None, 'full_strategy_revision_applied': False, 'worker_enforcement': 'NOT_VERIFIED', 'process_liveness_verified': False, 'broker_execution_verified': False}
    try:
        path = Path('/var/lib/si-equity-stop-guard/settings-observation.json')
        with path.open('rb') as handle:
            raw = handle.read(16385)
        if len(raw) > 16384:
            raise ValueError('Report too large')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('Invalid report')
        state = data.get('state')
        loaded = state == 'PARTIALLY_APPLIED'
        if state not in {'PARTIALLY_APPLIED', 'FIXED_FALLBACK'}:
            raise ValueError('Unsupported report state')
        if data.get('source') != '/api/v1/strategy-settings' or data.get('worker_applied_revision') is not None or data.get('full_strategy_revision_applied') is not False or (data.get('broker_execution_verified') is not False) or (data.get('current_desired_revision_match_verified') is not False) or (data.get('observation_only') is not (not loaded)):
            raise ValueError('Unexpected report semantics')
        pid = data.get('pid')
        if type(pid) is not int or pid <= 0:
            raise ValueError('Invalid PID')
        age = age_seconds(data.get('checked_at'))
        if age is None or age < 0:
            raise ValueError('Invalid timestamp')
        raw_stop = data.get('decision_stop_loss_pct')
        if not isinstance(raw_stop, str):
            raise ValueError('Invalid stop type')
        stop = Decimal(raw_stop)
        if not stop.is_finite() or not Decimal('0.1') <= stop <= Decimal('20'):
            raise ValueError('Invalid stop range')
        revision = data.get('stop_loaded_revision')
        expected_missing = ['profit_start_pct', 'profit_goal_pct', 'profit_floor_pct', 'daily_loss_limit_pct', 'max_positions']
        if data.get('not_applied_parameters') != expected_missing:
            raise ValueError('Unexpected unsupported parameters')
        if loaded:
            if type(revision) is not int or revision < 1 or data.get('applied_parameters') != ['stop_loss_pct'] or (data.get('decision_source') != 'SHARED_SETTINGS_LAST_SUCCESSFUL_LOAD') or (data.get('worker_enforcement') != 'STOP_CALCULATION_ONLY'):
                raise ValueError('Invalid partial application')
        elif revision is not None or stop != Decimal('1') or data.get('applied_parameters') != [] or (data.get('decision_source') != 'INITIAL_FIXED_FALLBACK') or (data.get('worker_enforcement') != 'FIXED_FALLBACK_ONLY'):
            raise ValueError('Invalid fallback')
        fresh = age <= 30
        result.update({'status': state if fresh else 'STALE', 'recorded_state': state, 'fresh': fresh, 'checked_at': data['checked_at'], 'age_seconds': age, 'reported_pid': pid, 'successful_observation': False, 'stop_setting_loaded': fresh and loaded, 'stop_loaded_revision': revision, 'decision_stop_loss_pct': raw_stop, 'decision_source': data['decision_source'], 'applied_parameters': data['applied_parameters'], 'not_applied_parameters': expected_missing, 'worker_enforcement': data['worker_enforcement'] if fresh else 'NOT_VERIFIED', 'observation_only': data['observation_only'], 'current_desired_revision_match_verified': False})
    except FileNotFoundError:
        pass
    except Exception as error:
        result.update({'status': 'INVALID_OR_UNREADABLE', 'error_type': type(error).__name__})
    return result

def equity_snapshot():
    config = json.loads(EQUITY_CONFIG.read_text())
    suffix = str(config['account_last4'])
    if len(suffix) != 4 or not suffix.isdigit():
        raise ValueError('Invalid equity account binding.')
    with sqlite3.connect('file:' + str(EQUITY_DB) + '?mode=ro', uri=True, timeout=5) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        health = db.execute('SELECT at,payload FROM health WHERE id=1').fetchone()
        latest = db.execute('SELECT at,summary,broker_data FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
    if latest is None:
        return {'status': 'NO_BROKER_SNAPSHOT', 'controls_connected': False}
    at, summary_text, broker_text = latest
    summary = json.loads(summary_text)
    broker = json.loads(broker_text)
    if summary.get('account') != '****' + suffix:
        raise ValueError('Equity snapshot account binding mismatch.')
    age = age_seconds(at)
    fresh = age is not None and 0 <= age <= 180
    portfolio = broker.get('portfolio')
    if not isinstance(portfolio, dict):
        raise ValueError('Invalid portfolio shape.')
    positions = broker.get('positions')
    orders = broker.get('orders')
    if not isinstance(positions, list) or not isinstance(orders, list):
        raise ValueError('Invalid broker row collections.')
    return {'status': 'SNAPSHOT_FRESH' if fresh else 'SNAPSHOT_STALE', 'snapshot_at': at, 'age_seconds': age, 'account': '****' + suffix, 'source': 'equity-monitor-broker-snapshot', 'settings_observation': equity_settings_observation(), 'controls_connected': False, 'automatic_entries_implemented': False, 'monitor_health': json.loads(health[1]).get('state') if health else None, 'portfolio': {key: portfolio[key] for key in ('currency', 'buying_power', 'cash', 'total_value', 'equity_value', 'crypto_value') if key in portfolio}, 'position_rows_total': len(positions), 'order_rows_total': len(orders), 'display_row_limit': 200, 'positions': safe_rows(positions, ('symbol', 'quantity', 'shares_available_for_sells', 'average_buy_price')), 'orders': safe_rows(orders, ('id', 'symbol', 'side', 'state', 'cumulative_quantity', 'average_price', 'fees', 'created_at', 'last_transaction_at'))}

def filter_legacy_feed(payload, native_suffix):
    import ast
    import hashlib

    def digest(value):
        return hashlib.sha256(str(value).encode()).hexdigest()
    if not isinstance(payload, dict) or not isinstance(payload.get('bots'), dict):
        raise ValueError('Invalid collector payload.')
    native_account = str(json.loads(NATIVE_CONFIG.read_text())['account_number'])
    collector = Path('/home/botuser/si-bots/dashboard_live_feed.py')
    tree = ast.parse(collector.read_text())
    mappings = [node for node in tree.body if isinstance(node, ast.Assign) and any((isinstance(target, ast.Name) and target.id == 'TARGETS' for target in node.targets))]
    if len(mappings) != 1:
        raise ValueError('Collector target mapping is not unique.')
    required = str(ast.literal_eval(mappings[0].value)['crypto'])
    result = dict(payload)
    bots = dict(payload['bots'])
    crypto = bots.get('crypto')
    proof = crypto.get('account_link', {}) if isinstance(crypto, dict) else {}
    proof_age = age_seconds(proof.get('verified_at'))
    verified = proof.get('verified') is True and proof.get('crypto_identifier_sha256') == digest(native_account) and (proof.get('brokerage_identifier_sha256') == digest(required)) and (proof.get('crypto_last4') == native_account[-4:]) and (proof.get('brokerage_last4') == required[-4:]) and (str(native_suffix) == native_account[-4:]) and isinstance(crypto, dict) and (crypto.get('account') == '****' + required[-4:]) and (proof_age is not None) and (0 <= proof_age <= 300)
    if not verified:
        bots.pop('crypto', None)
        models = result.get('accounting_v4')
        if isinstance(models, dict):
            models = dict(models)
            models['bots'] = {k: v for k, v in models.get('bots', {}).items() if k != 'crypto'}
            result['accounting_v4'] = models
    result['bots'] = bots
    result['crypto_account_binding_warning'] = None if verified else 'CRYPTO_FULL_ACCOUNT_LINK_NOT_YET_VERIFIED_OR_STALE'
    result['crypto_account_link'] = {'verified': bool(verified), 'verified_at': proof.get('verified_at'), 'brokerage_account': '****' + required[-4:], 'crypto_account': '****' + native_account[-4:]}
    result['account_suffix_check_is_not_full_identity_verification'] = False
    return result

def integration_snapshot():
    from strategy_settings_v1 import TOKEN
    import worker_controls_v1
    from crypto_worker_view import snapshot as runtime_snapshot
    result = {'checked_at': now(), 'csrf': TOKEN, 'crypto': None, 'equity': None, 'zerodha': {'status': 'RUNTIME_ADAPTER_NOT_CONNECTED', 'controls_connected': False}, 'manual_orders_connected': False}
    try:
        control = worker_controls_v1.snapshot()
        runtime = runtime_snapshot()
        control['runtime'] = {key: runtime.get(key) for key in ('account', 'process', 'heartbeat_fresh', 'display_state', 'configuration')}
        result['crypto'] = control
    except Exception as error:
        result['crypto'] = {'status': 'RUNTIME_UNAVAILABLE', 'error_type': type(error).__name__}
    try:
        result['equity'] = equity_snapshot()
    except Exception as error:
        result['equity'] = {'status': 'BROKER_SNAPSHOT_UNAVAILABLE', 'error_type': type(error).__name__, 'controls_connected': False}
    return result

def handle(handler):
    from strategy_settings_v1 import secure_request, send
    path = urlsplit(handler.path).path
    if path not in {'/api/v1/integration-v2', '/api/v1/live'}:
        return False
    if handler.command != 'GET':
        send(handler, 405, {'error': 'METHOD_NOT_ALLOWED'})
        return True
    if not secure_request(handler):
        send(handler, 403, {'error': 'LOCAL_SAME_ORIGIN_ACCESS_REQUIRED', 'message': 'Use the existing localhost SSH tunnel.'})
        return True
    try:
        if path == '/api/v1/integration-v2':
            payload = integration_snapshot()
        else:
            native = json.loads(NATIVE_CONFIG.read_text())
            suffix = str(native['account_number'])[-4:]
            payload = filter_legacy_feed(json.loads(OLD_FEED.read_text()), suffix)
        send(handler, 200, payload)
    except Exception as error:
        send(handler, 503, {'error': 'INTEGRATION_DATA_UNAVAILABLE', 'error_type': type(error).__name__})
    return True
