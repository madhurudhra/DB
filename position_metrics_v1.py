"""Read-only valuation. Never funds a ledger or submits broker orders."""
import asyncio,json,threading,time
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
from typing import Literal
from fastapi import APIRouter,Depends,Response
from app.api.trading_controls import authorize
from app.api.dashboard_data_v1 import bot_snapshot
router=APIRouter(prefix='/api/valuation/v1',dependencies=[Depends(authorize)])
_cache={};_locks={b:threading.Lock() for b in ('equity','crypto','zerodha')}

def num(value,signed=False):
    if value is None or isinstance(value,bool):return None
    try:v=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError):return None
    if not v.is_finite() or (not signed and v<0):return None
    return v

def text(value):return str(value) if value is not None else None

def rows(value,keys=()):
    if isinstance(value,list):return value
    if not isinstance(value,dict):return None
    for key in keys:
        if isinstance(value.get(key),list):return value[key]
    if isinstance(value.get('results'),list):return value['results']
    return None

def part(snapshot,name):
    p=snapshot.get('broker',{}).get('parts',{}).get(name,{})
    return p.get('data') if p.get('status')=='BROKER_RESPONSE_RECEIVED' else None

def data(value):return value.get('data',value) if isinstance(value,dict) else value

def total(items,key):
    values=[num(i.get(key),signed=True) for i in items]
    known=[v for v in values if v is not None]
    complete=bool(items) and len(known)==len(items)
    return {'value':text(sum(known,Decimal(0))) if complete else None,
            'known_subtotal':text(sum(known,Decimal(0))) if known else None,
            'known_rows':len(known),'total_rows':len(items),'complete':complete}

def quote_rows(response):
    values=rows(data(response),('quotes',))
    result={}
    if values is None:return result
    for value in values:
        if not isinstance(value,dict):continue
        q=value.get('quote',value)
        if not isinstance(q,dict):continue
        symbol=q.get('symbol');price=num(q.get('last_trade_price'))
        if isinstance(symbol,str) and price is not None and price>0:
            result[symbol.upper()]={'price':text(price),'kind':'Broker last trade','as_of':q.get('updated_at') or q.get('last_trade_at')}
    return result

def verified_lot_basis(response,quantity):
    lots=rows(data(response),('tax_lots','lots'))
    if not lots:return None
    qty=Decimal(0);basis=Decimal(0)
    for lot in lots:
        if not isinstance(lot,dict):return None
        q=num(lot.get('quantity'))
        if q is None:return None
        # Only unambiguous total/per-share cost-basis fields. Do not guess a generic cost_basis unit.
        b=num(lot.get('total_cost_basis'))
        if b is None:
            unit=num(lot.get('cost_basis_per_share'))
            if unit is not None:b=unit*q
        if b is None:return None
        qty+=q;basis+=b
    return basis if qty==quantity else None

def position_metric(bot,p,quote=None,basis=None):
    symbol=p.get('symbol') or p.get('asset_code') or p.get('tradingsymbol')
    exchange=p.get('exchange')
    ticker=(exchange+':'+symbol) if bot=='zerodha' and exchange and symbol else (symbol+'-USD' if bot=='crypto' and symbol and not symbol.endswith('-USD') else symbol)
    q=num(p.get('total_quantity',p.get('quantity')))
    avg=num(p.get('average_buy_price',p.get('average_price')))
    price=num((quote or {}).get('price'))
    if price is None:price=num(p.get('last_price'))
    value=q*price if q is not None and price is not None else None
    entry=q*avg if q is not None and avg is not None else None
    gross=value-entry if value is not None and entry is not None else None
    net=value-basis if value is not None and basis is not None else None
    return {'ticker':ticker or 'UNKNOWN','quantity':text(q),'sell_available':p.get('quantity_available_for_trading',p.get('shares_available_for_sells')),
        'average_entry':text(avg),'current_price':text(price),'price_kind':(quote or {}).get('kind','Broker holding last price'),
        'price_as_of':(quote or {}).get('as_of'),'position_value':text(value),'entry_value_before_fee_verification':text(entry),
        'gross_price_pnl':text(gross),'net_pnl':text(net),'net_pnl_per_unit':text(net/q) if net is not None and q is not None and q>0 else None,
        'net_status':'BROKER_LOT_BASIS_MATCHED_EXIT_FEES_NOT_INCLUDED' if net is not None else 'VERIFIED_FEE_INCLUSIVE_BASIS_REQUIRED',
        'verified_open_basis':text(basis)}

