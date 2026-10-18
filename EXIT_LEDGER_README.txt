OFFLINE COMPONENT ONLY - NOT A DEPLOYMENT PACKAGE

Run: python3 -B -m unittest -v test_exit_attribution_ledger.py

This separate ledger persists SELL ownership allocations. It does not modify the original bot journal, capital_guard.py, services, controls or UI. No account-wide live execution or capital migration is implemented. The executor must enforce fresh broker evidence, authorization, price/fee policy and live order reservations. Do not add attributed proceeds to existing capital balances without a reviewed integration.
