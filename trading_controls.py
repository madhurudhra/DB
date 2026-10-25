"""Saved dashboard control requests. Runner enforcement is NOT connected."""
import hmac
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from app.core.database import connect_database
from app.core.trading_policy_config import load_policy, budget_summary
router = APIRouter()
security = HTTPBearer(auto_error=False)
TOKEN_FILE = Path('/etc/super-intelligence/dashboard-control.token')
BOT_IDS = ('crypto', 'equity', 'zerodha')

def authorize(credentials: HTTPAuthorizationCredentials | None=Depends(security)):
    try:
        expected = TOKEN_FILE.read_text().strip()
    except OSError:
        raise HTTPException(503, 'Control authentication is unavailable')
    if not expected or credentials is None or credentials.scheme.lower() != 'bearer' or (not hmac.compare_digest(credentials.credentials.encode(), expected.encode())):
        raise HTTPException(401, 'Valid dashboard control token required')

class Changes(BaseModel):
    model_config = ConfigDict(extra='forbid')
    buy_enabled: StrictBool | None = None
    sell_enabled: StrictBool | None = None
    bot_enabled: StrictBool | None = None

class ControlRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=8, max_length=200)
    expected_revision: int = Field(ge=0)
    bots: list[Literal['crypto', 'equity', 'zerodha']] = Field(min_length=1, max_length=3)
    changes: Changes

def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS dashboard_control_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL, state_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS dashboard_control_requests (request_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, response_json TEXT NOT NULL)')
    initial = {bot: {'buy_enabled': None, 'sell_enabled': None, 'bot_enabled': None} for bot in BOT_IDS}
    db.execute('INSERT OR IGNORE INTO dashboard_control_state (singleton,revision,state_json) VALUES (1,0,?)', (json.dumps(initial),))

def snapshot(db):
    row = db.execute('SELECT revision,state_json FROM dashboard_control_state WHERE singleton=1').fetchone()
    return {'revision': row['revision'], 'requested': json.loads(row['state_json']), 'effective': None, 'enforcement_connected': False, 'status': 'SAVED_REQUESTS_ONLY', 'warning': 'These settings do not control the current runner. No broker orders are submitted or canceled by this API.'}

@router.get('/api/trading-policy', dependencies=[Depends(authorize)])
def trading_policy():
    try:
        data = load_policy()
        return {'policy': data, 'budgets': budget_summary(data), 'runner_integrated': False}
    except Exception:
        raise HTTPException(503, 'Trading policy could not be validated')

@router.get('/api/bot-controls', dependencies=[Depends(authorize)])
def read_controls():
    with connect_database() as db:
        db.execute('BEGIN IMMEDIATE')
        initialize(db)
        return snapshot(db)

@router.patch('/api/bot-controls', dependencies=[Depends(authorize)])
def update_controls(request: ControlRequest):
    if len(set(request.bots)) != len(request.bots):
        raise HTTPException(422, 'Duplicate bots are not allowed')
    raw_changes = request.changes.model_dump(exclude_unset=True)
    if not raw_changes or any((v is None for v in raw_changes.values())):
        raise HTTPException(422, 'Supply at least one boolean control change')
    payload = json.dumps(request.model_dump(), sort_keys=True, separators=(',', ':'))
    with connect_database() as db:
        db.execute('BEGIN IMMEDIATE')
        initialize(db)
        old = db.execute('SELECT payload_json,response_json FROM dashboard_control_requests WHERE request_id=?', (request.request_id,)).fetchone()
        if old:
            if old['payload_json'] != payload:
                raise HTTPException(409, 'Request ID already belongs to different content')
            return json.loads(old['response_json'])
        current = snapshot(db)
        if current['revision'] != request.expected_revision:
            raise HTTPException(409, 'Configuration changed; fetch current revision first')
        state = current['requested']
        for bot in request.bots:
            state[bot].update(raw_changes)
        db.execute('UPDATE dashboard_control_state SET revision=revision+1,state_json=? WHERE singleton=1', (json.dumps(state),))
        result = snapshot(db)
        result['request_id'] = request.request_id
        result['saved_at'] = datetime.now(timezone.utc).isoformat()
        db.execute('INSERT INTO dashboard_control_requests (request_id,payload_json,response_json) VALUES (?,?,?)', (request.request_id, payload, json.dumps(result)))
        db.execute('INSERT INTO audit_events (event_type,correlation_id,details_json) VALUES (?,?,?)', ('DASHBOARD_CONTROL_REQUEST_SAVED', request.request_id, json.dumps({'bots': request.bots, 'changes': raw_changes, 'revision': result['revision'], 'enforced': False})))
        return result
