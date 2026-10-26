#!/usr/bin/env python3
import contextlib, io, json, os, sys, hashlib, math
from datetime import datetime, timezone
from pathlib import Path

def now(): return datetime.now(timezone.utc).isoformat()
def number(v):
    if v is None or isinstance(v,bool): return None
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError): return None
class FeedError(Exception): pass
calls=[]
def install_guard():
    import requests
    original=requests.sessions.Session.request
    def guarded(self, method, url, *args, **kw):
        if method.upper()!='GET': raise FeedError('READ_ONLY_BLOCK')
        kw['allow_redirects']=False
        kw.setdefault('timeout',(5,15))
        response=original(self,method,url,*args,**kw)
        calls.append(response.status_code)
        if response.status_code in (401,403): raise FeedError('AUTH_REQUIRED')
        if not 200<=response.status_code<300: raise FeedError('BROKER_HTTP_ERROR')
        return response
    requests.sessions.Session.request=guarded

def sum_rows(rows, field):
    values=[number(r.get(field)) for r in rows]
    return sum(values) if all(v is not None for v in values) else None

def crypto(account_id):
    base='/home/ubuntu/crypto-bot/paper-trend-500'
    os.chdir(base); sys.path.insert(0,base)
    from robinhood_client import RobinhoodCryptoClient
    c=RobinhoodCryptoClient()
    raw=c.get_accounts()
    if not isinstance(raw,dict) or not isinstance(raw.get('results'),list): raise FeedError('ACCOUNTS_FAILED')
    if raw.get('next'): raise FeedError('ACCOUNT_PAGINATION_REQUIRES_REVIEW')
    accounts=[]; selected=None
    for row in raw['results']:
        value=row.get('account_number')
        if not value: raise FeedError('ACCOUNT_NUMBER_MISSING')
        fingerprint=hashlib.sha256(str(value).encode()).hexdigest()[:16]
        accounts.append({'id':fingerprint,'type':row.get('account_type'),'status':row.get('status'),'api_tradable':row.get('is_api_tradable')})
        if fingerprint==account_id: selected=row
    if selected is None:
        return {'ok':False,'connection':'ACCOUNT_SELECTION_REQUIRED','accounts':accounts,'errors':['Choose the intended broker account; no default account was guessed.']}
    account=selected['account_number']
    holdings=c.get_holdings(account)
    if not isinstance(holdings,dict) or not isinstance(holdings.get('results'),list): raise FeedError('HOLDINGS_FAILED')
    rows=[]; errors=[]
    if holdings.get('next'): errors.append('Holdings truncated: additional pages exist.')
    for h in holdings['results']:
        asset=h.get('asset_code'); quantity=number(h.get('total_quantity'))
        rows.append({'asset':asset,'quantity':quantity,'cost_basis':None,'market_value':None,'unrealized_pnl':None})
    symbols=[str(h['asset'])+'-USD' for h in rows if h.get('asset')]
    if symbols:
        quotes=c.get_best_bid_ask(*symbols)
        if isinstance(quotes,dict) and isinstance(quotes.get('results'),list):
            by_symbol={q.get('symbol'):q for q in quotes['results']}
            for h in rows:
                q=by_symbol.get(str(h['asset'])+'-USD',{})
                bid=number(q.get('bid_inclusive_of_sell_spread'))
                if bid is None: bid=number(q.get('bid_price'))
                h['reference_sell_price']=bid
                h['market_value']=bid*h['quantity'] if bid is not None and h['quantity'] is not None else None
        else: errors.append('Price read failed; holdings quantities remain available.')
    orders=c.get_orders(account)
    trades=[]
    if isinstance(orders,dict) and isinstance(orders.get('results'),list):
        if orders.get('next'): errors.append('Recent executions cover first broker order page only, not complete history.')
        for order in orders['results']:
            config=order.get('market_order_config') or order.get('limit_order_config') or order.get('stop_loss_order_config') or order.get('stop_limit_order_config') or {}
            executions=order.get('executions')
            if executions is None: executions=config.get('executions',[])
            if isinstance(executions,list):
                for fill in executions:
                    if not isinstance(fill,dict): continue
                    trades.append({'time':fill.get('timestamp') or fill.get('created_at'),'asset':order.get('symbol'),'side':order.get('side'),'quantity':number(fill.get('quantity')),'price':number(fill.get('effective_price') or fill.get('price'))})
    else: errors.append('Recent order/execution request failed.')
    errors.append('Crypto cost basis and realized P&L need reconciled acquisition/transfer history; no zero basis assumed.')
    return {'ok':True,'connection':'BROKER_READ_SUCCEEDED','accounts':accounts,'account_id':account_id,'currency':selected.get('buying_power_currency') or 'USD','scope':'Selected crypto account; valuation uses reference sell bid where supplied.','metrics':{'available_funds':number(selected.get('buying_power')),'realized_pnl':None,'unrealized_pnl':None,'invested_cost':None},'positions':rows,'trades':sorted(trades,key=lambda t:str(t.get('time') or ''),reverse=True)[:50],'errors':errors,'pnl_period':'Historical P&L not reconciled'}

