bash <<'INSTALL_PRIVATE_DASHBOARD'
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer in your root VPS terminal." >&2
  exit 1
fi
TOKEN=/etc/super-intelligence/dashboard-control.token
test -s "$TOKEN" || { echo "Existing backend dashboard token missing; nothing installed." >&2; exit 1; }
/usr/bin/python3 - <<'CHECK_API'
import json, socket
from urllib.request import urlopen
with urlopen('http://127.0.0.1:8000/health', timeout=5) as r:
    assert r.status == 200 and json.load(r).get('status') == 'ok', 'Backend health check failed'
CHECK_API
if ! systemctl is-active --quiet si-private-dashboard.service; then
  /usr/bin/python3 - <<'CHECK_PORT'
import socket
with socket.socket() as s:
    s.bind(('127.0.0.1', 8088))
CHECK_PORT
fi
install -d -m 0755 /opt/super-intelligence-dashboard
install -d -m 0700 /etc/super-intelligence
cat > /opt/super-intelligence-dashboard/server.py <<'DASHBOARD_PY'
import base64, hashlib, hmac, json, os, subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent
UPSTREAM = "http://127.0.0.1:8000"
ROUTES = {
    "/api/data/crypto": "/api/dashboard/v1/bots/crypto",
    "/api/data/equity": "/api/dashboard/v1/bots/equity",
    "/api/data/zerodha": "/api/dashboard/v1/bots/zerodha",
    "/api/capital/crypto": "/api/dashboard/v1/bots/crypto/capital",
}
UNITS = {"Backend": "super-intelligence.service", "Runner": "super-intelligence-runner.service",
         "Monitor": "super-intelligence-monitor.service", "Email worker": "super-intelligence-email-alerts.service"}

