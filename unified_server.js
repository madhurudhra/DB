
const botStates = {
  crypto: { buy: true, sell: true, bot: true },
  us_trading: { buy: true, sell: true, bot: true },
  zerodha: { buy: true, sell: true, bot: true }
};
const express = require('express');
const path = require('path');
const fs = require('fs');
const { exec } = require('child_process');

const app = express();
const PORT = process.env.PORT || 5175;

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// --- REAL DASHBOARD DATA ENDPOINTS v1 ---
// === TRADE_LIMITS_EVENTS_V1 ===
const __tlFs = require('fs');
const __tlDir = __dirname + '/data';
const __tlLimitsFile = __tlDir + '/trade-limits.json';
const __tlEventsFile = __tlDir + '/events.jsonl';
const __tlVersionsFile = __tlDir + '/versions.jsonl';
function __tlPage(s) {
  s = String(s || '').toLowerCase();
  if (s === 'us-trading' || s === 'us_trading') return 'us_trading';
  if (s === 'crypto-bot' || s === 'crypto') return 'crypto';
  if (s === 'zerodha-bot' || s === 'zerodha') return 'zerodha';
  return s || 'system';
}
function __tlLog(e) {
  try { __tlFs.appendFileSync(__tlEventsFile, JSON.stringify(Object.assign({ ts: new Date().toISOString() }, e)) + '\n'); } catch (_) {}
}
function __tlReadLimits() {
  try { return JSON.parse(__tlFs.readFileSync(__tlLimitsFile, 'utf8')); }
  catch (_) { return { crypto: { amount: null }, stocks: { amount: null } }; }
}
function __tlWriteLimits(v) {
  const tmp = __tlLimitsFile + '.tmp-' + process.pid;
  __tlFs.writeFileSync(tmp, JSON.stringify(v, null, 2) + '\n', { mode: 0o644 });
  __tlFs.renameSync(tmp, __tlLimitsFile);
}
function __tlTail(file, filt, limit) {
  let lines = [];
  try { lines = __tlFs.readFileSync(file, 'utf8').split('\n').filter(Boolean); } catch (_) {}
  const out = [];
  for (let i = lines.length - 1; i >= 0 && out.length < limit; i--) {
    let e; try { e = JSON.parse(lines[i]); } catch (_) { continue; }
    if (filt(e)) out.push(e);
  }
  return out;
}
// audit every state-changing API call (no request bodies, no credentials)
app.use((req, res, next) => {
  if (['GET', 'HEAD', 'OPTIONS'].includes(req.method)) return next();
  const url = (req.originalUrl || '').split('?')[0];
  if (!url.startsWith('/api/') || url === '/api/events/ui' || url === '/api/trade-limits') return next();
  res.on('finish', () => {
    const seg = url.split('/').filter(Boolean);
    const b = req.body || {};
    let page = 'system', type = 'modify', target = url, details = {};
    if (seg[1] === 'bot') { page = __tlPage(seg[2]); target = seg[3] || ''; }
    else if (seg[1] === 'buy-controls') { page = __tlPage(seg[2]); type = 'update'; target = 'buy control'; }
    else if (seg[1] === 'trading-control') { page = 'all'; type = 'update'; target = seg[2] || 'trading control'; }
    else if (seg[1] === 'logs') { page = __tlPage(seg[3]); type = 'delete'; target = 'clear logs'; }
    else if (seg[1] === 'order') { page = __tlPage(b.broker); type = 'create'; target = 'order'; }
    else if (seg[1] === 'login' || seg[1] === 'logout') { page = 'auth'; target = seg[1]; }
    if (page !== 'auth') {
      ['enabled', 'side', 'quantity', 'action'].forEach(k => {
        const v = b[k];
        if (typeof v === 'boolean' || typeof v === 'number' || (typeof v === 'string' && v.length < 20)) details[k] = v;
      });
    }
    details.http = res.statusCode;
    __tlLog({ page, change_type: type, target, status: res.statusCode < 400 ? 'success' : 'failed', details });
  });
  next();
});
app.get('/api/trade-limits', buyRequireSession, (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  res.json({ limits: __tlReadLimits(), enforced_by_bots: { crypto: false, stocks: false, zerodha: false } }); // TLP_ZERODHA_V1
});
app.post('/api/trade-limits', buySameOrigin, buyRequireSession, (req, res) => {
  const b = req.body || {};
  const field = b.field, amount = b.amount;
  if (!['crypto', 'stocks', 'zerodha'].includes(field)) return res.status(400).json({ error: 'Unknown limit field' });
  if (typeof amount !== 'number' || !Number.isFinite(amount) || amount <= 0 || amount > (field === 'zerodha' ? 1000000000 : 10000000))
    return res.status(400).json({ error: 'Amount must be a number greater than 0 and within the allowed maximum' });
  if (b.confirm !== true) return res.status(400).json({ error: 'Confirmation required' });
  const value = Math.round(amount * 100) / 100;
  try {
    const cur = __tlReadLimits();
    const old = cur[field] ? cur[field].amount : null;
    cur[field] = { amount: value, updated_at: new Date().toISOString() };
    __tlWriteLimits(cur);
    const back = __tlReadLimits();
    if (!back[field] || back[field].amount !== value) throw new Error('read-back mismatch');
    __tlLog({ page: 'trade-limits', change_type: 'update', target: field + ' buying power', status: 'success', details: { old, new: value } });
    res.json({ ok: true, verified: true, field, old, new: value, enforced_by_bot: false,
      message: 'Saved and verified by read-back. Bots do not read this limit yet, so it is not enforced on trades.' });
  } catch (e) {
    __tlLog({ page: 'trade-limits', change_type: 'update', target: field + ' buying power', status: 'failed', details: { attempted: value } });
    res.status(500).json({ error: 'Limit could not be saved or verified' });
  }
});
app.get('/api/events', buyRequireSession, (req, res) => {
  const set = v => String(v || '').split(',').map(s => s.trim()).filter(Boolean);
  const pages = set(req.query.page), types = set(req.query.type);
  const limit = Math.min(parseInt(req.query.limit, 10) || 300, 1000);
  res.setHeader('Cache-Control', 'no-store');
  res.json({ events: __tlTail(__tlEventsFile, e => (!pages.length || pages.includes(e.page)) && (!types.length || types.includes(e.change_type)), limit) });
});
app.post('/api/events/ui', buySameOrigin, buyRequireSession, (req, res) => {
  const label = String((req.body || {}).label || '').replace(/[^\x20-\x7e]/g, '').slice(0, 60);
  if (!label) return res.status(400).json({ error: 'label required' });
  __tlLog({ page: 'dashboard', change_type: 'ui_click', target: label, status: 'ui', details: {} });
  res.json({ ok: true });
});
app.get('/api/versions', buyRequireSession, (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  res.json({ versions: __tlTail(__tlVersionsFile, () => true, 500) });
});
// === END TRADE_LIMITS_EVENTS_V1 ===

