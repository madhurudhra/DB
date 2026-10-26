"""Atomic submission claim for the separate exit ledger. NOT a broker adapter.

Every executor using this ledger must claim before its first broker request.
A successful claim does not replace fresh holdings, SELL permission, kill-switch,
quantity, quote/fee, market-session or price checks. Reconcile stuck claims;
never reset them automatically. This module does not manage broker retries.
"""
import json
import sqlite3
import uuid
from datetime import datetime,timezone
from pathlib import Path

class ClaimError(RuntimeError):
    pass

def claim_exit(filename,*,cid,bot,account):
    """Return a claim token once, or None if this intent was already claimed.

    The existing database must use the ExitLedger schema. No database or table
    is created here. After committing the claim, a crash leaves SUBMITTING;
    broker reconciliation, not an automatic retry, must resolve it.
    """
    if bot not in {"crypto","us_trading","zerodha"}:
        raise ClaimError("Unknown bot")
    if any(not isinstance(v,str) or not v or v!=v.strip() for v in (cid,account)):
        raise ClaimError("Explicit client ID and account required")
    path=Path(filename).resolve()
    db=None
    try:
        db=sqlite3.connect(path.as_uri()+"?mode=rw",uri=True,timeout=10,isolation_level=None)
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        binding=db.execute("SELECT account FROM account_bindings WHERE bot=?",(bot,)).fetchone()
        if binding is None or binding[0]!=account:
            raise ClaimError("Account binding mismatch")
        row=db.execute("SELECT bot,account,state,broker_id FROM exit_orders WHERE cid=?",(cid,)).fetchone()
        if row is None or (row["bot"],row["account"])!=(bot,account):
            raise ClaimError("Unknown or mismatched exit intent")
        if row["state"]!="INTENT":
            db.rollback()
            return None
        if row["broker_id"] is not None:
            raise ClaimError("INTENT already contains broker evidence; reconcile first")
        token=str(uuid.uuid4())
        evidence=json.dumps({"claim_id":token,"claimed_at":datetime.now(timezone.utc).isoformat()},sort_keys=True)
        changed=db.execute(
            "UPDATE exit_orders SET state='SUBMITTING',last_update=? "
            "WHERE cid=? AND bot=? AND account=? AND state='INTENT' AND broker_id IS NULL",
            (evidence,cid,bot,account)).rowcount
        if changed!=1:
            raise ClaimError("Exit claim state changed unexpectedly")
        db.commit()
        return token
    except ClaimError:
        if db is not None:db.rollback()
        raise
    except (sqlite3.Error,OSError,ValueError) as exc:
        if db is not None:db.rollback()
        raise ClaimError("Exit claim unavailable; do not submit") from exc
    finally:
        if db is not None:db.close()
