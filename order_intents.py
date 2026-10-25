import json
import uuid
from decimal import Decimal, InvalidOperation

from app.core.database import connect_database


def create_order_intent(
    *,
    bot_name,
    broker,
    symbol,
    side,
    quantity,
    order_type,
    account_ref=None,
    decision_id=None,
    metadata=None,
    idempotency_key=None,
):
    bot_name = str(bot_name).strip()
    broker = str(broker).strip()
    symbol = str(symbol).strip().upper()
    side = str(side).strip().upper()
    order_type = str(order_type).strip().upper()

    if not bot_name or not broker or not symbol:
        raise ValueError("bot_name, broker, and symbol are required")
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if order_type not in {"MARKET", "LIMIT", "STOP_LIMIT"}:
        raise ValueError("Unsupported order type")

    try:
        qty = Decimal(str(quantity))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid quantity") from exc

    if not qty.is_finite() or qty <= 0:
        raise ValueError("Quantity must be finite and positive")

    key = str(idempotency_key or uuid.uuid4()).strip()
    if not key or len(key) > 200:
        raise ValueError("Invalid idempotency key")

    details = (
        bot_name, broker, account_ref, symbol,
        side, str(qty), order_type,
    )

    with connect_database() as db:
        db.execute("BEGIN IMMEDIATE")

        existing = db.execute(
            "SELECT * FROM order_intents WHERE idempotency_key = ?",
            (key,),
        ).fetchone()

        if existing:
            old = (
                existing["bot_name"],
                existing["broker"],
                existing["account_ref"],
                existing["symbol"],
                existing["side"],
                str(Decimal(str(existing["quantity"]))),
                existing["order_type"],
            )
            if old != details:
                raise ValueError(
                    "Idempotency key already belongs to a different order"
                )
            return dict(existing)

        cursor = db.execute(
            """
            INSERT INTO order_intents (
                idempotency_key, bot_name, broker, account_ref,
                symbol, side, quantity, order_type, status,
                decision_id, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CREATED', ?, ?)
            """,
            (
                key, bot_name, broker, account_ref, symbol,
                side, str(qty), order_type, decision_id,
                json.dumps(metadata or {}, separators=(",", ":")),
            ),
        )

        intent_id = cursor.lastrowid

        db.execute(
            """
            INSERT INTO audit_events (
                event_type, bot_name, correlation_id, details_json
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                "ORDER_INTENT_CREATED",
                bot_name,
                key,
                json.dumps(
                    {
                        "intent_id": intent_id,
                        "broker": broker,
                        "account_ref": account_ref,
                        "symbol": symbol,
                        "side": side,
                        "quantity": str(qty),
                        "order_type": order_type,
                    },
                    separators=(",", ":"),
                ),
            ),
        )

        row = db.execute(
            "SELECT * FROM order_intents WHERE id = ?",
            (intent_id,),
        ).fetchone()
        return dict(row)
