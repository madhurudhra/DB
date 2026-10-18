OFFLINE INTEGRATION CANDIDATE - NOT A PRODUCTION DEPLOYMENT

Run: python3 -B -m unittest -v test_offline_sell_backend.py

This package combines decision, account/quantity validation, intent reservation,
submission claiming, acknowledgement handling and cumulative fill attribution
in one reference implementation. Tests use only FakeBroker and temporary databases.
The synthetic broker fill prices are independent of quoted estimates; the suite
checks mechanics, not execution quality or realized threshold guarantees.

MISSING FOR LIVE USE:
- Actual Robinhood stock, Robinhood crypto and Zerodha broker adapters.
- Verified account-wide inventory, cost/activation references and fee estimates.
- Real cross-process SELL guard and preservation of all existing risk controls.
- Legacy capital/journal migration and reconciliation integration.
- Brokerage-specific markets, products, price/quantity/notional constraints.
- Deployment, service mapping, runtime acknowledgement and operational verification.
The coordinator supports only long positions. No UI/dashboard files are included.
Do not run this against live broker credentials or install over active bot code.
