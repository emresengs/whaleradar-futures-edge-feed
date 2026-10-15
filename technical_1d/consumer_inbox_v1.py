#!/usr/bin/env python3
"""Superman BTC 1D RSI advisory inbox. Read-only source, no orders and no trade changes."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import urllib.request

SOURCE="https://raw.githubusercontent.com/emresengs/whaleradar-futures-edge-feed/main/technical_1d/latest.json"
STATE=Path("/opt/futures-flow-bot/data/technical_1d_advisory_v1/latest_review.json")
STEP=86400000
RULE="1d_btc_confirm_rsi14_40_80_v1"
ID_RE=re.compile(r"^[0-9a-f]{64}$")

def parse_ms(s: str) -> int:
    if not isinstance(s,str) or not s.endswith("Z"):
        raise ValueError("INVALID_UTC_TIMESTAMP")
    return int(dt.datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()*1000)

def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()

def validate(p,now_ms):
    if not isinstance(p,dict) or p.get("schema_version")!="SUPERMAN_BTC_1D_RSI_ADVISORY_V1":
        raise ValueError("SCHEMA")
    if p.get("producer")!="futures_1d_btc_confirm_rsi_v1" or p.get("rule_version")!=RULE:
        raise ValueError("PRODUCER_RULE")
    if p.get("execution_command") is not False or p.get("decision_input_allowed") is not False or p.get("shadow_test_required") is not True:
        raise ValueError("UNSAFE_EXECUTION_INTENT")
    if p.get("timeframe")!="1d" or p.get("source_mode") not in ("SPOT","FUTURES"):
        raise ValueError("TIMEFRAME_MARKET")
    sid=p.get("scan_id","")
    if not isinstance(sid,str) or not ID_RE.fullmatch(sid) or sha({k:v for k,v in p.items() if k!="scan_id"})!=sid:
        raise ValueError("SCAN_INTEGRITY")
    asof=parse_ms(p["as_of_exchange_utc"])
    generated=parse_ms(p["generated_at_utc"])
    candle=parse_ms(p["evidence_timestamp_utc"])
    expiry=parse_ms(p["valid_until_utc"])
    day_start=(now_ms//STEP)*STEP
    if not (candle==day_start-1 and expiry==day_start+STEP and asof>candle and asof<=generated and generated<=now_ms+120000 and now_ms-generated<=STEP):
        raise ValueError("STALE_OR_INCONSISTENT_DAILY_CANDLE")
    gate=p.get("btc_gate") or {}
    rsi=gate.get("rsi14")
    if type(rsi) not in (float,int) or not math.isfinite(rsi) or not 0<=rsi<=100:
        raise ValueError("BTC_RSI")
    side="LONG" if rsi<40 else "SHORT" if rsi>80 else "NEUTRAL"
    if gate.get("symbol")!="BTCUSDT" or gate.get("source_market")!=p["source_mode"] or gate.get("side")!=side or gate.get("btc_confirmed_first") is not True:
        raise ValueError("BTC_GATE")
    if parse_ms(gate["candle_close_utc"])!=candle:
        raise ValueError("BTC_DAILY_CANDLE_MISMATCH")
    rows=p.get("symbol_signals")
    if not isinstance(rows,list) or len(rows)>5000:
        raise ValueError("ALTCOIN_ROWS")
    if side=="NEUTRAL" and rows:
        raise ValueError("NO_ADVICE_WITHOUT_BTC_CONFIRMATION")
    out=[]
    seen=set()
    for x in rows:
        if not isinstance(x,dict):
            raise ValueError("ROW_SHAPE")
        symbol=x.get("source_symbol")
        asset=x.get("base_asset")
        val=x.get("rsi14")
        if not isinstance(symbol,str) or not symbol.isalnum() or len(symbol)>40 or not isinstance(asset,str) or asset=="BTC" or symbol in seen:
            raise ValueError("UNSAFE_OR_DUPLICATE_SYMBOL")
        seen.add(symbol)
        if type(val) not in (float,int) or not math.isfinite(val) or not 0<=val<=100 or (not(val<40) if side=="LONG" else not(val>80)):
            raise ValueError("ALTCOIN_RSI_NOT_MATCHED")
        if x.get("decision_bias")!=side or x.get("source_market")!=p["source_mode"] or parse_ms(x["source_candle_close_utc"])!=candle:
            raise ValueError("ALTCOIN_SIDE_OR_TIME")
        if any(k in x for k in ("ema50","ema200","sma50","sma200")):
            raise ValueError("LEGACY_MA_EMA_PRESENT")
        if not isinstance(x.get("signal_id"),str) or not ID_RE.fullmatch(x["signal_id"]):
            raise ValueError("SIGNAL_ID")
        out.append({"symbol":symbol,"base_asset":asset,"rsi14":val,"recommendation":side,
                    "source_market":x["source_market"],"source_quote":x.get("source_quote_asset"),
                    "source_signal_id":x["signal_id"],"futures_mapping_verified":False})
    if side=="LONG":out.sort(key=lambda x:(x["rsi14"],x["symbol"]))
    if side=="SHORT":out.sort(key=lambda x:(-x["rsi14"],x["symbol"]))
    return {
      "schema":"SUPERMAN_BTC_1D_REVIEW_INBOX_V1",
      "status":"NO_BTC_CONFIRMATION" if side=="NEUTRAL" else "ADVISORY_REVIEW_ONLY",
      "rule":RULE,"scan_id":sid,
      "generated_at_utc":p["generated_at_utc"],
      "valid_until_utc":p["valid_until_utc"],
      "btc": {"symbol":"BTCUSDT","source_market":p["source_mode"],"rsi14":rsi,
              "side":side,"last_closed_candle":gate["candle_close_utc"]},
      "candidates":out,"candidate_count":len(out),
      "source":"PUBLIC_GITHUB_DAILY_RSI_PACKET",
      "futures_mapping_verified":False,
      "research_only":True,"orders_allowed":False,"paper_settings_changed":False,
      "note":"SPOT and FUTURES candidates need independent exchange mapping before decision impact."
    }

def download():
    req=urllib.request.Request(SOURCE,headers={"User-Agent":"Superman-BTC1D-RSI-Inbox/1.0","Cache-Control":"no-cache"})
    with urllib.request.urlopen(req,timeout=15) as r:
        if r.status!=200:raise ValueError("SOURCE_HTTP")
        data=r.read(5*1024*1024+1)
    if len(data)>5*1024*1024:raise ValueError("SOURCE_TOO_LARGE")
    return json.loads(data.decode("utf8"))

def main():
    p=download()
    review=validate(p,int(dt.datetime.now(dt.timezone.utc).timestamp()*1000))
    STATE.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if STATE.exists():
        old=json.loads(STATE.read_text(encoding="utf8"))
        if old.get("scan_id")==review["scan_id"]:
            print("BTC1D_INBOX_ALREADY_CURRENT",review["btc"]["side"],review["candidate_count"])
            return
    fd,tmp=tempfile.mkstemp(dir=str(STATE.parent),prefix=".inbox-",suffix=".tmp")
    try:
        with os.fdopen(fd,"w",encoding="utf8") as f:
            json.dump(review,f,ensure_ascii=False,indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp,0o600)
        os.replace(tmp,STATE)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    print("BTC1D_INBOX_VALIDATED",review["btc"]["side"],review["candidate_count"],"RESEARCH_ONLY")

if __name__=="__main__":
    main()