def read_upstream(path, token, timeout=75):
    req = Request(UPSTREAM + path, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                return 502, {"error": "Backend response too large"}
            return response.status, json.loads(raw)
    except HTTPError as exc:
        return exc.code, {"error": "Backend returned HTTP " + str(exc.code)}
    except Exception as exc:
        return 502, {"error": "Backend response unavailable", "error_type": type(exc).__name__}

class Handler(BaseHTTPRequestHandler):
    server_version = "PrivateDashboard/1"
    def reply(self, status, payload, content_type="application/json; charset=utf-8", challenge=False):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        if challenge:
            self.send_header("WWW-Authenticate", 'Basic realm="Super Intelligence private dashboard", charset="UTF-8"')
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(raw)
    def authorized(self):
        try:
            scheme, value = self.headers.get("Authorization", "").split(" ", 1)
            if scheme.lower() != "basic":
                return False
            user, password = base64.b64decode(value, validate=True).decode("utf-8").split(":", 1)
            if len(password) > 1024:
                return False
            actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(LOGIN["salt"]), LOGIN["iterations"]).hex()
            return hmac.compare_digest(user.encode(), LOGIN["username"].encode()) and hmac.compare_digest(actual, LOGIN["hash"])
        except Exception:
            return False
    def do_GET(self):
        if not self.authorized():
            return self.reply(401, {"error": "Dashboard login required"}, challenge=True)
        if self.path in ("/", "/index.html"):
            return self.reply(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/summary":
            services = {}
            for name, unit in UNITS.items():
                try:
                    result = subprocess.run(["/usr/bin/systemctl", "show", unit, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID"], capture_output=True, text=True, timeout=5, check=True)
                    services[name] = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
                except Exception:
                    services[name] = {"ActiveState": "unknown", "SubState": "unavailable"}
            code, data = read_upstream("/health", TOKEN, timeout=5)
            return self.reply(200, {"checked_at": datetime.now(timezone.utc).isoformat(), "services": services,
                                    "health": {"ok": code == 200 and data.get("status") == "ok", "http_status": code}})
        if self.path in ROUTES:
            code, data = read_upstream(ROUTES[self.path], TOKEN)
            return self.reply(code, data)
        self.reply(404, {"error": "Not found"})
    def do_POST(self):
        self.reply(405, {"error": "Read-only dashboard"})
    do_PATCH = do_POST
    do_PUT = do_POST
    do_DELETE = do_POST

if __name__ == "__main__":
    credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
    TOKEN = (credentials / "api-token").read_text().strip()
    LOGIN = json.loads((credentials / "login").read_text())
    if not TOKEN:
        raise RuntimeError("Backend token is empty")
    ThreadingHTTPServer(("127.0.0.1", 8088), Handler).serve_forever()

DASHBOARD_PY
cat > /opt/super-intelligence-dashboard/index.html <<'DASHBOARD_HTML'
<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Super Intelligence | Dashboard</title><style>
:root{color-scheme:dark;font-family:system-ui,sans-serif;background:#0b1220;color:#eef4ff}*{box-sizing:border-box}body{margin:0}aside{position:fixed;width:220px;inset:0 auto 0 0;background:#080e1a;padding:28px 18px;border-right:1px solid #24334b}aside h2{font-size:20px;color:#38bdf8;margin:0 0 35px}button{font:inherit;cursor:pointer;background:#152238;color:#eef4ff;border:1px solid #24334b;border-radius:9px;padding:11px 16px}aside button{display:block;text-align:left;width:100%;margin:10px 0}button.active{background:#12334b;border-color:#38bdf8}button:disabled{opacity:.45;cursor:not-allowed}aside p{font-size:12px;color:#90a2bc;margin-top:35px}main{margin-left:220px;padding:30px;max-width:1800px}h1{margin:0;font-size:30px}h2{font-size:20px}small,.muted{color:#90a2bc}.top{display:flex;justify-content:space-between;align-items:center;gap:15px}.notice{margin:24px 0;padding:15px;border:1px solid #254459;background:#102338;border-radius:10px;color:#a7c6da}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:15px}.card,.panel{background:#111c2e;border:1px solid #24334b;border-radius:14px;padding:20px}.card label{font-size:12px;color:#90a2bc;display:block}.card strong{display:block;font-size:25px;margin:14px 0;color:#38bdf8}.card small{display:block;overflow-wrap:anywhere}.section{display:flex;justify-content:space-between;gap:15px;align-items:center;margin:25px 0 15px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.panel{min-width:0;margin-bottom:16px}.panel h3{margin:0 0 15px;font-size:15px}pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;max-height:530px;overflow:auto;font:12px/1.65 ui-monospace,monospace;color:#b9cce5}.error{color:#fb7185}.badge{border:1px solid #254459;color:#38bdf8;padding:8px 12px;border-radius:8px;font-size:11px}footer{font-size:12px;color:#90a2bc;margin-top:20px}@media(max-width:1000px){.cards{grid-template-columns:1fr 1fr}.grid{grid-template-columns:1fr}}@media(max-width:650px){aside{position:static;width:auto;padding:18px}aside h2{margin:0 0 10px}aside button{display:inline-block;width:auto;margin-right:6px;font-size:12px}aside p{margin:12px 0 0}main{margin:0;padding:18px}h1{font-size:24px}.top{align-items:flex-start}.card{padding:14px}.card strong{font-size:20px}}
</style><aside><h2>SUPER INTELLIGENCE</h2><button class="active" data-bot="crypto">Crypto</button><button data-bot="equity">US Equity</button><button data-bot="zerodha">India / Zerodha</button><p>Authenticated read-only dashboard.<br><br>Trade controls are not connected here. This UI cannot submit or cancel orders.</p></aside><main><header class="top"><div><h1>Trading overview</h1><small id="checked">Checking backend…</small></div><span class="badge">READ ONLY / PRIVATE ACCESS</span></header><div class="notice">Process status is not proof of permission to trade. Missing or unreconciled data is shown as unavailable, never as a zero balance.</div><section class="cards" id="services"></section><div class="section"><div><h2 id="bot-title">Crypto</h2><small id="snapshot-status">Loading…</small></div><button id="refresh">Refresh account data</button></div><div class="grid"><div class="panel"><h3>Saved configuration</h3><pre id="configuration">Loading…</pre></div><div class="panel"><h3>Execution / control integration</h3><pre id="controls">Loading…</pre></div></div><div class="panel"><h3>Capital response — Crypto only</h3><pre id="capital">Loading…</pre></div><div class="panel"><h3>Broker response — not a fill confirmation</h3><pre id="broker">Loading…</pre></div><div class="panel"><h3>Closed-trade ledger</h3><pre id="ledger">Unavailable until verified by the backend.</pre></div><footer>No broker keys are included in this page. Dashboard API credentials remain on the VPS. Broker timestamps and coverage limitations are retained in responses.</footer></main><script>
let bot='crypto',generation=0;const names={crypto:'Crypto',equity:'US Equity',zerodha:'India / Zerodha'};const el=id=>document.getElementById(id);const show=(id,v)=>el(id).textContent=typeof v==='string'?v:JSON.stringify(v,null,2);async function get(path){const r=await fetch(path,{cache:'no-store',credentials:'same-origin'});const body=await r.json();if(!r.ok)throw Error(body.error||('HTTP '+r.status));return body}
async function summary(){try{const s=await get('/api/summary');el('checked').textContent='Service check: '+s.checked_at+' — server receipt time';el('services').replaceChildren();for(const [name,v] of Object.entries(s.services)){const card=document.createElement('article');card.className='card';const label=document.createElement('label');label.textContent=name;const state=document.createElement('strong');state.textContent=v.ActiveState||'unknown';if(v.ActiveState!=='active')state.style.color='#fbbf24';const detail=document.createElement('small');detail.textContent='Process '+(v.MainPID||'—')+' · '+(v.SubState||'unknown');card.append(label,state,detail);el('services').append(card)}const api=document.createElement('p');api.className='muted';api.textContent='Local API: '+(s.health.ok?'responding':'unavailable')+' · HTTP '+s.health.http_status;el('services').append(api)}catch(e){el('checked').textContent='Service check unavailable: '+e.message}}
async function capital(){show('capital','Loading…');try{show('capital',await get('/api/capital/crypto'))}catch(e){show('capital','Unavailable: '+e.message)}}
async function load(){const selected=bot,seq=++generation;el('bot-title').textContent=names[selected];el('snapshot-status').textContent='Requesting authenticated backend data…';['configuration','controls','broker','ledger'].forEach(id=>show(id,'Loading…'));el('refresh').disabled=true;try{const s=await get('/api/data/'+selected);if(seq!==generation)return;show('configuration',s.saved_state?.configuration??'Configuration unavailable');show('controls',{snapshot_status:s.status,automatic_execution_readiness:s.automatic_execution_readiness,controls:s.saved_state?.controls??null,account_selection_verified:s.account_selection_verified,account_scope:s.account_scope,warning:s.warning});show('broker',s.broker??'Broker data unavailable');show('ledger',s.closed_trade_ledger??'Ledger unavailable');el('snapshot-status').textContent=(s.status||'Unknown')+' · received '+(s.generated_at||'timestamp unavailable')+' · cache age '+(s.cache_age_seconds??'unknown')+'s'}catch(e){if(seq!==generation)return;el('snapshot-status').textContent='Snapshot unavailable: '+e.message;['configuration','controls','broker','ledger'].forEach(id=>show(id,'Unavailable — no data substituted'))}finally{if(seq===generation)el('refresh').disabled=false}}
document.querySelectorAll('[data-bot]').forEach(b=>b.addEventListener('click',()=>{bot=b.dataset.bot;document.querySelectorAll('[data-bot]').forEach(x=>x.classList.toggle('active',x===b));load()}));el('refresh').addEventListener('click',()=>{load();capital();summary()});summary();capital();load();setInterval(summary,30000);
</script></html>
DASHBOARD_HTML
chmod 0644 /opt/super-intelligence-dashboard/server.py /opt/super-intelligence-dashboard/index.html
/usr/bin/python3 -m py_compile /opt/super-intelligence-dashboard/server.py
/usr/bin/python3 - <<'CREATE_LOGIN'
import hashlib, json, os, secrets
from pathlib import Path
p=Path('/etc/super-intelligence/dashboard-login.json')
if p.exists():
    info=json.loads(p.read_text())
    assert info.get('username')=='admin' and info.get('hash') and info.get('salt')
    print('Login already exists: admin. Password unchanged; it cannot be recovered from its hash.')
else:
    password=secrets.token_urlsafe(24)
    salt=secrets.token_bytes(24)
    iterations=200000
    info={'username':'admin','salt':salt.hex(),'iterations':iterations,'hash':hashlib.pbkdf2_hmac('sha256',password.encode(),salt,iterations).hex()}
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f: json.dump(info,f)
    print('\nSAVE THESE PRIVATE DASHBOARD CREDENTIALS:')
    print('Username: admin')
    print('Password: '+password)
    print('The password above is shown only on first installation. Do not paste it into chat.\n')
os.chmod(p,0o600)
CREATE_LOGIN
cat > /etc/systemd/system/si-private-dashboard.service <<'DASHBOARD_UNIT'
[Unit]
Description=Private read-only Super Intelligence dashboard
After=network.target super-intelligence.service

[Service]
Type=simple
DynamicUser=yes
WorkingDirectory=/opt/super-intelligence-dashboard
ExecStart=/usr/bin/python3 /opt/super-intelligence-dashboard/server.py
LoadCredential=api-token:/etc/super-intelligence/dashboard-control.token
LoadCredential=login:/etc/super-intelligence/dashboard-login.json
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
UMask=0077

[Install]
WantedBy=multi-user.target

DASHBOARD_UNIT
systemctl daemon-reload
systemctl enable si-private-dashboard.service
systemctl restart si-private-dashboard.service
sleep 3
systemctl is-active --quiet si-private-dashboard.service || {
  journalctl -u si-private-dashboard.service -n 30 --no-pager
  exit 1
}
/usr/bin/python3 - <<'CHECK_AUTH'
from urllib.request import urlopen
from urllib.error import HTTPError
try:
    urlopen('http://127.0.0.1:8088/',timeout=5)
except HTTPError as e:
    if e.code != 401: raise
    print('Dashboard HTTP listener is responding and requires authentication (401).')
else:
    raise SystemExit('ERROR: unauthenticated request was not rejected.')
CHECK_AUTH
printf '\n=== PRIVATE DASHBOARD INSTALLED ===\n'
printf 'Backend and runner were not modified or restarted.\n'
printf 'On your PERSONAL COMPUTER, open a separate terminal and run:\n'
printf 'ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8088:127.0.0.1:8088 root@vmi3619462.contaboserver.net\n'
printf 'Keep that terminal open, then open http://127.0.0.1:8088/ in your computer browser.\n'
printf 'Log in with admin and the password printed during installation.\n'
printf 'This is private read-only access, not a public HTTPS deployment.\n'
INSTALL_PRIVATE_DASHBOARD
