# Equity buying worker — standalone reference source

STATUS: offline-tested implementation; NOT deployed, not a production acceptance
sign-off, and not a one-command replacement for the current installation.

## Files
- equity_worker.py: asyncio worker and concrete MCP/OAuth broker adapter.
- equity_commands_v1.py: durable Boolean control queue and CLI.
- publish_universe.py: publishes ticker selections from an upstream screener.
- config.example.json: disabled-by-default configuration.
- funding-authorization.example.json: account-specific allocated-capital authority.
- test_equity_worker.py: isolated tests using an in-memory SQLite ledger and fake broker.
- test-results.txt: actual test output from this package.
- si-equity-buy-worker.service.example: service template, not installed or enabled.

## What is implemented
- Exact full Equity account binding and account eligibility checks.
- Existing VPS OAuth transport, pagination, strict quotes and broker responses.
- Persistent positions derived from this worker's own reconciled buy/sell intents.
- 60 sampled midpoint observations; Crypto-derived 40/30/30 trend/momentum/breakout.
  Entry requires score 100, sample gaps 5-25 seconds, spread <=0.5%, and 600-second
  per-symbol cooldown. Scores are qualification rules, NOT predicted profits.
- Fractional MARKET quantities rounded down to six decimal places, regular hours
  only. Weekday/time guard is reinforced by fresh exchange quotes, tradability and
  broker review; a standalone exchange-holiday calendar is not included.
- Saved Equity allocation and utilization read from existing dashboard APIs.
  Example settings are not substituted if these APIs fail.
- New buys stop on stale configuration; own-position exits may use retained last
  good stop/profit policy. No valid policy ever loaded means no orders.
- Aggregate deployment ceiling, own cost/fees, realized losses, pending reservations,
  broker cash and unleveraged buying power bound purchases. No fixed per-ticker cap.
  Multiple qualifying tickers share capacity equally at that cycle. At most one new
  order is submitted per cycle. The saved maximum-position setting is respected.
- Additional buys of worker-owned positions can qualify; they do not count as new
  positions. Symbols managed by the legacy guard are deliberately excluded to avoid
  breaking that guard's fixed-quantity reconciliation.
- Stable UUID before submission; UNKNOWN and unexpected approval outcomes retain
  reservations. No blind re-submission. Missing broker evidence blocks new entries.
- Permission, policy, quote, signal, ownership and capacity rechecks after review.
- Own-position software loss stops, armed entry-price protection, profit floors and
  weak/unavailable-rally exits. Profit thresholds pin per lifecycle, while stop-loss
  settings refresh. Rebuys alter average entry basis but do not reset armed flags.
- Daily loss lock: first verified regular-session net-liquidation observation each
  New York day is the baseline; real execution fees count, future exit fees are not
  estimated in the mark. Baseline/lock persist within a day; authorized allocation
  changes adjust the baseline rather than appearing as profit. This is an explicit
  reference implementation of the previously unspecified daily-loss denominator.
- Auto Trade OFF blocks both buy and sell submissions. Existing broker orders are
  not automatically cancelled. Monitor/reconciliation loops continue.

## Broker authorization is not a configuration trick
programmatic_execution_authorized must reflect an independently permitted unattended
execution arrangement; this file does not create one or waive platform requirements.
The worker refuses new orders if review says app approval is required or unknown, or
if there are broker alerts. It does not auto-click or bypass confirmations. If an
approval is unexpectedly returned after a clear review, it records the approval ID
and blocks duplicates. It can associate a subsequently accepted order by that ID.
There is no verified trade-approval expiry/cancellation API adapter in this package;
absent final broker evidence, such an intent stays blocked rather than guessing.
The captured MCP workflow's separate confirmation requirements must be resolved for
the intended deployment. These offline tests do not establish that authorization.

## Inputs and limits — important
1. Dynamic universe selection is an external dependency. publish_universe.py validates
   and timestamps an upstream screener's symbols; it DOES NOT discover the best US
   stocks or implement market-wide intelligence/news screening. No fixed ticker list
   is silently supplied. An absent/stale feed blocks new buys, not owned-position exits.
2. Initial external holdings are excluded from the worker's managed cost/P&L. This is
   not an adoption/migration tool. Do not run it on another worker's state directory.
3. Funding authorization must match the full configured account and saved allocation.
   It is not proof of actual broker funding; live cash checks apply too. A funding
   increase needs the authorization record and dashboard allocation to agree.
4. Reservation double-counting during partial buy fills is intentionally conservative.
   Pending owned orders block NEW buys; other symbols' exits may continue.
5. MARKET orders have no price ceiling. The 2% cost buffer is an estimate, NOT a
   guaranteed maximum debit or guaranteed fill. A hard price ceiling needs limit
   orders, which cannot use fractional quantities through the inspected interface.
6. Do not run two independent workers against the SAME ledger under different lock
   paths. This package serializes its own worker, not all account-wide external writers.
   It is not a cross-bot treasury or verified external cash-attribution engine.
7. Defaults keep live_enabled=false and programmatic_execution_authorized=false.
   The private control store also defaults buy=false. No code auto-enables buying.
8. The example uses a separate control DB so it does not silently mutate the existing
   guard. Connecting the dashboard Buy button to this control store, migrating current
   controls, and production service installation are separate deployment work.
9. Crypto and Zerodha files are not modified or provided by this package.
10. Network timeouts, missing evidence and invalid responses may block trading. No
    successful broker connection, approval capability, accepted live order or fill has
    been tested here. SDK and live account compatibility still require verification.

## Isolated tests — no real broker calls
From the extracted source directory:
    /home/botuser/si-mcp-auth-venv/bin/python -B -m unittest -v test_equity_worker

## Configuration / execution interface (not an install instruction)
After independently reviewing and configuring a permitted deployment:
    python -B equity_worker.py --config /etc/si-equity-buy-worker/config.json

Control store inspection:
    python -B equity_commands_v1.py status --db /var/lib/si-equity-buy-worker/controls.sqlite3

Queueing controls is a REAL permission change; do not run it as a test:
    python -B equity_commands_v1.py queue --db /var/lib/si-equity-buy-worker/controls.sqlite3 --updates '{"bot":true,"auto_trade":true,"buy":true,"sell":true}'
The queue accepts Buy ON only after recent buy-worker presence. QUEUED is not APPLIED
and neither is a filled order. Turning permissions off does not cancel existing orders.

## Review before production
Verify actual backend automation authority, configure/publish a real screener feed,
confirm daily-loss baseline semantics, resolve ownership/treasury attribution, test
SDK schemas, protect required sell-worker continuity, and integrate dashboard controls
before activation. Exercise concurrency, crash recovery, broker refusal, partial fills,
and cancellation/approval lifecycle on the exact intended release. Offline tests are
not a substitute for those integration checks.
