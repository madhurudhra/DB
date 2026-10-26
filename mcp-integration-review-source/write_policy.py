"""Conditional write policy. No broker calls; success acquires a durable claim.
Requires the prepared-order producer/executor described in INTEGRATION.md.
Not a drop-in live activation for the legacy market-order runner.
"""
import os
from dataclasses import dataclass
@dataclass(frozen=True)
class BotPolicy:
    name:str
    broker:str
    timezone:str
    start_hour:int|None
    end_hour:int|None
    continuous:bool=False
BOT_POLICIES={
 'crypto':BotPolicy('US Crypto','robinhood_crypto','UTC',None,None,True),
 'equity':BotPolicy('US Equity','robinhood_equity','America/New_York',8,20),
 'zerodha':BotPolicy('India Zerodha','zerodha','Asia/Kolkata',8,17),
}

from datetime import datetime
from zoneinfo import ZoneInfo
from app.core.config import get_settings

def _enabled(name):return os.getenv(name,'false').strip().lower() in {'1','true','yes','on'}

def is_market_window_open(bot,now=None):
    if bot=='crypto':return True
    windows={'equity':('America/New_York',8,20),'zerodha':('Asia/Kolkata',8,17)}
    if bot not in windows:return False
    zone,start,end=windows[bot];current=now or datetime.now(ZoneInfo(zone))
    if current.tzinfo is None:return False
    current=current.astimezone(ZoneInfo(zone))
    return current.weekday()<5 and start<=current.hour<end

def can_submit_order(bot,*,risk_passed,market_calendar_passed,account_verified,
                     reconciliation_healthy,durable_order_intent_saved,
                     side=None,symbol=None,account_ref=None,intent_id=None):
    if bot not in {'crypto','equity','zerodha'}:return False,'UNKNOWN_BOT'
    if not _enabled('WRITE_ENABLED'):return False,'WRITE_DISABLED'
    if get_settings().live_trading_enabled is not True:return False,'LIVE_TRADING_DISABLED'
    # Bot runtime switch controls entries; independent SELL permission controls exits.
    if side=='BUY' and not _enabled({'crypto':'CRYPTO_BOT_ENABLED','equity':'EQUITY_BOT_ENABLED','zerodha':'ZERODHA_BOT_ENABLED'}[bot]):return False,'BOT_ENTRY_DISABLED'
    if not is_market_window_open(bot):return False,'OUTSIDE_CONFIGURED_MARKET_WINDOW'
    for passed,reason in [(risk_passed,'RISK_CHECK_FAILED'),(market_calendar_passed,'MARKET_CALENDAR_CHECK_FAILED'),(account_verified,'BROKER_ACCOUNT_NOT_VERIFIED'),(reconciliation_healthy,'RECONCILIATION_NOT_HEALTHY'),(durable_order_intent_saved,'ORDER_INTENT_NOT_PERSISTED')]:
        if passed is not True:return False,reason
    from app.core.automatic_order_guard import automatic_order_preflight
    return automatic_order_preflight(bot,side=side,symbol=symbol,account_ref=account_ref,intent_id=intent_id)
