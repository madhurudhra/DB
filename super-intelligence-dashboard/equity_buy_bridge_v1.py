"""Isolated buy-worker bridge. No DB writes or broker calls on import/GET."""
import importlib.util
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

CONFIG=Path('/etc/si-equity-buy-worker/config.json')
ROOT=Path('/var/lib/si-equity-buy-worker')
CODE=Path('/opt/si-equity-buy-worker')
FIELDS={'bot','auto_trade','buy','sell'}

def read_json(path, limit=262144):
    with Path(path).open('rb') as f:
        raw=f.read(limit+1)
    if len(raw)>limit: raise ValueError('JSON_TOO_LARGE')
    value=json.loads(raw)
    if not isinstance(value,dict): raise ValueError('JSON_OBJECT_REQUIRED')
    return value

def elapsed(stamp):
    d=datetime.fromisoformat(stamp.replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('TIMEZONE_REQUIRED')
    return (datetime.now(timezone.utc)-d).total_seconds()

def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def config():
    cfg=read_json(CONFIG)
    required={'state_directory':str(ROOT),
      'controls_database':str(ROOT/'controls.sqlite3'),
      'controls_module_path':str(CODE/'equity_commands_v1.py'),
      'funding_authorization_file':str(ROOT/'funding-authorization.json'),
      'universe_file':str(ROOT/'universe.json'),
      'broker_monitor_path':'/opt/si-equity-r1/broker_monitor.py',
      'settings_origin':'http://127.0.0.1:8000',
      'legacy_guard_database':'/var/lib/si-equity-stop-guard/guard.sqlite3'}
    if any(cfg.get(k)!=v for k,v in required.items()):
        raise ValueError('ISOLATED_CONFIGURATION_MISMATCH')
    for path in (CONFIG,ROOT,CODE,ROOT/'controls.sqlite3',CODE/'equity_commands_v1.py',ROOT/'funding-authorization.json',ROOT/'universe.json'):
        if path.is_symlink() or path.resolve()!=path:
            raise ValueError('ISOLATED_PATH_MUST_NOT_BE_A_SYMLINK')
    account=cfg.get('account_number')
    if not isinstance(account,str) or not account or '*' in account:
        raise ValueError('FULL_ACCOUNT_BINDING_REQUIRED')
    return cfg

def process_matches(pid):
    if type(pid) is not int or pid<=0: return False
    try:
        args=Path('/proc',str(pid),'cmdline').read_bytes().split(b'\0')
        return (str(CODE/'equity_worker.py').encode() in args
          and any(args[i]==b'--config' and args[i+1]==str(CONFIG).encode()
                  for i in range(len(args)-1)))
    except OSError: return False

def runtime():
    result={'worker_report_fresh':False,'process_liveness_verified':False,
      'controls':None,'commands':[],'worker_status':None}
    status=read_json(ROOT/'status.json')
    with sqlite3.connect((ROOT/'controls.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=5) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
        state=db.execute('SELECT * FROM equity_control_state WHERE id=1').fetchone()
        presence=db.execute("SELECT * FROM equity_worker_presence WHERE role='buy_worker'").fetchone()
        rows=db.execute('SELECT * FROM equity_commands ORDER BY id DESC LIMIT 15').fetchall()
    flags=json.loads(state['controls']) if state else None
    valid=isinstance(flags,dict) and set(flags)==FIELDS and all(type(v) is bool for v in flags.values())
    pid=status.get('pid')
    live=process_matches(pid)
    fresh=(live and valid and state is not None and presence is not None
      and state['worker_pid']==pid and presence['pid']==pid
      and 0<=elapsed(status['heartbeat_at'])<=45
      and 0<=elapsed(presence['at'])<=45
      and 0<=elapsed(state['acknowledged_at'])<=45)
    commands=[]
    for row in rows:
        item=dict(row)
        item['updates']=json.loads(item['updates'])
        item['result']=json.loads(item['result']) if item['result'] else None
        commands.append(item)
    result.update(worker_report_fresh=bool(fresh),process_liveness_verified=live,
      controls=flags if valid else None,commands=commands,
      worker_status={k:status.get(k) for k in ('state','reason','heartbeat_at','pid','last_cycle_completed','settings_revision')})
    return result

def snapshot():
    result={'bot':'equity','scope':'ISOLATED_EQUITY_BUY_WORKER',
      'broker_execution_verified':False,'worker_report_fresh':False,
      'process_liveness_verified':False,'controls':None,'commands':[],
      'buy_on_available':False,'blockers':[]}
    try: cfg=config()
    except Exception as exc:
        result['blockers'].append('CONFIGURATION_UNAVAILABLE:'+type(exc).__name__)
        return result
    result['account']='****'+cfg['account_number'][-4:]
    for key in ('live_enabled','programmatic_execution_authorized'):
        if cfg.get(key) is not True: result['blockers'].append(key.upper()+'_IS_FALSE')
    try:
        worker=load(CODE/'equity_worker.py','si_equity_buy_policy_reader')
        policy=_policy_snapshot_without_self_http(worker,cfg)
        if worker.number(policy['allocated_capital'])!=worker.number('130'):
            raise ValueError('AUTHORIZED_ALLOCATION_CHANGED')
        if worker.number(policy['capital_utilization_pct'])!=worker.number('90'):
            raise ValueError('SAVED_UTILIZATION_MUST_BE_90_PERCENT')
        result['settings_revision']=policy['revision']
    except Exception as exc:
        result['blockers'].append('POLICY_OR_FUNDING:'+str(exc)[:120])
    try:
        worker=load(CODE/'equity_worker.py','si_equity_buy_universe_reader')
        result['universe_symbols']=len(worker.universe(cfg))
    except Exception as exc:
        result['blockers'].append('UNIVERSE_UNAVAILABLE:'+type(exc).__name__)
    try: result.update(runtime())
    except Exception as exc:
        result['blockers'].append('WORKER_REPORT_UNAVAILABLE:'+type(exc).__name__)
    if result['worker_report_fresh'] is not True:
        result['blockers'].append('BUY_WORKER_NOT_FRESH_AND_RUNNING')
    result['buy_on_available']=not result['blockers']
    return result

def preflight():
    result=snapshot()
    ignored={'BUY_WORKER_NOT_FRESH_AND_RUNNING'}
    reasons=[r for r in result['blockers'] if r not in ignored and not r.startswith('WORKER_REPORT_UNAVAILABLE:')]
    if reasons: raise RuntimeError('; '.join(reasons))

def queue(body):
    if not isinstance(body,dict) or set(body)!={'bot','updates','request_id'} or body.get('bot')!='equity':
        raise ValueError('REQUIRE_EQUITY_COMMAND')
    if not isinstance(body['request_id'],str): raise ValueError('INVALID_REQUEST_ID')
    UUID(body['request_id'])
    updates=body['updates']
    if (not isinstance(updates,dict) or not updates or not set(updates)<=FIELDS
        or any(type(v) is not bool for v in updates.values())):
        raise ValueError('REQUIRE_BOOLEAN_CONTROLS')
    cfg=config()
    if any(updates.values()):
        status=snapshot()
        if status.get('worker_report_fresh') is not True:
            raise ValueError('BUY_WORKER_NOT_FRESH_AND_RUNNING')
        if updates.get('buy') is True and not status['buy_on_available']:
            raise ValueError('BUY_ON_BLOCKED:'+ '; '.join(status['blockers']))
        if cfg.get('live_enabled') is not True or cfg.get('programmatic_execution_authorized') is not True:
            raise ValueError('EXECUTION_NOT_ENABLED_AND_AUTHORIZED')
    if not (ROOT/'controls.sqlite3').is_file():
        raise ValueError('BUY_WORKER_COMMAND_STORE_NOT_INITIALIZED')
    module=load(CODE/'equity_commands_v1.py','si_equity_buy_commands_bridge')
    module.DB=ROOT/'controls.sqlite3'
    result=module.queue(body)
    result['scope']='ISOLATED_EQUITY_BUY_WORKER'
    return result

def handle(handler):
    if urlsplit(handler.path).path!='/api/v1/equity-buy-controls': return False
    from strategy_settings_v1 import secure_request, send
    post=handler.command=='POST'
    if not secure_request(handler,post=post):
        send(handler,403,{'error':'LOCAL_SAME_ORIGIN_ACCESS_REQUIRED'}); return True
    try:
        if handler.command=='GET': send(handler,200,snapshot())
        elif post:
            if handler.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                raise ValueError('JSON_REQUIRED')
            if handler.headers.get('Transfer-Encoding'): raise ValueError('CHUNKED_BODY_REFUSED')
            size=int(handler.headers.get('Content-Length','0'))
            if not 0<size<=4096: raise ValueError('INVALID_BODY_SIZE')
            send(handler,202,queue(json.loads(handler.rfile.read(size))))
        else: send(handler,405,{'error':'METHOD_NOT_ALLOWED'})
    except (ValueError,TypeError,UnicodeError) as exc:
        send(handler,409,{'error':str(exc)[:500]})
    except Exception as exc:
        send(handler,503,{'error':'BUY_CONTROLS_UNAVAILABLE','error_type':type(exc).__name__})
    return True


# SI_EQUITY_POLICY_NO_SELF_HTTP_V1
def _policy_snapshot_without_self_http(worker, cfg):
    import sys
    backend = sys.modules.get("__main__")
    filename = getattr(backend, "__file__", None)
    expected = Path("/opt/super-intelligence-dashboard/unified_control_api.py")
    in_backend = (
        isinstance(filename, str)
        and Path(filename).resolve() == expected
    )
    if not in_backend:
        return worker.policy_snapshot(cfg)

    import strategy_settings_v1
    saved = worker.read_local

    def read_in_process(base, route):
        if base != "http://127.0.0.1:8000":
            raise ValueError("UNAPPROVED_SETTINGS_ORIGIN")
        if route == "/api/v1/strategy-settings":
            return strategy_settings_v1.read_settings()
        if route == "/api/v1/config":
            return backend.load_state()
        raise ValueError("UNSUPPORTED_INTERNAL_SETTINGS_ROUTE")

    worker.read_local = read_in_process
    try:
        return worker.policy_snapshot(cfg)
    finally:
        worker.read_local = saved
