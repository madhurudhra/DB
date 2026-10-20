#!/usr/bin/env python3
"""Offline fixture tests. Does not read credentials or contact brokers."""
import ast, hashlib, runpy, subprocess, sys, types
from pathlib import Path
BASE=Path(__file__).resolve().parent
w=runpy.run_path(str(BASE/'account_worker.py'),run_name='offline_test')
class Crypto:
    def get_accounts(self):return {'results':[{'account_number':'fixture-1','account_type':'test','buying_power':'12.50'},{'account_number':'fixture-2','account_type':'test','buying_power':'22.50'}],'next':None}
    def get_holdings(self,account):return {'results':[{'asset_code':'BTC','total_quantity':'2'}],'next':None}
    def get_best_bid_ask(self,*symbols):return {'results':[{'symbol':'BTC-USD','bid_price':'10'}]}
    def get_orders(self,account):return {'results':[{'symbol':'BTC-USD','side':'buy','executions':[{'timestamp':'2026-01-01','quantity':'2','effective_price':'8'}]}],'next':None}
module=types.ModuleType('robinhood_client');module.RobinhoodCryptoClient=Crypto
sys.modules['robinhood_client']=module
# Replace only fixture function's os.chdir, so the test needs no VPS directory.
old_chdir=w['os'].chdir; w['os'].chdir=lambda _:None
try:
    unselected=w['crypto']('')
    assert not unselected['ok'] and len(unselected['accounts'])==2
    selected=w['crypto'](hashlib.sha256(b'fixture-2').hexdigest()[:16])
    assert selected['metrics']['available_funds']==22.5
    assert selected['positions'][0]['market_value']==20
    assert selected['metrics']['realized_pnl'] is None
    assert len(selected['trades'])==1
finally:w['os'].chdir=old_chdir
class Kite:
    def __init__(self,**kwargs):pass
    def set_access_token(self,value):pass
    def margins(self):return {'equity':{'net':100,'available':{'live_balance':90}}}
    def holdings(self):return [{'tradingsymbol':'TEST','quantity':2,'average_price':10,'last_price':12}]
    def positions(self):return {'net':[{'tradingsymbol':'CLOSED','quantity':0,'realised':7,'unrealised':0},{'tradingsymbol':'OPEN','quantity':1,'average_price':10,'last_price':12,'realised':0,'unrealised':2}]}
    def trades(self):return [{'tradingsymbol':'TEST','quantity':1,'average_price':12,'fill_timestamp':'2026-01-01','transaction_type':'SELL'}]
m=types.ModuleType('kiteconnect');m.KiteConnect=Kite;sys.modules['kiteconnect']=m
m=types.ModuleType('dotenv');m.dotenv_values=lambda *a,**k:{'KITE_API_KEY':'fixture','KITE_ACCESS_TOKEN':'fixture'};sys.modules['dotenv']=m
z=w['zerodha']()
assert z['metrics']['realized_pnl']==7
assert z['metrics']['unrealized_pnl']==2
assert z['metrics']['invested_cost'] is None
assert len(z['positions'])==2
for p in BASE.glob('*.py'):ast.parse(p.read_text())
page=(BASE/'bots-status.html').read_text()
for source in ((BASE/'account_routes.js').read_text(),page.split('<script>',1)[1].split('</script>',1)[0]):
    p=subprocess.run(['node','--check'],input=source,text=True,capture_output=True)
    assert p.returncode==0,p.stderr
print('PASS: explicit Crypto account selection, holdings valuation, missing-basis protection, execution mapping, closed Zerodha realized P&L, no double-counted invested total, Python/JS syntax.')
print('OFFLINE TESTS ONLY. Broker access, deployment and stock session validity are not verified.')
