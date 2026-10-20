import contextlib
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path('/var/lib/trading-control')
BOTS = ('crypto', 'us_trading', 'zerodha')
class BuyBlocked(RuntimeError):
    pass

def valid(value):
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 2:
        raise BuyBlocked('BUY blocked: invalid control schema')
    bots = value.get('bots')
    if not isinstance(bots, dict) or set(bots) != set(BOTS):
        raise BuyBlocked('BUY blocked: incomplete bot controls')
    for bot in BOTS:
        if not isinstance(bots[bot], dict) or type(bots[bot].get('enabled')) is not bool:
            raise BuyBlocked('BUY blocked: invalid Boolean control')
    return value

@contextlib.contextmanager
def locked(exclusive=False):
    fd = None
    try:
        fd = os.open(ROOT / 'buy-controls.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o660)
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        raise BuyBlocked('BUY blocked: control lock unavailable') from exc
    try:
        yield
    finally:
        os.close(fd)

def read():
    try:
        return valid(json.loads((ROOT / 'buy-controls.json').read_text()))
    except BuyBlocked:
        raise
    except Exception as exc:
        raise BuyBlocked('BUY blocked: control state unavailable') from exc

def require_buy_enabled(bot):
    if bot not in BOTS:
        raise BuyBlocked('BUY blocked: unknown bot')
    if read()['bots'][bot]['enabled'] is not True:
        raise BuyBlocked('BUY blocked: trading toggle is OFF')

@contextlib.contextmanager
def submission_guard(bot, side):
    if bot not in BOTS or side not in ('BUY', 'SELL'):
        raise BuyBlocked('Order blocked: invalid bot or side')
    if side == 'SELL':
        yield
        return
    with locked():
        require_buy_enabled(bot)
        yield

def persist(value):
    valid(value)
    fd, name = tempfile.mkstemp(prefix='.buy-controls-', dir=ROOT)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o640)
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, ROOT / 'buy-controls.json')
        directory = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return read()

def update(bot, enabled):
    if bot not in BOTS or type(enabled) is not bool:
        raise ValueError('Unknown bot or non-Boolean enabled')
    with locked(True):
        value = read()
        # Deployment interlock: no enable until all running code has been verified.
        # This patch deliberately supplies no automatic unlock mechanism.
        if enabled and (ROOT / 'BUY_ENABLE_DEPLOYMENT_LOCK').exists():
            raise BuyBlocked('BUY enable locked: activation and enforcement verification pending')
        value['bots'][bot]['enabled'] = enabled
        # LAST_MANUAL_BUY_ACTION_V6: do not infer a confirmed Stop from bootstrap OFF.
        value['bots'][bot]['last_action'] = 'resume' if enabled else 'stop'
        value['bots'][bot]['last_action_at'] = datetime.now(timezone.utc).isoformat()
        value['updated_at'] = datetime.now(timezone.utc).isoformat()
        value['source'] = 'authenticated-dashboard'
        result = persist(value)
        print(json.dumps({'event': 'buy_control_changed', 'bot': bot,
                          'enabled': enabled, 'actor': 'validated-dashboard-session',
                          'updated_at': result['updated_at']}), file=sys.stderr)
        return result

if __name__ == '__main__':
    try:
        command = json.load(sys.stdin)
        if command.get('action') == 'get':
            result = read()
        elif command.get('action') == 'set':
            result = update(command.get('bot'), command.get('enabled'))
        else:
            raise ValueError('Invalid action')
        print(json.dumps({'ok': True, 'verified': True, 'state': result,
                          'activation_pending': (ROOT / 'BUY_ENABLE_DEPLOYMENT_LOCK').exists()}))
    except Exception as exc:
        print(json.dumps({'ok': False, 'verified': False, 'error': str(exc)
                          if isinstance(exc, (BuyBlocked, ValueError))
                          else 'Control persistence/read failed'}))
        sys.exit(1)
