"""Independent saved configurations. Does not authorize broker execution."""

import json
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.trading_controls import authorize
from app.core.database import connect_database
from app.core.trading_policy_config import load_policy

router = APIRouter(
    prefix="/api/configurations",
    dependencies=[Depends(authorize)],
)
BOT_IDS = ("crypto", "equity", "zerodha")
BotId = Literal["crypto", "equity", "zerodha"]


def decimal_value(value, *, positive=False, percentage=False):
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid decimal string") from exc

    if not number.is_finite() or number < 0:
        raise ValueError("Value must be finite and nonnegative")
    if positive and number <= 0:
        raise ValueError("Quantity must be positive")
    if percentage and number >= 100:
        raise ValueError("Reserve percentage must be below 100")
    if number > Decimal("1000000000000"):
        raise ValueError("Value exceeds supported maximum")
    if number.as_tuple().exponent < -12:
        raise ValueError("At most 12 decimal places are supported")

    return number


class SellProtection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=80)
    mode: Literal["all", "quantity"]
    quantity: str | None = None

    @model_validator(mode="after")
    def validate_rule(self):
        self.symbol = self.symbol.strip().upper()
        if not self.symbol or any(c.isspace() for c in self.symbol):
            raise ValueError("Invalid symbol")
        if self.mode == "all":
            if self.quantity is not None:
                raise ValueError("All-position protection takes no quantity")
        else:
            if self.quantity is None:
                raise ValueError("Quantity is required")
            decimal_value(self.quantity, positive=True)
        return self


class BuyRestriction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=80)
    mode: Literal["block_all"] = "block_all"

    @model_validator(mode="after")
    def validate_rule(self):
        self.symbol = self.symbol.strip().upper()
        if not self.symbol or any(c.isspace() for c in self.symbol):
            raise ValueError("Invalid symbol")
        return self


class Changes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_ref: str | None = Field(default=None, min_length=1, max_length=120)
    allocated_capital: str | None = None
    cash_reserve_pct: str | None = None
    sell_protections: list[SellProtection] | None = Field(
        default=None, max_length=500
    )
    buy_restrictions: list[BuyRestriction] | None = Field(
        default=None, max_length=500
    )

    @model_validator(mode="after")
    def validate_changes(self):
        if not self.model_fields_set:
            raise ValueError("Supply at least one changed field")

        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError("Explicit null changes are not supported")

        if self.allocated_capital is not None:
            decimal_value(self.allocated_capital)
        if self.cash_reserve_pct is not None:
            decimal_value(self.cash_reserve_pct, percentage=True)

        for rules in (self.sell_protections, self.buy_restrictions):
            if rules is not None:
                symbols = [rule.symbol for rule in rules]
                if len(symbols) != len(set(symbols)):
                    raise ValueError("Duplicate symbols in a restriction list")
        return self


class BotUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot: BotId
    expected_revision: int = Field(ge=0)
    changes: Changes


class BatchUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=8, max_length=200)
    updates: list[BotUpdate] = Field(min_length=1, max_length=3)


