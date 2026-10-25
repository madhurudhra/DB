"""Read-only dashboard capital endpoint. No funding/execution HTTP routes."""
from contextlib import closing
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Response
from app.api.trading_controls import authorize
from app.core.database import connect_database
from app.core.capital_ledger_v1 import snapshot
router = APIRouter(prefix='/api/dashboard/v1', dependencies=[Depends(authorize)])

@router.get('/bots/{bot}/capital')
def capital(bot: Literal['crypto', 'equity', 'zerodha'], response: Response):
    response.headers['Cache-Control'] = 'no-store, private'
    try:
        with closing(connect_database()) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            return snapshot(db, bot)
    except Exception as exc:
        raise HTTPException(503, {'status': 'CAPITAL_STATE_UNAVAILABLE', 'error_type': type(exc).__name__}) from None