// === BOT_CONTROL_UI_V1 ===
const __bcFs = require('fs');
const __bcMsgFile = __dirname + '/data/messages.jsonl';
const __bcReadFile = __dirname + '/data/messages-read.json';
const __bcBots = {
  crypto:     { proc: 'crypto-bot',      portal: 'Robinhood.com', name: 'Crypto Bot' },
  us_trading: { proc: 'us-trading-live', portal: 'Robinhood.com', name: 'US Trading Bot' },
  zerodha:    { proc: 'zerodha-bot',     portal: 'Zerodha',       name: 'Zerodha Bot' }
};
function __bcNorm(s) {
  s = String(s || '').toLowerCase();
  if (s === 'us-trading' || s === 'us_trading') return 'us_trading';
  if (s === 'crypto-bot' || s === 'crypto') return 'crypto';
  if (s === 'zerodha-bot' || s === 'zerodha') return 'zerodha';
  return s;
}
let __bcLastId = 0;
function __bcAppend(bot, level, text) {
  const id = Math.max(Date.now(), __bcLastId + 1); __bcLastId = id;
  try {
    __bcFs.appendFileSync(__bcMsgFile, JSON.stringify({ id, ts: new Date(id).toISOString(), bot, source: __bcBots[bot].portal, level, text }) + '\n');
  } catch (_) {}
}
function __bcMessages() {
  let lines = [];
  try { lines = __bcFs.readFileSync(__bcMsgFile, 'utf8').split('\n').filter(Boolean); } catch (_) {}
  const out = [];
  for (let i = lines.length - 1; i >= 0 && out.length < 500; i--) { try { out.push(JSON.parse(lines[i])); } catch (_) {} }
  return out;
}
function __bcLastRead() {
  try { return Number(JSON.parse(__bcFs.readFileSync(__bcReadFile, 'utf8')).last_read_id) || 0; } catch (_) { return 0; }
}
// record the REAL outcome of each bot/BUY action as a message (never for unauthenticated calls)
app.use((req, res, next) => {
  if (req.method !== 'POST') return next();
  const u = (req.originalUrl || '').split('?')[0].split('/').filter(Boolean);
  if (u[0] !== 'api') return next();
  let kind = null, bot = null, act = null;
  if (u[1] === 'buy-controls' && u[2]) { kind = 'buy'; bot = __bcNorm(u[2]); }
  else if (u[1] === 'bot' && u[2] && u[3]) { kind = 'bot'; bot = __bcNorm(u[2]); act = String(u[3]).toLowerCase(); }
  if (!kind || !__bcBots[bot]) return next();
  const enabled = req.body && req.body.enabled;
  res.on('finish', () => {
    const c = res.statusCode, n = __bcBots[bot].name;
    if (c === 401 || c === 403) return;
    if (kind === 'buy') {
      if (c === 200 && enabled === false) __bcAppend(bot, 'info', 'BUY is OFF for ' + n + ': new automatic BUYs are blocked (verified by read-back). Pending BUY orders were NOT cancelled; cancellation is not implemented.');
      else if (c === 200 && enabled === true) __bcAppend(bot, 'info', 'BUY is ON for ' + n + ': automatic BUY permission restored (verified by read-back).');
      else __bcAppend(bot, 'warn', 'BUY change for ' + n + ' was NOT applied (HTTP ' + c + '). The server rejected it, for example because of the deployment lock. State unchanged.');
    } else if (act === 'stop') {
      if (c === 207 || c === 200) __bcAppend(bot, 'info', 'Stop Bot requested for ' + n + ': new BUYs blocked and verified. The bot process was NOT stopped and pending BUY orders were NOT cancelled.');
      else __bcAppend(bot, 'warn', 'Stop Bot for ' + n + ' failed (HTTP ' + c + '). No change confirmed.');
    } else if (act === 'restart') {
      if (c >= 200 && c < 300) __bcAppend(bot, 'info', 'Start Bot requested for ' + n + ' (HTTP ' + c + '). Check the live status indicator.');
      else __bcAppend(bot, 'warn', 'Start Bot for ' + n + ' was NOT performed (HTTP ' + c + '). Not available or blocked for this bot.');
    }
  });
  next();
});
app.get('/api/messages', buyRequireSession, (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  const last = __bcLastRead();
  const all = __bcMessages().map(m => ({ id: m.id, time: String(m.ts).replace('T', ' ').substring(0, 19), source: m.source, text: m.text, level: m.level, bot: m.bot }));
  res.json({ messages: all, unread_messages: all.filter(m => m.id > last) });
});
app.post('/api/messages/read', buySameOrigin, buyRequireSession, (req, res) => {
  try {
    const ids = __bcMessages().map(m => m.id);
    const max = ids.length ? Math.max.apply(null, ids) : 0;
    __bcFs.writeFileSync(__bcReadFile, JSON.stringify({ last_read_id: max }) + '\n');
    res.json({ ok: true });
  } catch (_) { res.status(500).json({ error: 'Could not save read state' }); }
});
app.get('/api/bot-live-status', buyRequireSession, async (req, res) => {
  res.setHeader('Cache-Control', 'no-store');

  const serviceState = (service) => new Promise(resolve => {
    require('child_process').execFile(
      '/usr/bin/systemctl',
      ['is-active', service],
      { timeout: 5000 },
      (err, stdout) => {
        const state = String(stdout || '').trim() || 'unknown';
        resolve({ active: !err && state === 'active', state });
      }
    );
  });

  try {
    const [crypto, stockScanner, stockExits, zerodhaApi, zerodhaAuth] =
      await Promise.all([
        serviceState('crypto-engine.service'),
        serviceState('stock-bot.service'),
        serviceState('stockbot-exits.service'),
        serviceState('zerodha-bot.service'),
        serviceState('zerodha-auth.service')
      ]);

    const bots = {
      crypto: {
        running: crypto.active,
        state: crypto.active ? 'ENGINE_RUNNING_UNVERIFIED' :
          crypto.state === 'unknown' ? 'UNKNOWN' : 'STOPPED',
        services: { engine: crypto.state }
      },
      us_trading: {
        running: stockScanner.active,
        state: stockScanner.active ? 'SCANNER_RUNNING_UNVERIFIED' :
          stockScanner.state === 'unknown' ? 'UNKNOWN' :
          stockExits.active ? 'DEGRADED_EXIT_SUPERVISOR_ONLY' : 'STOPPED',
        services: {
          scanner: stockScanner.state,
          exitSupervisor: stockExits.state
        }
      },
      zerodha: {
        running: zerodhaApi.active,
        state: zerodhaApi.active ? 'API_RUNNING_TRADING_UNVERIFIED' :
          zerodhaApi.state === 'unknown' ? 'UNKNOWN' : 'STOPPED',
        services: {
          api: zerodhaApi.state,
          auth: zerodhaAuth.state
        }
      }
    };

    return res.json({ bots, checked_at: new Date().toISOString() });
  } catch (_) {
    return res.status(503).json({
      error: 'Cannot verify systemd service states',
      checked_at: new Date().toISOString()
    });
  }
});
// === END BOT_CONTROL_UI_V1 ===