def history_metric(bot,o,pnl_by_order):
    q=num(o.get('filled_asset_quantity',o.get('cumulative_quantity',o.get('filled_quantity'))))
    if q is None or q<=0:return None
    price=num(o.get('average_price'));side=str(o.get('side',o.get('transaction_type',''))).upper()
    symbol=o.get('symbol') or o.get('tradingsymbol') or 'UNKNOWN'
    if bot=='zerodha' and o.get('exchange'):symbol=o['exchange']+':'+symbol
    broker_id=o.get('id',o.get('order_id'));r=pnl_by_order.get(str(broker_id),{})
    closed_qty=num(r.get('quantity',r.get('closed_quantity')))
    net=None
    # A realized record must identify the same broker order and full displayed filled quantity.
    if side=='SELL' and closed_qty==q:
        for key in ('net_realized_pnl','net_pnl','net_profit_loss'):
            net=num(r.get(key),signed=True)
            if net is not None:break
    return {'ticker':symbol,'side':side,'quantity':text(q),'execution_price':text(price),
        'executed_value':text(q*price) if price is not None else None,
        'actual_fee':text(num(o.get('fee_charged',o.get('fees')))),
        'net_pnl':text(net),'net_pnl_per_unit':text(net/q) if net is not None else None,
        'net_status':'BROKER_NET_RECORD_MATCHED' if net is not None else ('NOT_REALIZED_BUY' if side=='BUY' else 'MATCHED_NET_RECORD_REQUIRED'),
        'created_at':o.get('created_at',o.get('order_timestamp')),'broker_state':o.get('state',o.get('status'))}

async def mcp_read(name,args):
    allowed={'get_equity_quotes','get_equity_tax_lots','get_accounts','get_pnl_trade_history'}
    if name not in allowed:raise ValueError('Read tool not allowed')
    from rh_equity_review import call
    value=json.loads(await asyncio.wait_for(call(name,args),timeout=20))
    if not isinstance(value,dict) or value.get('error') or value.get('errors') or (isinstance(value.get('data'),dict) and (value['data'].get('error') or value['data'].get('errors'))):raise ValueError('Broker read failed')
    return value

async def equity_extras(snapshot,positions,symbols):
    quotes={};bases={};history={};notes=[]
    deadline=time.monotonic()+45
    async def limited(name,args):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('Valuation read budget exhausted')
        return await asyncio.wait_for(mcp_read(name,args),timeout=min(20,remaining))
    for offset in range(0,min(len(symbols),60),20):
        try:quotes.update(quote_rows(await limited('get_equity_quotes',{'symbols':symbols[offset:offset+20]})))
        except Exception as exc:notes.append('Equity quotes unavailable: '+type(exc).__name__)
    config=snapshot.get('saved_state',{}).get('configuration') or {};account=config.get('account_ref')
    if not isinstance(account,str) or not account:return quotes,bases,history,notes+['Saved Equity account required for basis/history reads']
    try:
        accts=data(await limited('get_accounts',{})).get('accounts',[])
        matches=[a for a in accts if a.get('account_number')==account]
        if len(matches)!=1:raise ValueError('Account selection mismatch')
        selected=matches[0]
    except Exception as exc:return quotes,bases,history,notes+['Account verification unavailable: '+type(exc).__name__]
    for position in positions[:10]:
        symbol=position.get('symbol');quantity=num(position.get('quantity'))
        if not symbol or quantity is None:continue
        try:
            lots=await limited('get_equity_tax_lots',{'account_number':account,'symbol':symbol})
            bases[symbol]=verified_lot_basis(lots,quantity)
            if bases[symbol] is None:notes.append(symbol+': unambiguous complete cost-basis fields not confirmed')
        except Exception as exc:notes.append(symbol+': basis unavailable '+type(exc).__name__)
    # P&L account mapping is read from the already selected account; never substitute another account.
    rhs=selected.get('rhs_account_number')
    if isinstance(rhs,str) and rhs:
        try:
            response=await limited('get_pnl_trade_history',{'account_number':rhs,'span':'3month'})
            values=rows(data(response),('trades','trade_history','history'))
            if values is None:notes.append('Historical P&L response shape not recognized')
            else:
                for row in values:
                    if isinstance(row,dict) and row.get('order_id'):
                        key=str(row['order_id'])
                        if key in history:history[key]={}  # multiple lot records need a verified aggregation, not overwriting
                        else:history[key]=row
            notes.append('Historical net records, if matched, are for the broker 3-month window; not lifetime totals')
        except Exception as exc:notes.append('Historical broker P&L unavailable: '+type(exc).__name__)
    else:notes.append('Selected-account RHS mapping unavailable; no guessed P&L account was used')
    if len(positions)>10:notes.append('Basis lookups capped at 10 positions; remaining net fields unavailable')
    return quotes,bases,history,notes

