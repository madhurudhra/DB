import argparse
import base64
import fcntl
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from nacl.signing import SigningKey

ROOT = Path('/var/lib/si-crypto-r1')
CONFIG = Path('/etc/si-crypto-r1/config.json')
CREDS = Path('/home/botuser/.config/super-intelligence/rh_native_crypto.json')
BASE = 'https://trading.robinhood.com'
PREFIX = '/api/v2/crypto/'
SYMBOL = 'BTC-USD'
TERMINAL = {'filled', 'canceled', 'failed'}
LOG = logging.getLogger('si-crypto-r1')

class Blocked(RuntimeError):
    pass

def dec(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise Blocked('NONFINITE_NUMBER')
    return result

def utc():
    return datetime.now(timezone.utc).isoformat()

def database():
    db = sqlite3.connect(ROOT / 'runtime.sqlite3', timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA synchronous=FULL')
    db.executescript("""
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS samples(
      t REAL PRIMARY KEY,price TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS commands(
      id INTEGER PRIMARY KEY,at TEXT NOT NULL,action TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'QUEUED',applied_at TEXT);
    CREATE TABLE IF NOT EXISTS orders(
      seq INTEGER PRIMARY KEY,cid TEXT UNIQUE NOT NULL,bid TEXT,
      side TEXT NOT NULL,qty TEXT NOT NULL,state TEXT NOT NULL,
      created REAL NOT NULL,filled TEXT NOT NULL DEFAULT '0',
      average TEXT NOT NULL DEFAULT '0',fee TEXT NOT NULL DEFAULT '0');
    """)
    db.commit()
    return db

def get(db, key, default=None):
    row = db.execute('SELECT v FROM kv WHERE k=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default

def put(db, key, value):
    db.execute('INSERT INTO kv VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
               (key,json.dumps(value,allow_nan=False)))

def event(db, kind, **data):
    db.execute('INSERT INTO events(at,kind,data) VALUES(?,?,?)',
               (utc(),kind,json.dumps(data,allow_nan=False)))
    db.commit()
    LOG.info('%s %s',kind,json.dumps(data))

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class API:
    def __init__(self, config):
        saved = json.loads(CREDS.read_text())
        self.account = str(saved['account_number'])
        if self.account != config['account_number'] or not self.account.endswith('5576'):
            raise Blocked('NATIVE_ACCOUNT_BINDING_MISMATCH')
        self.key = saved['api_key']
        seed = base64.b64decode(saved['private_key_b64'], validate=True)
        if len(seed) not in (32,64):
            raise Blocked('INVALID_SIGNING_KEY_LENGTH')
        self.signer = SigningKey(seed[:32])

    def request(self, method, path, payload=None):
        if not path.startswith(PREFIX):
            raise Blocked('UNEXPECTED_API_PATH')
        raw = '' if payload is None else json.dumps(payload,separators=(',',':'))
        stamp = str(int(time.time()))
        signature = base64.b64encode(self.signer.sign(
            (self.key+stamp+path+method+raw).encode()).signature).decode()
        req = Request(BASE+path, data=raw.encode() if method=='POST' else None,
            method=method, headers={'x-api-key':self.key,'x-timestamp':stamp,
            'x-signature':signature,'Content-Type':'application/json','Accept':'application/json'})
        started = time.monotonic()
        try:
            with build_opener(NoRedirect).open(req,timeout=8) as response:
                if response.geturl() != BASE+path:
                    raise Blocked('UNEXPECTED_REDIRECT')
                data = json.load(response)
        except HTTPError as exc:
            raise Blocked('BROKER_HTTP_'+str(exc.code)) from None
        if not isinstance(data,dict) or 'errors' in data:
            raise Blocked('BROKER_RESPONSE_SHAPE')
        if method=='GET' and time.monotonic()-started>5:
            raise Blocked('SLOW_BROKER_READ')
        return data

    def path(self, resource, params=None):
        return PREFIX+resource+'/' + ('?'+urlencode(params) if params else '')

    def rows(self, resource, params=None):
        path = self.path(resource,params)
        rows, seen = [], set()
        for _ in range(100):
            if path in seen:
                raise Blocked('REPEATED_BROKER_CURSOR')
            seen.add(path)
            data = self.request('GET',path)
            if not isinstance(data.get('results'),list):
                raise Blocked('BROKER_LIST_SHAPE')
            rows.extend(data['results'])
            nxt = data.get('next')
            if not nxt:
                return rows
            parsed = urlsplit(nxt)
            if parsed.netloc and (parsed.scheme!='https' or parsed.netloc!='trading.robinhood.com'):
                raise Blocked('UNEXPECTED_PAGINATION_HOST')
            path = parsed.path+('?' + parsed.query if parsed.query else '')
        raise Blocked('PAGINATION_LIMIT')

    def verified(self):
        matches = [a for a in self.rows('trading/accounts')
                   if str(a.get('account_number'))==self.account]
        if len(matches)!=1:
            raise Blocked('EXACT_ACCOUNT_NOT_FOUND')
        a = matches[0]
        if a.get('status')!='active' or a.get('buying_power_currency')!='USD' or a.get('is_api_tradable') is not True:
            raise Blocked('ACCOUNT_NOT_API_TRADABLE')
        return a

    def orders(self):
        return self.rows('trading/orders',{'account_number':self.account,'symbol':SYMBOL})

    def quote(self, *, quantity, budget, step, fee_rate, maximum):
        if step<=0 or not 0<=fee_rate<1:
            raise Blocked('INVALID_PRICING_ARGUMENTS')

        if quantity<=0:
            rows=self.rows('marketdata/best_bid_ask', {'symbol':SYMBOL})
            matches=[r for r in rows if r.get('symbol')==SYMBOL]
            if len(matches)!=1:
                raise Blocked('SEED_QUOTE_NOT_UNIQUE')
            seed_ask=dec(matches[0]['ask'])
            if seed_ask<=0 or budget<=0:
                raise Blocked('INVALID_PRICING_BUDGET_OR_ASK')
            quantity=(
                budget/(seed_ask*(1+fee_rate)*Decimal('1.02'))/step
            ).to_integral_value(rounding=ROUND_DOWN)*step

        if not 0<quantity<maximum:
            raise Blocked('ESTIMATE_QUANTITY_OUTSIDE_RULES')

        data=self.request('GET',self.path('trading/estimated_price',{
            'symbol':SYMBOL,
            'side':'both',
            'quantity':format(quantity,'f'),
        }))
        rows=data.get('results')
        if not isinstance(rows,list) or len(rows)!=2:
            raise Blocked('ESTIMATE_SIDES_MISSING')

        sides={r.get('side'):r for r in rows if isinstance(r,dict)}
        if set(sides)!={'bid','ask'}:
            raise Blocked('ESTIMATE_SIDES_INVALID')

        stamps=[]
        for row in sides.values():
            if abs(dec(row['quantity'])-quantity)>step/1000:
                raise Blocked('ESTIMATE_QUANTITY_MISMATCH')

            stamp=datetime.fromisoformat(
                row['timestamp'].replace('Z','+00:00')
            )
            if stamp.tzinfo is None:
                raise Blocked('ESTIMATE_TIMESTAMP_WITHOUT_ZONE')

            age=(datetime.now(timezone.utc)-stamp).total_seconds()
            if not -3<=age<=15:
                raise Blocked('ESTIMATE_STALE_OR_CLOCK_SKEW')
            stamps.append(stamp)

            observed_fee=dec(row['fee_ratio'])
            if not 0<=observed_fee<=fee_rate:
                raise Blocked('ESTIMATE_FEE_EXCEEDS_PLANNING_RATE')
            if dec(row['est_fee'])<0:
                raise Blocked('NEGATIVE_ESTIMATED_FEE')

        if abs((stamps[0]-stamps[1]).total_seconds())>2:
            raise Blocked('ESTIMATE_SIDE_TIMESTAMP_MISMATCH')

        bid=dec(sides['bid']['bid'])
        ask=dec(sides['ask']['ask'])
        credit=dec(sides['bid']['est_total_credit'])
        cost=dec(sides['ask']['est_total_cost'])

        if min(bid,ask,credit,cost)<=0 or credit>=cost:
            raise Blocked('INVALID_FEE_INCLUSIVE_ESTIMATE')

        if abs(
            cost-(quantity*ask+dec(sides['ask']['est_fee']))
        )>Decimal('0.02'):
            raise Blocked('BUY_ESTIMATE_ARITHMETIC_MISMATCH')

        if abs(
            credit-(quantity*bid-dec(sides['bid']['est_fee']))
        )>Decimal('0.02'):
            raise Blocked('SELL_ESTIMATE_ARITHMETIC_MISMATCH')

        for side,price in [('bid',bid),('ask',ask)]:
            expected_fee=quantity*price*dec(sides[side]['fee_ratio'])
            if abs(
                expected_fee-dec(sides[side]['est_fee'])
            )>Decimal('0.02'):
                raise Blocked('ESTIMATE_FEE_ARITHMETIC_MISMATCH')

        self.last_quote={
            'quantity':str(quantity),
            'source_timestamp':min(stamps).isoformat(),
            'raw_prices_crossed':bid>ask,
            'broker_estimated_cost':str(cost),
            'broker_estimated_credit':str(credit),
            'planned_cost':str(quantity*ask*(1+fee_rate)),
            'planned_credit':str(quantity*bid*(1-fee_rate)),
        }
        return bid,ask

    def holdings(self):
        rows = self.rows('trading/holdings',{'account_number':self.account,'asset_code':'BTC'})
        rows = [r for r in rows if r.get('asset_code')=='BTC']
        if not rows:
            return Decimal(0),Decimal(0)
        if len(rows)!=1 or str(rows[0].get('account_number'))!=self.account:
            raise Blocked('HOLDINGS_ACCOUNT_MISMATCH')
        total = dec(rows[0]['total_quantity'])
        available = dec(rows[0]['quantity_available_for_trading'])
        if not 0<=available<=total:
            raise Blocked('INVALID_HOLDINGS')
        return total, available

# Only actual broker-reported fills contribute to the managed position.
def position(db):
    qty, basis = Decimal(0),Decimal(0)
    for row in db.execute('SELECT * FROM orders ORDER BY seq'):
        fill, avg, fee = dec(row['filled']),dec(row['average']),dec(row['fee'])
        if fill==0:
            continue
        if fill<0 or avg<=0 or fee<0:
            raise Blocked('INVALID_RECORDED_FILL')
        if row['side']=='buy':
            qty += fill
            basis += fill*avg+fee
        else:
            if fill>qty:
                raise Blocked('SELL_EXCEEDS_MANAGED_POSITION')
            basis *= (qty-fill)/qty
            qty -= fill
    return qty,basis

def save_order(db, api, order, cid):
    row = db.execute('SELECT * FROM orders WHERE cid=?',(cid,)).fetchone()
    if (str(order.get('account_number'))!=api.account or
        order.get('client_order_id')!=cid or order.get('symbol')!=SYMBOL or
        order.get('side')!=row['side'] or not order.get('id')):
        raise Blocked('ORDER_IDENTITY_MISMATCH')
    state = order.get('state')
    if state not in TERMINAL|{'open','pending','partially_filled'}:
        raise Blocked('UNRECOGNIZED_ORDER_STATE')
    fill = dec(order['filled_asset_quantity'])
    avg = dec(order.get('average_price') or 0)
    fee = dec(order['fee_charged']) if fill>0 else dec(order.get('fee_charged') or 0)
    if not 0<=fill<=dec(row['qty']) or fill<dec(row['filled']) or fee<0 or (fill>0 and avg<=0):
        raise Blocked('INVALID_BROKER_FILL')
    db.execute('UPDATE orders SET bid=?,state=?,filled=?,average=?,fee=? WHERE cid=?',
               (order['id'],state,str(fill),str(avg),str(fee),cid))
    db.commit()
    if state!=row['state'] or str(fill)!=row['filled']:
        event(db,'ORDER_UPDATE',reference=cid,state=state,filled=str(fill))

def reconcile(db, api):
    live = api.orders()
    own = {r['cid'] for r in db.execute('SELECT cid FROM orders')}
    for order in live:
        if order.get('symbol')==SYMBOL and order.get('state') not in TERMINAL and order.get('client_order_id') not in own:
            raise Blocked('EXTERNAL_BTC_ORDER_ACTIVE')
    by_ref = {}
    for order in live:
        key = order.get('client_order_id')
        if key in own:
            if key in by_ref:
                raise Blocked('DUPLICATE_BROKER_REFERENCE')
            by_ref[key] = order
    pending = False
    for row in db.execute("SELECT * FROM orders WHERE state NOT IN ('filled','canceled','failed')").fetchall():
        order = by_ref.get(row['cid'])
        if order is None and row['bid']:
            order = api.request('GET', api.path('trading/orders/'+row['bid'],{'account_number':api.account}))
        if order is None:
            raise Blocked('UNKNOWN_ORDER_OUTCOME_NO_RESUBMISSION')
        save_order(db,api,order,row['cid'])
        if order['state'] not in TERMINAL:
            pending = True
            if time.time()-row['created']>30:
                api.request('POST',api.path('trading/orders/'+order['id']+'/cancel'))
                event(db,'CANCEL_REQUESTED',reference=row['cid'])
    return pending

def submit(db, api, side, quantity, limit=None):
    commands(db)
    from worker_controls_v1 import permitted
    if not permitted(db, side):
        raise Blocked("DASHBOARD_PERMISSION_BLOCKED_" + side.upper())
    cid = str(uuid.uuid4())
    kind = 'limit' if limit is not None else 'market'
    payload = {'client_order_id':cid,'symbol':SYMBOL,'side':side,'type':kind}
    settings = {'asset_quantity':str(quantity)}
    if limit is not None:
        settings.update(limit_price=str(limit),time_in_force='gtc')
    payload[kind+'_order_config'] = settings
    db.execute('INSERT INTO orders(cid,side,qty,state,created) VALUES(?,?,?,?,?)',
               (cid,side,str(quantity),'SUBMITTING',time.time()))
    db.commit()
    event(db,'SUBMITTING_LIVE_ORDER',reference=cid,side=side,quantity=str(quantity))
    try:
        order = api.request('POST',api.path('trading/orders',{'account_number':api.account}),payload)
        save_order(db,api,order,cid)
    except Exception:
        db.execute("UPDATE orders SET state='UNKNOWN' WHERE cid=? AND state='SUBMITTING'",(cid,))
        db.commit()
        event(db,'ORDER_OUTCOME_REQUIRES_RECONCILIATION',reference=cid)
        raise

def commands(db):
    import sys
    directory = "/opt/super-intelligence-dashboard"
    if directory not in sys.path:
        sys.path.insert(0, directory)
    import worker_controls_v1
    worker_controls_v1.apply_commands(db)

def strategy(prices):
    fast = sum(prices[-20:])/20
    slow = sum(prices)/len(prices)
    trend = fast>slow
    momentum = prices[-1]>prices[-7]
    breakout = prices[-1]>max(prices[-21:-1])
    score = 40*trend+30*momentum+30*breakout
    return score, {'trend':bool(trend),'momentum':bool(momentum),'breakout':bool(breakout)}


# DAILY_ROLLOVER_REFERENCE_V1
def protective_reference(db, qty, basis, net_value):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(os.getenv("SI_ROLLOVER_TZ", "UTC"))
    day = datetime.now(zone).date().isoformat()
    stored = get(db, "protective_reference_v1")

    if stored is None:
        ref = basis
    else:
        previous_qty = dec(stored["quantity"])
        if previous_qty <= 0:
            raise Blocked("INVALID_SAVED_PROTECTIVE_QUANTITY")
        if qty > previous_qty:
            raise Blocked("UNEXPECTED_MANAGED_POSITION_INCREASE")

        scale = qty / previous_qty
        ref = dec(stored["reference_value"]) * scale

        if stored["day"] != day:
            carried_value = dec(stored["last_net_value"]) * scale
            ref = max(ref, carried_value)
            event(
                db,
                "DAILY_REFERENCE_ROLLED",
                timezone=str(zone),
                reference_value=str(ref),
                source_observed_at=stored["last_mark_at"],
                source_day=stored["day"],
                new_day=day,
            )

    if ref <= 0:
        raise Blocked("INVALID_PROTECTIVE_REFERENCE")

    put(db, "protective_reference_v1", {
        "day": day,
        "timezone": str(zone),
        "quantity": str(qty),
        "reference_value": str(ref),
        "last_net_value": str(net_value),
        "last_mark_at": utc(),
        "stop_value": str(ref * (1-dec(config()["stop_loss_fraction"]))),
    })
    db.commit()
    return ref


# EXIT_POLICY_V3_PRICE_ONLY

def exit_price_basis_v3(db):
    quantity = Decimal(0)
    gross = Decimal(0)
    lifecycle = None
    started = None
    for row in db.execute('SELECT * FROM orders ORDER BY seq'):
        fill, avg = dec(row['filled']), dec(row['average'])
        if fill == 0:
            continue
        if fill < 0 or avg <= 0:
            raise Blocked('INVALID_PRICE_BASIS_FILL')
        if row['side'] == 'buy':
            if quantity == 0:
                lifecycle, started = row['cid'], row['created']
            quantity += fill
            gross += fill * avg
        elif row['side'] == 'sell':
            if fill > quantity:
                raise Blocked('PRICE_BASIS_SELL_EXCEEDS_POSITION')
            gross *= (quantity - fill) / quantity
            quantity -= fill
            if quantity == 0:
                lifecycle, started = None, None
        else:
            raise Blocked('UNKNOWN_POSITION_SIDE')
    return quantity, gross, lifecycle, started


def exit_reason_v3(bid, average, net_return, crossed, score):
    """Shared policy adapter. Existing caller retains order safeguards."""
    import importlib.util

    policy_path = "/opt/si-trading-policy-v1/exit_policy_v1.py"
    try:
        spec = importlib.util.spec_from_file_location(
            "si_shared_exit_policy_v1", policy_path
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("POLICY_LOADER_UNAVAILABLE")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.evaluate_exit(
            bot="crypto",
            bid=bid,
            average_purchase_price=average,
            estimated_net_return_fraction=net_return,
            rally_score=score,
        )
    except Exception as error:
        raise Blocked("SHARED_EXIT_POLICY_UNAVAILABLE") from error

    action = result.get("action")
    if action == "BLOCKED":
        raise Blocked("SHARED_EXIT_POLICY: " + result["reason"])
    if action == "EXIT_SIGNAL":
        return (result["reason"], False)
    if action == "HOLD":
        return (None, False)
    raise Blocked("SHARED_EXIT_POLICY_INVALID_ACTION")


def exit_policy_v3(db, qty, basis, bid, fee_rate, score):
    actual_qty, gross, lifecycle, started = exit_price_basis_v3(db)
    if actual_qty != qty or gross <= 0 or basis <= 0 or lifecycle is None:
        raise Blocked('PRICE_BASIS_RECONCILIATION_FAILED')

    average = gross / qty
    net_value = qty * bid * (1-fee_rate)
    pnl = (net_value-basis)/basis

    saved = get(db, 'exit_policy_v3')
    if saved is not None and saved.get('lifecycle') == lifecycle:
        if type(saved.get('crossed_four_percent')) is not bool:
            raise Blocked('INVALID_PROFIT_PROTECTION_STATE')
        crossed = saved['crossed_four_percent']
    else:
        crossed = False
        start = datetime.fromtimestamp(started, timezone.utc).isoformat()
        for row in db.execute(
            "SELECT data FROM events WHERE kind='CYCLE' AND at>=? ORDER BY id",
            (start,)
        ):
            previous = json.loads(row['data'])
            historical = previous.get('estimated_net_return_pct')
            if historical is not None and dec(
                previous.get('managed_quantity', '0')
            ) > 0:
                crossed = crossed or dec(historical) > Decimal('4')

    reason, crossed = exit_reason_v3(
        bid, average, pnl, crossed, score
    )

    put(db, 'exit_policy_v3', {
        'lifecycle': lifecycle,
        'crossed_four_percent': crossed,
        'average_fill_price_ex_fees': str(average),
        'updated_at': utc(),
        'rally_policy': 'EXISTING_R1_SCORE_WEAK_BELOW_70',
        'history_policy': 'OBSERVED_CYCLE_RETURNS_CURRENT_LIFECYCLE',
    })
    db.commit()
    return reason, pnl, net_value, average, crossed


def cycle(db, api, cfg):
    commands(db)
    a = api.verified()
    pending = reconcile(db,api)
    qty,basis = position(db)
    # Do not compare holdings against an in-flight order's changing fills.
    if pending:
        return {'state':'ORDER_PENDING','decision':'WAIT_FOR_RECONCILIATION'}
    total, available = api.holdings()
    if get(db,'external_baseline') is None:
        if qty!=0:
            raise Blocked('BASELINE_MISSING_WITH_POSITION')
        put(db,'external_baseline',str(total))
        db.commit()
        event(db,'EXTERNAL_HOLDINGS_EXCLUDED',quantity=str(total))
    baseline = dec(get(db,'external_baseline'))
    pair_rows = api.rows('trading/trading_pairs',{'symbol':SYMBOL})
    pairs = [p for p in pair_rows if p.get('symbol')==SYMBOL]
    if len(pairs)!=1 or pairs[0].get('is_api_tradable') is not True:
        raise Blocked('PAIR_NOT_API_TRADABLE')
    pair = pairs[0]
    step, tick = dec(pair['asset_increment']),dec(pair['quote_increment'])
    minimum, maximum = dec(pair['min_order_amount']),dec(pair['max_order_size'])
    if min(step,tick,minimum,maximum)<=0:
        raise Blocked('INVALID_PAIR_RULES')
    if abs(total-baseline-qty)>step/Decimal(100):
        raise Blocked('HOLDINGS_CHANGED_EXTERNALLY_OR_NOT_SETTLED')
    broker_fee_rate = dec(a['fee_tier_status']['fee_ratio'])
    if not 0<=broker_fee_rate<1:
        raise Blocked('INVALID_FEE_RATE')
    fee_rate = max(
        broker_fee_rate,
        dec(cfg['fee_allowance_per_side']),
    )
    if not 0<=fee_rate<1:
        raise Blocked('INVALID_CONFIGURED_FEE_ALLOWANCE')
    quote_budget = min(
        dec(cfg['entry_budget_usd']),
        dec(cfg['capital_usd']),
        dec(a['buying_power']),
    )
    bid,ask = api.quote(
        quantity=qty,
        budget=quote_budget,
        step=step,
        fee_rate=fee_rate,
        maximum=maximum,
    )
    received = time.time()
    mid = (bid+ask)/2
    db.execute('INSERT INTO samples VALUES(?,?)',(received,str(mid)))
    db.execute('DELETE FROM samples WHERE t<?',(received-3600,))
    db.commit()
    samples = db.execute('SELECT t,price FROM samples ORDER BY t DESC LIMIT 60').fetchall()[::-1]
    spread = max(Decimal(0),(ask-bid)/mid)
    details = {'state':'PROCESSING','entries_enabled':get(db,'entries_enabled',False),
               'managed_quantity':str(qty),'quote_received_at':utc(),
               'source_quote_timestamp':api.last_quote['source_timestamp'],
               'spread_pct':str(spread*100),'sample_count':len(samples)}
    valid_history = len(samples)==60 and all(5<=samples[i]['t']-samples[i-1]['t']<=25 for i in range(1,len(samples)))
    score, factors = (strategy([dec(s['price']) for s in samples]) if valid_history else (None,{}))
    details.update(score=score,factors=factors)
    details.update(quote_source='SIZE_AWARE_ESTIMATE', **api.last_quote)
    details.update(
        broker_fee_rate=str(broker_fee_rate),
        planning_fee_rate_per_side=str(fee_rate),
        stop_loss_pct=str(dec(cfg['stop_loss_fraction'])*100),
        exit_policy_version='crypto-price-exit-v3',
    )
    if qty>0:
        reason, pnl, net_value, average, crossed = exit_policy_v3(
            db, qty, basis, bid, fee_rate, score
        )
        price_reference = qty * average
        details.update(
            estimated_net_return_pct=str(pnl*100),
            price_return_pct=str((bid/average-1)*100),
            average_fill_price_ex_fees=str(average),
            protective_reference=str(price_reference),
            protective_stop_value=str(price_reference*Decimal('0.985')),
            stop_price=str(average*Decimal('0.985')),
            estimated_net_position_value=str(net_value),
            reference_policy='ACTUAL_AVERAGE_FILL_EXCLUDING_FEES',
            crossed_four_percent=crossed,
            rally_policy='EXISTING_R1_SCORE_WEAK_BELOW_70',
        )
        if reason:
            if available<qty:
                raise Blocked('MANAGED_QUANTITY_NOT_SELLABLE')
            sell_qty = (qty/step).to_integral_value(rounding=ROUND_DOWN)*step
            if sell_qty<=0 or sell_qty*bid<minimum or sell_qty>maximum:
                raise Blocked('EXIT_BELOW_MINIMUM_OR_EXCEEDS_BROKER_SIZE')
            submit(db,api,'sell',sell_qty)
            details.update(decision='SELL_SUBMITTED',reason=reason)
        else:
            details['decision']='HOLD'
        return details
    put(db, 'protective_reference_v1', None)
    put(db, 'exit_policy_v3', None)
    db.commit()
    details['decision']='NO_TRADE'
    if not get(db,'entries_enabled',False):
        details['state']='ENTRIES_PAUSED'
        return details
    flat_return = (
        dec(api.last_quote['planned_credit']) /
        dec(api.last_quote['planned_cost']) - 1
    )
    details['estimated_round_trip_return_pct'] = str(flat_return*100)
    if flat_return<=-dec(cfg['entry_cost_limit_fraction']):
        details['state']='ENTRY_COST_BLOCKED'
        details['reason']='ROUND_TRIP_COST_EXCEEDS_ENTRY_COST_LIMIT'
        return details
    if not valid_history:
        details['state']='WARMING_UP'
        return details
    if score!=100:
        return details
    if spread>Decimal('0.005'):
        details['reason']='SPREAD_TOO_WIDE'
        return details
    last = db.execute('SELECT MAX(created) FROM orders').fetchone()[0]
    if last and received-last<600:
        details['reason']='COOLDOWN'
        return details
    price = (ask/tick).to_integral_value(rounding=ROUND_UP)*tick
    budget = min(dec(cfg['entry_budget_usd']),dec(cfg['capital_usd']),dec(a['buying_power']))
    # Price/fee estimate includes a 2% funding buffer, not a fill guarantee.
    buy_qty = dec(api.last_quote['quantity'])
    if buy_qty*price*(1+fee_rate)*Decimal('1.02')>budget:
        details['reason']='ESTIMATE_EXCEEDS_ENTRY_BUDGET'
        return details
    if buy_qty<=0 or buy_qty*price<minimum or buy_qty>maximum:
        details['reason']='BUDGET_CANNOT_MEET_BROKER_RULES'
        return details
    commands(db)
    if not get(db,'entries_enabled',False):
        details['state']='ENTRIES_PAUSED'
        return details
    if time.time()-received>5:
        raise Blocked('QUOTE_RECEIPT_TOO_OLD_FOR_ENTRY')
    submit(db,api,'buy',buy_qty,price)
    details['decision']='BUY_SUBMITTED'
    return details

def config():
    cfg = json.loads(CONFIG.read_text())
    capital,entry = dec(cfg['capital_usd']),dec(cfg['entry_budget_usd'])
    if not 0<entry<=capital:
        raise Blocked('INVALID_CAPITAL_CONFIGURATION')
    return cfg

def run():
    cfg = config()
    with open(ROOT/'worker.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db = database()
        api = API(cfg)
        # A restart pauses NEW entries; position exits remain active.
        put(db,'entries_enabled',False)
        import sys
        directory = "/opt/super-intelligence-dashboard"
        if directory not in sys.path:
            sys.path.insert(0, directory)
        from worker_controls_v1 import on_start
        on_start(db)
        db.execute("UPDATE commands SET state='SUPERSEDED_ON_RESTART' WHERE state='QUEUED'")
        db.commit()
        event(db,'WORKER_STARTED',release='crypto-r1',account='****'+api.account[-4:])
        while True:
            started = time.monotonic()
            try:
                info = cycle(db,api,cfg)
                info.update(last_cycle_completed=utc(),account='****'+api.account[-4:],symbol=SYMBOL)
                event(db,'CYCLE',**info)
            except Exception as exc:
                code = str(exc) if isinstance(exc,Blocked) else type(exc).__name__
                info = {'state':'BLOCKED','reason':code,'last_attempt':utc(),
                        'entries_enabled':get(db,'entries_enabled',False)}
                event(db,'CYCLE_BLOCKED',**info)
            put(db,'status',info)
            put(db,'heartbeat',utc())
            db.commit()
            time.sleep(max(1,10-(time.monotonic()-started)))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=['run','status','events','pause','resume'])
    args = parser.parse_args()
    if args.action=='run':
        run()
        return
    db = database()
    if args.action=='status':
        result = {'status':get(db,'status'), 'heartbeat':get(db,'heartbeat'),
                  'entries_enabled':get(db,'entries_enabled',False),
                  'pending_commands':[dict(r) for r in db.execute("SELECT * FROM commands WHERE state='QUEUED'")]}
        print(json.dumps(result,indent=2))
    elif args.action=='events':
        for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 30').fetchall()[::-1]:
            print(r['at'],r['kind'],r['data'])
    else:
        cfg=config()
        if args.action=='resume':
            answer=input('Enable REAL BTC-USD orders on native account ****'+cfg['account_number'][-4:]+'? Type LIVE: ')
            if answer!='LIVE':
                raise SystemExit('Not enabled.')
        cur=db.execute('INSERT INTO commands(at,action) VALUES(?,?)',(utc(),args.action))
        db.commit()
        print('Queued command',cur.lastrowid,';',args.action,'; inspect status for acknowledgement.')
        if args.action=='pause':
            print('Pauses new entries only. Existing position exits continue. Orders already submitted are not canceled.')

if __name__=='__main__':
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    main()


# CRYPTO_FEE_STOP_POLICY_V2

# SIZE_AWARE_PRICING_V1
