#!/usr/bin/env python3
"""Read-only finalized Paper daily KPI scorecard; %50 win and %5 equity targets.
No changes to active strategies, orders, capital, Guardian or scientific epochs.
"""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile

ROOT=Path("/opt/futures-flow-bot")
STATE=Path("/var/lib/futures-guardian-v1/pnl_quality_shadow_v1")
REPORTS=ROOT/"data/paper_daily_v1/reports"
OUT=STATE/"daily_kpi_scorecard.json"

def timestamp(s):
    return dt.datetime.fromisoformat(s.replace("Z","+00:00"))

def evaluate(records,spec,shadow=None):
    freeze=timestamp(spec["start_utc"])
    records=sorted(records,key=lambda r:r.get("settledUtc",""))
    observed=[]
    previous_end=None
    previous_settlement=None
    for r in records:
        if r.get("status")!="FINAL":continue
        start=float(r["startEquity"])
        end=float(r["endEquity"])
        net=float(r["dailyNetPnl"])
        wins=int(r["wins"]);losses=int(r["losses"])
        closed=int(r["closedTrades"])
        if start<=0 or closed<0 or wins+losses!=closed:
            raise ValueError("FINAL_DAILY_LEDGER_INCONSISTENT")
        if abs((end-start)-net)>0.02:
            raise ValueError("FINAL_DAILY_EQUITY_RECONCILIATION")
        if previous_end is not None and abs(previous_end-start)>0.02:
            raise ValueError("FINAL_DAILY_CHAIN_GAP")
        settlement=timestamp(r["settledUtc"])
        fully_future=(previous_settlement is not None and previous_settlement>=freeze)
        win_pct=100*wins/closed if closed else None
        ret_pct=100*net/start
        observed.append({
            "date":r["date"],"settledUtc":r["settledUtc"],
            "startEquity":round(start,6),"endEquity":round(end,6),
            "dailyNetPnl":round(net,6),"dailyNetReturnPct":round(ret_pct,4),
            "trades":closed,"wins":wins,"losses":losses,
            "closedTradeWinPct":round(win_pct,3) if win_pct is not None else None,
            "meetsWinTarget":win_pct is not None and win_pct>=50,
            "meetsReturnTarget":ret_pct>=5,
            "fullyAfterShadowFreeze":fully_future,
            "validForProspectiveComparison":fully_future and closed>=20
        })
        previous_end=end;previous_settlement=settlement
    all_rows=observed
    prospective=[r for r in all_rows if r["validForProspectiveComparison"]]
    def summary(rows):
        returns=[x["dailyNetReturnPct"] for x in rows]
        return {"final_days":len(rows),"days_win_ge_50":sum(x["meetsWinTarget"] for x in rows),
            "days_return_ge_5":sum(x["meetsReturnTarget"] for x in rows),
            "avg_daily_net_return_pct":round(sum(returns)/len(returns),4) if returns else None,
            "both_targets_days":sum(x["meetsWinTarget"] and x["meetsReturnTarget"] for x in rows)}
    snapshots=[]
    if shadow and shadow.get("scope")=="future_only_holdout":
        for name,info in shadow.get("arms",{}).items():
            snapshots.append({
                "arm":name, "observed_closed":info["trades"],
                "win_pct":info["win_pct"],"observed_selection_net_pnl":info["net_pnl_usdt"],
                "sample_mature":bool(shadow.get("sample_mature")),
                "promotion_authorized":False})
    return {
        "schema":"SUPERMAN_PAPER_DAILY_KPI_V1",
        "targets":{"daily_close_win_pct_min":50,"avg_daily_net_equity_return_pct_target":5},
        "goal_is_not_guarantee":True,"increase_risk_to_force_target":False,
        "daily_records":observed,"available_final_records":summary(all_rows),
        "eligible_future_days":summary(prospective),
        "frozen_shadow_start_utc":spec["start_utc"],
        "shadow_selection_observations":snapshots,
        "note":"Percent returns come only from FINAL equity ledgers; historical incomplete before daily accounting start. Filter-selection PnL is not hypothetical compounded equity.",
        "no_live_or_paper_strategy_mutations":True,
        "promotion_authorized":False
    }

def load():
    frozen=json.loads((STATE/"frozen_spec.json").read_text(encoding="utf8"))
    data=[]
    for file in sorted(REPORTS.glob("????-??-??.json")):
        d=json.loads(file.read_text(encoding="utf8"))
        if d.get("date")==file.stem and d.get("status")=="FINAL":data.append(d)
    shadow_file=STATE/"latest.json"
    shadow=json.loads(shadow_file.read_text(encoding="utf8")) if shadow_file.exists() else None
    return evaluate(data,frozen,shadow)

def save(obj):
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,temp=tempfile.mkstemp(prefix=".kpi-",suffix=".tmp",dir=STATE)
    try:
        with os.fdopen(fd,"w",encoding="utf8") as f:
            json.dump(obj,f,ensure_ascii=False,indent=2)
            f.write("\n");f.flush();os.fsync(f.fileno())
        os.chmod(temp,0o600)
        os.replace(temp,OUT)
    finally:
        if os.path.exists(temp):os.unlink(temp)

if __name__=="__main__":
    kpi=load()
    save(kpi)
    print(json.dumps({
        "historical_final_days":kpi["available_final_records"],
        "prospective_fully_eligible_days":kpi["eligible_future_days"],
        "prospective_shadow_arms":len(kpi["shadow_selection_observations"]),
        "promotion_authorized":False
    },ensure_ascii=False))
