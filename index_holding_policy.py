"""Index-stock holding decisions. No broker calls or order execution.

Trusted upstream integrations must supply verified position, membership,
quote, control and ledger data. This module does not verify their sources.
"""

from decimal import Decimal, InvalidOperation

VERSION = "index-holding-policy-v1"


def decimal_value(value):
    if value is None or isinstance(value, bool):
        raise ValueError("Numeric value required")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Finite numeric value required")
    return result


def evaluate_position(position, rules):
    result = {
        "policy_version": VERSION,
        "status": "BLOCKED",
        "action": "NONE",
        "order_submission_allowed": False,
        "quantity": None,
        "reasons": [],
        "add_decision": {
            "action": "NONE",
            "quantity": None,
            "reasons": [],
        },
    }

    def blocked(reason):
        result["reasons"].append(reason)
        return result

    try:
        bot = position.get("bot")
        if bot not in ("equity", "zerodha"):
            return blocked("POLICY_APPLIES_TO_EQUITY_AND_ZERODHA_ONLY")

        if position.get("security_type") != "COMMON_STOCK":
            return blocked("INDEX_STOCK_POLICY_NOT_APPLICABLE")

        required = (
            "account_verified",
            "position_verified",
            "quote_verified_fresh",
            "reconciliation_verified",
            "membership_verified_current",
            "controls_verified",
        )
        for field in required:
            if position.get(field) is not True:
                return blocked(field.upper() + "_REQUIRED")

        expected_index = "SP500" if bot == "equity" else "NIFTY50"
        membership = position.get("index_memberships")

        if not isinstance(membership, list):
            return blocked("VERIFIED_MEMBERSHIP_LIST_REQUIRED")

        is_index_stock = expected_index in membership

        if is_index_stock:
            configured = rules.get("index_stop_loss_pct")

            if configured is None:
                return blocked("EXACT_INDEX_STOP_BETWEEN_3_AND_5_REQUIRED")

            stop_pct = decimal_value(configured)

            if not Decimal("3") <= stop_pct <= Decimal("5"):
                return blocked("INDEX_STOP_MUST_BE_BETWEEN_3_AND_5_PERCENT")
        else:
            stop_pct = Decimal("1.5")

        # Stable original risk reference supplied by the execution ledger.
        # Never substitute a new average cost after adding shares.
        reference = decimal_value(position.get("original_risk_reference_price"))
        previous_trigger = position.get("previous_stop_trigger_price")
        bid = decimal_value(position.get("executable_bid"))
        quantity = decimal_value(position.get("owned_quantity"))
        protected = decimal_value(position.get("protected_quantity"))

        if reference <= 0 or bid <= 0:
            return blocked("POSITIVE_REFERENCE_AND_BID_REQUIRED")

        if quantity < 0 or protected < 0 or protected > quantity:
            return blocked("INVALID_OWNED_OR_PROTECTED_QUANTITY")

        trigger = reference * (Decimal("1") - stop_pct / Decimal("100"))

        # An existing protective trigger may tighten this rule,
        # but this module must never lower it automatically.
        if previous_trigger is not None:
            previous_trigger = decimal_value(previous_trigger)
            if previous_trigger <= 0:
                return blocked("INVALID_PREVIOUS_STOP_TRIGGER")
            trigger = max(trigger, previous_trigger)

        unprotected = quantity - protected

        result.update({
            "category": expected_index if is_index_stock else "STANDARD",
            "configured_stop_loss_pct": str(stop_pct),
            "risk_reference_basis": "ORIGINAL_LEDGER_REFERENCE",
            "risk_reference_price": str(reference),
            "proposed_stop_trigger_price": str(trigger),
            "unprotected_quantity": str(unprotected),
            "stop_execution_guaranteed": False,
        })

        if unprotected == 0:
            result.update({
                "status": "PROTECTED",
                "action": "NONE",
                "reasons": ["NO_UNPROTECTED_SHARES"],
            })
            return result

        if position.get("sell_enabled") is not True:
            result.update({
                "status": "AUTOMATIC_SELL_DISABLED",
                "action": "NONE",
                "reasons": ["SELL_CONTROL_BLOCKS_AUTOMATIC_EXITS"],
            })
            result["add_decision"]["reasons"] = [
                "NO_ADD_WHILE_AUTOMATIC_EXIT_PROTECTION_DISABLED"
            ]
            return result

        if bid <= trigger:
            result.update({
                "status": "EXIT_CANDIDATE",
                "action": "SELL_CANDIDATE",
                "quantity": str(unprotected),
                "reasons": ["DOWNSIDE_TRIGGER_REACHED"],
            })
            result["add_decision"]["reasons"] = [
                "NO_ADD_WHEN_DOWNSIDE_EXIT_TRIGGERED"
            ]
            return result

        # Profit policy remains an explicit, separately approved decision.
        profit_mode = rules.get("profit_mode")

        if profit_mode not in ("ADAPTIVE_5_TO_15", "LONG_TERM_NO_FIXED_TARGET"):
            return blocked("INDEX_PROFIT_POLICY_CONFIRMATION_REQUIRED")

        result.update({
            "status": "HOLD_CANDIDATE",
            "action": "HOLD_CANDIDATE",
            "reasons": ["DOWNSIDE_TRIGGER_NOT_REACHED"],
            "profit_mode": profit_mode,
        })

        if profit_mode == "ADAPTIVE_5_TO_15":
            result["profit_exit_status"] = (
                "SEPARATE_ADAPTIVE_EXIT_ENGINE_REQUIRED"
            )
        else:
            result["profit_exit_status"] = (
                "NO_FIXED_PROFIT_TARGET_REQUESTED"
            )

        add = result["add_decision"]

        if not is_index_stock:
            add["reasons"] = ["INDEX_ADD_POLICY_NOT_APPLICABLE"]
            return result

        if rules.get("allow_additions") is not True:
            add["reasons"] = ["ADDITIONS_NOT_EXPLICITLY_ENABLED"]
            return result

        max_additions = rules.get("maximum_additions")
        max_fraction = rules.get("maximum_add_fraction_of_original")

        if (
            type(max_additions) is not int
            or max_additions < 1
            or max_fraction is None
        ):
            add["reasons"] = ["EXPLICIT_ADDITION_LIMITS_REQUIRED"]
            return result

        fraction = decimal_value(max_fraction)

        if not Decimal("0") < fraction <= Decimal("1"):
            add["reasons"] = ["INVALID_ADDITION_FRACTION"]
            return result

        completed = position.get("completed_additions")

        if type(completed) is not int or completed < 0:
            add["reasons"] = ["VERIFIED_ADDITION_COUNT_REQUIRED"]
            return result

        if completed >= max_additions:
            add["reasons"] = ["ADDITION_COUNT_LIMIT_REACHED"]
            return result

        checks = {
            "bot_entry_enabled": "BOT_ENTRY_DISABLED",
            "buy_enabled": "AUTOMATIC_BUY_DISABLED",
            "fresh_recovery_signal": "RECOVERY_SIGNAL_REQUIRED",
            "buy_restrictions_clear": "BUY_RESTRICTIONS_NOT_CLEAR",
            "capital_reservation_verified": "CAPITAL_RESERVATION_REQUIRED",
            "position_limit_verified": "POSITION_LIMIT_CHECK_REQUIRED",
            "sector_limit_verified": "SECTOR_LIMIT_CHECK_REQUIRED",
            "projected_total_loss_within_original_budget":
                "ADDITION_EXCEEDS_ORIGINAL_RISK_BUDGET",
            "no_unresolved_orders": "UNRESOLVED_ORDER_CHECK_REQUIRED",
            "protective_exit_verified": "WORKING_EXIT_PROTECTION_REQUIRED",
        }

        failed = [
            reason for field, reason in checks.items()
            if position.get(field) is not True
        ]

        if failed:
            add["reasons"] = failed
            return result

        original_value = decimal_value(position.get("original_entry_value"))
        reserved = decimal_value(position.get("reserved_addition_value"))

        if original_value <= 0 or reserved <= 0:
            add["reasons"] = ["POSITIVE_VERIFIED_ENTRY_AND_RESERVATION_REQUIRED"]
            return result

        cap = original_value * fraction

        if reserved > cap:
            add["reasons"] = ["ADDITION_VALUE_EXCEEDS_APPROVED_CAP"]
            return result

        add.update({
            "action": "ADD_CANDIDATE",
            "quantity": None,
            "maximum_addition_value": str(cap),
            "reserved_addition_value": str(reserved),
            "reasons": [
                "RECOVERY_SIGNAL_AND_ADDITION_RISK_CHECKS_PASSED"
            ],
        })

        return result

    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return blocked("INVALID_OR_INCOMPLETE_POLICY_INPUT")
