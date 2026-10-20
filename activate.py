#!/usr/bin/env python3
import json, os, subprocess
from pathlib import Path
if os.geteuid()==0:raise SystemExit('STOP: Run as ubuntu / existing PM2 owner, not sudo.')
result=subprocess.run(['pm2','jlist'],capture_output=True,text=True,timeout=15)
try:
    processes=json.loads(result.stdout)
    matches=[p for p in processes if (p.get('pm2_env') or {}).get('pm_exec_path')=='/opt/us-trading-bot/unified_server.js']
except Exception:raise SystemExit('STOP: Cannot safely inspect this user\'s PM2 list.')
if len(matches)!=1:raise SystemExit('STOP: Expected exactly one PM2 dashboard process. No process restarted.')
p=matches[0]
print('Restarting ONLY dashboard API: '+str(p.get('name'))+' (PM2 ID '+str(p['pm_id'])+')',flush=True)
subprocess.run(['pm2','restart',str(p['pm_id'])],check=True,timeout=45)
print('Dashboard restart command succeeded; account reads still require browser verification.')
