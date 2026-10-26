"""Private reference-style dashboard bridge. No broker-order submission routes."""
import base64, hashlib, hmac, json, os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
ROOT = Path(__file__).resolve().parent
UPSTREAM = 'http://127.0.0.1:8000'
UNITS = {'Backend': 'super-intelligence.service', 'Runner': 'super-intelligence-runner.service', 'Monitor': 'super-intelligence-monitor.service', 'Email worker': 'super-intelligence-email-alerts.service'}
READS = {'/api/settings': '/api/configurations', '/api/events': '/api/dashboard-bridge/v2/events'}
for bot in ('equity', 'crypto', 'zerodha'):
    READS['/api/data/' + bot] = '/api/dashboard/v1/bots/' + bot
    READS['/api/capital/' + bot] = '/api/dashboard/v1/bots/' + bot + '/capital'

def upstream(path, method='GET', payload=None, timeout=80):
    data = json.dumps(payload, separators=(',', ':')).encode() if payload is not None else None
    req = Request(UPSTREAM + path, data=data, method=method, headers={'Authorization': 'Bearer ' + TOKEN, 'Accept': 'application/json', 'Content-Type': 'application/json'})
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=timeout) as r:
            raw = r.read(8000001)
            if len(raw) > 8000000:
                return (502, {'error': 'Backend response too large'})
            return (r.status, json.loads(raw))
    except HTTPError as exc:
        try:
            body = json.loads(exc.read(65536))
        except Exception:
            body = {'error': 'Backend HTTP ' + str(exc.code)}
        return (exc.code, body)
    except Exception as exc:
        return (502, {'error': 'Backend unavailable', 'error_type': type(exc).__name__})

def summary():
    status = 'UNAVAILABLE'
    captured = None
    problem = None
    services = {name: {'ActiveState': 'unknown', 'SubState': 'unavailable', 'MainPID': '0'} for name in UNITS}
    try:
        p = Path('/run/si-dashboard-status/status.json')
        st = p.stat()
        if st.st_uid != 0 or st.st_mode & 18:
            raise ValueError('Untrusted status snapshot')
        value = json.loads(p.read_text())
        captured = value['captured_at']
        when = datetime.fromisoformat(captured)
        if when.tzinfo is None or not 0 <= (datetime.now(timezone.utc) - when).total_seconds() <= 45:
            raise ValueError('Stale status snapshot')
        if value.get('schema_version') != 1:
            raise ValueError('Unknown snapshot schema')
        services = {name: {k: str(row[k]) for k in ('ActiveState', 'SubState', 'MainPID', 'LoadState', 'error') if k in row} for name in UNITS for row in [value['services'][name]]}
        status = 'COLLECTED'
    except Exception as exc:
        problem = type(exc).__name__ + ': ' + str(exc)[:120]
    code, health = upstream('/health', timeout=5)
    return {'services': services, 'service_snapshot_status': status, 'service_snapshot_captured_at': captured, 'service_snapshot_error': problem, 'health': {'ok': code == 200 and isinstance(health, dict) and (health.get('status') == 'ok'), 'http_status': code}, 'checked_at': datetime.now(timezone.utc).isoformat(), 'capabilities': {'saved_settings_write': True, 'equity_order_review': True, 'broker_submission': False, 'automatic_execution_verified': False}}

def validate_settings(value):
    if not isinstance(value, dict) or set(value) != {'request_id', 'updates'}:
        raise ValueError('Invalid envelope')
    UUID(value['request_id'])
    updates = value['updates']
    if not isinstance(updates, list) or len(updates) != 1:
        raise ValueError('Save one bot at a time')
    item = updates[0]
    if not isinstance(item, dict) or set(item) != {'bot', 'expected_revision', 'changes'}:
        raise ValueError('Invalid update')
    if item['bot'] not in ('equity', 'crypto', 'zerodha') or type(item['expected_revision']) is not int or item['expected_revision'] < 0:
        raise ValueError('Invalid target/revision')
    changes = item['changes']
    allowed = {'allocated_capital', 'cash_reserve_pct', 'sell_protections', 'buy_restrictions'}
    if not isinstance(changes, dict) or not changes or (not set(changes) <= allowed):
        raise ValueError('Unsupported configuration fields')
    for key, v in changes.items():
        if key in ('allocated_capital', 'cash_reserve_pct'):
            if not isinstance(v, str):
                raise ValueError('Decimal strings required')
            n = Decimal(v)
            if not n.is_finite() or n < 0 or n > Decimal('1000000000000') or (n.as_tuple().exponent < -12):
                raise ValueError('Invalid amount')
            if key == 'cash_reserve_pct' and n >= 100:
                raise ValueError('Invalid reserve')
        elif not isinstance(v, list) or len(v) > 500:
            raise ValueError('Invalid rules')
    return True
READS.update({f'/api/valuation/{b}': f'/api/valuation/v1/{b}' for b in ('equity', 'crypto', 'zerodha')})

