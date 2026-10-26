"""Pure cumulative SELL-fill attribution; requires executor-side identity checks."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

@dataclass(frozen=True)
class Attribution:
    filled_quantity: Decimal
    bot_filled_quantity: Decimal
    external_filled_quantity: Decimal
    gross_proceeds: Decimal
    exit_fee: Decimal
    bot_net_credit: Decimal
    external_net_credit: Decimal

def _number(value,name):
    if isinstance(value,bool) or value is None:
        raise ValueError(name+" must be finite and nonnegative")
    try:
        value=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError):
        raise ValueError(name+" must be numeric") from None
    if not value.is_finite() or value<0:
        raise ValueError(name+" must be finite and nonnegative")
    return value

def attribute_fill(*,planned_bot_quantity,planned_external_quantity,
                   cumulative_filled_quantity,cumulative_average_price,cumulative_exit_fee):
    bot=_number(planned_bot_quantity,"bot quantity")
    external=_number(planned_external_quantity,"external quantity")
    filled=_number(cumulative_filled_quantity,"filled quantity")
    price=_number(cumulative_average_price,"average price")
    fee=_number(cumulative_exit_fee,"fee")
    with localcontext() as context:
        context.prec=50
        planned=bot+external
        if planned<=0 or filled>planned:
            raise ValueError("Invalid planned quantity or overfill")
        if filled==0:
            if fee!=0:raise ValueError("Fee without fill requires reconciliation")
            zero=Decimal(0)
            return Attribution(zero,zero,zero,zero,zero,zero,zero)
        if price<=0:raise ValueError("Filled order requires a positive average price")
        bot_filled=filled*bot/planned
        gross=filled*price
        net=gross-fee
        bot_credit=net*bot/planned
        return Attribution(filled,bot_filled,filled-bot_filled,gross,fee,bot_credit,net-bot_credit)
