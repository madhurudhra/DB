"""Internal sizing helpers; never submit or authorize an order."""
from decimal import (
    Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext,
)


def _number(value, *, zero=False):
    if value is None or isinstance(value, bool):
        raise ValueError("Invalid numeric input")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid numeric input") from None
    if (
        not result.is_finite()
        or result < 0
        or (not zero and result == 0)
        or result > Decimal("1000000000000")
        or result.as_tuple().exponent < -18
    ):
        raise ValueError("Numeric input outside supported range")
    return result


def plan_buy(
    *,
    max_total,
    limit_price,
    asset_increment,
    quote_increment,
    min_order_amount,
    max_order_size,
    fee_ratio,
):
    """Calculate a fee-inclusive plan, not permission to submit.

    Caller must supply verified, fresh v2 inputs and a bounded limit price.
    Other charges, rate changes, capital ownership, and broker execution
    require separate validation.
    """
    with localcontext() as context:
        context.prec = 80

        budget = _number(max_total)
        price = _number(limit_price)
        asset_step = _number(asset_increment)
        quote_step = _number(quote_increment)
        minimum = _number(min_order_amount, zero=True)
        maximum = _number(max_order_size)
        rate = _number(fee_ratio, zero=True)

        if rate >= 1:
            raise ValueError("Fee ratio must be less than one")

        price_units = price / quote_step
        if price_units != price_units.to_integral_value():
            raise ValueError("Limit price violates quote increment")

        cent = Decimal("0.01")

        def costs(units):
            quantity = asset_step * units
            notional = quantity * price
            principal_bound = notional.quantize(
                cent, rounding=ROUND_CEILING
            )
            fee_bound = (notional * rate).quantize(
                cent, rounding=ROUND_CEILING
            )
            return quantity, notional, principal_bound, fee_bound

        # Keep quantity strictly below max_order_size, as required
        # when requesting v2 estimated prices.
        maximum_units = int(
            (maximum / asset_step).to_integral_value(
                rounding=ROUND_CEILING
            )
        ) - 1

        budget_units = int(
            (budget / price / asset_step).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )

        low = 0
        high = min(maximum_units, budget_units)

        while low < high:
            middle = (low + high + 1) // 2
            _, _, principal, fee = costs(middle)
            if principal + fee <= budget:
                low = middle
            else:
                high = middle - 1

        if low <= 0:
            raise ValueError("Budget cannot fund one asset increment")

        quantity, notional, principal, fee = costs(low)
        if notional < minimum:
            raise ValueError("Budget cannot satisfy minimum order amount")

        total = principal + fee
        if total > budget or quantity >= maximum:
            raise ValueError("Sizing bounds violated")

        return {
            "schema_version": 1,
            "asset_quantity": format(quantity, "f"),
            "limit_price": format(price, "f"),
            "fee_ratio": format(rate, "f"),
            "principal_bound": format(principal, "f"),
            "fee_bound": format(fee, "f"),
            "reservation_total": format(total, "f"),
            "budget_ceiling": format(budget, "f"),
            "broker_estimate_verified": False,
            "capital_reserved": False,
            "order_submission_allowed": False,
        }


def require_verified_crypto_capital(db, *, account_ref):
    """Readiness gate only. Does not reserve capital or permit submission."""
    import json
    from app.core.capital_ledger_v1 import snapshot

    if db.in_transaction:
        raise RuntimeError("Capital readiness requires a clean connection")

    db.execute("BEGIN")
    try:
        row = db.execute(
            "SELECT revision,config_json "
            "FROM independent_bot_configurations WHERE bot=?",
            ("crypto",),
        ).fetchone()

        if row is None:
            raise RuntimeError("BOT_CONFIGURATION_MISSING")

        config = json.loads(row["config_json"])
        if config.get("account_ref") != account_ref:
            raise RuntimeError("CONFIGURED_ACCOUNT_CHANGED")

        report = snapshot(db, "crypto")

        if report.get("configuration_revision") != row["revision"]:
            raise RuntimeError("CAPITAL_CONFIGURATION_CHANGED")

        if report.get("reservation_checks_ready") is not True:
            raise RuntimeError("VERIFIED_CAPITAL_REQUIRED")

        _number(report.get("available_automatic_budget"))
        return report
    finally:
        db.rollback()
