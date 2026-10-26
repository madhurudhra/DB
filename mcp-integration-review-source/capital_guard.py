from pathlib import Path
from decimal import Decimal
import json
import sqlite3

SETTINGS = Path(__file__).with_name("allocation.json")

def configuration():
    config = json.loads(SETTINGS.read_text())
    basis = Decimal(str(config["crypto_funding_basis"]))
    if not basis.is_finite() or basis < 0:
        raise RuntimeError("Invalid crypto funding basis")
    if not isinstance(config["account"], str) or not config["account"]:
        raise RuntimeError("Invalid capital allocation account")
    return config

def crypto_cash(db=None, reserve_pending=True):
    config = configuration()
    own_connection = db is None
    if own_connection:
        path = Path(config["journal"]).resolve()
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
    try:
        metadata = dict(db.execute("SELECT key,value FROM meta"))
        if metadata.get("account") != config["account"]:
            raise RuntimeError("Capital allocation account mismatch")

        cash = Decimal(config["crypto_funding_basis"])
        states = {}
        spent = {}
        sides = {}
        for row in db.execute("SELECT * FROM orders ORDER BY rowid"):
            q = Decimal(row["filled"])
            p = Decimal(row["average"])
            fee = Decimal(row["fee"])
            if not all(v.is_finite() for v in (q, p, fee)):
                raise RuntimeError("Invalid capital accounting value")
            if q < 0 or p < 0 or fee < 0:
                raise RuntimeError("Negative capital accounting value")
            if row["side"] == "buy":
                cash -= q * p + fee
            elif row["side"] == "sell":
                cash += q * p - fee
            else:
                raise RuntimeError("Unknown journal order side")
            if q > 0 and p <= 0:
                raise RuntimeError("Filled order has no positive average price")
            states[row["cid"]] = row["state"]
            spent[row["cid"]] = q * p + fee
            sides[row["cid"]] = row["side"]

        if reserve_pending:
            for cid, amount in db.execute(
                "SELECT cid,amount FROM capital_reservations"
            ):
                reservation = Decimal(str(amount))
                if not reservation.is_finite() or reservation < 0:
                    raise RuntimeError("Invalid capital reservation")
                if cid not in states or sides[cid] != "buy":
                    raise RuntimeError("Reservation has no matching buy order")
                if states[cid] not in {"filled", "canceled", "failed"}:
                    cash -= max(Decimal("0"), reservation - spent[cid])

        if not cash.is_finite():
            raise RuntimeError("Invalid available crypto capital")
        return max(Decimal("0"), cash)
    finally:
        if own_connection:
            db.close()

def stock_cash(total_available):
    total = Decimal(str(total_available))
    if not total.is_finite() or total < 0:
        raise RuntimeError("Invalid stock buying-power response")
    return max(Decimal("0"), total - crypto_cash(reserve_pending=False))
