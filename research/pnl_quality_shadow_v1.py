#!/usr/bin/env python3
"""Superman PnL Quality Shadow V1: observational, read-only trades/signals.

Runs only after a frozen UTC deployment cut-off. No Binance credentials, orders,
Paper configuration, research candidate promotion or Guardian mutation.
Blocked-trade PnL is an observational opportunity-cost proxy, NOT a backtest.
"""
import argparse
import collections
import datetime as dt
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import tempfile

ROOT=Path("/opt/futures-flow-bot")
STATE=Path("/var/lib/futures-guardian-v1/pnl_quality_shadow_v1")
SPEC=STATE/"frozen_spec.json"
LATEST=STATE/"latest.json"
RULE="SUPERMAN_PNL_QUALITY_SHADOW_V1"
MIN_HOLDOUT_TRADES=200
MIN_HOLDOUT_DAYS=7

def now_ms():
    return int(dt.datetime.now(dt.timezone.utc).timestamp()*1000)

def utc(ts):
    return dt.datetime.fromtimestamp(ts/1000,dt.timezone.utc).isoformat()

def atomic_json(target,obj):
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,p=tempfile.mkstemp(prefix=".shadow-",suffix=".tmp",dir=target.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf8") as f:
            json.dump(obj,f,ensure_ascii=False,indent=2,allow_nan=False)
            f.write("\n");f.flush();os.fsync(f.fileno())
        os.chmod(p,0o600)
        os.replace(p,target)
    finally:
        if os.path.exists(p):os.unlink(p)

def frozen_spec(stamp=None):
    t=now_ms() if stamp is None else int(stamp)
    d={"schema":RULE,"start_ms":t,"start_utc":utc(t),
       "parameters":{
          "cooldown_after_realized_sl_minutes":45,
          "min_trade_flow_usdt":300000,
          "max_spread_ratio":0.0003,
          "min_book_imbalance_abs":0.08,
          "min_short_sell_ratio":0.75,
          "max_short_book_imbalance":-0.10,
       },
       "measures":["after_fees_net_pnl","win_rate","kept_trade_count",
          "missed_profitable_pnl","avoided_losing_pnl","max_observed_pnl_drawdown",
          "daily_win_rate","min_sample"],
       "scope":"future-only observational Paper closed trades",
       "research_only":True,"production_changes":False,"live_orders":False,
       "limitations":"Only realized trades; rejected trades do not free simulated margin. No historical strategy re-run. No causal inference."}
    d["sha256"]=hashlib.sha256(json.dumps(d,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    return d

def load_spec():
    d=json.loads(SPEC.read_text())
    digest=d.pop("sha256","")
    calc=hashlib.sha256(json.dumps(d,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    if calc!=digest or d.get("schema")!=RULE or d.get("research_only") is not True:
        raise ValueError("FROZEN_SPEC_INTEGRITY_FAILURE")
    d["sha256"]=digest
    return d

def load_jsonl(path):
    data=[]
    with path.open(encoding="utf8") as f:
        for line in f:
            if line.strip():
                data.append(json.loads(line))
    return data

def match_signals(signals):
    out={}
    dup=0
    for s in signals:
        try:
            ts=int(s["ts"]);symbol=s["symbol"];side=s["side"]
        except (KeyError,ValueError,TypeError):continue
        key=(ts,symbol,side)
        if key in out:
            dup+=1
            out[key]=None
        else:out[key]=s
    return out,dup

def num(obj,key):
    v=obj.get(key) if isinstance(obj,dict) else None
    if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v):
        return None
    return float(v)

def admission(trade,signal,params,previous_sl_at):
    """At open time, use only contemporaneous signal and PREVIOUS closes."""
    opening=int(trade["openedAt"])
    symbol=trade["symbol"]
    cooldown=(symbol in previous_sl_at and 0<=opening-previous_sl_at[symbol]<params["cooldown_after_realized_sl_minutes"]*60000)
    m=signal.get("metrics") if isinstance(signal,dict) else None
    spread=num(m,"spreadPct")
    volume=num(m,"totalUsd")
    book=num(m.get("orderBook"),"avg") if isinstance(m,dict) else None
    sell=num(m,"sellRatio")
    side=trade["side"]
    sidebook=book if side=="LONG" else -book if book is not None else None
    liquid_ok=(spread is not None and volume is not None and book is not None
       and 0<=spread<=params["max_spread_ratio"]
       and volume>=params["min_trade_flow_usdt"]
       and sidebook is not None and sidebook>=params["min_book_imbalance_abs"])
    short_ok=(side=="LONG" or (sell is not None and book is not None
       and sell>=params["min_short_sell_ratio"]
       and book<=params["max_short_book_imbalance"]))
    return {
       "baseline":True,
       "cooldown":not cooldown,
       "flow_quality":liquid_ok,
       "short_quality":short_ok,
       "combined":(not cooldown) and liquid_ok and short_ok,
       "matched_signal":signal is not None
    }

def statistics(rows):
    n=len(rows);wins=[r for r in rows if r["net"]>0]
    net=sum(r["net"] for r in rows)
    byclose=sorted(rows,key=lambda x:(x["closedAt"],x["id"]))
    cumulative=high=maxdd=0.0
    for r in byclose:
        cumulative+=r["net"];high=max(high,cumulative)
        maxdd=max(maxdd,high-cumulative)
    return {
        "trades":n,"wins":len(wins),"losses":n-len(wins),
        "win_pct":round(100*len(wins)/n,3) if n else None,
        "net_pnl_usdt":round(net,6),
        "avg_net_usdt":round(net/n,6) if n else None,
        "max_observed_pnl_drawdown_usdt":round(maxdd,6),
        "gross_usdt":round(sum(r["gross"] for r in rows),6),
        "fees_usdt":round(sum(r["fees"] for r in rows),6),
        "distinct_close_days":len({utc(r["closedAt"])[:10] for r in rows})
    }

def calculate(trades,signals,spec,now=None,historical=False):
    clock=now_ms() if now is None else int(now)
    start=spec["start_ms"]
    events,dups=match_signals(signals)
    parsed=[]
    invalid=0
    for t in trades:
        try:
            opened=int(t["openedAt"]);closed=int(t["closedAt"])
            if opened<start or closed>clock or closed<opened:continue
            net=float(t["netPnl"]);gross=float(t["grossPnl"])
            fees=float(t["feeOpen"])+float(t["feeClose"])
            if not all(math.isfinite(x) for x in (net,gross,fees)):raise ValueError("invalid pnl")
            if abs((gross-fees)-net)>0.05:raise ValueError("net fee inconsistency")
            if t["side"] not in ("LONG","SHORT"):raise ValueError("SIDE")
            parsed.append({"id":str(t["id"]),"openedAt":opened,"closedAt":closed,
                "net":net,"gross":gross,"fees":fees,"symbol":t["symbol"],
                "side":t["side"],"exitReason":t["exitReason"]})
        except (ValueError,TypeError,KeyError):invalid+=1
    if len({x["id"] for x in parsed})!=len(parsed):raise ValueError("DUPLICATE_TRADE_IDS")
    parsed.sort(key=lambda r:(r["openedAt"],r["id"]))
    arms={name:[] for name in ["baseline","cooldown","flow_quality","short_quality","combined"]}
    rejected={name:[] for name in arms if name!="baseline"}
    missed_signal=0
    prior_sl={}
    pending=[];seq=0
    for t in parsed:
        while pending and pending[0][0]<=t["openedAt"]:
            _,_,x=heapq.heappop(pending)
            if x["exitReason"]=="SL":
                prior_sl[x["symbol"]]=max(prior_sl.get(x["symbol"],-1),x["closedAt"])
        signal=events.get((t["openedAt"],t["symbol"],t["side"]))
        if signal is None:missed_signal+=1
        verdict=admission(t,signal,spec["parameters"],prior_sl)
        for name,allowed in verdict.items():
            if name=="matched_signal":continue
            if allowed:arms[name].append(t)
            elif name in rejected:rejected[name].append(t)
        seq+=1;heapq.heappush(pending,(t["closedAt"],seq,t))
    baseline=statistics(arms["baseline"])
    detail={}
    for name in arms:
        s=statistics(arms[name])
        if name!="baseline":
            rejected_rows=rejected[name]
            s.update({
                "rejected":len(rejected_rows),
                "rejected_profitable":sum(x["net"]>0 for x in rejected_rows),
                "missed_profit_usdt":round(sum(x["net"] for x in rejected_rows if x["net"]>0),6),
                "observed_losses_filtered_usdt":round(-sum(x["net"] for x in rejected_rows if x["net"]<=0),6),
                "selection_only_pnl_delta_vs_baseline_usdt":round(s["net_pnl_usdt"]-baseline["net_pnl_usdt"],6),
                "WARN":"Selection on real trades only; not actual alternative strategy equity."})
        detail[name]=s
    days=[]
    for day in sorted({utc(t["closedAt"])[:10] for t in parsed}):
        vals={}
        for name in arms:
            vals[name]=statistics([t for t in arms[name] if utc(t["closedAt"])[:10]==day])
        days.append({"utc_day":day,"arms":vals,"baseline_win_ge_50":(vals["baseline"]["win_pct"] or 0)>=50})
    holdout_ok=(len(parsed)>=MIN_HOLDOUT_TRADES and len(days)>=MIN_HOLDOUT_DAYS
       and sum(x["arms"]["baseline"]["trades"]>=20 for x in days)>=MIN_HOLDOUT_DAYS)
    return {
       "schema":RULE+"_RESULT","evaluated_utc":utc(clock),
       "source_period_start_utc":spec["start_utc"],"spec_sha256":spec["sha256"],
       "scope":"historical_descriptive_DO_NOT_PROMOTE" if historical else "future_only_holdout",
       "closed_trades_observed":len(parsed),"signals_ambiguous_duplicates":dups,
       "matched_signal_coverage_pct":round(100*(len(parsed)-missed_signal)/len(parsed),2) if parsed else None,
       "invalid_closed_trades":invalid,"baseline":baseline,"arms":detail,"days":days,
       "sample_mature":holdout_ok,"promotion_authorized":False,"paper_settings_changed":False,
       "live_trading_changed":False,
       "targets":{"daily_closed_trade_win_rate_pct":50,"daily_net_equity_return_pct":5,
                  "daily_equity_target_verifiable_with_this_data":False},
       "notes":[
          "Shadow filters applied to actual closed trades, not a simulated margin/execution book.",
          "Already opened and unfinished trades are excluded; conclusions must await their closure.",
          "Spread and orderbook measures are contemporaneous signals, never exit-time values.",
          "Official 5% daily equity return requires FINAL daily ledger and correct per-day start equity.",
          "Retrospective statistics cannot establish future performance; no auto promotion."
       ]}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--init",action="store_true")
    ap.add_argument("--historical-descriptive",action="store_true")
    ap.add_argument("--dry-run",action="store_true")
    args=ap.parse_args()
    if args.init:
        STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
        if SPEC.exists():raise SystemExit("SPEC_ALREADY_FROZEN")
        p=frozen_spec()
        with SPEC.open("x",encoding="utf8") as f:
            json.dump(p,f,indent=2);f.write("\n")
        os.chmod(SPEC,0o600)
        print("SHADOW_SPEC_FROZEN",p["start_utc"],p["sha256"])
        return
    spec=load_spec()
    if args.historical_descriptive:
        spec=dict(spec,start_ms=int(dt.datetime(2026,10,1,tzinfo=dt.timezone.utc).timestamp()*1000),
            start_utc="2026-10-01T00:00:00+00:00")
        cutoff=int(dt.datetime(2026,10,15,tzinfo=dt.timezone.utc).timestamp()*1000)
    else:cutoff=now_ms()
    report=calculate(load_jsonl(ROOT/"data/trades.jsonl"),
                     load_jsonl(ROOT/"data/signals.jsonl"),spec,cutoff,args.historical_descriptive)
    target=STATE/("historical_diagnostic.json" if args.historical_descriptive else "latest.json")
    if not args.dry_run:atomic_json(target,report)
    print(json.dumps({"scope":report["scope"],"rows":report["closed_trades_observed"],
        "coverage":report["matched_signal_coverage_pct"],
        "baseline":report["baseline"],"arms":report["arms"],
        "sample_mature":report["sample_mature"],"promotion":False},ensure_ascii=False))

if __name__=="__main__":
    main()
