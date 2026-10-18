"""Pure position-reduction checks. Not an order executor or full risk validator.

Call inside the executor's submission lock using fresh, account-bound data.
Successful validation does not replace SELL permission, fees, price checks,
market rules, existing kill switches, order journaling or fill reconciliation.
Available quantity must already account for broker restrictions and reservations.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

class ExitValidationError(ValueError):
    pass

@dataclass(frozen=True)
class Reduction:
    side: str
    quantity: int
    before: int
    after: int

def _integer(value, name, *, signed=False):
    if isinstance(value, bool) or value is None:
        raise ExitValidationError(name + " must be an integer")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ExitValidationError(name + " must be an integer") from None
    if not number.is_finite() or number != number.to_integral_value():
        raise ExitValidationError(name + " must be a finite integer")
    if not signed and number < 0:
        raise ExitValidationError(name + " cannot be negative")
    return int(number)

def _key(row):
    if not isinstance(row, dict):
        raise ExitValidationError("Invalid instrument record")
    values = tuple(row.get(k) for k in ("account", "exchange", "symbol", "product"))
    if any(not isinstance(v, str) or not v or v != v.strip() for v in values):
        raise ExitValidationError("Missing or ambiguous instrument identity")
    return values

def validate_reduction(
    *, expected_account, intent, position, pending_orders,
    broker_reconciled, allowed_instruments,
):
    """Check normalized INTEGER-unit positions without silently rounding quantity.

    intent: account, exchange, symbol, product, side, quantity.
    position: same identity, signed quantity, available_to_close.
    pending_orders: normalized nonterminal orders, each with the same identity keys.
    allowed_instruments: explicit (exchange, symbol, product) approval tuples.
    The caller must preserve separately configured execution restrictions.
    This function does not support fractional-unit instruments.
    """
    if broker_reconciled is not True:
        raise ExitValidationError("Fresh broker reconciliation required")
    if not isinstance(expected_account, str) or not expected_account:
        raise ExitValidationError("Pinned account required")
    intent_key, position_key = _key(intent), _key(position)
    if intent_key[0] != expected_account or position_key[0] != expected_account:
        raise ExitValidationError("Account mismatch")
    if intent_key != position_key:
        raise ExitValidationError("Position identity mismatch")
    if not isinstance(allowed_instruments, (set, frozenset)):
        raise ExitValidationError("Explicit instrument approval set required")
    if intent_key[1:] not in allowed_instruments:
        raise ExitValidationError("Instrument/product not approved")
    if not isinstance(pending_orders, list):
        raise ExitValidationError("Pending-order snapshot unavailable")
    for order in pending_orders:
        order_key = _key(order)
        if order_key[0] != expected_account:
            raise ExitValidationError("Pending-order account mismatch")
        if order_key == intent_key:
            raise ExitValidationError("An order for this position is already pending")

    held = _integer(position.get("quantity"), "held quantity", signed=True)
    available = _integer(position.get("available_to_close"), "available quantity")
    requested = _integer(intent.get("quantity"), "requested quantity")
    if held == 0 or requested == 0:
        raise ExitValidationError("No positive closeable order quantity")
    if available > abs(held):
        raise ExitValidationError("Available quantity exceeds position")
    if requested > min(abs(held), available):
        raise ExitValidationError("Order exceeds closeable quantity")
    side = intent.get("side")
    required_side = "SELL" if held > 0 else "BUY"
    if side != required_side:
        raise ExitValidationError("Order direction would not reduce the position")
    after = held - requested if held > 0 else held + requested
    return Reduction(side, requested, held, after)
