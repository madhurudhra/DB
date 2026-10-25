"""Read-only configuration loader. Does not authorize or submit trades."""
import json
from decimal import Decimal
from pathlib import Path

POLICY_PATH = Path("/etc/super-intelligence/trading-policy.json")

def load_policy(path=POLICY_PATH):
    data = json.loads(Path(path).read_text())
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported policy schema")
    if data.get("live_activation_authorized") is not False:
        raise ValueError("This configuration-only deployment cannot authorize live trading")
    defaults = data["defaults"]
    for key in ("stop_loss_pct", "take_profit_pct", "cash_reserve_pct", "deployment_ceiling_pct"):
        value = Decimal(defaults[key])
        if not value.is_finite() or not 0 < value < 100:
            raise ValueError("Invalid percentage: " + key)
    if Decimal(defaults["cash_reserve_pct"]) + Decimal(defaults["deployment_ceiling_pct"]) != 100:
        raise ValueError("Reserve and deployment must sum to 100")
    if set(data["bots"]) != {"crypto", "equity", "zerodha"}:
        raise ValueError("Unexpected bot set")
    for name, bot in data["bots"].items():
        if bot["currency"] != ("INR" if name == "zerodha" else "USD"):
            raise ValueError("Invalid bot currency")
        if bot["allocated_capital"] is not None:
            amount = Decimal(bot["allocated_capital"])
            if not amount.is_finite() or amount <= 0:
                raise ValueError("Invalid allocation")
    return data

def budget_summary(data):
    result = {}
    for name, bot in data["bots"].items():
        reserve_pct = Decimal(
            bot.get(
                "cash_reserve_pct",
                data["defaults"]["cash_reserve_pct"],
            )
        )
        deployment_pct = Decimal(
            bot.get(
                "deployment_ceiling_pct",
                data["defaults"]["deployment_ceiling_pct"],
            )
        )
        if (
            not reserve_pct.is_finite()
            or not deployment_pct.is_finite()
            or not 0 < reserve_pct < 100
            or reserve_pct + deployment_pct != 100
        ):
            raise ValueError("Invalid allocation percentages: " + name)

        amount = bot["allocated_capital"]
        if amount is None:
            dynamic = (
                bot.get("allocation_mode")
                == "verified_eligible_account_capital"
            )
            result[name] = {
                "currency": bot["currency"],
                "status": (
                    "AWAITING_VERIFIED_CAPITAL"
                    if dynamic else "BUDGET_NOT_SET"
                ),
                "allocation_mode": bot.get("allocation_mode"),
                "reserve_pct": str(reserve_pct),
                "deployment_ceiling_pct": str(deployment_pct),
                "fixed_budget_ceiling": None,
                "runner_enforcement_verified": False,
            }
        else:
            capital = Decimal(amount)
            if not capital.is_finite() or capital <= 0:
                raise ValueError("Invalid capital: " + name)
            result[name] = {
                "currency": bot["currency"],
                "allocated": str(capital),
                "reserve": str(capital * reserve_pct / 100),
                "deployment_ceiling": str(
                    capital * deployment_pct / 100
                ),
                "reserve_pct": str(reserve_pct),
                "deployment_ceiling_pct": str(deployment_pct),
                "runner_enforcement_verified": False,
            }
    return result
