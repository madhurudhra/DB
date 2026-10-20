'use strict';

const path = require('path');
const { spawn } = require('child_process');

module.exports = function installSellRoutes(app, requireSession) {
  const bots = ['crypto', 'us_trading', 'zerodha'];
  const trustedOrigin = 'https://169.58.25.254';

  function command(payload) {
    return new Promise((resolve, reject) => {
      const db = process.env.SELL_CONTROL_DB || '';
      if (!path.isAbsolute(db)) {
        return reject(new Error('SELL database path not configured'));
      }

      const child = spawn(
        '/usr/bin/python3',
        [path.join(__dirname, 'sell_control_cli.py')],
        {
          shell: false,
          stdio: ['pipe', 'pipe', 'pipe'],
          env: { ...process.env, SELL_CONTROL_DB: db }
        }
      );

      let output = '';
      let errorBytes = 0;
      let settled = false;
      const timer = setTimeout(() => {
        child.kill('SIGKILL');
        finish(new Error('SELL operation timed out; refresh state'));
      }, 20000);

      function finish(error, value) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (error) reject(error);
        else resolve(value);
      }

      child.stdout.on('data', chunk => {
        output += chunk.toString('utf8');
        if (Buffer.byteLength(output) > 65536) {
          child.kill('SIGKILL');
          finish(new Error('SELL response too large'));
        }
      });
      child.stderr.on('data', chunk => {
        errorBytes += chunk.length;
        if (errorBytes > 65536) {
          child.kill('SIGKILL');
          finish(new Error('SELL subprocess failed'));
        }
      });
      child.on('error', error => finish(error));
      child.on('close', code => {
        if (settled) return;
        try {
          const value = JSON.parse(output);
          const validSuccess = code === 0 && value.ok === true;
          const validRejection =
            code === 2 && value.ok === false &&
            value.kind === 'validation';
          if (!validSuccess && !validRejection) {
            throw new Error('SELL operation unavailable');
          }
          finish(null, value);
        } catch (error) {
          finish(error);
        }
      });
      child.stdin.on('error', error => finish(error));
      child.stdin.end(JSON.stringify(payload));
    });
  }

  function noStore(req, res, next) {
    res.setHeader('Cache-Control', 'no-store');
    next();
  }

  function writeOrigin(req, res, next) {
    if (
      req.get('Origin') !== trustedOrigin ||
      req.get('Sec-Fetch-Site') === 'cross-site'
    ) {
      return res.status(403).json({ error: 'Trusted Origin required' });
    }
    if (!req.is('application/json')) {
      return res.status(415).json({ error: 'application/json required' });
    }
    next();
  }

  app.get('/api/sell-controls', noStore, requireSession, async (req, res) => {
    try {
      const result = await command({ action: 'get' });
      return res.status(result.ok ? 200 : 400).json(result);
    } catch (_) {
      return res.status(503).json({
        ok: false,
        error: 'SELL state unavailable; runtime enforcement is unverified'
      });
    }
  });

  app.post(
    '/api/sell-controls/:bot',
    noStore,
    writeOrigin,
    requireSession,
    async (req, res) => {
      const body = req.body;
      if (
        !bots.includes(req.params.bot) ||
        !body || typeof body !== 'object' || Array.isArray(body) ||
        Object.keys(body).sort().join(',') !== 'confirm,enabled,request_id' ||
        typeof body.enabled !== 'boolean' ||
        body.confirm !== true ||
        typeof body.request_id !== 'string' ||
        !/^[A-Za-z0-9_-]{1,128}$/.test(body.request_id)
      ) {
        return res.status(400).json({
          ok: false,
          error: 'Canonical bot, Boolean enabled, request_id and confirm:true required'
        });
      }

      try {
        const result = await command({
          action: 'set',
          bot: req.params.bot,
          enabled: body.enabled,
          request_id: body.request_id,
          actor: 'authenticated-dashboard-admin',
          confirm: true
        });
        return res.status(result.ok ? 202 : 400).json(result);
      } catch (_) {
        return res.status(503).json({
          ok: false,
          error: 'Save outcome unconfirmed; refresh state and retry with the same request_id',
          runtime_acknowledged: false,
          effective_enabled: null
        });
      }
    }
  );
};