def dashboard_validate_write(path, value):
    if path == '/api/settings':
        return validate_settings(value)
    if path != '/api/controls':
        raise ValueError('Unsupported write route')
    if not isinstance(value, dict) or set(value) != {'request_id', 'expected_revision', 'bots', 'changes'}:
        raise ValueError('Invalid control envelope')
    UUID(value['request_id'])
    if type(value['expected_revision']) is not int or value['expected_revision'] < 0:
        raise ValueError('Invalid revision')
    if not isinstance(value['bots'], list) or len(value['bots']) != 1 or value['bots'][0] not in ('equity', 'crypto', 'zerodha'):
        raise ValueError('Select exactly one bot')
    changes = value['changes']
    if not isinstance(changes, dict) or not changes or (not set(changes) <= {'buy_enabled', 'sell_enabled', 'bot_enabled'}) or any((type(v) is not bool for v in changes.values())):
        raise ValueError('Only boolean control requests accepted')
    return True
READS.update({'/api/policy': '/api/trading-policy', '/api/controls': '/api/bot-controls'})

class Handler(BaseHTTPRequestHandler):
    server_version = 'IshaPrivateBridge/2'

    def reply(self, status, payload, kind='application/json; charset=utf-8', challenge=False):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        if challenge:
            self.send_header('WWW-Authenticate', 'Basic realm="Super Intelligence private dashboard", charset="UTF-8"')
        for key, value in [('Content-Type', kind), ('Content-Length', str(len(raw))), ('Cache-Control', 'no-store, private'), ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'no-referrer'), ('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")]:
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self):
        try:
            scheme, value = self.headers.get('Authorization', '').split(' ', 1)
            if scheme.lower() != 'basic':
                return False
            user, password = base64.b64decode(value, validate=True).decode().split(':', 1)
            if len(password) > 1024:
                return False
            hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(LOGIN['salt']), LOGIN['iterations']).hex()
            return hmac.compare_digest(user.encode(), LOGIN['username'].encode()) and hmac.compare_digest(hashed, LOGIN['hash'])
        except Exception:
            return False

    def do_GET(self):
        if not self.authorized():
            return self.reply(401, {'error': 'Dashboard login required'}, challenge=True)
        assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/index.html': ('index.html', 'text/html; charset=utf-8'), '/styles.css': ('styles.css', 'text/css; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/metrics.js': ('metrics.js', 'text/javascript; charset=utf-8'), '/configurations.js': ('configurations.js', 'text/javascript; charset=utf-8')}
        if self.path in assets:
            name, kind = assets[self.path]
            return self.reply(200, (ROOT / name).read_bytes(), kind)
        if self.path == '/api/summary':
            return self.reply(200, summary())
        if self.path in READS:
            code, value = upstream(READS[self.path])
            return self.reply(code, value)
        return self.reply(404, {'error': 'Not found'})

    def write_request(self, method):
        if not self.authorized():
            return self.reply(401, {'error': 'Dashboard login required'}, challenge=True)
        target = {('PATCH', '/api/settings'): ('/api/configurations', 65536), ('POST', '/api/equity-review'): ('/api/dashboard-bridge/v2/equity/review', 8192), ('PATCH', '/api/controls'): ('/api/bot-controls', 8192)}.get((method, self.path))
        if target is None:
            return self.reply(405, {'error': 'Order submission and runtime-control writes are not supported'})
        host = self.headers.get('Host', '')
        if host not in ('127.0.0.1:8088', 'localhost:8088') or self.headers.get('Origin') != 'http://' + host or self.headers.get('X-Dashboard-Write') != '1':
            return self.reply(403, {'error': 'Same-origin dashboard request required'})
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
            return self.reply(415, {'error': 'JSON required'})
        try:
            if self.headers.get('Transfer-Encoding'):
                raise ValueError('Chunked writes not accepted')
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= target[1]:
                raise ValueError('Invalid request size')
            value = json.loads(self.rfile.read(size))
            if method == 'PATCH':
                dashboard_validate_write(self.path, value)
            elif not isinstance(value, dict):
                raise ValueError('Object required')
        except Exception as exc:
            return self.reply(400, {'error': 'Invalid request', 'error_type': type(exc).__name__})
        code, result = upstream(target[0], method, value)
        return self.reply(code, result)

    def do_PATCH(self):
        return self.write_request('PATCH')

    def do_POST(self):
        return self.write_request('POST')

    def do_PUT(self):
        return self.reply(405, {'error': 'Method not supported'})
    do_DELETE = do_PUT
if __name__ == '__main__':
    credentials = Path(os.environ['CREDENTIALS_DIRECTORY'])
    TOKEN = (credentials / 'api-token').read_text().strip()
    LOGIN = json.loads((credentials / 'login').read_text())
    if not TOKEN:
        raise RuntimeError('Backend token empty')
    ThreadingHTTPServer(('127.0.0.1', 8088), Handler).serve_forever()
