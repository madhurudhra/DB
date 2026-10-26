
import ast
import hashlib
import json
import os
import signal
import sys
from pathlib import Path

import cryptobot_supervision as supervision

ROOT = Path(__file__).resolve().parent
EXPECTED = (
    "547827dfb10102e3dae2d54552ea92c69b9e2e7ccc8ffe982b04854b5eaa3708"
)


def interrupt(signum, frame):
    raise KeyboardInterrupt


def launch():
    supervision.load_environment()
    os.chdir(ROOT)
    os.umask(0o077)

    path = ROOT / "equal_allocation_bot.py"
    raw = path.read_bytes()

    if hashlib.sha256(raw).hexdigest() != EXPECTED:
        supervision.enqueue(
            "CRITICAL: controller integrity mismatch",
            "Supervised launch blocked. Inspect installed controller.",
            dedup="integrity",
        )
        raise SystemExit(78)

    client_path = ROOT / "robinhood_client.py"
    if hashlib.sha256(client_path.read_bytes()).hexdigest() != "67119c81c7bbabbabf303e1aa09e93f05bf6b96e1d5e0c0bafa9460c5347492d":
        supervision.enqueue("CRITICAL: client integrity mismatch", "Launch blocked; inspect client.", dedup="client-integrity")
        raise SystemExit(78)
    guard_path = Path("/home/ubuntu/bot-capital/capital_guard.py")
    if hashlib.sha256(guard_path.read_bytes()).hexdigest() != "9fdd7325dba3091e94f18b39e914680647b57348dbc23c5460e270976e115274":
        supervision.enqueue("CRITICAL: capital guard integrity mismatch", "Launch blocked; inspect capital guard.", dedup="capital-integrity")
        raise SystemExit(78)
    ast.parse(raw.decode())

    namespace = {
        "__name__": "cryptobot_supervised_module",
        "__file__": str(path),
    }
    exec(compile(raw.decode(), str(path), "exec"), namespace)

    supervision.load_environment()
    namespace["send_alert"] = supervision.enqueue
    cycle_issues = []
    original_log = namespace["log"]

    def supervised_log(*args):
        original_log(*args)
        if args and args[0] in ("EXIT MONITOR ERROR", "SUBMISSIONS BLOCKED", "EXIT RESEARCH UNAVAILABLE"):
            detail = " ".join(str(value) for value in args)
            if detail not in cycle_issues:
                cycle_issues.append(detail[:1500])

        if args and args[0] in (
            "EXIT MONITOR ERROR",
            "SUBMISSIONS BLOCKED",
            "EXIT RESEARCH UNAVAILABLE",
            "EMAIL WORKER FAILED",
        ):
            supervision.enqueue(
                "CRITICAL: monitoring requires attention",
                "Event: " + str(args[0])
                + "\nInspect journalctl. Exits may be blocked.",
                dedup="monitor-" + str(args[0]),
            )

    namespace["log"] = supervised_log

    engine_class = namespace["Engine"]
    reliability_path = ROOT / "cryptobot_reliability.py"
    if hashlib.sha256(reliability_path.read_bytes()).hexdigest() != "562b08a76fe35a6d119e1b7e944b05fe3f8bba28d4d4cb85b2dc6bfd935cd8f9":
        supervision.enqueue(
            "CRITICAL: reliability module integrity mismatch",
            "Controller launch blocked; inspect reliability module.",
            dedup="reliability-integrity",
        )
        raise SystemExit(78)
    import cryptobot_reliability
    cryptobot_reliability.install(namespace, supervision)
    original_beat = supervision.beat

    def reliability_beat(issues, *args, **kwargs):
        result = original_beat(issues, *args, **kwargs)
        try:
            engine = cryptobot_reliability.CURRENT_ENGINE
            if engine is not None:
                payload = cryptobot_reliability.heartbeat_payload(engine, issues)
                temporary = supervision.STATE / (
                    "heartbeat." + str(os.getpid()) + ".reliability.tmp"
                )
                temporary.write_text(
                    json.dumps(payload, separators=(",", ":"))
                )
                temporary.replace(supervision.HEARTBEAT)
        except Exception as exc:
            supervised_log(
                "RELIABILITY HEARTBEAT ERROR "
                + type(exc).__name__ + ": " + str(exc)
            )
        return result

    supervision.beat = reliability_beat
    original_cycle = engine_class.cycle

    def supervised_cycle(self):
        cycle_issues.clear()
        result = original_cycle(self)
        supervision.beat(cycle_issues)
        supervision.notify("STATUS=" + ("Exit protection degraded" if cycle_issues else "Completed cycle; no observed exit blockers"))
        return result

    engine_class.cycle = supervised_cycle

    signal.signal(signal.SIGTERM, interrupt)
    # SHADOW VALIDATION: no live order submissions.
    sys.argv = [str(path)]

    supervision.enqueue(
        "Supervised controller starting",
        "Mode: SHADOW VALIDATION. Live orders disabled.\n"
        "Validate signals and strategy behavior before live trading.",
        dedup="startup",
        interval=300,
    )

    supervision.notify(
        "READY=1\nSTATUS=Shadow validation; live orders disabled"
    )

    namespace["main"]()


if __name__ == "__main__":
    try:
        launch()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        supervision.enqueue(
            "Controller stopped",
            "Supervised controller interrupted.",
            dedup="interrupted",
            interval=60,
        )
        raise SystemExit(0)
    except Exception as exc:
        supervision.enqueue(
            "CRITICAL: supervised launch failed",
            "Failure type: " + type(exc).__name__
            + "\nInspect journalctl."
            + "\nNo orders blindly resubmitted.",
            dedup="launch-failed",
        )
        raise