def stocks():
    import pickle, stat
    path=Path.home()/'.tokens/robinhood.pickle'
    st=path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.geteuid() or st.st_mode & 0o077: raise FeedError('SESSION_PERMISSIONS_REQUIRE_600_AND_CURRENT_OWNER')
    if st.st_size>1000000: raise FeedError('SESSION_FILE_TOO_LARGE')
    class SafePickle(pickle.Unpickler):
        def find_class(self,module,name): raise FeedError('UNSAFE_SESSION_FORMAT')
    with path.open('rb') as handle: session=SafePickle(handle).load()
    if not isinstance(session,dict) or not session.get('access_token') or not session.get('token_type'): raise FeedError('AUTH_REQUIRED')
    import robin_stocks.robinhood as rh
    from robin_stocks.robinhood.helper import set_login_state, update_session
    set_login_state(True)
    update_session('Authorization',str(session['token_type'])+' '+str(session['access_token']))
    profile=rh.profiles.load_account_profile()
    if not isinstance(profile,dict) or not profile.get('account_number'): raise FeedError('AUTH_REQUIRED')
    raw=rh.account.get_open_stock_positions()
    if not isinstance(raw,list): raise FeedError('POSITIONS_FAILED')
    rows=[]
    for p in raw:
        if not isinstance(p,dict): raise FeedError('INVALID_POSITION')
        qty=number(p.get('quantity')); avg=number(p.get('average_buy_price'))
        symbol=rh.stocks.get_symbol_by_url(p.get('instrument'))
        quote=rh.stocks.get_quotes(symbol) if symbol else None
        q=quote[0] if isinstance(quote,list) and quote and isinstance(quote[0],dict) else {}
        last=number(q.get('last_trade_price'))
        rows.append({'asset':symbol or 'Unknown','quantity':qty,'cost_basis':qty*avg if qty is not None and avg is not None else None,'market_value':qty*last if qty is not None and last is not None else None,'unrealized_pnl':qty*(last-avg) if None not in (qty,last,avg) else None,'quote_timestamp':q.get('updated_at')})
    orders=rh.orders.get_all_stock_orders()
    trades=[]; errors=['Account-wide stock positions, not verified bot attribution. Realized P&L requires reconciled history.']
    if isinstance(orders,list):
        for o in orders:
            if not isinstance(o,dict): continue
            executions=o.get('executions') or []
            if not executions: continue
            symbol=rh.stocks.get_symbol_by_url(o.get('instrument'))
            for fill in executions:
                trades.append({'time':fill.get('timestamp'),'asset':symbol,'side':o.get('side'),'quantity':number(fill.get('quantity')),'price':number(fill.get('price'))})
    else: errors.append('Execution-history read failed.')
    return {'ok':True,'connection':'BROKER_READ_SUCCEEDED','currency':'USD','scope':'Robinhood stock account; funds may be shared with other strategies.','metrics':{'available_funds':number(profile.get('buying_power')),'realized_pnl':None,'unrealized_pnl':sum_rows(rows,'unrealized_pnl'),'invested_cost':sum_rows(rows,'cost_basis')},'positions':rows,'trades':sorted(trades,key=lambda t:str(t.get('time') or ''),reverse=True)[:50],'errors':errors,'pnl_period':'Realized history not reconciled'}

