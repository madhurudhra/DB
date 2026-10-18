from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from decimal import Decimal, InvalidOperation

from kiteconnect import KiteConnect


STATE_DIR = Path("/opt/zerodha-bot/state")
STATE_FILE = STATE_DIR / "live_reconciliation_latest.json"
ENV_FILE = Path("/opt/zerodha-bot/.env")


def load_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1]:
            if value[0] in {"'", '"'}:
                value = value[1:-1]

        os.environ.setdefault(key, value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): _json_safe(v)
            for k, v in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [
            _json_safe(v)
            for v in value
        ]

    try:
        import decimal

        if isinstance(value, decimal.Decimal):
            return str(value)
    except Exception:
        pass

    return value


def _net_positions(raw: Any) -> list[dict]:
    if not isinstance(raw, dict):
        return []

    net = raw.get("net", [])

    if not isinstance(net, list):
        return []

    result = []

    for position in net:
        if not isinstance(position, dict):
            continue

        quantity = int(
            position.get("quantity", 0) or 0
        )

        overnight_quantity = int(
            position.get("overnight_quantity", 0) or 0
        )

        if quantity == 0 and overnight_quantity == 0:
            continue

        result.append(
            {
                "exchange": position.get("exchange"),
                "tradingsymbol": position.get(
                    "tradingsymbol"
                ),
                "quantity": quantity,
                "overnight_quantity": overnight_quantity,
                "average_price": str(
                    position.get(
                        "average_price",
                        "",
                    )
                ),
                "last_price": str(
                    position.get(
                        "last_price",
                        "",
                    )
                ),
                "pnl": str(
                    position.get(
                        "pnl",
                        "",
                    )
                ),
                "product": position.get("product"),
            }
        )

    return result


def _open_orders(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise RuntimeError("Invalid broker orders response")

    terminal_statuses = {"COMPLETE", "CANCELLED", "REJECTED"}

    result = []

    for order in raw:
        if not isinstance(order, dict):
            raise RuntimeError("Invalid broker order row")

        status = str(
            order.get("status", "")
        ).upper()

        if not status.strip():
            raise RuntimeError("Missing broker order status")
        if status in terminal_statuses:
            continue

        result.append(
            {
                "order_id": order.get(
                    "order_id"
                ),
                "exchange": order.get(
                    "exchange"
                ),
                "tradingsymbol": order.get(
                    "tradingsymbol"
                ),
                "transaction_type": order.get(
                    "transaction_type"
                ),
                "quantity": order.get(
                    "quantity"
                ),
                "filled_quantity": order.get(
                    "filled_quantity"
                ),
                "pending_quantity": order.get(
                    "pending_quantity"
                ),
                "price": str(
                    order.get(
                        "price",
                        "",
                    )
                ),
                "status": order.get(
                    "status"
                ),
                "product": order.get(
                    "product"
                ),
                "order_type": order.get(
                    "order_type"
                ),
            }
        )

    return result


def reconcile_live_broker() -> dict[str, Any]:
    load_env()

    mode = os.getenv(
        "TRADING_MODE",
        "PAPER",
    ).strip().upper()

    kill_switch = os.getenv(
        "KILL_SWITCH",
        "false",
    ).strip().lower()

    # Read-only broker queries do not enable order placement.
    # The kill switch continues to protect trading execution elsewhere.
    api_key = os.getenv(
        "KITE_API_KEY",
        "",
    ).strip()

    access_token = os.getenv(
        "KITE_ACCESS_TOKEN",
        "",
    ).strip()

    if not api_key or not access_token:
        raise RuntimeError(
            "Kite credentials are missing"
        )

    kite = KiteConnect(
        api_key=api_key
    )

    kite.set_access_token(
        access_token
    )

    profile = kite.profile()
    margins = kite.margins()
    orders = kite.orders()
    trades = kite.trades()
    positions = kite.positions()
    holdings = kite.holdings()

    _validate_exit_snapshot(profile, margins, orders, trades, positions, holdings)

    equity = {}

    if isinstance(margins, dict):
        equity = margins.get(
            "equity",
            {},
        )

    available = {}

    if isinstance(equity, dict):
        available = equity.get(
            "available",
            {},
        )

    live_balance = None

    if isinstance(available, dict):
        live_balance = available.get(
            "live_balance"
        )

    net_positions = _net_positions(
        positions
    )

    open_orders = _open_orders(
        orders
    )

    result = {
        "timestamp_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "mode": mode,
        "kill_switch": kill_switch,
        "read_only": True,
        "broker": "ZERODHA_KITE",
        "profile_ok": bool(profile),
        "margins_ok": bool(margins),
        "orders_ok": isinstance(
            orders,
            list,
        ),
        "trades_ok": isinstance(
            trades,
            list,
        ),
        "positions_ok": isinstance(
            positions,
            dict,
        ),
        "holdings_ok": isinstance(
            holdings,
            list,
        ),
        "broker_live_balance": (
            str(live_balance)
            if live_balance is not None
            else None
        ),
        "net_positions": net_positions,
        "open_orders": open_orders,
        "trade_count": (
            len(trades)
            if isinstance(
                trades,
                list,
            )
            else 0
        ),
        "holding_count": (
            len(holdings)
            if isinstance(
                holdings,
                list,
            )
            else 0
        ),
        "reconciled": True,
    }

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = STATE_FILE.with_suffix(
        ".tmp"
    )

    tmp.write_text(
        json.dumps(
            _json_safe(result),
            indent=2,
        ),
        encoding="utf-8",
    )

    tmp.replace(
        STATE_FILE
    )

    return result


if __name__ == "__main__":
    try:
        result = reconcile_live_broker()
        print(
            json.dumps(
                _json_safe(result),
                indent=2,
            )
        )
    except Exception as exc:
        print(
            f"LIVE RECONCILIATION FAILED: {exc}"
        )
        raise


def _validate_exit_snapshot(profile, margins, orders, trades, positions, holdings):
    """Reject incomplete snapshots; never interpret unavailable data as empty."""
    if not isinstance(profile, dict) or not profile:
        raise RuntimeError("Invalid broker profile response")
    if not isinstance(margins, dict):
        raise RuntimeError("Invalid broker margins response")
    equity = margins.get("equity")
    if not isinstance(equity, dict):
        raise RuntimeError("Invalid equity margin response")
    available = equity.get("available")
    if not isinstance(available, dict):
        raise RuntimeError("Invalid available-margin response")
    balance = available.get("live_balance")
    if isinstance(balance, bool) or balance is None:
        raise RuntimeError("Missing broker live balance")
    try:
        balance = Decimal(str(balance))
    except (InvalidOperation, ValueError, TypeError):
        raise RuntimeError("Invalid broker live balance") from None
    if not balance.is_finite() or balance < 0:
        raise RuntimeError("Invalid broker live balance")
    for label, rows in (("orders", orders), ("trades", trades), ("holdings", holdings)):
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("Invalid broker " + label + " response")
    if not isinstance(positions, dict) or not isinstance(positions.get("net"), list):
        raise RuntimeError("Invalid broker net positions response")
    for row in positions["net"]:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid broker position row")
        for field in ("quantity", "overnight_quantity"):
            value = row.get(field, 0)
            if isinstance(value, bool) or value is None:
                raise RuntimeError("Invalid position quantity")
            try:
                quantity = Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                raise RuntimeError("Invalid position quantity") from None
            if not quantity.is_finite() or quantity != quantity.to_integral_value():
                raise RuntimeError("Non-integral position quantity")
