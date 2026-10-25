"""Fail-closed Robinhood equity adapter scaffold.

Live execution remains disabled until a supported equity API/authentication
contract and the application's order-journal integration are implemented.
"""
from __future__ import annotations

import os


class RobinhoodEquity:
    broker = "robinhood"
    asset_class = "equity"

    def __init__(self, *args, **kwargs):
        self.write_enabled = (
            os.getenv("ROBINHOOD_EQUITY_WRITE_ENABLED", "false").lower()
            == "true"
        )

    def health(self) -> dict:
        return {
            "broker": self.broker,
            "asset_class": self.asset_class,
            "status": "NOT_IMPLEMENTED",
            "live_write_enabled": False,
            "reason": (
                "A supported Robinhood equity API client and verified "
                "account-routing implementation are required."
            ),
        }

    def submit_order(self, payload: dict) -> dict:
        raise RuntimeError(
            "LIVE_EQUITY_SUBMISSION_BLOCKED: equity execution adapter "
            "is not implemented. No order was submitted."
        )