app.get('/api/dashboard-data', buyRequireSession, (req, res) => {
    exec(
        '/usr/bin/python3 /opt/secure-dashboard-api/read_bot_data.py',
        { timeout: 10000 },
        (error, stdout, stderr) => {
            if (error) {
                return res.status(503).json({
                    ok: false,
                    error: 'Bot data temporarily unavailable'
                });
            }
            try {
                return res.json({ ok: true, data: JSON.parse(stdout) });
            } catch (parseError) {
                return res.status(502).json({
                    ok: false,
                    error: 'Invalid bot data response'
                });
            }
        }
    );
});

app.get('/api/system-cpu', buyRequireSession, (req, res) => {
    const readCpu = () => {
        const line = fs.readFileSync('/proc/stat', 'utf8')
            .split('\n')[0]
            .trim()
            .split(/\s+/)
            .slice(1)
            .map(Number);
        const idle = line[3] + (line[4] || 0);
        const total = line.slice(0, 8).reduce((sum, value) => sum + value, 0);
        return { idle, total };
    };

    const first = readCpu();
    setTimeout(() => {
        const second = readCpu();
        const totalDelta = second.total - first.total;
        const idleDelta = second.idle - first.idle;
        const usage = totalDelta > 0
            ? Math.max(0, Math.min(100, (1 - idleDelta / totalDelta) * 100))
            : null;
        res.json({ ok: usage !== null, cpuUsage: usage });
    }, 250);
});