def initialize(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS independent_bot_configurations (
            bot TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            config_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS independent_config_requests (
            request_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            response_json TEXT NOT NULL
        )
    """)

    missing = [
        bot for bot in BOT_IDS
        if db.execute(
            "SELECT 1 FROM independent_bot_configurations WHERE bot=?",
            (bot,),
        ).fetchone() is None
    ]
    if not missing:
        return

    policy = load_policy()
    for bot in missing:
        source = policy["bots"][bot]
        reserve = source.get(
            "cash_reserve_pct", policy["defaults"]["cash_reserve_pct"]
        )
        config = {
            "account_ref": None,
            "currency": source["currency"],
            "allocated_capital": source["allocated_capital"],
            "cash_reserve_pct": str(reserve),
            "sell_protections": [],
            "buy_restrictions": [],
        }
        db.execute("""
            INSERT INTO independent_bot_configurations
            (bot,revision,config_json,updated_at)
            VALUES (?,0,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        """, (bot, json.dumps(config)))


def snapshot(db, bot):
    row = db.execute(
        "SELECT * FROM independent_bot_configurations WHERE bot=?",
        (bot,),
    ).fetchone()
    config = json.loads(row["config_json"])
    allocated = config["allocated_capital"]
    reserve = None
    ceiling = None

    if allocated is not None:
        capital = decimal_value(allocated)
        rate = decimal_value(config["cash_reserve_pct"], percentage=True)
        reserve = format(capital * rate / 100, "f")
        ceiling = format(capital - Decimal(reserve), "f")

    return {
        "bot": bot,
        "revision": row["revision"],
        "updated_at": row["updated_at"],
        "configuration": config,
        "configured_reserve": reserve,
        "configured_deployment_ceiling": ceiling,
        "funded_capital": None,
        "remaining_automatic_budget": None,
        "attributable_realized_net_profit": None,
        "enforcement_connected": False,
        "runner_enforcement_verified": False,
        "broker_positions_validated": False,
        "status": "SAVED_CONFIGURATION_ONLY",
        "warning": (
            "These saved settings are not connected to automatic execution. "
            "No broker orders are placed or canceled by this API. "
            "Saving an account or symbol does not verify broker ownership."
        ),
    }


@router.get("")
def read_configurations(
    bots: list[BotId] | None = Query(default=None),
):
    selected = list(BOT_IDS) if bots is None else bots
    if not selected or len(selected) != len(set(selected)):
        raise HTTPException(422, "Select unique bot IDs")

    with connect_database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        return {
            "selected_bots": selected,
            "configurations": [snapshot(db, bot) for bot in selected],
        }


@router.patch("")
def update_configurations(request: BatchUpdate):
    selected = [item.bot for item in request.updates]
    if len(selected) != len(set(selected)):
        raise HTTPException(422, "A bot may appear only once per request")

    payload = json.dumps(
        request.model_dump(), sort_keys=True, separators=(",", ":")
    )
    with connect_database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)

        prior = db.execute(
            "SELECT payload_json,response_json "
            "FROM independent_config_requests WHERE request_id=?",
            (request.request_id,),
        ).fetchone()
        if prior:
            if prior["payload_json"] != payload:
                raise HTTPException(409, "Request ID belongs to another change")
            return json.loads(prior["response_json"])

        current = {bot: snapshot(db, bot) for bot in selected}
        conflicts = [
            {
                "bot": item.bot,
                "expected_revision": item.expected_revision,
                "actual_revision": current[item.bot]["revision"],
            }
            for item in request.updates
            if item.expected_revision != current[item.bot]["revision"]
        ]
        if conflicts:
            raise HTTPException(409, {
                "message": "Configuration changed; refresh before saving",
                "conflicts": conflicts,
            })

        for item in request.updates:
            config = current[item.bot]["configuration"]
            changes = item.changes.model_dump(exclude_unset=True)

            changing_account = (
                "account_ref" in changes
                and changes["account_ref"] != config["account_ref"]
            )
            has_rules = (
                config["sell_protections"] or config["buy_restrictions"]
            )
            if changing_account and has_rules:
                raise HTTPException(
                    409,
                    "Remove existing restrictions before changing accounts",
                )

            config.update(changes)
            if (
                config["sell_protections"] or config["buy_restrictions"]
            ) and not config["account_ref"]:
                raise HTTPException(
                    422, "Restrictions require an explicit account_ref"
                )

            db.execute("""
                UPDATE independent_bot_configurations
                SET revision=revision+1,config_json=?,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE bot=?
            """, (json.dumps(config), item.bot))

            db.execute("""
                INSERT INTO audit_events
                (event_type,bot_name,correlation_id,details_json)
                VALUES (?,?,?,?)
            """, (
                "INDEPENDENT_BOT_CONFIGURATION_SAVED",
                item.bot,
                request.request_id,
                json.dumps({
                    "changes": changes,
                    "revision": current[item.bot]["revision"] + 1,
                    "enforced": False,
                }),
            ))

        result = {
            "request_id": request.request_id,
            "saved": True,
            "enforced": False,
            "configurations": [snapshot(db, bot) for bot in selected],
        }
        db.execute(
            "INSERT INTO independent_config_requests "
            "(request_id,payload_json,response_json) VALUES (?,?,?)",
            (request.request_id, payload, json.dumps(result)),
        )
        return result
