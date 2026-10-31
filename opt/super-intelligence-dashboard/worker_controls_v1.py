import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID
DB = Path('/var/lib/si-crypto-r1/runtime.sqlite3')
KEY = 'dashboard_controls_v1'
FIELDS = {'bot', 'auto_trade', 'buy', 'sell'}
PREFIX = 'dashboard-v1:'

def now():
    return datetime.now(timezone.utc).isoformat()

def value(db, key, default=None):
    row = db.execute('SELECT v FROM kv WHERE k=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default

def store(db, key, item):
    db.execute('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (key, json.dumps(item, allow_nan=False)))

def controls(db):
    item = value(db, KEY)
    if item is None:
        item = {'bot': True, 'auto_trade': True, 'buy': value(db, 'entries_enabled', False) is True, 'sell': True}
    if not isinstance(item, dict) or set(item) != FIELDS or any((type(v) is not bool for v in item.values())):
        raise RuntimeError('INVALID_CONTROL_STATE')
    return item

def on_start(db):
    item = controls(db)
    item['buy'] = False
    store(db, KEY, item)
    store(db, 'entries_enabled', False)

def apply_commands(db):
    current = controls(db)
    rows = db.execute("SELECT * FROM commands WHERE state='QUEUED' ORDER BY id").fetchall()
    for row in rows:
        action = row['action']
        update = None
        if action in {'pause', 'resume'}:
            update = {'buy': action == 'resume'}
        elif action.startswith(PREFIX):
            try:
                update = json.loads(action[len(PREFIX):])
            except ValueError:
                pass
        valid = isinstance(update, dict) and bool(update) and (set(update) <= FIELDS) and all((type(v) is bool for v in update.values()))
        if valid:
            current = {**current, **update}
            store(db, KEY, current)
            store(db, 'entries_enabled', current['bot'] and current['auto_trade'] and current['buy'])
            store(db, 'dashboard_control_ack_v1', {'command_id': row['id'], 'applied_at': now(), 'worker_pid': os.getpid(), 'controls': current})
        db.execute('UPDATE commands SET state=?,applied_at=? WHERE id=?', ('APPLIED' if valid else 'REJECTED', now(), row['id']))
        db.commit()
    store(db, KEY, current)
    db.commit()

def permitted(db, side):
    item = controls(db)
    return side in {'buy', 'sell'} and item['bot'] and item['auto_trade'] and item[side] and (side != 'buy' or value(db, 'entries_enabled', False) is True)

def queue(body):
    if not isinstance(body, dict) or set(body) != {'bot', 'updates', 'request_id'} or body['bot'] != 'crypto':
        raise ValueError('Unsupported request.')
    request_id = body['request_id']
    if not isinstance(request_id, str):
        raise ValueError('Invalid request ID.')
    UUID(request_id)
    update = body['updates']
    if not isinstance(update, dict) or not update or (not set(update) <= FIELDS) or any((type(v) is not bool for v in update.values())):
        raise ValueError('Require Boolean control values.')
    payload = json.dumps(update, sort_keys=True)
    with sqlite3.connect('file:' + str(DB) + '?mode=rw', uri=True, timeout=5) as db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS dashboard_requests_v1(request_id TEXT PRIMARY KEY,payload TEXT NOT NULL,command_id INTEGER NOT NULL)')
        saved = db.execute('SELECT payload,command_id FROM dashboard_requests_v1 WHERE request_id=?', (request_id,)).fetchone()
        if saved:
            if saved[0] != payload:
                raise ValueError('Request ID already used for another command.')
            command_id = saved[1]
        else:
            cursor = db.execute('INSERT INTO commands(at,action,state) VALUES(?,?,?)', (now(), PREFIX + payload, 'QUEUED'))
            command_id = cursor.lastrowid
            db.execute('INSERT INTO dashboard_requests_v1 VALUES(?,?,?)', (request_id, payload, command_id))
        state = db.execute('SELECT state FROM commands WHERE id=?', (command_id,)).fetchone()[0]
    return {'request_id': request_id, 'command_id': command_id, 'command_state': state, 'worker_applied': state == 'APPLIED', 'order_submitted_by_this_http_request': False}

def snapshot():
    with sqlite3.connect('file:' + str(DB) + '?mode=ro', uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        heartbeat = value(db, 'heartbeat')
        flags = value(db, KEY)
        ack = value(db, 'dashboard_control_ack_v1')
        entries = value(db, 'entries_enabled')
        status = value(db, 'status', {})
        rows = db.execute('SELECT id,at,action,state,applied_at FROM commands WHERE action LIKE ? ORDER BY id DESC LIMIT 15', (PREFIX + '%',)).fetchall()
    age = None
    try:
        stamp = datetime.fromisoformat(heartbeat.replace('Z', '+00:00'))
        if stamp.tzinfo is not None:
            age = round((datetime.now(timezone.utc) - stamp).total_seconds(), 1)
    except (AttributeError, ValueError, TypeError):
        pass
    return {'bot': 'crypto', 'checked_at': now(), 'heartbeat_age_seconds': age, 'heartbeat_fresh': age is not None and 0 <= age <= 30, 'worker_controls': flags, 'entries_enabled': entries, 'worker_acknowledgement': ack, 'worker_status': {k: status.get(k) for k in ('state', 'reason', 'decision', 'score', 'managed_quantity', 'estimated_net_return_pct', 'last_cycle_completed')} if isinstance(status, dict) else {}, 'commands': [dict(row) for row in rows]}

def handle(handler):
    from strategy_settings_v1 import TOKEN, secure_request, send
    path = urlsplit(handler.path).path
    if path == '/api/v1/equity-controls':
        post = handler.command == 'POST'
        if not secure_request(handler, post=post):
            send(handler, 403, {'error': 'LOCAL_SAME_ORIGIN_ACCESS_REQUIRED'})
            return True
        try:
            import sys
            if '/opt/si-equity-r1' not in sys.path:
                sys.path.insert(0, '/opt/si-equity-r1')
            import equity_commands_v1 as equity_commands
            if handler.command == 'GET':
                send(handler, 200, equity_commands.snapshot())
            elif post:
                kind = handler.headers.get('Content-Type', '').split(';')[0].strip()
                if kind != 'application/json':
                    raise ValueError('JSON required.')
                size = int(handler.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096:
                    raise ValueError('Invalid body size.')
                body = json.loads(handler.rfile.read(size))
                send(handler, 202, equity_commands.queue(body))
            else:
                send(handler, 405, {'error': 'METHOD_NOT_ALLOWED'})
        except (ValueError, TypeError, UnicodeError) as error:
            send(handler, 400, {'error': str(error)})
        except Exception as error:
            send(handler, 503, {'error': 'EQUITY_CONTROLS_UNAVAILABLE', 'error_type': type(error).__name__})
        return True
    if path == '/api/v1/config/update' and handler.command == 'POST':
        send(handler, 409, {'error': 'LEGACY_TOGGLES_NOT_WORKER_CONTROLS', 'message': 'Use /worker-controls for installed Crypto controls.'})
        return True
    if path not in {'/worker-controls', '/api/v1/worker-controls'}:
        return False
    post = handler.command == 'POST'
    if not secure_request(handler, post=post):
        send(handler, 403, {'error': 'LOCAL_SAME_ORIGIN_ACCESS_REQUIRED'})
        return True
    try:
        if path == '/worker-controls' and handler.command == 'GET':
            page = PAGE.replace('__NONCE__', TOKEN).replace('__TOKEN__', json.dumps(TOKEN))
            send(handler, 200, page, html=True)
        elif path == '/api/v1/worker-controls' and handler.command == 'GET':
            send(handler, 200, snapshot())
        elif path == '/api/v1/worker-controls' and post:
            kind = handler.headers.get('Content-Type', '').split(';')[0].strip()
            if kind != 'application/json':
                raise ValueError('JSON required.')
            size = int(handler.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                raise ValueError('Invalid body size.')
            body = json.loads(handler.rfile.read(size))
            send(handler, 202, queue(body))
        else:
            send(handler, 405, {'error': 'METHOD_NOT_ALLOWED'})
    except (ValueError, TypeError, UnicodeError):
        send(handler, 400, {'error': 'INVALID_CONTROL_REQUEST'})
    except Exception as error:
        send(handler, 503, {'error': 'CONTROLS_UNAVAILABLE', 'error_type': type(error).__name__})
    return True
PAGE = '<!doctype html>\n<html><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>Crypto Worker Control Room</title>\n<style>\n:root{color-scheme:dark}\nbody{margin:0;color:#edf4ff;font:15px "Segoe UI",Arial,sans-serif;\nbackground:radial-gradient(at 5% 0%,#124d62,transparent 50%),\nradial-gradient(at 95% 10%,#452674,transparent 50%),#091223}\nmain{max-width:1080px;margin:auto;padding:24px}\nh1{font-size:28px}h2{font-size:18px}\nsection{background:linear-gradient(135deg,#1b2b45,#121e33);\nborder:1px solid #41577b;border-radius:15px;padding:20px;margin:18px 0;\nbox-shadow:0 12px 32px #0003}\na{color:#8feaff}button{font:inherit;padding:9px 20px;margin:4px;\nborder:1px solid #7185ae;border-radius:8px;color:#f5f9ff;background:#273b5d;\ncursor:pointer}button:disabled{opacity:.45;cursor:wait}\n.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;\nborder-bottom:1px solid #364966;padding:10px 0}\n.row span{min-width:125px}\npre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px monospace}\n.green{color:#61f0ac}.red{color:#ff91a6}.orange{color:#ffca7b}\n.grey{color:#b2bfd6}.warning{color:#ffd092}\n</style>\n<style id="si-top-worker-panel-style">\nmain{max-width:1480px}\n.si-top-worker-panels{\n display:grid;\n grid-template-columns:repeat(2,minmax(0,1fr));\n gap:16px;\n align-items:start;\n margin:18px 0;\n}\n.si-top-worker-panels>section{\n min-width:0;\n margin:0;\n}\n.si-top-worker-panels .row{gap:8px}\n.si-top-worker-panels .row span{min-width:82px}\n.si-top-worker-panels pre{\n max-height:340px;\n overflow:auto;\n white-space:pre-wrap;\n overflow-wrap:anywhere;\n}\n@media(max-width:900px){\n .si-top-worker-panels{grid-template-columns:1fr}\n}\n</style>\n\n</head><body><main>\n<a href="/">← Dashboard</a>\n<h1>Crypto &amp; Equity · Worker Control Room</h1>\n<!-- SI_CONTROL_WARNING_SCOPE_V1 -->\n<p class="warning">Crypto controls: OFF blocks the corresponding new automatic\nsubmissions, including software stop-loss exits when a required permission is\nOFF. Existing broker orders are not cancelled.\nEquity controls are not connected: these buttons do not start or stop the\nseparate sell-authorized Equity stop guard.</p>\n<!-- SI_SIDE_BY_SIDE_WORKER_PANELS_V1 -->\n<div class="si-top-worker-panels">\n<section><h2>Crypto · Worker-confirmed permissions</h2>\n<p id="health">Reading worker…</p><div id="controls"></div>\n<pre id="runtime"></pre></section>\n<section id="equity-control-section">\n<h2>US Equity · Worker Controls</h2>\n<p id="equity-monitor-status">Reading Equity monitor status…</p>\n<p class="warning">\nAutomatic-entry execution and worker-command controls are not connected.\nThe buttons below cannot start or stop the existing Equity stop guard.\n</p>\n<div id="equity-control-rows"></div>\n<pre id="equity-runtime"></pre>\n</section>\n</div>\n\n<section><h2>Action response</h2><pre id="message">No request sent.</pre>\n<h2>Durable command history</h2><pre id="history"></pre></section>\n\n<!-- SI_EQUITY_CONTROL_SECTION_V1 -->\n\n<script nonce="__NONCE__">\n(()=>{\n\'use strict\';\nconst byId=id=>document.getElementById(id);\nconst labels={\n bot:\'Bot\',\n auto_trade:\'Auto Trade\',\n buy:\'BUY\',\n sell:\'SELL\'\n};\nconst rows=byId(\'equity-control-rows\');\nfor(const field of [\'bot\',\'auto_trade\',\'buy\',\'sell\']){\n const row=document.createElement(\'div\');\n row.className=\'row\';\n const label=document.createElement(\'span\');\n label.textContent=labels[field];\n const status=document.createElement(\'span\');\n status.textContent=\'NOT CONNECTED\';\n status.className=\'grey\';\n row.append(label,status);\n for(const text of [\'ON\',\'OFF\']){\n  const button=document.createElement(\'button\');\n  button.type=\'button\';\n  button.textContent=text;\n  button.disabled=true;\n  button.title=\'Equity execution command adapter is not implemented.\';\n  row.append(button);\n }\n rows.append(row);\n}\nlet reading=false;\nasync function refreshEquity(){\n if(reading||document.hidden)return;\n reading=true;\n try{\n  const response=await fetch(\'/api/v1/integration-v2\',{\n   cache:\'no-store\',\n   credentials:\'same-origin\',\n   signal:AbortSignal.timeout(12000)\n  });\n  if(!response.ok)throw Error(\'HTTP \'+response.status);\n  const data=await response.json();\n  const equity=data.equity;\n  if(!equity||typeof equity!==\'object\'){\n   throw Error(\'Equity status missing\');\n  }\n  byId(\'equity-monitor-status\').textContent=\n   \'Broker monitor: \'+(equity.status||\'UNKNOWN\')+\n   \' · account \'+(equity.account||\'unverified\')+\n   \'. Monitoring status is not Auto Trade permission.\';\n  byId(\'equity-runtime\').textContent=JSON.stringify({\n   snapshot_at:equity.snapshot_at,\n   age_seconds:equity.age_seconds,\n   monitor_health:equity.monitor_health,\n   controls_connected:equity.controls_connected,\n   automatic_entries_implemented:equity.automatic_entries_implemented\n  },null,2);\n }catch(error){\n  byId(\'equity-monitor-status\').textContent=\n   \'Equity status unavailable: \'+error.message;\n  byId(\'equity-runtime\').textContent=\n   \'Current execution permissions are UNVERIFIED.\';\n }finally{\n  reading=false;\n }\n}\nrefreshEquity();\nsetInterval(refreshEquity,5000);\n})();\n</script>\n\n<section><h2>Scope</h2>\n<p>Crypto Bot OFF blocks new automatic execution; monitoring and\nreconciliation remain running. Crypto Bot ON does not mean an order will\nbe submitted. These Crypto permissions do not control the Equity stop guard.</p>\n<p>Equity and Zerodha worker-command controls, manual orders, and\ncancellation of existing orders are not installed by this release.\nThe separate Equity stop guard retains its existing sell authority;\nobserving desired settings does not apply them to its decisions.</p></section>\n<script nonce="__NONCE__">\nconst token=__TOKEN__,el=id=>document.getElementById(id);\nlet sending=false,reading=false;\nasync function request(options={}){\n const controller=new AbortController();\n const timer=setTimeout(()=>controller.abort(),12000);\n try{\n  const response=await fetch("/api/v1/worker-controls",{\n   ...options,cache:"no-store",credentials:"same-origin",\n   signal:controller.signal\n  });\n  const data=await response.json();\n  if(!response.ok)throw Error(data.error||response.status);\n  return data;\n }finally{clearTimeout(timer)}\n}\nasync function refresh(){\n if(reading)return;reading=true;\n try{\n  const data=await request(),fresh=data.heartbeat_fresh;\n  el("health").className=fresh?"green":"grey";\n  el("health").textContent=fresh?\n   "Fresh worker heartbeat · permissions are not proof of broker execution":\n   "Stale or unknown heartbeat · current operation is unverified";\n  el("controls").replaceChildren();\n  for(const key of ["bot","auto_trade","buy","sell"]){\n   const row=document.createElement("div");row.className="row";\n   const label=document.createElement("span");label.textContent=key;\n   const state=document.createElement("span");\n   const v=data.worker_controls?.[key];\n   const known=fresh&&typeof v==="boolean";\n   state.textContent=known?(v?"ON":"OFF"):"UNVERIFIED";\n   state.className=!known?"grey":v?"green":"red";\n   row.append(label,state);\n   for(const value of [true,false]){\n    const button=document.createElement("button");\n    button.textContent=value?"ON":"OFF";button.disabled=sending;\n    button.onclick=()=>send(key,value);row.append(button);\n   }\n   el("controls").append(row);\n  }\n  el("runtime").textContent=JSON.stringify({\n   entries_enabled:data.entries_enabled,\n   status:data.worker_status,\n   last_ack:data.worker_acknowledgement\n  },null,2);\n  el("history").textContent=JSON.stringify(data.commands,null,2);\n }catch(error){\n  el("health").className="grey";\n  el("health").textContent="Connection failed: "+error.message;\n  el("runtime").textContent="Current worker operation is UNVERIFIED.";\n  el("controls").replaceChildren();\n }finally{reading=false}\n}\nasync function send(key,value){\n if(sending)return;\n const warning=!value&&["bot","auto_trade","sell"].includes(key)?\n  "\\nThis also blocks new software stop-loss exits.":"";\n if(!confirm("Set Crypto "+key+" = "+value+"?"+warning))return;\n sending=true;\n document.querySelectorAll("button").forEach(b=>b.disabled=true);\n el("message").className="orange";\n const body={bot:"crypto",updates:{[key]:value},\n  request_id:window.crypto.randomUUID()};\n el("message").textContent="Sending "+body.request_id+"…";\n try{\n  const data=await request({\n   method:"POST",\n   headers:{"Content-Type":"application/json","X-SI-CSRF":token},\n   body:JSON.stringify(body)\n  });\n  el("message").textContent=JSON.stringify(data,null,2)+\n   "\\nQUEUED is not APPLIED. Check command history for worker acknowledgement.";\n }catch(error){\n  el("message").className="grey";\n  el("message").textContent="Outcome uncertain: "+error.message+\n   ". Check command history before sending another request.\\nRequest: "+\n   body.request_id;\n }finally{sending=false;await refresh()}\n}\nrefresh();setInterval(refresh,3000);\n</script>\n<!-- SI_EQUITY_SHARED_SETTINGS_V1 -->\n<section id="equity-shared-settings">\n<h2>US Equity · Configuration and application status</h2>\n<p>\n<a href="/strategy-settings">Edit desired strategy settings</a>\n</p>\n<p class="warning">\nReported configuration is not proof of worker enforcement.\nEquity entry execution and configuration acknowledgement are not connected.\n</p>\n<p id="equity-settings-health" class="grey">Loading configuration…</p>\n<pre id="equity-settings-values"></pre>\n</section>\n<script nonce="__NONCE__">\n(()=>{\n\'use strict\';\nconst health=document.getElementById(\'equity-settings-health\');\nconst output=document.getElementById(\'equity-settings-values\');\nlet reading=false;\n\nasync function get(path){\n const response=await fetch(path,{\n  cache:\'no-store\',\n  credentials:\'same-origin\',\n  signal:AbortSignal.timeout(10000)\n });\n if(!response.ok)throw Error(\'HTTP \'+response.status);\n return response.json();\n}\nfunction amount(value){\n return typeof value===\'number\'&&Number.isFinite(value)&&value>=0\n  ?value:\'NOT_PROVIDED\';\n}\nasync function refresh(){\n if(reading||document.hidden)return;\n reading=true;\n try{\n  const [capital,strategy,integration]=await Promise.all([\n   get(\'/api/v1/config\'),\n   get(\'/api/v1/strategy-settings\'),\n   get(\'/api/v1/integration-v2\').catch(()=>null)\n  ]);\n  const allocation=capital?.bot_allocations?.equity;\n  const profile=strategy?.profiles?.equity;\n  if(!allocation||!profile)throw Error(\'Equity configuration missing\');\n  // SI_EQUITY_OBSERVATION_PANEL_V1\n  const observation=integration?.equity?.settings_observation;\n  const available=observation&&typeof observation===\'object\'\n   &&!Array.isArray(observation);\n  const observed=available&&observation.fresh===true\n   &&observation.successful_observation===true\n   &&observation.status===\'OBSERVED_NOT_APPLIED\';\n  const revisionMatches=observed\n   &&observation.desired_revision===strategy.revision;\n  const parameters={};\n  for(const key of [\n   \'stop_loss_pct\',\'profit_start_pct\',\'profit_goal_pct\',\n   \'profit_floor_pct\',\'daily_loss_limit_pct\',\'max_positions\'\n  ]){\n   parameters[key]=amount(profile[key]);\n  }\n  output.textContent=JSON.stringify({\n   gateway_reported_capital:{\n    allocated_capital_usd:amount(allocation.allocated_capital),\n    reserve_capital_usd:amount(allocation.reserve_capital),\n    spendable_ceiling_usd:amount(allocation.spendable_ceiling),\n    maximum_entry_usd:\'NOT_EXPOSED_BY_CURRENT_CONFIGURATION_API\',\n    source:\'/api/v1/config\',\n    persisted_vs_gateway_fallback:\'NOT_DISTINGUISHED_BY_THIS_API\'\n   },\n   desired_strategy:{\n    revision:strategy.revision,\n    updated_at:strategy.updated_at,\n    parameters,\n    source:\'/api/v1/strategy-settings\'\n   },\n   worker_settings_observation:available?observation:{\n    status:\'UNAVAILABLE\',fresh:false,successful_observation:false,\n    worker_applied_revision:null,worker_enforcement:\'NOT_VERIFIED\',\n    observation_only:true\n   },\n   observed_revision_matches_current_desired:observed?revisionMatches:null,\n   worker_applied_revision:null,\n   worker_enforcement:\'NOT_VERIFIED\',\n   automatic_entry_execution:\'NOT_CONNECTED\'\n  },null,2);\n  health.className=\'grey\';\n  health.textContent=\'Configuration read \'+new Date().toLocaleTimeString()+\n   (observed\n    ?(revisionMatches\n      ?\' · guard observed current desired revision; NOT applied\'\n      :\' · guard observed a different revision; NOT applied\')\n    :\' · guard observation unavailable, failed, invalid, or stale\')+\n   \' · worker enforcement remains unverified\';\n }catch(error){\n  health.className=\'grey\';\n  health.textContent=\'Configuration unavailable: \'+error.message;\n  output.textContent=\'Current settings and worker application are UNVERIFIED.\';\n }finally{\n  reading=false;\n }\n}\nrefresh();\nsetInterval(refresh,5000);\n})();\n</script>\n\n</main></body></html>'
_EQUITY_CONTROLS_SCRIPT = '\n<script nonce="__NONCE__">\n(()=>{\n\'use strict\';\nconst rows=document.getElementById(\'equity-control-rows\');\nconst runtime=document.getElementById(\'equity-runtime\');\nconst section=document.getElementById(\'equity-control-section\');\nconst notice=document.createElement(\'p\');\nnotice.className=\'warning\';\nnotice.textContent=\'These controls govern the existing Equity sell guard only. \'\n +\'Bot OFF, Auto Trade OFF, or Sell OFF blocks new software stop-loss sells. \'\n +\'Existing broker orders are not cancelled. Automatic buying and shared \'\n +\'strategy-settings application remain disconnected.\';\nsection.prepend(notice);\nconst message=document.createElement(\'pre\');\nsection.append(message);\nlet busy=false,reading=false;\nasync function request(options={}){\n const response=await fetch(\'/api/v1/equity-controls\',{\n  ...options,cache:\'no-store\',credentials:\'same-origin\',\n  signal:AbortSignal.timeout(12000)\n });\n const data=await response.json();\n if(!response.ok)throw Error(data.error||(\'HTTP \'+response.status));\n return data;\n}\nasync function send(field,value){\n if(busy)return;\n const warning=!value\n  ?\' This blocks new software stop-loss sell submissions.\':\'\';\n if(!confirm(\'Set Equity guard \'+field+\' = \'+value+\'?\'+warning))return;\n const requestId=window.crypto.randomUUID();\n busy=true;\n rows.querySelectorAll(\'button\').forEach(b=>b.disabled=true);\n message.textContent=\'Sending \'+requestId+\'…\';\n try{\n  const result=await request({\n   method:\'POST\',\n   headers:{\'Content-Type\':\'application/json\',\'X-SI-CSRF\':token},\n   body:JSON.stringify({\n    bot:\'equity\',updates:{[field]:value},request_id:requestId\n   })\n  });\n  message.textContent=JSON.stringify(result,null,2)\n   +\'\\nQUEUED is not APPLIED. See worker command history below.\';\n }catch(error){\n  message.textContent=\'Outcome uncertain: \'+error.message\n   +\'\\nRequest: \'+requestId\n   +\'\\nCheck history before submitting another request.\';\n }finally{\n  busy=false;\n  await refresh();\n }\n}\nasync function refresh(){\n if(reading||busy)return;\n reading=true;\n try{\n  const data=await request();\n  rows.replaceChildren();\n  for(const field of [\'bot\',\'auto_trade\',\'buy\',\'sell\']){\n   const row=document.createElement(\'div\');row.className=\'row\';\n   const label=document.createElement(\'span\');\n   label.textContent=field;\n   const state=document.createElement(\'span\');\n   const known=data.worker_report_fresh===true\n    &&typeof data.controls?.[field]===\'boolean\';\n   state.textContent=field===\'buy\'?\'NOT IMPLEMENTED\':\n    known?(data.controls[field]?\'ON\':\'OFF\'):\'UNVERIFIED\';\n   state.className=field===\'buy\'||!known?\'grey\':\n    data.controls[field]?\'green\':\'red\';\n   row.append(label,state);\n   for(const value of [true,false]){\n    const button=document.createElement(\'button\');\n    button.textContent=value?\'ON\':\'OFF\';\n    button.disabled=field===\'buy\'||!known||busy;\n    button.onclick=()=>send(field,value);\n    row.append(button);\n   }\n   rows.append(row);\n  }\n  runtime.textContent=JSON.stringify(data,null,2);\n }catch(error){\n  rows.replaceChildren();\n  message.textContent=\'Equity controls unavailable: \'+error.message;\n }finally{reading=false}\n}\nrefresh();\nsetInterval(refresh,5000);\n})();\n</script>\n'
PAGE = PAGE.replace('</main></body></html>', _EQUITY_CONTROLS_SCRIPT + '</main></body></html>')
PAGE = PAGE.replace("byId('equity-runtime')", "byId('equity-monitor-runtime')")
PAGE = PAGE.replace('<pre id="equity-runtime"></pre>', '<pre id="equity-monitor-runtime"></pre><h3>Equity guard commands and permissions</h3><pre id="equity-runtime"></pre>')
PAGE = PAGE.replace('Equity controls are not connected: these buttons do not start or stop the\nseparate sell-authorized Equity stop guard.', 'Equity guard controls govern new sell submissions, not service start/stop. Automatic buying remains disconnected.')
PAGE = PAGE.replace('Automatic-entry execution and worker-command controls are not connected.\nThe buttons below cannot start or stop the existing Equity stop guard.', 'Automatic-entry execution is not connected. Guard permission buttons do not start or stop the service.')
PAGE = PAGE.replace('Equity and Zerodha worker-command controls, manual orders, and\ncancellation of existing orders are not installed by this release.', 'Equity controls cover the existing sell guard only. Zerodha worker controls, manual orders, and cancellation of existing orders are not installed by this release.')
PAGE = PAGE.replace('<h2>Durable command history</h2>', '<h2>Crypto · Durable command history</h2><p>These acknowledgements belong to the Crypto worker only. They do not authorize Equity buying or confirm Equity orders. Equity command history is reported separately in the Equity guard commands and permissions panel.</p>')
_settings_marker = '<!-- SI_EQUITY_SHARED_SETTINGS_V1 -->'
if PAGE.count(_settings_marker) != 1:
    raise RuntimeError('Shared-settings panel marker mismatch')
_head, _tail = PAGE.split(_settings_marker, 1)
_script_start = _tail.find('<script nonce="__NONCE__">')
_script_end = _tail.find('</script>', _script_start)
if _script_start < 0 or _script_end < 0:
    raise RuntimeError('Shared-settings script missing')
_tail = _tail.replace('Reported configuration is not proof of worker enforcement.\nEquity entry execution and configuration acknowledgement are not connected.', 'Stop-loss application is reported separately below. The other five strategy parameters and automatic buying remain disconnected.')
_new_script = '<script nonce="__NONCE__">\n(()=>{\n\'use strict\';\nconst health=document.getElementById(\'equity-settings-health\');\nconst output=document.getElementById(\'equity-settings-values\');\nlet reading=false;\nasync function get(path){\n const r=await fetch(path,{\n  cache:\'no-store\',credentials:\'same-origin\',\n  signal:AbortSignal.timeout(10000)\n });\n if(!r.ok)throw Error(\'HTTP \'+r.status);\n return r.json();\n}\nasync function refresh(){\n if(reading||document.hidden)return;\n reading=true;\n try{\n  const [strategy,integration,capital]=await Promise.all([\n   get(\'/api/v1/strategy-settings\'),\n   get(\'/api/v1/integration-v2\'),\n   get(\'/api/v1/config\')\n  ]);\n  const report=integration?.equity?.settings_observation;\n  const profile=strategy?.profiles?.equity;\n  if(!report||!profile)throw Error(\'Settings report missing\');\n  const loaded=report.fresh===true\n   &&report.status===\'PARTIALLY_APPLIED\'\n   &&report.stop_setting_loaded===true;\n  const sameRevision=loaded\n   &&report.stop_loaded_revision===strategy.revision;\n  const sameStop=loaded\n   &&Number(report.decision_stop_loss_pct)===Number(profile.stop_loss_pct);\n  health.className=loaded?\'orange\':\'grey\';\n  health.textContent=loaded\n   ?\'PARTIALLY APPLIED: stop-loss calculation uses \'+\n     report.decision_stop_loss_pct+\'% · loaded revision \'+\n     report.stop_loaded_revision+\n     (sameRevision&&sameStop\n      ?\' · matches desired snapshot read by this page\'\n      :\' · differs from desired snapshot read by this page\')+\n     \' · other five parameters NOT applied\'\n   :\'Stop-setting application unverified: \'+report.status;\n  output.textContent=JSON.stringify({\n   desired_strategy:{\n    revision:strategy.revision,parameters:profile\n   },\n   gateway_reported_capital:capital?.bot_allocations?.equity,\n   worker_settings_report:report,\n   page_snapshot_revision_and_stop_match:loaded\n    ?sameRevision&&sameStop:null,\n   comparison_is_atomic:false,\n   full_strategy_revision_applied:false,\n   automatic_entry_execution:\'NOT_CONNECTED\',\n   broker_execution_verified:false\n  },null,2);\n }catch(error){\n  health.className=\'grey\';\n  health.textContent=\'Settings application unavailable: \'+error.message;\n  output.textContent=\'Current enforcement is UNVERIFIED.\';\n }finally{reading=false}\n}\nrefresh();setInterval(refresh,5000);\n})();\n</script>'
_script_start = _tail.find('<script nonce="__NONCE__">')
_script_end = _tail.find('</script>', _script_start)
PAGE = _head + _settings_marker + _tail[:_script_start] + _new_script + _tail[_script_end + len('</script>'):]
