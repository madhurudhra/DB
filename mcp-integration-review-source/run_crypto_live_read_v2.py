"""Root launcher: GET-only worker with existing ubuntu Crypto credentials.
Private evidence is written under the review checkout, never to trading tables.
"""
import json, os, signal, sqlite3, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
WORKER = "\nimport contextlib,io,json,os,sys\nfrom datetime import datetime,timezone\nsys.dont_write_bytecode=True\nnamespace={};statuses=[];native=None;reader=None\nsummary={'status':'LIVE_READ_FAILED','order_submitted':False,'capital_baseline_published':False,'automatic_trading_activated':False}\ntry:\n    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):\n        import requests\n        exec(compile(COLLECTOR_SOURCE,'<readonly-collector>','exec'),namespace)\n        namespace['install_get_guard'](requests.sessions.Session,statuses)\n        os.chdir('/opt/super-intelligence')\n        sys.path.insert(0,'/opt/super-intelligence')\n        from app.brokers.robinhood.crypto import RobinhoodCrypto\n        for key in ('RH_ALLOW_LIVE_ORDERS','ROBINHOOD_CRYPTO_WRITE_ENABLED','WRITE_ENABLED','LIVE_TRADING_ENABLED'):\n            os.environ[key]='false'\n        native=RobinhoodCrypto()\n        class ReadOnlyV2Adapter:\n            def _request(self,method,path,params=None):\n                namespace['check'](method=='GET','NON_GET_BLOCKED')\n                # Reuse the inspected native signer's support for arbitrary\n                # signed GET paths; do not invoke its v1 account helper.\n                return native.request('GET',path,params=params)\n        reader=namespace['Reader'](ReadOnlyV2Adapter())\n        summary=namespace['collect'](reader,CONFIGURED_ACCOUNT)\nexcept Exception as exc:\n    cls=namespace.get('EvidenceError')\n    summary.update(error_type=type(exc).__name__,reason=str(exc) if cls is not None and isinstance(exc,cls) else 'READ_WORKER_FAILED')\n    if isinstance(exc,ModuleNotFoundError):summary['missing_module']=exc.name\nfinally:\n    if native is not None:\n        try:native.session.close()\n        except Exception:pass\nsummary['checked_at']=datetime.now(timezone.utc).isoformat()\nsummary['http_statuses']=statuses\nsummary['runtime_profile']='super-intelligence.service / superintel / .venv'\nprint(json.dumps({'summary':summary,'receipts':getattr(reader,'receipts',[])}))\n"

def configured_account():
    path = '/var/lib/super-intelligence/super-intelligence.db'
    try:
        result = subprocess.run(['systemctl', 'show', 'super-intelligence.service', '--property=MainPID', '--value'], capture_output=True, text=True, timeout=5)
        pid = int(result.stdout.strip())
        if pid > 0:
            for item in Path('/proc/' + str(pid) + '/environ').read_bytes().split(b'\x00'):
                if b'=' in item:
                    key, value = item.split(b'=', 1)
                    if key.lower() == b'database_path':
                        path = value.decode()
    except Exception:
        pass
    db = None
    try:
        db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        row = db.execute("SELECT config_json FROM independent_bot_configurations WHERE bot='crypto'").fetchone()
        if row is None:
            return (None, 'CONFIGURATION_MISSING')
        value = json.loads(row[0]).get('account_ref')
        return (value, 'CONFIGURED') if isinstance(value, str) and value.strip() == value and value else (None, 'ACCOUNT_NOT_CONFIGURED')
    except Exception:
        return (None, 'CONFIGURATION_NOT_READABLE')
    finally:
        if db is not None:
            db.close()
import pwd

class RuntimeProfileError(Exception):

    def __init__(self, code, details=None):
        super().__init__(code)
        self.details = details or {}

