import csv, io, json, os, re, sys, tempfile
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import urlsplit, urljoin

SECTORS = {
 "Information Technology":"XLK", "Financials":"XLF", "Health Care":"XLV",
 "Energy":"XLE", "Consumer Discretionary":"XLY", "Consumer Staples":"XLP",
 "Industrials":"XLI", "Materials":"XLB", "Real Estate":"XLRE",
 "Utilities":"XLU", "Communication Services":"XLC",
}
FUNDS = {
 "IVV":("239726","ishares-core-s-p-500-etf",450),
 "IJH":("239763","ishares-core-s-p-mid-cap-etf",350),
 "IJR":("239774","ishares-core-s-p-small-cap-etf",550),
 "ITOT":("239724","ishares-core-s-p-total-u-s-stock-market-etf",1500),
 "OEF":("239723","ishares-s-p-100-etf",90),
 "IVW":("239725","ishares-s-p-500-growth-etf",50),
 "IVE":("239728","ishares-s-p-500-value-etf",100),
}
CORE = {"S&P 500":"IVV", "S&P MidCap 400":"IJH", "S&P SmallCap 600":"IJR",
        "S&P Total Market Index":"ITOT", "S&P 100":"OEF",
        "S&P 500 Growth":"IVW", "S&P 500 Value":"IVE"}
HOSTS = {"www.ishares.com","www.proshares.com"}
NOW = datetime.now(timezone.utc)

def require(value, message):
 if not value: raise ValueError(message)

class Redirect(HTTPRedirectHandler):
 def redirect_request(self, req, fp, code, msg, headers, url):
  p = urlsplit(url)
  require(p.scheme == "https" and p.hostname in HOSTS, "UNAPPROVED_REDIRECT")
  return super().redirect_request(req, fp, code, msg, headers, url)

def download(url):
 p = urlsplit(url)
 require(p.scheme == "https" and p.hostname in HOSTS, "UNAPPROVED_SOURCE")
 req = Request(url, headers={"User-Agent":"Mozilla/5.0", "Cache-Control":"no-cache"})
 with build_opener(Redirect()).open(req, timeout=25) as response:
  raw = response.read(8000001)
 require(len(raw)<=8000000, "SOURCE_TOO_LARGE")
 return raw.decode("utf-8-sig")

def valid_date(text, pattern):
 stamp = datetime.strptime(text.strip(),pattern).replace(tzinfo=timezone.utc)
 require(-86400 < (NOW-stamp).total_seconds() < 7*86400, "STALE_SOURCE_DATE:"+text)
 return stamp

def symbol(text):
 s = text.strip().upper().replace("/", ".")
 s = {"BRKB":"BRK.B", "BFB":"BF.B"}.get(s,s)
 require(re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}",s) is not None and s!="VIX", "INVALID_EQUITY_SYMBOL:"+s)
 return s

def weight(text):
 return float(text.replace(",", "").replace("%", "").strip())

def ishares(fund, info):
 number, slug, minimum = info
 page = "https://www.ishares.com/us/products/"+number+"/"+slug
 # This endpoint pattern is published by iShares; reject unusable responses.
 url = page+"/latest-holdings.csv"
 rows = list(csv.reader(io.StringIO(download(url))))
 dates = [r[1] for r in rows if len(r)>1 and "Fund Holdings as of" in r[0]]
 require(len(dates)==1, fund+":HOLDINGS_DATE_MISSING")
 stamp = valid_date(dates[0],"%b %d, %Y")
 headers = [i for i,r in enumerate(rows) if "Ticker" in r and "Asset Class" in r and "Sector" in r]
 require(len(headers)==1, fund+":CSV_SCHEMA_CHANGED")
 at=headers[0]; header=[v.strip() for v in rows[at]]
 require("Weight (%)" in header, fund+":WEIGHTS_MISSING")
 result={}; total=0.0
 for row in rows[at+1:]:
  if len(row)!=len(header): continue
  d=dict(zip(header,row))
  if d["Asset Class"].strip().lower()!="equity": continue
  s=symbol(d["Ticker"])
  require(s not in result, fund+":DUPLICATE_EQUITY:"+s)
  sec=d["Sector"].strip()
  if sec=="Communication": sec="Communication Services"
  require(sec in SECTORS, fund+":UNKNOWN_SECTOR:"+sec)
  result[s]={"instrument_type":"stock", "sector":sec, "name":d.get("Name", "")}
  total+=weight(d["Weight (%)"])
 require(len(result)>=minimum and 90<=total<=101, fund+":INCOMPLETE_HOLDINGS")
 return {"stocks":result,"as_of":stamp.isoformat(),"source_url":url,
         "classification_basis":"ISSUER_EQUITY_ROW_NOT_BROKER_VERIFIED"}

