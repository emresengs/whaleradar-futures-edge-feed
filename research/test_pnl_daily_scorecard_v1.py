#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest

p=Path(__file__).with_name("pnl_daily_scorecard_v1.py")
spec=importlib.util.spec_from_file_location("scorecard",p)
s=importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)
def row(day,start,net,wins,losses,ts):
    return {"schema":"SUPERMAN_PAPER_DAILY_V1","status":"FINAL",
      "date":day,"startEquity":start,"endEquity":start+net,
      "dailyNetPnl":net,"closedTrades":wins+losses,
      "wins":wins,"losses":losses,"settledUtc":ts}
class ScoreboardTest(unittest.TestCase):
    def test_targets_and_epoch(self):
        before=row("2026-10-14",1000,10,4,6,"2026-10-14T21:00:00Z")
        mixed=row("2026-10-15",1010,20,6,4,"2026-10-15T21:00:00Z")
        after=row("2026-10-16",1030,60,12,8,"2026-10-16T21:00:00Z")
        d=s.evaluate([after,mixed,before],{"start_utc":"2026-10-15T07:00:00Z"})
        self.assertEqual(d["available_final_records"]["final_days"],3)
        self.assertEqual(d["eligible_future_days"]["final_days"],1)
        self.assertTrue(d["daily_records"][-1]["meetsWinTarget"])
        self.assertTrue(d["daily_records"][-1]["meetsReturnTarget"])
        self.assertFalse(d["promotion_authorized"])
    def test_frozen_mixed_day_never_counted(self):
        a=row("2026-10-14",1000,30,7,3,"2026-10-14T21:00:00Z")
        b=row("2026-10-15",1030,60,8,2,"2026-10-15T21:00:00Z")
        d=s.evaluate([a,b],{"start_utc":"2026-10-15T07:00:00Z"})
        self.assertEqual(d["eligible_future_days"]["final_days"],0)
    def test_final_equity_inconsistency_aborts(self):
        a=row("2026-10-14",1000,50,7,3,"2026-10-14T21:00:00Z")
        a["endEquity"]=1200
        with self.assertRaises(ValueError):s.evaluate([a],{"start_utc":"2026-10-15T07:00:00Z"})
if __name__=="__main__":
    unittest.main(verbosity=2)
