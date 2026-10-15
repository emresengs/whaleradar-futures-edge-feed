#!/usr/bin/env python3
"""Conservative PAPER-ONLY auto promotion of frozen prospective PnL quality shadow.
Requires 7 meaningful future days, 200 kept trades, actual after-fee advantages.
No live orders, no changes to risk sizing, TP/SL, Guardian or other research.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import sys

SCHEMA="SUPERMAN_PAPER_QUALITY_AUTOPILOT_V1"
ROOT=Path("/opt/futures-flow-bot")
STATE=Path("/var/lib/futures-guardian-v1/pnl_quality_shadow_v1")
ACTIVE=ROOT/"data/paper_quality_auto_v1/active_policy.json"
DECISION=STATE/"auto_promotion_decision.json"
REVOKED=STATE/"revoked_auto_pilots.json"
SNAP=STATE/"latest.json"
FROZEN=STATE/"frozen_spec.json"
REPORTS=ROOT/"data/paper_daily_v1/reports"
ARMS=("cooldown","flow_quality","short_quality","combined")
MAX_AGE_MIN=30
MIN_BASE_TRADES=300
MIN_ARM_TRADES=200
MIN_QUALIFIED_DAYS=7
USER_AUTH="USER_STANDING_APPROVAL_2026-10-15_PAPER_ONLY"

def utcnow():
    return dt.datetime.now(dt.timezone.utc)

def iso(t):
    return t.isoformat(timespec="milliseconds").replace("+00:00","Z")

def parse(s):
    return dt.datetime.fromisoformat(s.replace("Z","+00:00"))

def sha(obj):
    return hashlib.sha256(json.dumps(obj,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()

def atomic_write(path,data):
    path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix=".auto-",suffix=".tmp")
    try:
        with os.fdopen(fd,"w",encoding="utf8") as f:
            json.dump(data,f,ensure_ascii=False,separators=(",",":"))
            f.write("\n");f.flush();os.fsync(f.fileno())
        os.chmod(tmp,0o600);os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def load_json(path,default=None):
    return json.loads(path.read_text(encoding="utf8")) if path.exists() else default

def validate_spec(d):
    if not isinstance(d,dict) or d.get("schema")!="SUPERMAN_PNL_QUALITY_SHADOW_V1":raise ValueError("NO_FROZEN_EXPERIMENT")
    h=d.get("sha256")
    clean={k:v for k,v in d.items() if k!="sha256"}
    check=hashlib.sha256(json.dumps(clean,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    if h!=check or not d.get("research_only") or d.get("production_changes") is not False:raise ValueError("CORRUPTED_FROZEN_SPEC")
    return d

def verify_paper_env(p):
    allowed={}
    for row in p.read_text().splitlines():
        if row.startswith(("EXECUTION_MODE=","LIVE_TRADING_ENABLED=","PAPER_QUALITY_AUTO_GATE_ENABLED=")):
            k,v=row.split("=",1);allowed[k]=v.strip().lower()
    if allowed.get("EXECUTION_MODE")!="paper" or allowed.get("LIVE_TRADING_ENABLED")!="false" or allowed.get("PAPER_QUALITY_AUTO_GATE_ENABLED")!="true":
        raise ValueError("PAPER_ENV_GUARD_BLOCKED")
    return True

def arm_criteria(rep,name,now):
    reasons=[]
    baseline=rep.get("baseline") or {}
    arm=(rep.get("arms") or {}).get(name) or {}
    days=rep.get("days") or []
    if rep.get("scope")!="future_only_holdout" or rep.get("promotion_authorized") is not False:
        reasons.append("WRONG_COHORT")
    if not rep.get("sample_mature"):reasons.append("NOT_MATURE")
    if (rep.get("closed_trades_observed") or 0)<MIN_BASE_TRADES:reasons.append("BASELINE_TOO_SMALL")
    if arm.get("trades",0)<MIN_ARM_TRADES:reasons.append("ARM_TOO_SMALL")
    if not isinstance(rep.get("matched_signal_coverage_pct"),(int,float)) or rep["matched_signal_coverage_pct"]<99:
        reasons.append("MISSING_PRE_ENTRY_EVIDENCE")
    if rep.get("invalid_closed_trades",0)>0 or rep.get("signals_ambiguous_duplicates",0)>0:
        reasons.append("CORRUPT_OR_AMBIGUOUS_RECORDS")
    try:
        if (now-parse(rep["evaluated_utc"])).total_seconds()>MAX_AGE_MIN*60:
            reasons.append("STALE_SHADOW_RESULT")
    except Exception:reasons.append("INVALID_SHADOW_TIMESTAMP")
    win=arm.get("win_pct")
    basewin=baseline.get("win_pct")
    if win is None or basewin is None or win<52 or win<basewin+2:
        reasons.append("INSUFFICIENT_WIN_RATE_ADVANTAGE")
    armnet=float(arm.get("net_pnl_usdt",float("-inf")))
    basenet=float(baseline.get("net_pnl_usdt",float("inf")))
    if not all(math.isfinite(x) for x in (armnet,basenet)) or armnet<max(5,basenet+max(5,abs(basenet)*.10)):
        reasons.append("INSUFFICIENT_REALIZED_PNL_ADVANTAGE")
    if arm.get("max_observed_pnl_drawdown_usdt",float("inf"))>baseline.get("max_observed_pnl_drawdown_usdt",0)*.85:
        reasons.append("DRAWDOWN_NOT_BETTER")
    if arm.get("rejected",0)<max(20,int(.05*(rep.get("closed_trades_observed") or 0))):
        reasons.append("TOO_FEW_FILTERED_DECISIONS")
    missed=arm.get("missed_profit_usdt")
    avoided=arm.get("observed_losses_filtered_usdt")
    if not isinstance(missed,(int,float)) or not isinstance(avoided,(int,float)) or avoided<=0 or avoided<1.30*missed:
        reasons.append("OPPORTUNITY_COST_TOO_HIGH")
    # Last seven complete days: no retroactively chosen best periods.
    eligible=[d for d in days if d.get("arms",{}).get("baseline",{}).get("trades",0)>=20]
    if len(eligible)<MIN_QUALIFIED_DAYS:
        reasons.append("TOO_FEW_ACTIVE_DAYS")
    else:
        tail=eligible[-MIN_QUALIFIED_DAYS:]
        selected=[d["arms"].get(name,{}) for d in tail]
        if any(x.get("trades",0)<10 or x.get("win_pct") is None or x["win_pct"]<50 for x in selected):
            reasons.append("DAILY_50_PERCENT_NOT_STABLE")
        if sum(x.get("net_pnl_usdt",0)>0 for x in selected)<6:
            reasons.append("INSUFFICIENT_POSITIVE_DAYS")
        if sum(x.get("net_pnl_usdt",0) for x in selected)<=sum(d["arms"]["baseline"].get("net_pnl_usdt",0) for d in tail):
            reasons.append("LAST_SEVEN_DAYS_NO_NET_PNL_EDGE")
    return reasons

def latest_final_reports():
    rows=[]
    for p in sorted(REPORTS.glob("????-??-??.json")):
        d=load_json(p,{})
        if d.get("status")=="FINAL" and d.get("date")==p.stem:
            rows.append(d)
    return sorted(rows,key=lambda r:r["settledUtc"])

def rollback_trigger(policy,reports):
    started=parse(policy["activatedUtc"])
    after=[r for r in reports if parse(r["settledUtc"])>started]
    # First report may contain pre-activation signals. Exclude it.
    if len(after)<2:return None
    full=after[1:]
    most=full[-1]
    start=float(most["startEquity"])
    if start<=0:return "INVALID_DAILY_EQUITY"
    ret=100*float(most["dailyNetPnl"])/start
    n=int(most["closedTrades"]);w=int(most["wins"])
    if ret<=-3:return "FULL_DAY_LOSS_GT_3_PERCENT"
    if n>=20 and len(full)>=2:
        old=full[-2];prevn=int(old["closedTrades"])
        if prevn>=20 and w/n<.5 and int(old["wins"])/prevn<.5:
            return "TWO_FULL_DAYS_WIN_BELOW_50"
    return None

def policy_valid(raw,now):
    if not isinstance(raw,dict) or raw.get("schema")!=SCHEMA:return False
    obj=raw.get("policy")
    return isinstance(obj,dict) and obj.get("schema")==SCHEMA and sha(obj)==raw.get("sha256") and obj.get("arm") in ARMS and obj.get("executionMode")=="paper" and obj.get("liveTradingEnabled") is False and parse(obj["activatedUtc"])<=now

def emit_decision(now,status,details):
    item={"schema":SCHEMA+"_DECISION","timestampUtc":iso(now),"status":status,
          "authorization":USER_AUTH,"paperOnly":True,"liveOrders":False,
          "automaticPromotionEnabled":True,"noRiskAmplification":True,
          "details":details}
    atomic_write(DECISION,item)
    print(json.dumps({"status":status,"details":details},ensure_ascii=False))

def main():
    now=utcnow()
    verify_paper_env(Path("/etc/futures-flow-bot-paper.env"))
    spec=validate_spec(load_json(FROZEN,{}))
    result=load_json(SNAP,{})
    if result.get("spec_sha256")!=spec["sha256"] or result.get("scope")!="future_only_holdout":
        raise ValueError("FROZEN_PROSPECTIVE_COHORT_MISMATCH")
    existing=load_json(ACTIVE)
    revoked=load_json(REVOKED,{"arms":[]})
    blocked=set(revoked.get("arms") or [])
    reports=latest_final_reports()
    if existing and policy_valid(existing,now):
        obj=existing["policy"]
        triggered=rollback_trigger(obj,reports)
        if triggered:
            blocked.add(obj["arm"])
            atomic_write(REVOKED,{"arms":sorted(blocked),"lastReason":triggered,"atUtc":iso(now),"previousPilotArm":obj["arm"]})
            # Removing active policy makes paper gate immediately fall back to baseline.
            ACTIVE.unlink(missing_ok=True)
            emit_decision(now,"PAPER_PILOT_ROLLED_BACK",{"arm":obj["arm"],"reason":triggered})
            return
        # Existing pilot cannot be replaced based on filtered, post-pilot baseline.
        updated=dict(obj,issuedUtc=iso(now),expiresUtc=iso(now+dt.timedelta(minutes=30)))
        atomic_write(ACTIVE,{"schema":SCHEMA,"policy":updated,"sha256":sha(updated)})
        emit_decision(now,"PAPER_PILOT_RENEWED",{"arm":obj["arm"],"expiresUtc":updated["expiresUtc"]})
        return
    checks={arm:arm_criteria(result,arm,now) for arm in ARMS}
    eligible=[arm for arm in ARMS if not checks[arm] and arm not in blocked]
    if not eligible:
        emit_decision(now,"WAITING_FOR_VALIDATED_RESULT",{
            "futureClosedTrades":result.get("closed_trades_observed"),
            "sampleMature":result.get("sample_mature"),
            "reasons":checks,"revoked":sorted(blocked),
            "daily5PercentTarget":"TARGET_TRACKED_NOT_GUARANTEED",
            "paperGate":"BASELINE_UNCHANGED"})
        return
    # Prefer the greatest AFTER-FEE PnL improvement; stable tie break.
    arm=max(eligible,key=lambda k:(result["arms"][k]["net_pnl_usdt"]-result["baseline"]["net_pnl_usdt"],-ARMS.index(k)))
    obj={
       "schema":SCHEMA,"status":"PAPER_PILOT_ENABLED","arm":arm,
       "executionMode":"paper","liveTradingEnabled":False,
       "researchCohort":"future_only_holdout","scienceResultValidated":True,
       "frozenSpecSha256":spec["sha256"],"killSwitch":False,
       "activatedUtc":iso(now),"issuedUtc":iso(now),
       "expiresUtc":iso(now+dt.timedelta(minutes=30)),
       "parameters":spec["parameters"],
       "authorization":USER_AUTH
    }
    atomic_write(ACTIVE,{"schema":SCHEMA,"policy":obj,"sha256":sha(obj)})
    emit_decision(now,"PAPER_PILOT_AUTOMATICALLY_STARTED",{
        "arm":arm,"futureClosedTrades":result["closed_trades_observed"],
        "filteredWinPct":result["arms"][arm]["win_pct"],
        "filteredNetPnl":result["arms"][arm]["net_pnl_usdt"],
        "baselineNetPnl":result["baseline"]["net_pnl_usdt"],
        "expiresUtc":obj["expiresUtc"],
        "tradingAuthorization":"PAPER_ONLY_NEW_SIGNALS"
    })

if __name__=="__main__":
    main()