class Tables(HTMLParser):
 def __init__(self):
  super().__init__(); self.text=""; self.tables=[]; self.depth=0
  self.hidden=0; self.cell=None; self.row=[]; self.rows=[]; self.context=""
 def handle_starttag(self,tag,attrs):
  if tag in ("script","style"): self.hidden+=1
  if self.hidden: return
  if tag=="table":
   if self.depth==0:
    self.rows=[]; self.context=self.text[-2000:]
   self.depth+=1
  elif self.depth and tag=="tr": self.row=[]
  elif self.depth and tag in ("th","td"): self.cell=[]
 def handle_data(self,data):
  if self.hidden: return
  self.text+=" "+data.strip()
  if self.cell is not None: self.cell.append(data)
 def handle_endtag(self,tag):
  if tag in ("script","style"):
   self.hidden=max(0,self.hidden-1); return
  if self.hidden: return
  if self.depth and tag in ("th","td") and self.cell is not None:
   self.row.append(" ".join(" ".join(self.cell).split())); self.cell=None
  elif self.depth and tag=="tr" and self.row: self.rows.append(self.row)
  elif tag=="table" and self.depth:
   self.depth-=1
   if self.depth==0: self.tables.append((self.context,self.rows))

def nobl():
 url="https://www.proshares.com/our-etfs/strategic/nobl"
 parser=Tables(); parser.feed(download(url)); candidates=[]
 for context,rows in parser.tables:
  for i,header in enumerate(rows):
   if all(k in header for k in ("Weight","Ticker","Description")):
    candidates.append((context,header,rows[i+1:])); break
 require(len(candidates)==1,"NOBL:HOLDINGS_TABLE_MISSING_OR_AMBIGUOUS")
 context,header,rows=candidates[0]
 dates=re.findall(r"as\s+of\s+(\d{1,2}/\d{1,2}/\d{4})",context,re.I)
 require(dates,"NOBL:HOLDINGS_DATE_MISSING")
 stamp=valid_date(dates[-1],"%m/%d/%Y")
 stocks={}; total=0.0
 for row in rows:
  if len(row)!=len(header): continue
  d=dict(zip(header,row)); raw=d["Ticker"].strip()
  if raw in ("","-","--"): continue
  s=symbol(raw)
  require(s not in stocks,"NOBL:DUPLICATE_SYMBOL")
  stocks[s]={"instrument_type":"stock","name":d["Description"]}
  total+=weight(d["Weight"])
 require(40<=len(stocks)<=200 and 90<=total<=101,"NOBL:INCOMPLETE_HOLDINGS")
 return {"stocks":stocks,"as_of":stamp.isoformat(),"source_url":url,
         "classification_basis":"ISSUER_HOLDINGS_TABLE_NOT_BROKER_VERIFIED"}

