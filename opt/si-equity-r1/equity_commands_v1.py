"""Equity command persistence.

Importing this module does not open a database, change permissions,
or submit orders. The dashboard and worker must explicitly call it.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
DB = Path('/var/lib/si-equity-stop-guard/dashboard-commands.sqlite3')
FIELDS = {'bot', 'auto_trade', 'buy', 'sell'}

def now():
    return datetime.now(timezone.utc).isoformat()

def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA synchronous=FULL')
    return db

def initialize():
    db = connect()
    try:
        db.executescript('\n            CREATE TABLE IF NOT EXISTS equity_commands(\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                request_id TEXT NOT NULL UNIQUE,\n                requested_at TEXT NOT NULL,\n                updates TEXT NOT NULL,\n                state TEXT NOT NULL,\n                completed_at TEXT,\n                worker_pid INTEGER,\n                result TEXT\n            );\n        ')
        db.commit()
    finally:
        db.close()

def validated_request(body):
    if not isinstance(body, dict) or set(body) != {'bot', 'updates', 'request_id'} or body['bot'] != 'equity':
        raise ValueError('Require an Equity command.')
    request_id = body['request_id']
    if not isinstance(request_id, str):
        raise ValueError('Invalid request_id.')
    UUID(request_id)
    updates = body['updates']
    if not isinstance(updates, dict) or not updates or (not set(updates) <= FIELDS) or any((type(value) is not bool for value in updates.values())):
        raise ValueError('Require Boolean control values.')
    if updates.get('buy') is True:
        raise ValueError('EQUITY_BUY_EXECUTOR_NOT_CONNECTED')
    return (request_id, json.dumps(updates, sort_keys=True, allow_nan=False))

def queue(body):
    request_id, payload = validated_request(body)
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        saved = db.execute('SELECT * FROM equity_commands WHERE request_id=?', (request_id,)).fetchone()
        if saved is not None:
            if saved['updates'] != payload:
                raise ValueError('Request ID already used for another command.')
            command_id = saved['id']
            state = saved['state']
        else:
            cursor = db.execute("INSERT INTO equity_commands(request_id,requested_at,updates,state) VALUES(?,?,?,'QUEUED')", (request_id, now(), payload))
            command_id = cursor.lastrowid
            state = 'QUEUED'
        db.commit()
        return {'bot': 'equity', 'request_id': request_id, 'command_id': command_id, 'command_state': state, 'worker_applied': state == 'APPLIED', 'order_submitted_by_this_http_request': False}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def history():
    db = connect()
    try:
        rows = db.execute('SELECT * FROM equity_commands ORDER BY id DESC LIMIT 30').fetchall()
        return [{**dict(row), 'updates': json.loads(row['updates']), 'result': json.loads(row['result']) if row['result'] is not None else None} for row in rows]
    finally:
        db.close()

def initialize_worker():
    initialize()
    db = connect()
    try:
        db.execute('\n            CREATE TABLE IF NOT EXISTS equity_control_state(\n                id INTEGER PRIMARY KEY CHECK(id=1),\n                controls TEXT NOT NULL,\n                acknowledged_at TEXT,\n                worker_pid INTEGER\n            )\n        ')
        initial = {'bot': True, 'auto_trade': True, 'buy': False, 'sell': True}
        db.execute('INSERT OR IGNORE INTO equity_control_state(id,controls) VALUES(1,?)', (json.dumps(initial, sort_keys=True),))
        db.commit()
    finally:
        db.close()

def apply_pending():
    import os
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT controls FROM equity_control_state WHERE id=1').fetchone()
        if row is None:
            raise RuntimeError('EQUITY_CONTROL_STATE_MISSING')
        controls = json.loads(row['controls'])
        if not isinstance(controls, dict) or set(controls) != FIELDS or any((type(v) is not bool for v in controls.values())) or (controls['buy'] is not False):
            raise RuntimeError('INVALID_EQUITY_CONTROL_STATE')
        rows = db.execute("SELECT * FROM equity_commands WHERE state='QUEUED' ORDER BY id").fetchall()
        for command in rows:
            try:
                updates = json.loads(command['updates'])
                _, payload = validated_request({'bot': 'equity', 'updates': updates, 'request_id': command['request_id']})
                controls = {**controls, **json.loads(payload)}
                state = 'APPLIED'
                result = {'controls': controls, 'scope': 'EXISTING_EQUITY_SELL_GUARD_ONLY', 'buy_executor_connected': False}
            except (ValueError, TypeError):
                state = 'REJECTED'
                result = {'error': 'INVALID_EQUITY_COMMAND'}
            db.execute('UPDATE equity_commands SET state=?,completed_at=?,worker_pid=?,result=? WHERE id=?', (state, now(), os.getpid(), json.dumps(result, sort_keys=True), command['id']))
        db.execute('UPDATE equity_control_state SET controls=?,acknowledged_at=?,worker_pid=? WHERE id=1', (json.dumps(controls, sort_keys=True), now(), os.getpid()))
        db.commit()
        return controls
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def require_sell_permission():
    controls = apply_pending()
    if not (controls['bot'] and controls['auto_trade'] and controls['sell']):
        raise RuntimeError('EQUITY_SELL_BLOCKED_BY_WORKER_CONTROLS')

def snapshot():
    if not DB.is_file():
        return {'bot': 'equity', 'worker_report_fresh': False, 'controls': None, 'scope': 'EXISTING_EQUITY_SELL_GUARD_ONLY', 'buy_executor_connected': False, 'reason': 'WORKER_HAS_NOT_INITIALIZED_COMMAND_STORE', 'commands': []}
    db = sqlite3.connect('file:' + str(DB) + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        row = db.execute('SELECT * FROM equity_control_state WHERE id=1').fetchone()
        commands = db.execute('SELECT * FROM equity_commands ORDER BY id DESC LIMIT 15').fetchall()
    finally:
        db.close()
    age = None
    if row and row['acknowledged_at']:
        stamp = datetime.fromisoformat(row['acknowledged_at'])
        if stamp.tzinfo is not None:
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
    controls = json.loads(row['controls']) if row else None
    valid = isinstance(controls, dict) and set(controls) == FIELDS and all((type(v) is bool for v in controls.values())) and (controls['buy'] is False)
    return {'bot': 'equity', 'worker_report_fresh': valid and age is not None and (0 <= age <= 210), 'report_age_seconds': age, 'reported_worker_pid': row['worker_pid'] if row else None, 'process_liveness_verified': False, 'controls': controls if valid else None, 'scope': 'EXISTING_EQUITY_SELL_GUARD_ONLY', 'buy_executor_connected': False, 'settings_application_connected': False, 'commands': [{**dict(command), 'updates': json.loads(command['updates']), 'result': json.loads(command['result']) if command['result'] else None} for command in commands]}
