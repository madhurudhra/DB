# Conditional enforcement implementation candidate

STATUS: tested component, NOT a finished live-trading system or an activation package.
No installer, network calls, broker orders, service restarts, credential changes,
or live configuration writes are included. Do not overwrite production modules
until the missing producer, executor and reconciler are implemented and reviewed.

## Contents
- automatic_order_guard.py: conditional transactional order claim/reservation.
- write_policy.py: conditional policy, replacing both layers of hardcoded refusal
  when integrated; retains BotPolicy/BOT_POLICIES compatibility names.
- tests/test_enforcement.py: 26 local tests using temporary SQLite databases.
- TEST_RESULTS.txt: actual local test output.

## Test
From this extracted directory:
    python3 -m unittest discover -s tests -v

No credentials, third-party packages or network access are needed by the tests.
Tests install simulated app modules in the test process and NEVER import the
production app or connect to its database. The capital-reader/reservation boundary
is simulated, not the real deployed capital_ledger_v1. Tests use Crypto fixtures;
Equity/Zerodha broker integration has NOT been tested.

## What a successful claim means
The function returns (True, 'SUBMISSION_CLAIM_ACQUIRED') after a transaction:
1. Saved account, currency, config/control revisions and permissions match.
2. Restrictions, numeric bounds, quantity/price increments and fresh metadata pass.
3. The prepared bounded-limit payload matches the persisted asset-quantity intent.
4. A recorded matching decision/risk evaluation and latest passing reconciliation exist.
5. No pending or ambiguous account order exists; existing Crypto caps pass.
6. BUY: existing capital snapshot and latest reconciliation agree and a new
   fee-inclusive capital reservation is acquired.
7. SELL: fresh normalized sellable position, fill-attributable bot ownership,
   protected quantity and local reservations permit the proposed quantity.
8. Intent transitions CREATED -> SUBMITTING, with an audit record, atomically.

It DOES NOT mean an order was submitted, accepted, filled or settled.
Unlike the old function, automatic_order_preflight is SIDE EFFECTING when given
intent_id. Never call it from a dashboard status endpoint or readiness poll.
It must only be used by the designated execution worker immediately before a send.

## Mandatory integration contract -- currently missing from the legacy runner
A trusted authenticated broker adapter and risk engine must create the prepared
records. Never accept execution_plan_v2, risk records, position-normalization
fields or reconciliation PASS flags from dashboard/client JSON. Hashes here bind
payload content; they are NOT proof of broker authentication or authoritative data.

The current runner's market-order / quote_amount_usd intent is incompatible by
DESIGN. Changing its call to supply intent_id is NOT enough. It must be rewritten
to prepare a bounded limit order, then send the EXACT claimed payload, without
resizing or rebuilding it. This package does not perform that rewrite.

Persist under metadata_json:
    quantity_is: 'asset_quantity'
    execution_plan_v2:
        config_revision: exact current integer revision
        control_revision: exact current integer revision
        prepared_at, quote_at: timezone-aware ISO timestamps, maximum age 10s
        fees_at, account_verified_at, instrument_verified_at: maximum age 60s
        account_ref: verified account, matching configured account
        asset_quantity, limit_price: positive decimal strings
        asset_increment, quote_increment: positive broker-confirmed increments
        min_order_amount, max_order_size: broker-confirmed bounds
        fee_ratio: verified upper-bound rate in [0,1)
        fee_bound: verified upper-bound fee; >= ceil(quantity*limit*rate, cents)
        other_charges_bound: explicit verified bound, including zero when verified
        reservation_total: ceil(quantity*limit,cents)+fee_bound+other_charges_bound
        payload: exact broker-supported limit order payload
        payload_sha256: SHA256 of sorted compact JSON payload (canonical helper)

Payload format is checked against the existing adapter's limit-order interface;
it has NOT been validated against a live broker. Verify supported time-in-force,
limits and fees against the chosen broker/version before implementation.

Database requirements:
- Existing app database and saved configuration/control tables, both with revision.
- Durable matching order_intents (UUID idempotency key, decision_id, type='limit').
- Matching decisions and fresh latest PASS risk_checks.
- Latest PASS reconciliation_runs, account-specific, fresh <=60s, no discrepancies.
- BUY: authoritative capital_verified_states populated by a real reconciler;
  matching latest reconciliation and existing capital_ledger_v1 infrastructure.
- SELL: fills attributable through broker_orders/order_intents; manual/pre-existing
  holdings are not assigned to the bot. Broker-position reader must supply
  raw_json.verified_sellable_quantity from a real, normalized broker response.

Fees and freshness values in metadata are claims from the trusted producer.
This package cannot make an absent/untrusted producer authoritative. A producer
must verify these values using broker responses, not set flags to pass the gate.

Consumer responsibilities AFTER a claim:
- Validate the broker adapter and explicit broker-write settings BEFORE claiming.
- Send exact persisted payload once through the proper broker adapter.
- A timeout, crash or ambiguous result retains reservations and blocks new claims.
- Resolve using the same client_order_id; NEVER blindly resubmit or expire holds.
- Durably record broker order IDs and all fills and actual fees.
- Reconcile partial fills, cancellations, rejections and final settlement.
- Settle/release reservations and publish a fresh non-overlapping capital baseline
  atomically. Never publish a baseline double-counting unsettled local holds.
- SELL reservations in automatic_sell_reservations_v2 also require reconciler handling.
- Preserve independent exit scheduling even when entry/bot switches are OFF.
- Update dashboard states from actual runner/broker acknowledgements, not saved flags.

The claim's transaction serializes local claims; it is NOT an atomic transaction
with the broker, and it is NOT a global firewall for other scripts or manual trades.
Existing direct adapter calls are not intercepted. Dedicated execution-worker
integration and broker reconciliation remain necessary.

## Expected results with the CURRENT legacy runner
No intent_id -> ORDER_INTENT_CONTEXT_REQUIRED.
Legacy market intent -> BOUNDED_LIMIT_ORDER_REQUIRED.
No authoritative capital baseline -> VERIFIED_CAPITAL_REQUIRED or capital-reader error.
These are actual unmet prerequisites. This is NOT an all-green workaround.

## Production status
Not deployed. Not reviewed for live use. No live broker or actual deployed capital
ledger integration test performed. No complete producer/executor/reconciler included.
This package does not finish the requested end-to-end live trading implementation.
