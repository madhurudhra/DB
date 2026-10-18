import json
import os
import time

SCAN_STALL_SECONDS = 900
RECONCILE_STALE_SECONDS = 180
EXIT_CHECK_STALE_SECONDS = 180
CURRENT_ENGINE = None


def _now():
    return time.time()


def initial_progress():
    return {
        "last_reconcile": 0.0,
        "last_exit_check": 0.0,
        "scan_started": 0.0,
        "scan_completed": 0.0,
        "scan_error": None,
    }


def evaluate_progress(progress, now=None, has_positions=False):
    now = _now() if now is None else float(now)
    problems = []

    reconcile = float(progress.get("last_reconcile") or 0)
    if reconcile <= 0 or now - reconcile > RECONCILE_STALE_SECONDS:
        problems.append("broker reconciliation is stale")

    if has_positions:
        exit_check = float(progress.get("last_exit_check") or 0)
        if exit_check <= 0 or now - exit_check > EXIT_CHECK_STALE_SECONDS:
            problems.append("exit monitoring is stale")

    started = float(progress.get("scan_started") or 0)
    completed = float(progress.get("scan_completed") or 0)
    if started > completed:
        age = now - started
        if age > SCAN_STALL_SECONDS:
            problems.append(f"entry scan stalled for {age:.0f}s")

    if progress.get("scan_error"):
        problems.append("latest entry scan failed")

    return problems


def install(namespace, supervision):
    engine_class = namespace["Engine"]
    original_init = engine_class.__init__
    original_reconcile = engine_class.reconcile
    original_monitor = engine_class.monitor_exits
    original_scan_worker = engine_class.scan_worker
    original_cycle = engine_class.cycle

    def patched_init(self, *args, **kwargs):
        global CURRENT_ENGINE
        original_init(self, *args, **kwargs)
        self.reliability_progress = initial_progress()
        self.reliability_positions_present = False
        CURRENT_ENGINE = self

    def patched_reconcile(self, *args, **kwargs):
        result = original_reconcile(self, *args, **kwargs)
        self.reliability_progress["last_reconcile"] = _now()
        return result

    def patched_monitor(self, positions, *args, **kwargs):
        result = original_monitor(self, positions, *args, **kwargs)
        self.reliability_positions_present = bool(positions)
        self.reliability_progress["last_exit_check"] = _now()
        return result

    def patched_scan_worker(self, *args, **kwargs):
        self.reliability_progress["scan_started"] = _now()
        self.reliability_progress["scan_error"] = None
        try:
            return original_scan_worker(self, *args, **kwargs)
        except Exception as exc:
            self.reliability_progress["scan_error"] = (
                type(exc).__name__ + ": " + str(exc)
            )[:1000]
            raise
        finally:
            self.reliability_progress["scan_completed"] = _now()

    def patched_cycle(self, *args, **kwargs):
        result = original_cycle(self, *args, **kwargs)
        problems = evaluate_progress(
            self.reliability_progress,
            has_positions=self.reliability_positions_present,
        )
        if problems:
            supervision.enqueue(
                "CRITICAL: controller progress degraded",
                "\n".join(problems)
                + "\nNew orders remain subject to normal reconciliation safeguards.",
                dedup="progress-" + "|".join(problems),
                interval=300,
            )

        started = self.reliability_progress["scan_started"]
        completed = self.reliability_progress["scan_completed"]
        if started > completed and _now() - started > SCAN_STALL_SECONDS:
            raise RuntimeError("Background entry scan exceeded 15 minutes")

        return result

    engine_class.__init__ = patched_init
    engine_class.reconcile = patched_reconcile
    engine_class.monitor_exits = patched_monitor
    engine_class.scan_worker = patched_scan_worker
    engine_class.cycle = patched_cycle


def heartbeat_payload(engine, issues):
    return {
        "time": _now(),
        "pid": os.getpid(),
        "issues": list(issues or []),
        "progress": dict(
            getattr(engine, "reliability_progress", initial_progress())
        ),
        "has_positions": bool(
            getattr(engine, "reliability_positions_present", False)
        ),
    }


def health_from_heartbeat(data, now=None):
    now = _now() if now is None else float(now)
    progress = data.get("progress")
    if not isinstance(progress, dict):
        return ("PROGRESS_MISSING", "Heartbeat lacks progress telemetry.")

    problems = evaluate_progress(
        progress,
        now=now,
        has_positions=bool(data.get("has_positions")),
    )
    if problems:
        return (
            "PROGRESS_DEGRADED",
            "; ".join(problems),
        )
    return (
        "HEALTHY",
        "Controller cycle, reconciliation, exits, and entry scan are fresh.",
    )
