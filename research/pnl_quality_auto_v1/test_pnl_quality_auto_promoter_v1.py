#!/usr/bin/env python3
"""Offline promotion and rollback test suite, with no live data writes."""
import datetime as dt
import importlib.util
from pathlib import Path
import unittest

p=Path(__file__).with_name("pnl_quality_auto_promoter_v1.py")
spec=importlib.util.spec_from_file_location("quality_auto_promoter",p)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW=dt.datetime(2026,10,30,12,0,tzinfo=dt.timezone.utc)
def report(n=400,armname="short_quality"):
    baseline={"trades":n,"win_pct":51,"net_pnl_usdt":100,"max_observed_pnl_drawdown_usdt":40}
    good={"trades":300,"win_pct":60,"net_pnl_usdt":130,"max_observed_pnl_drawdown_usdt":20,
        "rejected":100,"missed_profit_usdt":20,"observed_losses_filtered_usdt":45}
    row={"trades":48,"win_pct":60,"net_pnl_usdt":19}
    d={"scope":"future_only_holdout","sample_mature":True,"closed_trades_observed":n,
       "matched_signal_coverage_pct":100,"invalid_closed_trades":0,"signals_ambiguous_duplicates":0,
       "evaluated_utc":m.iso(NOW),
       "baseline":baseline,"arms":{armname:good},
       "days":[{"utc_day":f"2026-10-{i+20:02d}","arms":{
           "baseline":{"trades":55,"win_pct":51,"net_pnl_usdt":12},
           armname:row
           }} for i in range(7)],
       "promotion_authorized":False}
    return d
def daily(date,settled,wins,losses,start=1000,net=10):
    return {"date":date,"settledUtc":settled,"wins":wins,"losses":losses,"closedTrades":wins+losses,
      "startEquity":start,"endEquity":start+net,"dailyNetPnl":net}
class Gate(unittest.TestCase):
    def test_good_future_cohort_eligible_for_paper_only(self):
        self.assertEqual(m.arm_criteria(report(),"short_quality",NOW),[])
    def test_immature_never_promoted(self):
        r=report(100)
        r["sample_mature"]=False
        reasons=m.arm_criteria(r,"short_quality",NOW)
        self.assertIn("NOT_MATURE",reasons)
        self.assertIn("BASELINE_TOO_SMALL",reasons)
    def test_bad_pnl_must_not_promote_even_at_60_win(self):
        r=report()
        r["arms"]["short_quality"]["net_pnl_usdt"]=80
        self.assertIn("INSUFFICIENT_REALIZED_PNL_ADVANTAGE",m.arm_criteria(r,"short_quality",NOW))
    def test_bad_opportunity_cost(self):
        r=report()
        r["arms"]["short_quality"]["missed_profit_usdt"]=50
        self.assertIn("OPPORTUNITY_COST_TOO_HIGH",m.arm_criteria(r,"short_quality",NOW))
    def test_stale_result(self):
        r=report()
        r["evaluated_utc"]=m.iso(NOW-dt.timedelta(hours=2))
        self.assertIn("STALE_SHADOW_RESULT",m.arm_criteria(r,"short_quality",NOW))
    def test_below_50_day_violates_daily_stability(self):
        r=report()
        r["days"][-1]["arms"]["short_quality"]["win_pct"]=48
        self.assertIn("DAILY_50_PERCENT_NOT_STABLE",m.arm_criteria(r,"short_quality",NOW))
    def test_no_future_data_promoted(self):
        r=report()
        r["scope"]="historical_descriptive_DO_NOT_PROMOTE"
        self.assertIn("WRONG_COHORT",m.arm_criteria(r,"short_quality",NOW))
    def test_policy_hash_canonical(self):
        p={"schema":m.SCHEMA,"status":"PAPER_PILOT_ENABLED"}
        self.assertEqual(len(m.sha(p)),64)
    def test_single_full_negative_day_rolls_back(self):
        p={"activatedUtc":"2026-10-16T07:00:00Z"}
        a=daily("2026-10-16","2026-10-16T21:00:00Z",5,5)
        b=daily("2026-10-17","2026-10-17T21:00:00Z",5,5,net=-40)
        self.assertEqual(m.rollback_trigger(p,[a,b]),"FULL_DAY_LOSS_GT_3_PERCENT")
    def test_two_full_days_win_below_50_roll_back(self):
        p={"activatedUtc":"2026-10-16T07:00:00Z"}
        a=daily("2026-10-16","2026-10-16T21:00:00Z",15,5)
        b=daily("2026-10-17","2026-10-17T21:00:00Z",8,12)
        c=daily("2026-10-18","2026-10-18T21:00:00Z",9,11)
        self.assertEqual(m.rollback_trigger(p,[a,b,c]),"TWO_FULL_DAYS_WIN_BELOW_50")
    def test_first_mixed_day_no_rollback(self):
        p={"activatedUtc":"2026-10-16T20:00:00Z"}
        a=daily("2026-10-16","2026-10-16T21:00:00Z",1,39,net=-80)
        self.assertIsNone(m.rollback_trigger(p,[a]))
if __name__=="__main__":
    unittest.main(verbosity=2)