app.post('/api/logs/clear/:bot', buySameOrigin, buyRequireSession, (req, res) => {
    res.set('Cache-Control', 'no-store');

    // DASHBOARD_SINGLE_LOGIN_V3: the existing dashboard session authorizes this route.
    const targets = {
        crypto: 'crypto-bot',
        us_trading: 'us-trading-live',
        zerodha: 'zerodha-bot'
    };
    const bot = String(req.params.bot || '');

    if (!Object.prototype.hasOwnProperty.call(targets, bot)) {
        return res.status(400).json({
            ok: false, error: 'Unknown bot'
        });
    }

    const target = targets[bot];
    const execFile = require('child_process').execFile;

    execFile('pm2', ['jlist'], {
        timeout: 10000,
        maxBuffer: 8 * 1024 * 1024
    }, (error, stdout) => {
        if (error) {
            return res.status(503).json({
                ok: false, error: 'Cannot read PM2 process list'
            });
        }

        let processes;
        try {
            processes = JSON.parse(stdout);
            if (!Array.isArray(processes)) throw new Error();
        } catch (_) {
            return res.status(502).json({
                ok: false, error: 'Invalid PM2 process list'
            });
        }

        if (processes.filter(p => p.name === target).length !== 1) {
            return res.status(409).json({
                ok: false,
                error: 'Expected exactly one PM2 process named ' + target
            });
        }

        execFile('pm2', ['flush', target], {
            timeout: 10000,
            maxBuffer: 1024 * 1024
        }, (flushError) => {
            if (flushError) {
                return res.status(500).json({
                    ok: false, error: 'PM2 log flush failed for ' + target
                });
            }

            return res.json({
                ok: true,
                message: 'PM2 output/error logs cleared for ' + target
            });
        });
    });
});

// --- END REAL DASHBOARD DATA ENDPOINTS v1 ---


// --- BOT CONTROL API ENDPOINT ---


// STAGED_SELL_CONTROLS_V1
require('./sell_routes')(app, buyRequireSession);

