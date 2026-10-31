"""Equity entry coordinator; no activity at import.

Required adapter contract:
- snapshot(order): returns verified inputs for entry_policy_v2.evaluate().
- review(order): raises unless broker review passes and unattended execution
  is permitted. It must not submit an order.
- submit(order, ref_id): submits exactly once and returns the broker order ID.
- budget(order): returns verified capital available before this ledger's
  reservations, excluding deployed capital and other commitments.

All adapter callbacks are async.
The order quantity is a share quantity, not a dollar amount.
maximum_cost must conservatively include price limits and fees.
This coordinator does not support unbounded market-order cost estimates.

No automatic recovery, cancellation, reservation release, or fill adoption.
Do not connect live entries until exit coverage and reconciliation exist.
"""
import fcntl
from pathlib import Path

import entry_policy_v2 as policy
import entry_ledger_v2 as ledger

LOCK = Path("/var/lib/si-equity-stop-guard/entry-coordinator-v2.lock")


def blocked(reason, **details):
    return {
        "state": "BLOCKED",
        "reason": reason,
        "orders_submitted": False,
        **details,
    }


def verify_binding(snapshot, order):
    if not isinstance(snapshot, dict):
        raise RuntimeError("INVALID_ADAPTER_SNAPSHOT")
    if (
        snapshot.get("symbol") != order["symbol"]
        or snapshot.get("account") != order["account"]
        or snapshot.get("desired_revision") != order["revision"]
    ):
        raise RuntimeError("SNAPSHOT_ORDER_BINDING_FAILED")
    if (
        policy.number(snapshot.get("maximum_order_cost_with_fees"))
        != policy.number(order["maximum_cost"])
    ):
        raise RuntimeError("SNAPSHOT_COST_BINDING_FAILED")


async def attempt(
    request_id, order, *, snapshot, review, submit, budget
):
    """Database must be explicitly initialized during a future deployment.

    A RESERVED row from a prior attempt is not automatically reused.
    This is deliberately conservative until recovery is implemented.
    """
    # SI_ENTRY_COORDINATOR_DISABLED_PENDING_INTEGRATION
    return blocked(
        "EXECUTION_DISABLED_PENDING_COMPLETE_INTEGRATION"
    )

    with LOCK.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return blocked("ANOTHER_ENTRY_ATTEMPT_IS_RUNNING")

        # Take a copy so callbacks cannot alter the order being authorized.
        original = dict(order)

        first = await snapshot(dict(original))
        verify_binding(first, original)
        decision = policy.evaluate(first)
        if not decision["entry_authorized"]:
            return blocked(
                "POLICY_REJECTED", blockers=decision["blockers"]
            )

        await review(dict(original))

        # Refresh risk, permissions, and capital after broker review.
        current = await snapshot(dict(original))
        verify_binding(current, original)
        decision = policy.evaluate(current)
        if not decision["entry_authorized"]:
            return blocked(
                "POST_REVIEW_POLICY_REJECTED",
                blockers=decision["blockers"],
            )

        available = await budget(dict(original))
        reservation = ledger.reserve(
            request_id, dict(original), available
        )
        if not reservation["new_reservation"]:
            return blocked(
                "EXISTING_INTENT_REQUIRES_RECONCILIATION",
                request_id=reservation["request_id"],
                intent_state=reservation["state"],
            )

        # If this check fails, retain the reservation for reconciliation.
        final = await snapshot(dict(original))
        verify_binding(final, original)
        decision = policy.evaluate(final)
        if not decision["entry_authorized"]:
            return blocked(
                "RESERVED_BUT_FINAL_POLICY_REJECTED",
                reservation_retained=True,
                blockers=decision["blockers"],
            )

        reference = reservation["request_id"]
        ledger.mark_submitting(reference)

        try:
            broker_id = await submit(dict(original), reference)
            ledger.record_broker_id(reference, broker_id)
        except BaseException:
            # Includes cancellation. Submission may have reached the broker.
            # Leave SUBMITTING in the database; never automatically resend.
            raise RuntimeError(
                "SUBMISSION_OUTCOME_UNRESOLVED_DO_NOT_RESUBMIT:"
                + reference
            ) from None

        return {
            "state": "BROKER_ACKNOWLEDGED_NOT_FILL_VERIFIED",
            "request_id": reference,
            "broker_id": broker_id,
            "submission_attempted": True,
            "fill_verified": False,
            "reservation_retained": True,
        }