def build(bot,snapshot):
    positions=rows(data(part(snapshot,'holdings')),('positions',))
    orders=rows(data(part(snapshot,'orders')),('orders',))
    if positions is None:positions=[]
    if orders is None:orders=[]
    symbols=sorted({str(p.get('symbol') or p.get('asset_code') or p.get('tradingsymbol')) for p in positions if p.get('symbol') or p.get('asset_code') or p.get('tradingsymbol')}|{str(o.get('symbol') or o.get('tradingsymbol')) for o in orders if o.get('symbol') or o.get('tradingsymbol')})
    quotes={};bases={};pnl={};notes=[]
    if bot=='equity':quotes,bases,pnl,notes=asyncio.run(equity_extras(snapshot,positions,symbols))
    elif bot=='crypto':
        from app.brokers.robinhood.crypto import RobinhoodCrypto
        adapter=RobinhoodCrypto()
        try:
            deadline=time.monotonic()+30
            for symbol in symbols[:20]:
                if time.monotonic()>=deadline:
                    notes.append('Crypto quote time budget reached; remaining quotes unavailable');break
                ticker=symbol if symbol.endswith('-USD') else symbol+'-USD'
                try:
                    value=adapter.quote(ticker);values=value.get('results') if isinstance(value,dict) else None
                    row=values[0] if isinstance(values,list) and len(values)==1 else value
                    if not isinstance(row,dict) or row.get('symbol') not in (None,ticker):raise ValueError('Quote mismatch')
                    price=num(row.get('bid_inclusive_of_sell_spread'))
                    if price is not None and price>0:quotes[ticker]={'price':text(price),'kind':'Broker bid including sell spread','as_of':row.get('updated_at')}
                except Exception as exc:notes.append(ticker+': quote unavailable '+type(exc).__name__)
        finally:adapter.session.close()
        notes.append('Crypto net basis/history is unavailable in this adapter; gross price change is separate')
    else:
        try:
            from app.brokers.zerodha.kite import ZerodhaKite
            instruments=sorted({p['exchange']+':'+p['tradingsymbol'] for p in positions if p.get('exchange') in ('NSE','BSE') and p.get('tradingsymbol')})
            if instruments:
                raw=ZerodhaKite().quote(*instruments[:50])
                for key,value in raw.items():
                    price=num(value.get('last_price'))
                    if price is not None:quotes[key]={'price':text(price),'kind':'Zerodha last price','as_of':str(value.get('timestamp')) if value.get('timestamp') is not None else None}
        except Exception as exc:notes.append('Zerodha quote unavailable: '+type(exc).__name__)
        notes.append('Zerodha charges and matched closed-lot ledger are not supplied by holdings; net fields remain unverified')
    metric_rows=[]
    for p in positions:
        symbol=p.get('symbol') or p.get('asset_code') or p.get('tradingsymbol')
        key=(p.get('exchange','')+':'+symbol) if bot=='zerodha' and symbol else (symbol+'-USD' if bot=='crypto' and symbol and not symbol.endswith('-USD') else symbol)
        metric_rows.append(position_metric(bot,p,quotes.get(key),bases.get(symbol)))
    past=[row for o in orders if (row:=history_metric(bot,o,pnl)) is not None]
    def totals_for(ps,hs):
        sells=[r for r in hs if r['side']=='SELL']
        return {'position_value':total(ps,'position_value'),'open_net_pnl':total(ps,'net_pnl'),'open_gross_price_pnl':total(ps,'gross_price_pnl'),'executed_value':total(hs,'executed_value'),'realized_net_pnl':total(sells,'net_pnl')}
    keys=sorted({r['ticker'] for r in metric_rows+past})
    by_ticker={key:totals_for([r for r in metric_rows if r['ticker']==key],[r for r in past if r['ticker']==key]) for key in keys}
    tickers=[]
    for symbol,quote in quotes.items():tickers.append({'ticker':symbol,'price':quote['price'],'price_kind':quote['kind'],'as_of':quote.get('as_of')})
    return {'bot':bot,'currency':'INR' if bot=='zerodha' else 'USD','generated_at':datetime.now(timezone.utc).isoformat(),
        'position_snapshot_received_at':snapshot.get('broker_fetch_completed_at'),'positions':metric_rows,'past_trades':past,'tickers':tickers,
        'totals':totals_for(metric_rows,past),'by_ticker':by_ticker,
        'notes':notes+['Quotes are last available broker observations, not guaranteed executable prices.','Open net P&L excludes future exit fees. Unknown basis/fees are never zeroed.','Totals cover returned rows only; incomplete totals are labelled partial.'],
        'read_only':True,'automatic_trading_enabled_by_this_api':False}

@router.get('/{bot}')
def valuation(bot:Literal['equity','crypto','zerodha'],response:Response):
    response.headers['Cache-Control']='no-store, private'
    with _locks[bot]:
        cached=_cache.get(bot)
        if cached is None or time.monotonic()-cached[0]>=45:
            snapshot=bot_snapshot(bot,Response())
            result=build(bot,snapshot);cached=(time.monotonic(),result);_cache[bot]=cached
        return cached[1]

