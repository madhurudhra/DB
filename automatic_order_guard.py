"""Restrictive automatic-order preflight.

This module never submits broker orders or authorizes execution.
Missing, invalid, or unverified state always blocks.
"""
import json
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.core.config import get_settings

BOT_IDS = frozenset(("crypto", "equity", "zerodha"))


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Invalid numeric value")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Invalid numeric value")
    return result


def automatic_order_preflight(
    bot,
    *,
    side=None,
    symbol=None,
    account_ref=None,
):
    if bot not in BOT_IDS:
        return False, "UNKNOWN_BOT"

    try:
        path = Path(get_settings().database_path).resolve()
        uri = path.as_uri() + "?mode=ro"

        with sqlite3.connect(
            uri,
            uri=True,
            timeout=5,
        ) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")

            control_row = db.execute(
                """
                SELECT state_json
                FROM dashboard_control_state
                WHERE singleton = 1
                """
            ).fetchone()

            config_row = db.execute(
                """
                SELECT config_json
                FROM independent_bot_configurations
                WHERE bot = ?
                """,
                (bot,),
            ).fetchone()

        if control_row is None:
            return False, "CONTROL_STATE_MISSING"
        if config_row is None:
            return False, "BOT_CONFIGURATION_MISSING"

        controls = json.loads(control_row[0])[bot]
        config = json.loads(config_row[0])

        if not isinstance(controls, dict):
            return False, "CONTROL_STATE_INVALID"
        if not isinstance(config, dict):
            return False, "BOT_CONFIGURATION_INVALID"

        for key in ("bot_enabled", "buy_enabled", "sell_enabled"):
            value = controls.get(key)
            if value is not None and type(value) is not bool:
                return False, "CONTROL_STATE_INVALID"

        # The legacy runner does not yet supply trustworthy side,
        # symbol and account context. Do not guess them.
        if side not in ("BUY", "SELL"):
            return False, "ORDER_SIDE_CONTEXT_REQUIRED"

        if not isinstance(symbol, str) or not symbol.strip():
            return False, "ORDER_SYMBOL_CONTEXT_REQUIRED"

        symbol = symbol.strip().upper()

        if side == "BUY":
            if controls.get("bot_enabled") is not True:
                return False, "AUTOMATIC_ENTRY_RUNTIME_NOT_ENABLED"
            if controls.get("buy_enabled") is not True:
                return False, "AUTOMATIC_BUY_NOT_ENABLED"
        else:
            # Entry/runtime stop does not implicitly disable exits.
            # SELL permission remains an independent control.
            if controls.get("sell_enabled") is not True:
                return False, "AUTOMATIC_SELL_NOT_ENABLED"

        configured_account = config.get("account_ref")
        if (
            not isinstance(configured_account, str)
            or not configured_account.strip()
        ):
            return False, "CONFIGURED_ACCOUNT_REQUIRED"

        if not account_ref or account_ref != configured_account:
            return False, "VERIFIED_ACCOUNT_CONTEXT_REQUIRED"

        expected_currency = "INR" if bot == "zerodha" else "USD"
        if config.get("currency") != expected_currency:
            return False, "CONFIGURATION_CURRENCY_INVALID"

        reserve_pct = _number(config.get("cash_reserve_pct"))
        if reserve_pct >= 100:
            return False, "RESERVE_PERCENTAGE_INVALID"

        allocation = config.get("allocated_capital")
        if allocation is not None:
            if _number(allocation) <= 0:
                return False, "AUTOMATIC_ALLOCATION_NOT_POSITIVE"
        elif bot != "zerodha":
            return False, "AUTOMATIC_ALLOCATION_REQUIRED"

        buy_rules = config.get("buy_restrictions", [])
        sell_rules = config.get("sell_protections", [])

        if not isinstance(buy_rules, list):
            return False, "BUY_RESTRICTIONS_INVALID"
        if not isinstance(sell_rules, list):
            return False, "SELL_PROTECTIONS_INVALID"

        for rule in buy_rules:
            if not isinstance(rule, dict):
                return False, "BUY_RESTRICTIONS_INVALID"

            restricted = str(rule.get("symbol", "")).strip().upper()
            if side == "BUY" and restricted == symbol:
                return False, "SYMBOL_AUTOMATIC_BUY_RESTRICTED"

        for rule in sell_rules:
            if not isinstance(rule, dict):
                return False, "SELL_PROTECTIONS_INVALID"

            protected = str(rule.get("symbol", "")).strip().upper()

            if side == "SELL" and protected == symbol:
                if rule.get("mode") == "all":
                    return False, "POSITION_FULLY_SELL_PROTECTED"

                # Partial protection requires verified holdings,
                # pending SELL reservations and order quantity.
                return False, "PARTIAL_PROTECTION_VERIFICATION_REQUIRED"

    except (ValueError, TypeError, KeyError, InvalidOperation):
        return False, "SAVED_AUTOMATIC_POLICY_INVALID"
    except Exception:
        return False, "SAVED_AUTOMATIC_POLICY_UNAVAILABLE"

    # Saved configuration is not proof of funding, ownership,
    # available sellable quantity or successful reconciliation.
    return False, "AUTOMATIC_ENFORCEMENT_NOT_IMPLEMENTED"
