import getpass
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from kiteconnect import KiteConnect
ENV_FILE = Path('/etc/super-intelligence/brokers.env')

def main():
    if os.geteuid() != 0:
        raise RuntimeError('Run through the root systemd command provided')
    key = os.environ.get('ZERODHA_API_KEY', '').strip()
    secret = os.environ.get('ZERODHA_API_SECRET', '').strip()
    if not key or not secret:
        raise RuntimeError('ZERODHA_API_KEY or ZERODHA_API_SECRET is missing')
    kite = KiteConnect(api_key=key)
    print('\nOpen this URL in your own browser and complete login/2FA:\n')
    print(kite.login_url())
    print('\nAfter login, copy the complete redirected URL from your browser.\nPaste it at the hidden prompt below—not into chat.')
    redirected = getpass.getpass('Paste redirected URL: ').strip()
    query = parse_qs(urlsplit(redirected).query)
    tokens = query.get('request_token', [])
    if len(tokens) != 1 or not tokens[0]:
        raise RuntimeError('The redirected URL has no unique request_token')
    if query.get('status') and query['status'] != ['success']:
        raise RuntimeError('Browser login did not report success')
    session = kite.generate_session(tokens[0], api_secret=secret)
    access = session.get('access_token')
    if not isinstance(access, str) or not access:
        raise RuntimeError('No access token returned')
    kite.set_access_token(access)
    profile = kite.profile()
    user_id = profile.get('user_id')
    if not user_id:
        raise RuntimeError('New session could not verify the account')
    print('\nVerified Zerodha account:', user_id)
    print('No order has been placed.')
    if input('Type SAVE to save this account session: ').strip() != 'SAVE':
        print('Canceled. Environment file unchanged.')
        return
    original = ENV_FILE.read_text()
    original_stat = ENV_FILE.stat()
    pattern = re.compile('^\\s*(?:export\\s+)?ZERODHA_ACCESS_TOKEN\\s*=')
    lines = [line for line in original.splitlines() if not pattern.match(line)]
    lines.append('ZERODHA_ACCESS_TOKEN=' + json.dumps(access))
    updated = '\n'.join(lines) + '\n'
    backup_dir = Path('/var/backups/super-intelligence/zerodha-auth')
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 448)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = backup_dir / ('brokers.env.' + stamp)
    shutil.copy2(ENV_FILE, backup)
    os.chmod(backup, 384)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=ENV_FILE.parent, prefix='.brokers.env.', delete=False) as handle:
            temporary = Path(handle.name)
            os.fchown(handle.fileno(), original_stat.st_uid, original_stat.st_gid)
            os.fchmod(handle.fileno(), original_stat.st_mode & 416 | 384)
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        if ENV_FILE.read_text() != original:
            raise RuntimeError('Environment file changed during repair. Refusing to overwrite it.')
        os.replace(temporary, ENV_FILE)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    print('\nZERODHA_SESSION_SAVED_AND_PROFILE_VERIFIED')
    print('Access token was not printed.')
    print('No trading services restarted. No orders placed.')
    print('Already-running processes still have their previous environment.')
if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('\nREPAIR_FAILED:', type(exc).__name__)
        safe_reasons = {'Run through the root systemd command provided', 'ZERODHA_API_KEY or ZERODHA_API_SECRET is missing', 'The redirected URL has no unique request_token', 'Browser login did not report success', 'No access token returned', 'New session could not verify the account', 'Environment file changed during repair. Refusing to overwrite it.'}
        reason = str(exc)
        if isinstance(exc, RuntimeError) and reason in safe_reasons:
            print('REASON:', reason)
        else:
            print('REASON: Broker or system error; sensitive details withheld.')
        print('Do not share tokens, secrets, or redirected login URLs in chat.')
        raise SystemExit(1)