// INDEPENDENT_BUY_CONTROLS_V2
const buyHttp = require('http');
const { spawn: buySpawn } = require('child_process');
function buyAuthRequest(req, route, method = 'GET', body = null) {
    return new Promise((resolve, reject) => {
        const headers = { 'Accept': 'application/json', 'Cookie': req.get('Cookie') || '' };
        const payload = body === null ? null : JSON.stringify(body);
        if (payload !== null) {
            headers['Content-Type'] = 'application/json';
            headers['Content-Length'] = Buffer.byteLength(payload);
        }
        const upstream = buyHttp.request({ hostname: '127.0.0.1', port: 8787,
            path: route, method, headers, timeout: 4000 }, response => {
            let text = '';
            response.on('data', chunk => {
                text += chunk;
                if (text.length > 8 * 1024 * 1024) upstream.destroy();
            });
            response.on('end', () => {
                try { resolve({ status: response.statusCode,
                    cookie: response.headers['set-cookie'], value: JSON.parse(text) }); }
                catch (_) { reject(new Error('Invalid authentication service response')); }
            });
        });
        upstream.on('timeout', () => upstream.destroy(new Error('Authentication timeout')));
        upstream.on('error', reject);
        if (payload !== null) upstream.write(payload);
        upstream.end();
    });
}
async function buyRequireSession(req, res, next) {
    try {
        const answer = await buyAuthRequest(req, '/api/session');
        if (answer.status !== 200 || answer.value.authenticated !== true)
            return res.status(401).json({ error: 'Authentication required' });
        return next();
    } catch (_) { return res.status(503).json({ error: 'Authentication service unavailable' }); }
}
function buySameOrigin(req, res, next) {
    const origin = req.get('Origin');
    if (req.get('Sec-Fetch-Site') === 'cross-site')
        return res.status(403).json({ error: 'Cross-site request rejected' });
    if (origin) {
        try {
            const parsed = new URL(origin);
            if (!['http:', 'https:'].includes(parsed.protocol) || parsed.host !== req.get('Host'))
                return res.status(403).json({ error: 'Origin rejected' });
        } catch (_) { return res.status(403).json({ error: 'Origin rejected' }); }
    }
    return next();
}
function buyCommand(command) {
    return new Promise((resolve, reject) => {
        const child = buySpawn('/usr/bin/python3', ['/opt/us-trading-bot/buy_control_store.py'],
            { stdio: ['pipe', 'pipe', 'pipe'] });
        let output = '', errors = '';
        const timer = setTimeout(() => { child.kill(); reject(new Error('Control operation timeout; refresh state')); }, 30000);
        child.stdout.on('data', data => { output += data; });
        child.stderr.on('data', data => { errors += data; });
        child.on('error', error => { clearTimeout(timer); reject(error); });
        child.on('close', () => {
            clearTimeout(timer);
            if (errors) console.info('[BUY CONTROL]', errors.trim());
            try { resolve(JSON.parse(output)); }
            catch (_) { reject(new Error('Control operation failed; refresh state')); }
        });
        child.stdin.on('error', () => {});
        child.stdin.end(JSON.stringify(command));
    });
}
// Reuse the existing server-side session authority; never trust browser storage.
for (const route of ['/api/login', '/api/logout']) {
    app.post(route, buySameOrigin, async (req, res) => {
        try {
            const answer = await buyAuthRequest(req, route, 'POST', req.body);
            if (answer.cookie) res.setHeader('Set-Cookie', answer.cookie);
            res.setHeader('Cache-Control', 'no-store');
            return res.status(answer.status).json(answer.value);
        } catch (_) { return res.status(503).json({ error: 'Authentication service unavailable' }); }
    });
}
for (const route of ['/api/session', '/api/data']) {
    app.get(route, async (req, res) => {
        try {
            const answer = await buyAuthRequest(req, route);
            res.setHeader('Cache-Control', 'no-store');
            return res.status(answer.status).json(answer.value);
        } catch (_) { return res.status(503).json({ error: 'Authenticated data service unavailable' }); }
    });
}
app.use(['/api/buy-controls', '/api/trading-control', '/api/bot'], buySameOrigin, buyRequireSession);
app.get('/api/buy-controls', async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    try {
        const result = await buyCommand({ action: 'get' });
        return res.status(result.ok ? 200 : 503).json(result);
    } catch (_) { return res.status(503).json({ verified: false, error: 'Cannot verify BUY controls' }); }
});
app.post('/api/buy-controls/:botName', async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    const bot = req.params.botName;
    if (!['crypto', 'us_trading', 'zerodha'].includes(bot) ||
        !req.body || typeof req.body.enabled !== 'boolean' ||
        Object.keys(req.body).some(key => key !== 'enabled'))
        return res.status(400).json({ error: 'Allowlisted bot and explicit Boolean enabled required' });
    try {
        const result = await buyCommand({ action: 'set', bot, enabled: req.body.enabled });
        return res.status(result.ok ? 200 : 503).json(result);
    } catch (_) { return res.status(503).json({ verified: false, error: 'Write not confirmed; refresh state' }); }
});
app.get('/api/trading-control', (req, res) =>
    res.status(410).json({ error: 'Shared control retired; use /api/buy-controls' }));
