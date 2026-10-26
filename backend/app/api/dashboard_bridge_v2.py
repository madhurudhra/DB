"""Authenticated review and audit reads; never submits or cancels broker orders."""
import asyncio
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID
from contextlib import closing
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal
from app.api.trading_controls import authorize
from app.core.database import connect_database

router=APIRouter(prefix="/api/dashboard-bridge/v2",dependencies=[Depends(authorize)])

def safe(value):
    denied={"access_token","refresh_token","api_key","apikey","password","authorization","private_key","private_key_b64","secret","enctoken","public_token","request_token"}
    if isinstance(value,dict):
        result={}
        for k,v in value.items():
            key=str(k).lower()
            if key in denied:continue
            if key in {"account_number","rhs_account_number","account_ref","crypto_account_number"} and isinstance(v,str):
                result[k]="****"+v[-4:]
            else:result[k]=safe(v)
        return result
    if isinstance(value,list):return [safe(v) for v in value]
    return value

class Review(BaseModel):
    model_config=ConfigDict(extra="forbid")
    request_id: UUID
    symbol: str = Field(min_length=1,max_length=24)
    side: Literal["buy","sell"]
    size_kind: Literal["dollar_amount","quantity"]
    amount: str = Field(min_length=1,max_length=40)
    order_type: Literal["market"]
    @model_validator(mode="after")
    def check_fields(self):
        self.symbol=self.symbol.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,23}",self.symbol):raise ValueError("Invalid US equity ticker")
        try:n=Decimal(self.amount)
        except Exception:raise ValueError("Amount must be a decimal string")
        if not n.is_finite() or n<=0 or n>Decimal("1000000000000") or n.as_tuple().exponent < -12:raise ValueError("Invalid positive amount")
        self.amount=format(n,"f")
        return self

@router.post("/equity/review")
async def review_equity(request:Review):
    from rh_equity_review import call
    with closing(connect_database()) as db:
        db.row_factory=__import__('sqlite3').Row
        row=db.execute("SELECT revision,config_json FROM independent_bot_configurations WHERE bot='equity'").fetchone()
        if row is None:raise HTTPException(409,"Saved Equity configuration required")
        config=json.loads(row["config_json"]);revision=row["revision"]
    account=config.get("account_ref")
    if config.get("currency")!="USD" or not isinstance(account,str) or not account or account!=account.strip():raise HTTPException(409,"Exact saved USD Equity account required")
    async def invoke(tool,args):
        raw=await asyncio.wait_for(call(tool,args),timeout=25)
        value=json.loads(raw)
        if not isinstance(value,dict) or not isinstance(value.get("data"),dict) or value.get("error") or value.get("errors") or value["data"].get("error") or value["data"].get("errors"):raise ValueError("Broker read/review failed")
        return value
    try:
        response=await invoke("get_accounts",{})
        accounts=response["data"].get("accounts")
        if not isinstance(accounts,list):raise ValueError("Account list missing")
        matches=[a for a in accounts if isinstance(a,dict) and a.get("account_number")==account and a.get("agentic_allowed") is True]
        if len(matches)!=1:raise HTTPException(409,"Configured Equity account is not uniquely verified")
        args={"account_number":account,"symbol":request.symbol,"side":request.side,"type":request.order_type,request.size_kind:request.amount}
        result=await invoke("review_equity_order",args)
    except HTTPException:raise
    except Exception as exc:raise HTTPException(502,{"status":"BROKER_REVIEW_FAILED","error_type":type(exc).__name__}) from None
    # Confirm the operator has not changed account/configuration while the review ran.
    with closing(connect_database()) as db:
        db.row_factory=__import__('sqlite3').Row
        db.execute("BEGIN IMMEDIATE")
        current=db.execute("SELECT revision,config_json FROM independent_bot_configurations WHERE bot='equity'").fetchone()
        if current is None or current['revision']!=revision or json.loads(current['config_json']).get('account_ref')!=account:
            db.rollback();raise HTTPException(409,"Configuration changed during review; refresh and review again")
        details={"symbol":request.symbol,"side":request.side,"order_type":request.order_type,"size_kind":request.size_kind,"amount":request.amount,"account_ending":account[-4:],"configuration_revision":revision,"order_submitted":False}
        db.execute("INSERT INTO audit_events (event_type,bot_name,correlation_id,details_json) VALUES ('DASHBOARD_EQUITY_REVIEW','equity',?,?)",(str(request.request_id),json.dumps(details)))
        db.commit()
    return {"status":"REVIEW_ONLY","request_id":str(request.request_id),"reviewed_at":datetime.now(timezone.utc).isoformat(),"parameters":details,"broker_review":safe(result),"order_submitted":False,"submission_available":False,"warning":"Broker review is not order acceptance or execution. This endpoint cannot submit orders."}

@router.get("/events")
def events():
    with closing(connect_database()) as db:
        db.row_factory=__import__('sqlite3').Row
        rows=db.execute("SELECT id,event_type,bot_name,created_at,correlation_id,details_json FROM audit_events WHERE event_type IN ('DASHBOARD_EQUITY_REVIEW','INDEPENDENT_BOT_CONFIGURATION_SAVED','DASHBOARD_CONTROL_REQUEST_SAVED','EQUITY_READ_IMPORT') ORDER BY id DESC LIMIT 100").fetchall()
    result=[]
    for row in rows:
        item=dict(row)
        try:item['details']=safe(json.loads(item.pop('details_json')))
        except Exception:item.pop('details_json',None);item['details']={"status":"DETAILS_UNAVAILABLE"}
        result.append(item)
    return {"status":"AUDIT_ROWS_RECEIVED","coverage":"LATEST_100_SUPPORTED_EVENTS_NOT_FULL_HISTORY","events":result}

