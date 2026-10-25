import os
import re
import hashlib
import tempfile
import requests
from pathlib import Path
ENV = Path('/etc/super-intelligence/brokers.env')

def read_env():
    result = {}
    for line in ENV.read_text().splitlines():
        m = re.match('^\\s*(?:export\\s+)?([A-Z0-9_]+)\\s*=\\s*(.*?)\\s*$', line)
        if m:
            value = m.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and (value[0] in '"\''):
                value = value[1:-1]
            result[m.group(1)] = value
    return result

def save_access_token(token):
    content = ENV.read_text()
    line = 'ZERODHA_ACCESS_TOKEN=' + token
    pattern = '(?m)^\\s*(?:export\\s+)?ZERODHA_ACCESS_TOKEN\\s*=.*$'
    if re.search(pattern, content):
        content = re.sub(pattern, lambda _: line, content, count=1)
    else:
        content = content.rstrip() + '\n' + line + '\n'
    fd, tmp = tempfile.mkstemp(prefix='.brokers.', dir=str(ENV.parent))
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        os.chown(tmp, 0, 0)
        os.chmod(tmp, 416)
        os.replace(tmp, ENV)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
cfg = read_env()
key = cfg.get('ZERODHA_API_KEY', '')
secret = cfg.get('ZERODHA_API_SECRET', '')
request_token = cfg.get('ZERODHA_REQUEST_TOKEN', '')
if not key or not secret or (not request_token):
    raise SystemExit('BLOCKED: API key, API secret, or request token is missing.')
if request_token.lower() in {'your_fresh_request_token', '[REDACTED_LITERAL]'}:
    raise SystemExit('BLOCKED: request token is a placeholder.')
checksum = hashlib.sha256((key + request_token + secret).encode()).hexdigest()
try:
    response = requests.post('https://api.kite.trade/session/token', data={'api_key': key, 'request_token': request_token, 'checksum': checksum}, headers={'X-Kite-Version': '3'}, timeout=20)
    data = response.json()
except Exception as exc:
    raise SystemExit('Exchange request failed: ' + type(exc).__name__)
if not response.ok or data.get('status') != 'success':
    err = data.get('error_type', 'ExchangeFailed')
    msg = data.get('message', 'No details returned')
    raise SystemExit(f'EXCHANGE FAILED: {err}: {msg}')
access_token = data.get('[REDACTED_LITERAL]', {}).get('[REDACTED_LITERAL]')
if not access_token:
    raise SystemExit('Exchange response did not contain an access token.')
save_access_token(access_token)
print('Access token exchanged and saved; value hidden.')
try:
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=key)
    kite.set_access_token(access_token)
    profile = kite.profile()
    print('ZERODHA PROFILE: PASS')
    print('Authenticated account returned by Kite; account details hidden.')
except Exception as exc:
    print('ZERODHA PROFILE: FAIL (' + type(exc).__name__ + ')')
    raise SystemExit(2)