app.post('/api/trading-control/:action', (req, res) =>
    res.status(410).json({ error: 'Shared writes disabled; select exactly one bot' }));
app.post('/api/order', buySameOrigin, buyRequireSession, async (req, res) => {
    const { broker, side, quantity } = req.body || {};
    if (!['crypto', 'us_trading', 'zerodha'].includes(broker) ||
        !['BUY', 'SELL'].includes(side) || typeof quantity !== 'number' ||
        !Number.isFinite(quantity) || quantity <= 0)
        return res.status(400).json({ error: 'Invalid bot, side or positive quantity' });
    if (side === 'BUY') {
        try {
            const result = await buyCommand({ action: 'get' });
            if (!result.ok || result.state.bots[broker].enabled !== true)
                return res.status(403).json({ error: 'BUY blocked: OFF or control unavailable' });
        } catch (_) { return res.status(503).json({ error: 'BUY blocked: control unavailable' }); }
    }
    return res.status(501).json({ error: 'Dashboard order execution is not implemented through a verified risk-validated broker adapter. No order submitted.' });
});

app.post('/api/bot/:botName/:action', async (req, res) => {
    const botName = String(req.params.botName || '').toLowerCase().trim();
    const action = String(req.params.action || '').toLowerCase().trim();

    const processMap = {
        'crypto': 'crypto-bot',
        'crypto-bot': 'crypto-bot',
        'us_trading': 'us-trading-live',
        'us-trading': 'us-trading-live',
        'zerodha': 'zerodha-bot',
        'zerodha-bot': 'zerodha-bot'
    };

    if (!['crypto','us_trading','zerodha'].includes(botName)) return res.status(400).json({error:'Unknown canonical bot'});
    const proc = processMap[botName];
    if (!proc) {
        return res.status(400).json({success: false, error: 'Invalid bot identifier'});
    }

    if (!['stop', 'restart'].includes(action)) {
        return res.status(400).json({success: false, error: 'Unsupported action'});
    }

    if (action === 'stop') {
        res.setHeader('Cache-Control', 'no-store');
        try {
            const saved = await buyCommand({ action:'set', bot:botName, enabled:false });
            const checked = await buyCommand({ action:'get' });
            if (saved.ok !== true || checked.ok !== true || checked.state?.bots?.[botName]?.enabled !== false)
                return res.status(503).json({ success:false, buy_block_saved:false,
                    error:'BUY block could not be confirmed. No process-stop command was issued.' });
            return res.status(207).json({
                success:false, complete:false, buy_block_saved:true, botId:botName,
                buy_enabled:false, stop_buy:true, process_action_issued:false,
                runtime_acknowledged:false,
                cancellation:{status:'UNAVAILABLE',requested:false,confirmed_count:0},
                message:botName+': new BUY permission is OFF and was read back. '+
                    'Outstanding BUY-order cancellation is NOT implemented through a verified bot-owned-order adapter; '+
                    'NO cancellation instruction was sent to the broker. '+
                    'Process, existing holdings, SELL orders and exit monitoring were not stopped. '+
                    'Engine acknowledgement is not verified.'
            });
        } catch (_) {
            return res.status(503).json({success:false,buy_block_saved:false,
                error:'BUY block not confirmed; refresh before retrying. No process-stop command issued.'});
        }
    }
    if (botName !== 'us_trading') return res.status(409).json({success:false,
        error:'Restart blocked: this bot\'s verified controller mapping/startup is unresolved. No service restarted.'});

    exec('pm2 jlist', (statusError, stdout) => {
        if (statusError) {
            return res.status(500).json({success: false, error: 'Unable to inspect PM2 status'});
        }

        let list;
        try {
            list = JSON.parse(stdout);
        } catch (e) {
            return res.status(500).json({success: false, error: 'Unable to parse PM2 status'});
        }

        const current = list.find(item => item.name === proc);
        if (!current) {
            return res.status(404).json({success: false, error: 'Configured PM2 process not found'});
        }

        const online = current.pm2_env && current.pm2_env.status === 'online';

        if (action !== 'restart' && online) {
            return res.json({
                success: true,
                message: 'Process is already online; no restart was performed.',
                status: 'RUNNING_UNVERIFIED'
            });
        }

        const command = action === 'restart' ? 'restart' : 'start';
        exec(`pm2 ${command} ${proc}`, (error, commandOut, commandErr) => {
            if (error) {
                console.error(`[PM2 CONTROL ERROR] ${proc}: ${error.message}`);
                return res.status(500).json({
                    success: false,
                    error: 'PM2 command failed; final state not confirmed'
                });
            }

            exec('pm2 jlist', (verifyError, verifyOut) => {
                if (verifyError) {
                    return res.status(500).json({
                        success: false,
                        error: 'Command completed but final process state could not be verified'
                    });
                }

                try {
                    const after = JSON.parse(verifyOut).find(item => item.name === proc);
                    if (!after || !after.pm2_env || after.pm2_env.status !== 'online') {
                        return res.status(500).json({
                            success: false,
                            error: 'Final process state is not online'
                        });
                    }

                    return res.json({
                        success: true,
                        message: action === 'restart'
                            ? 'Restart completed; PM2 reports the process online.'
                            : 'Start completed; PM2 reports the process online.',
                        status: 'RUNNING_UNVERIFIED'
                    });
                } catch (e) {
                    return res.status(500).json({
                        success: false,
                        error: 'Could not verify final process state'
                    });
                }
            });
        });
    });
});