def resolve_api_runtime():
    result = subprocess.run(['systemctl', 'show', 'super-intelligence.service', '--property=MainPID', '--value'], capture_output=True, text=True, timeout=5, check=True)
    pid = int(result.stdout.strip())
    if pid <= 0:
        raise RuntimeProfileError('API_SERVICE_NOT_RUNNING')
    profile = pwd.getpwnam('superintel')
    proc = Path('/proc') / str(pid)
    if proc.stat().st_uid != profile.pw_uid:
        raise RuntimeProfileError('API_SERVICE_USER_MISMATCH')
    raw = proc.joinpath('environ').read_bytes()
    service_env = {}
    for item in raw.split(b'\x00'):
        if b'=' in item:
            key, value = item.split(b'=', 1)
            service_env[os.fsdecode(key)] = os.fsdecode(value)
    required = ('ROBINHOOD_CRYPTO_API_KEY', 'ROBINHOOD_CRYPTO_PRIVATE_KEY_B64')
    presence = {key: bool(service_env.get(key)) for key in required}
    if not all(presence.values()):
        raise RuntimeProfileError('NATIVE_CRYPTO_CREDENTIALS_MISSING_IN_API_SERVICE', {'credential_presence': presence})
    directory = Path('/opt/super-intelligence')
    if not (directory / '.venv/pyvenv.cfg').is_file():
        raise RuntimeProfileError('API_VENV_CONFIGURATION_MISSING')
    candidates = [directory / '.venv/bin/python', directory / '.venv/bin/python3']
    python = next((p for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)
    if python is None:
        raise RuntimeProfileError('API_VENV_INTERPRETER_MISSING')
    permitted = set(required) | {'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY', 'https_proxy', 'http_proxy', 'all_proxy', 'no_proxy'}
    environment = {key: value for key, value in service_env.items() if key in permitted}
    environment.update(HOME=profile.pw_dir, USER=profile.pw_name, LOGNAME=profile.pw_name, PYTHONDONTWRITEBYTECODE='1', RH_ALLOW_LIVE_ORDERS='false', ROBINHOOD_CRYPTO_WRITE_ENABLED='false', WRITE_ENABLED='false', LIVE_TRADING_ENABLED='false')
    return {'python': python, 'environment': environment, 'uid': profile.pw_uid, 'gid': profile.pw_gid, 'groups': os.getgrouplist(profile.pw_name, profile.pw_gid)}

def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this launcher as root.')
    root = Path(__file__).resolve().parent
    try:
        api_runtime = resolve_api_runtime()
    except RuntimeProfileError as exc:
        print(json.dumps(dict(status='LIVE_READ_BLOCKED', reason=str(exc), order_submitted=False, **exc.details), indent=2))
        return 1
    except Exception as exc:
        print(json.dumps({'status': 'LIVE_READ_BLOCKED', 'reason': 'API_RUNTIME_DISCOVERY_FAILED', 'error_type': type(exc).__name__, 'order_submitted': False}))
        return 1
    python = api_runtime['python']
    if not python.is_file():
        print(json.dumps({'status': 'LIVE_READ_BLOCKED', 'reason': 'API_VENV_INTERPRETER_MISSING', 'order_submitted': False}))
        return 1
    account, configuration_status = configured_account()
    source = (root / 'crypto_live_evidence_v2.py').read_text()
    code = 'COLLECTOR_SOURCE=' + repr(source) + '\nCONFIGURED_ACCOUNT=' + repr(account) + '\n' + WORKER
    proc = subprocess.Popen([str(python), '-'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, env=api_runtime['environment'], user=api_runtime['uid'], group=api_runtime['gid'], extra_groups=api_runtime['groups'], cwd='/opt/super-intelligence')
    try:
        stdout, stderr = proc.communicate(code, timeout=180)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        print(json.dumps({'status': 'LIVE_READ_BLOCKED', 'reason': 'READ_TIMEOUT', 'order_submitted': False}))
        return 1
    if proc.returncode != 0:
        print(json.dumps({'status': 'LIVE_READ_BLOCKED', 'reason': 'WORKER_DID_NOT_COMPLETE', 'worker_exit_code': proc.returncode, 'raw_output_withheld': True}))
        return 1
    try:
        packet = json.loads(stdout)
        summary = packet['summary']
        if not isinstance(summary, dict) or not isinstance(packet.get('receipts'), list):
            raise ValueError()
    except Exception:
        print(json.dumps({'status': 'LIVE_READ_BLOCKED', 'reason': 'INVALID_WORKER_OUTPUT', 'raw_output_withheld': True}))
        return 1
    private = root / 'private-evidence'
    if private.is_symlink():
        raise SystemExit('Refusing symlink evidence directory.')
    private.mkdir(exist_ok=True, mode=448)
    private.chmod(448)
    name = 'crypto-v2-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '-' + str(os.getpid()) + '.json'
    destination = private / name
    fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 384)
    with os.fdopen(fd, 'w') as handle:
        json.dump(packet, handle, indent=2)
    summary.update(saved_configuration_status=configuration_status, private_evidence_saved=True, private_evidence_file=str(destination))
    print(json.dumps(summary, indent=2))
    print('Share only this summary. Do not paste the private evidence file.')
    return 0 if summary.get('status') in ('LIVE_READ_EVIDENCE_COLLECTED', 'LIVE_READ_EVIDENCE_COLLECTED_WITH_WARNINGS') else 1
if __name__ == '__main__':
    raise SystemExit(main())