def main():
 funds={}
 for name,info in FUNDS.items():
  funds[name]=ishares(name,info)
  print(name,len(funds[name]["stocks"]),funds[name]["as_of"],flush=True)
 funds["NOBL"]=nobl()
 stocks={}
 for fund in funds.values():
  for s,record in fund["stocks"].items(): stocks.setdefault(s,record)
 groups={}
 def group(name,members,trackers,sources,basis):
  require(members,"EMPTY_GROUP:"+name)
  groups[name]={"symbols":sorted(members),"tracker_etfs":trackers,
   "sources":sources,"membership_basis":basis,"exact_index_membership":False}
 for name,fund in CORE.items():
  group(name,set(funds[fund]["stocks"]),[fund],[fund],"ETF_HOLDINGS_PROXY")
 core=set().union(*(set(funds[f]["stocks"]) for f in ("IVV","IJH","IJR")))
 group("S&P Composite 1500",core,["SPTM"],["IVV","IJH","IJR"],"UNION_OF_THREE_CORE_ETF_PROXIES")
 group("S&P 500 Equal Weight",set(funds["IVV"]["stocks"]),["RSP"],["IVV"],"SAME_STOCK_SET_AS_SP500_PROXY_NOT_RSP_HOLDINGS")
 group("S&P 500 Dividend Aristocrats",set(funds["NOBL"]["stocks"]),["NOBL"],["NOBL"],"ETF_HOLDINGS_PROXY")
 for sector,etf in SECTORS.items():
  members={s for s,d in funds["ITOT"]["stocks"].items() if d.get("sector")==sector}
  group(sector,members,[etf],["ITOT"],"ITOT_SECTOR_CLASSIFICATION_PROXY_NOT_OFFICIAL_SECTOR_INDEX_MEMBERSHIP")
 require(len(groups)==21,"INDEX_GROUP_COVERAGE_FAILED")
 etfs=set().union(*(set(g["tracker_etfs"]) for g in groups.values()))
 require(len(etfs)==21 and not (etfs & set(stocks)),"ETF_ALLOWLIST_ERROR")
 for etf in etfs:
  stocks[etf]={"instrument_type":"etf","classification_basis":"EXPLICIT_TRACKER_ALLOWLIST_NOT_BROKER_VERIFIED"}
 require(1500<len(stocks)<=20000,"UNIVERSE_SIZE_INVALID")
 expires=min(NOW+timedelta(hours=36), min(datetime.fromisoformat(f["as_of"]) for f in funds.values())+timedelta(days=7))
 document={"version":"SI_SP_PROXY_UNIVERSE_V1","mode":"PUBLIC_HOLDINGS_PROXY",
  "updated_at":NOW.isoformat(),"expires_at":expires.isoformat(),
  "exact_index_membership":False,"connected_to_live_worker":False,
  "indexes":groups,"sources":{k:{a:b for a,b in v.items() if a!="stocks"} for k,v in funds.items()},
  "instruments":stocks,"symbols":sorted(stocks),"excluded_symbols":["VIX"],
  "sector_basis":"TMI_PROXY_STOCKS_WITH_ISSUER_SECTOR_LABELS; ETF_ALLOWLIST_USES_SP500_SELECT_SECTOR_TRACKERS"}
 target=__import__("pathlib").Path(sys.argv[1] if len(sys.argv)>1 else "/var/lib/si-sp-universe-v1/universe.json")
 require(not target.is_symlink(),"OUTPUT_MUST_NOT_BE_SYMLINK")
 fd,temp=tempfile.mkstemp(prefix=".sp-universe-",dir=target.parent)
 try:
  with os.fdopen(fd,"w") as stream:
   os.fchmod(stream.fileno(),0o600); json.dump(document,stream,indent=2,allow_nan=False)
   stream.flush(); os.fsync(stream.fileno())
  os.replace(temp,target)
 finally:
  if os.path.exists(temp): os.unlink(temp)
 print(json.dumps({"published_file":str(target),"groups":len(groups),"stock_candidates":len(stocks)-len(etfs),"etf_candidates":len(etfs),"expires_at":expires.isoformat(),"live_worker_changed":False}),flush=True)

if __name__=="__main__":
 try: main()
 except Exception as error:
  print("PUBLISH_FAILED:",type(error).__name__,str(error),flush=True)
  raise SystemExit(1)
