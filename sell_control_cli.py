import json
import os
import sys
from pathlib import Path

from sell_control_store import BOTS, SellControlStore


def main():
    command = json.loads(sys.stdin.read(8193))
    if not isinstance(command, dict):
        raise ValueError("JSON object required")

    filename = os.environ.get("SELL_CONTROL_DB", "")
    if not filename or not Path(filename).is_absolute():
        raise RuntimeError("Absolute SELL_CONTROL_DB path required")

    action = command.get("action")
    fields = {
        "init": {"action"},
        "get": {"action"},
        "set": {
            "action", "bot", "enabled", "request_id", "actor", "confirm"
        },
    }
    if action not in fields or set(command) != fields[action]:
        raise ValueError("Unsupported action or fields")

    store = SellControlStore(filename)
    if action == "init":
        store.initialize()
        return {"ok": True, "initialized": True}

    if not Path(filename).is_file():
        raise RuntimeError("SELL database is not initialized")

    if action == "get":
        return {
            "ok": True,
            "bots": {bot: store.state(bot) for bot in BOTS},
        }

    result = store.request_change(
        command["bot"],
        command["enabled"],
        command["request_id"],
        command["actor"],
        command["confirm"],
    )
    return {"ok": True, **result}


if __name__ == "__main__":
    try:
        answer = main()
    except ValueError as error:
        print(json.dumps({
            "ok": False, "error": str(error), "kind": "validation"
        }))
        sys.exit(2)
    except Exception:
        print(json.dumps({
            "ok": False,
            "error": "SELL store unavailable; refresh state before retrying",
            "kind": "unavailable",
        }))
        sys.exit(1)
    print(json.dumps(answer))
