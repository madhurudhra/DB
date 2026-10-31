"""Publish a validated dynamic universe from an upstream screener's JSON stdin."""
import argparse
import json
import re
import sys
from equity_worker import atomic_json, stamp

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    data=json.load(sys.stdin)
    symbols=data.get("symbols") if isinstance(data,dict) else data
    if not isinstance(symbols,list) or not symbols or len(symbols)>100:
        raise SystemExit("Require 1-100 screened ticker symbols")
    if any(not isinstance(s,str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}",s) for s in symbols):
        raise SystemExit("Require canonical uppercase tickers")
    atomic_json(args.output,{"updated_at":stamp(),"symbols":list(dict.fromkeys(symbols))})

if __name__=="__main__": main()
