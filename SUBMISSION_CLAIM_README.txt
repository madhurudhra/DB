OFFLINE COMPONENT - NOT A LIVE DEPLOYMENT PACKAGE

Run: python3 -B -m unittest -v test_exit_submission_claim.py

Requires the separate ExitLedger database schema. All executors must use this claim before sending a broker request. A committed claim is not broker acceptance or a fill. No permission, price, holdings, fee or kill-switch checks are performed here; preserve them in the actual executor. Stuck SUBMITTING/UNCERTAIN intents require reconciliation, never automatic resetting.