def zerodha():
    from dotenv import dotenv_values
    from kiteconnect import KiteConnect
    env=dotenv_values('/opt/zerodha-bot/.env',interpolate=False)
    keys={(env.get(k) or '').strip() for k in ('KITE_API_KEY','ZERODHA_API_KEY')}-{''}
    token=(env.get('KITE_ACCESS_TOKEN') or '').strip()
    if len(keys)!=1 or not token: raise FeedError('CREDENTIAL_CONFIGURATION')
    k=KiteConnect(api_key=keys.pop(),timeout=10); k.set_access_token(token)
    margins=k.margins(); holdings=k.holdings(); positions=k.positions(); trades=k.trades()
    if not isinstance(margins,dict) or not isinstance(holdings,list) or not isinstance(positions,dict) or not isinstance(positions.get('net'),list) or not isinstance(trades,list): raise FeedError('INVALID_BROKER_RESPONSE')
    net=positions['net']; rows=[]
    for category, entries in (('holding',holdings),('net_position',net)):
        for p in entries:
            qty=number(p.get('quantity')); avg=number(p.get('average_price')); last=number(p.get('last_price'))
            if category=='net_position' and qty==0: continue
            rows.append({'category':category,'asset':p.get('tradingsymbol'),'exchange':p.get('exchange'),'quantity':qty,'cost_basis':qty*avg if qty is not None and avg is not None else None,'market_value':qty*last if qty is not None and last is not None else None,'unrealized_pnl':number(p.get('unrealised')) if category=='net_position' else (qty*(last-avg) if None not in (qty,last,avg) else None)})
    equity=margins.get('equity') or {}; available=equity.get('available') or {}
    normalized=[{'time':str(t.get('fill_timestamp') or t.get('exchange_timestamp') or t.get('order_timestamp') or ''),'asset':t.get('tradingsymbol'),'side':t.get('transaction_type'),'quantity':number(t.get('quantity')),'price':number(t.get('average_price'))} for t in trades]
    return {'ok':True,'connection':'BROKER_READ_SUCCEEDED','currency':'INR','scope':'Kite equity account. Holdings and net positions shown separately; no combined total that could double count.','metrics':{'available_funds':number(equity.get('net')),'realized_pnl':sum_rows(net,'realised'),'unrealized_pnl':sum_rows(net,'unrealised'),'invested_cost':None},'positions':rows,'trades':sorted(normalized,key=lambda t:t['time'],reverse=True)[:50],'errors':['P&L uses broker net-position fields, not lifetime P&L or a combined holdings total. Invested total withheld until holdings/position overlap is reconciled.'],'pnl_period':'Broker current-day net-position P&L; recent executions are today only','configured_engine_mode':env.get('TRADING_MODE'),'reader_mode':'READ_ONLY','broker_live_balance':number(available.get('live_balance'))}

def main():
    bot=sys.argv[1]; selected=sys.argv[2] if len(sys.argv)>2 else ''
    try:
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            install_guard()
            data=crypto(selected) if bot=='crypto' else stocks() if bot=='us_trading' else zerodha() if bot=='zerodha' else None
        if not isinstance(data,dict): raise FeedError('INVALID_BOT')
        if any(v in (401,403) for v in calls): raise FeedError('AUTH_REQUIRED')
        if any(not 200<=v<300 for v in calls): raise FeedError('BROKER_HTTP_ERROR')
        data.update(bot=bot,read_completed_at=now(),http_statuses=calls)
    except Exception as exc:
        code=getattr(exc,'code',None)
        auth=code in (401,403) or any(v in (401,403) for v in calls) or type(exc).__name__=='TokenException'
        reason=str(exc) if isinstance(exc,FeedError) else type(exc).__name__
        data={'ok':False,'bot':bot,'connection':'AUTH_REQUIRED' if auth else 'READ_FAILED','error':reason,'http_statuses':calls,'attempted_at':now()}
        if isinstance(exc,ModuleNotFoundError): data['missing_module']=exc.name
    print(json.dumps(data,default=str,allow_nan=False))
if __name__=='__main__': main()