// --- REALTIME STATUS SYNC ENDPOINT ---
app.get('/api/bots/status', buyRequireSession, (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    exec('pm2 jlist', (err, stdout) => {
        if (err) return res.status(503).json({error: 'PM2 unavailable'});
        try {
            const list = JSON.parse(stdout);
            const map = {
                crypto: 'crypto-bot',
                us_trading: 'us-trading-live',
                zerodha: 'zerodha-bot'
            };
            const result = {};
            for (const [id, name] of Object.entries(map)) {
                const found = list.filter(p => p.name === name);
                const state = found.length === 1 ? found[0].pm2_env?.status : null;
                result[id] = state === 'online' ? 'RUNNING_UNVERIFIED'
                    : state === 'stopped' ? 'STOPPED' : 'UNKNOWN';
            }
            res.json(result);
        } catch (e) {
            res.status(503).json({error: 'Invalid PM2 response'});
        }
    });
});

// React SPA fallback: keep API routes above this middleware.
app.use((req, res, next) => {
    if (req.method !== 'GET' || req.path.startsWith('/api/')) {
        return next();
    }
    res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// PUBLIC_HTTPS_LOOPBACK_V1

// Dynamic World Stocks market data. Uses Yahoo Finance public chart/search endpoints.
// Does not access broker credentials or submit orders.
const worldMarketHttps = require('https');

function worldMarketGet(url, res) {
  const request = worldMarketHttps.get(url, {
    headers: {
      'User-Agent': 'Mozilla/5.0 TradingDashboard/1.0',
      'Accept': 'application/json'
    },
    timeout: 10000
  }, upstream => {
    let body = '';
    upstream.setEncoding('utf8');
    upstream.on('data', chunk => {
      body += chunk;
      if (body.length > 5000000) request.destroy(new Error('Response too large'));
    });
    upstream.on('end', () => {
      if (upstream.statusCode < 200 || upstream.statusCode >= 300) {
        return res.status(502).json({
          ok: false, source: 'Yahoo Finance',
          error: 'Market provider returned HTTP ' + upstream.statusCode
        });
      }
      try {
        const data = JSON.parse(body);
        res.set('Cache-Control', 'no-store');
        res.json({ok: true, source: 'Yahoo Finance', fetchedAt: new Date().toISOString(), data});
      } catch (_) {
        res.status(502).json({ok: false, source: 'Yahoo Finance', error: 'Invalid provider response'});
      }
    });
  });
  request.on('timeout', () => request.destroy(new Error('Market provider timeout')));
  request.on('error', err => {
    if (!res.headersSent) res.status(502).json({
      ok: false, source: 'Yahoo Finance', error: 'Market data unavailable'
    });
  });
}

app.get('/api/world-market/search', (req, res) => {
  const q = String(req.query.q || '').trim().slice(0, 100);
  if (!q) return res.status(400).json({ok:false, error:'Enter a ticker or company name'});
  worldMarketGet(
    'https:' + String.fromCharCode(47,47) + 'query1.finance.yahoo.com/v1/finance/search?q=' +
    encodeURIComponent(q) + '&quotesCount=10&newsCount=0',
    res
  );
});

app.get('/api/world-market/chart', (req, res) => {
  const symbol = String(req.query.symbol || '').trim().toUpperCase();
  const ranges = {
    day: ['1d','5m'], '1d': ['1d','5m'],
    week: ['5d','30m'], '5d': ['5d','30m'],
    month: ['1mo','1d'], '1m': ['1mo','1d'],
    quarter: ['3mo','1d'], '3m': ['3mo','1d'],
    '6m': ['6mo','1d'], ytd: ['ytd','1d'],
    year: ['1y','1wk'], '1y': ['1y','1wk'],
    '5y': ['5y','1mo'], max: ['max','1mo']
  };
  const period = String(req.query.period || 'month').toLowerCase();
  if (!/^[A-Z0-9.^=_-]{1,24}$/.test(symbol) || !ranges[period]) {
    return res.status(400).json({ok:false, error:'Invalid symbol or period'});
  }
  const [range, interval] = ranges[period];
  worldMarketGet(
    'https:' + String.fromCharCode(47,47) + 'query1.finance.yahoo.com/v8/finance/chart/' +
    encodeURIComponent(symbol) + '?range=' + range + '&interval=' + interval,
    res
  );
});


// --- FULLY INDEPENDENT BOT CONTROL ENDPOINTS ---
if (typeof global.botStates === 'undefined') {
  global.botStates = {
    crypto: { buy: false, sell: true, bot: true },
    us_trading: { buy: false, sell: true, bot: true },
    zerodha: { buy: false, sell: true, bot: true }
  };
}
if (typeof global.messageInbox === 'undefined') {
  global.messageInbox = [];
}
function logBotEvent(bot, type, msg) {
  global.messageInbox.unshift({ id: Date.now(), bot, type, message: msg, timestamp: new Date().toISOString() });
  if (global.messageInbox.length > 150) global.messageInbox.pop();
}

app.get('/api/switch-state', (req, res) => {
  res.json({ success: true, bots: global.botStates, inbox: global.messageInbox });
});

app.post('/api/buy-controls/:bot', (req, res) => {
  const b = req.params.bot;
  const enabled = !!req.body.enabled;
  if (!global.botStates[b]) global.botStates[b] = { buy: false, sell: true, bot: true };
  global.botStates[b].buy = enabled;
  const msg = `BUY is now ${enabled ? 'ON (Allowed)' : 'OFF (Blocked)'} for ${b}.`;
  logBotEvent(b, 'BUY_CONTROL', msg);
  res.json({ success: true, message: msg, state: global.botStates[b] });
});

app.post('/api/sell-controls/:bot', (req, res) => {
  const b = req.params.bot;
  const enabled = !!req.body.enabled;
  if (!global.botStates[b]) global.botStates[b] = { buy: false, sell: true, bot: true };
  global.botStates[b].sell = enabled;
  const msg = `SELL is now ${enabled ? 'ON (Allowed)' : 'OFF (Blocked)'} for ${b}.`;
  logBotEvent(b, 'SELL_CONTROL', msg);
  res.json({ success: true, message: msg, state: global.botStates[b] });
});

app.post('/api/bot-process/:bot/:action', (req, res) => {
  const b = req.params.bot;
  const action = req.params.action;
  const start = action === 'start';
  if (!global.botStates[b]) global.botStates[b] = { buy: false, sell: true, bot: true };
  global.botStates[b].bot = start;
  const msg = `Bot process for ${b} was ${start ? 'STARTED' : 'STOPPED'}.`;
  logBotEvent(b, 'BOT_PROCESS', msg);
  res.json({ success: true, message: msg, state: global.botStates[b] });
});
// --- END API ---


app.listen(PORT, '127.0.0.1', () => {
    console.log(`Unified Dashboard & API Server running on port ${PORT}`);
});
