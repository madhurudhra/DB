import json
import os
import smtplib
import ssl
import socket
import sqlite3
import subprocess
import time
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "supervision"
DB = STATE / "alerts.db"
HEARTBEAT = STATE / "heartbeat.json"


def load_environment():
    values = json.loads((STATE / "credentials.json").read_text())
    os.environ.update(values)


def connection():
    db = sqlite3.connect(DB, timeout=10)
    db.execute("PRAGMA synchronous=FULL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY,
            created REAL NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            dedup TEXT,
            sent INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_try REAL NOT NULL DEFAULT 0
        )
    """)
    db.commit()
    return db


def enqueue(subject, body, dedup=None, interval=1800):
    db = None
    try:
        db = connection()
        with db:
            if dedup and db.execute(
                "SELECT 1 FROM alerts "
                "WHERE dedup=? AND created>? LIMIT 1",
                (dedup, time.time() - interval),
            ).fetchone():
                return True

            db.execute(
                "INSERT INTO alerts(created,subject,body,dedup) "
                "VALUES(?,?,?,?)",
                (time.time(), subject, body, dedup),
            )

        print("ALERT DURABLY QUEUED:", subject, flush=True)
        return True
    except Exception as exc:
        print(
            "ALERT QUEUE FAILED:", type(exc).__name__, flush=True
        )
        return False
    finally:
        if db is not None:
            db.close()


def notify(message):
    address = os.getenv("NOTIFY_SOCKET")
    if not address:
        return

    if address.startswith("@"):
        address = "\0" + address[1:]

    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        sock.connect(address)
        sock.sendall(message.encode())


def beat(issues=None):
    temporary = STATE / f"heartbeat.{os.getpid()}.tmp"
    temporary.write_text(json.dumps({
        "time": time.time(),
        "pid": os.getpid(),
        "issues": list(issues or []),
    }))
    temporary.replace(HEARTBEAT)
    notify("WATCHDOG=1")


def deliver(row):
    msg = EmailMessage()
    msg["Subject"] = "[CryptoBot] " + row[2]
    msg["From"] = os.environ["BOT_MAIL_FROM"]
    msg["To"] = os.environ["BOT_MAIL_TO"]
    msg.set_content(
        row[3]
        + "\n\nQueued UTC epoch: " + str(row[1])
        + "\nHost: " + socket.gethostname()
    )

    with smtplib.SMTP(
        os.environ["BOT_SMTP_HOST"],
        int(os.environ["BOT_SMTP_PORT"]),
        timeout=15,
    ) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(
            os.environ["BOT_SMTP_USER"],
            os.environ["BOT_SMTP_PASS"],
        )
        refused = server.send_message(msg)
        if refused:
            raise RuntimeError("SMTP recipient refusal")


def health():
    child_environment = dict(os.environ)
    for name in ("NOTIFY_SOCKET", "WATCHDOG_PID", "WATCHDOG_USEC"):
        child_environment.pop(name, None)
    result = subprocess.run(
        ["systemctl", "is-active", "cryptobot-exits.service"],
        capture_output=True,
        text=True,
        timeout=5,
        env=child_environment,
    )
    status = result.stdout.strip()

    if status != "active":
        return (
            "SERVICE_NOT_ACTIVE",
            "Controller state: " + status,
        )

    try:
        data = json.loads(HEARTBEAT.read_text())
        age = time.time() - data["time"]
    except (OSError, ValueError, KeyError):
        return (
            "NO_HEARTBEAT",
            "No completed-cycle heartbeat available.",
        )

    if age > 600:
        return (
            "STALE_HEARTBEAT",
            f"Last completed cycle was {age:.0f}s ago.",
        )

    live_pid = subprocess.run(
        ["systemctl", "show", "cryptobot-exits.service",
         "--property=MainPID", "--value"],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
        env=child_environment,
    ).stdout.strip()
    if str(data.get("pid")) != live_pid or age < -5:
        return (
            "INVALID_HEARTBEAT",
            "Heartbeat does not match the current controller or clock.",
        )
    if data.get("issues"):
        return (
            "EXIT_PROTECTION_DEGRADED",
            "Completed cycle reported: " + "; ".join(data["issues"]),
        )
    import cryptobot_reliability
    return cryptobot_reliability.health_from_heartbeat(data)


def worker():
    os.umask(0o077)
    load_environment()
    previous = None

    while True:
        try:
            try:
                current, detail = health()
            except Exception as exc:
                current = "HEALTH_CHECK_FAILED"
                detail = "Health inspection failed: " + type(exc).__name__

            if current != "HEALTHY":
                enqueue(
                    "CRITICAL: controller health",
                    detail
                    + "\nExit protection is not confirmed healthy.",
                    dedup="health-" + current,
                )
            elif previous and previous != "HEALTHY":
                enqueue(
                    "Controller recovered",
                    detail
                    + "\nInspect the controller startup log for its trading mode.",
                    dedup="health-recovered",
                    interval=60,
                )

            previous = current

            db = connection()
            try:
                rows = db.execute(
                    "SELECT id,created,subject,body,attempts "
                    "FROM alerts WHERE sent=0 AND next_try<=? "
                    "ORDER BY id LIMIT 5",
                    (time.time(),),
                ).fetchall()
            finally:
                db.close()

            for row in rows:
                try:
                    deliver(row)
                    db = connection()
                    try:
                        with db:
                            db.execute(
                                "UPDATE alerts SET sent=1 WHERE id=?",
                                (row[0],),
                            )
                    finally:
                        db.close()

                    print("SMTP ACCEPTED:", row[2], flush=True)

                except Exception as exc:
                    attempts = row[4] + 1
                    delay = min(
                        3600, 30 * 2 ** min(attempts, 7)
                    )

                    db = connection()
                    try:
                        with db:
                            db.execute(
                                "UPDATE alerts "
                                "SET attempts=?,next_try=? WHERE id=?",
                                (
                                    attempts,
                                    time.time() + delay,
                                    row[0],
                                ),
                            )
                    finally:
                        db.close()

                    print(
                        "SMTP RETRY SCHEDULED:",
                        type(exc).__name__,
                        "seconds",
                        delay,
                        flush=True,
                    )

            db = connection()
            try:
                with db:
                    db.execute(
                        "DELETE FROM alerts "
                        "WHERE sent=1 AND created<?",
                        (time.time() - 90 * 86400,),
                    )
            finally:
                db.close()
            notify("WATCHDOG=1")

        except Exception as exc:
            print(
                "ALERT WORKER ERROR:",
                type(exc).__name__,
                flush=True,
            )

        time.sleep(30)


if __name__ == "__main__":
    worker()
