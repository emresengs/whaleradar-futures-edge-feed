#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import unittest

F=Path(__file__).with_name("pnl_quality_shadow_v1.py")
sp=importlib.util.spec_from_file_location("pnl_shadow",F)
m=importlib.util.module_from_spec(sp)
sp.loader.exec_module(m)

P={"cooldown_after_realized_sl_minutes":45,
  "min_trade_flow_usdt":300000,"max_spread_ratio":0.0003,
  "min_book_imbalance_abs":0.08,"min_short_sell_ratio":0.75,
  "max_short_book_imbalance":-0.10}
SPEC=m.frozen_spec(1000)
def sig(at,side="SHORT",sell=.80,book=-.20,spread=.0001,volume=500000):
    return {"ts":at,"symbol":"STRKUSDT","side":side,
        "metrics":{"sellRatio":sell,"spreadPct":spread,"totalUsd":volume,
            "orderBook":{"avg":book}}}
def trade(at,close,net=-1,exit="SL",side="SHORT",i="a"):
    return {"id":i,"openedAt":at,"closedAt":close,"netPnl":net,
        "grossPnl":net+.1,"feeOpen":.05,"feeClose":.05,
        "symbol":"STRKUSDT","side":side,"exitReason":exit}

class ProspectivePnlShadowTests(unittest.TestCase):
    def test_cooldown_from_prior_closed_sl_only(self):
        first=trade(1000,2000,-1,"SL",i="a")
        second=trade(2001,3000,2,"TP",i="b")
        d=m.calculate([second,first],[sig(1000),sig(2001)],SPEC,now=4000)
        self.assertEqual(d["arms"]["baseline"]["trades"],2)
        self.assertEqual(d["arms"]["cooldown"]["trades"],1)
        self.assertEqual(d["arms"]["cooldown"]["rejected"],1)
        self.assertEqual(d["arms"]["cooldown"]["missed_profit_usdt"],2)
        self.assertFalse(d["promotion_authorized"])
    def test_future_close_cannot_block_past_entry(self):
        first=trade(1000,5000,-1,"SL",i="a")
        second=trade(2001,3000,2,"TP",i="b")
        d=m.calculate([second,first],[sig(1000),sig(2001)],SPEC,now=6000)
        self.assertEqual(d["arms"]["cooldown"]["trades"],2)
    def test_quality_rejects_expensive_spread(self):
        x=m.admission(trade(1000,2000),sig(1000,spread=.0005),P,{})
        self.assertTrue(x["short_quality"])
        self.assertFalse(x["flow_quality"])
        self.assertFalse(x["combined"])
    def test_short_filter_rejects_low_sell_ratio(self):
        x=m.admission(trade(1000,2000),sig(1000,sell=.70),P,{})
        self.assertFalse(x["short_quality"])
        self.assertTrue(x["flow_quality"])
    def test_long_does_not_use_short_filter(self):
        x=m.admission(trade(1000,2000,side="LONG"),sig(1000,side="LONG",sell=.35,book=.25),P,{})
        self.assertTrue(x["short_quality"])
        self.assertTrue(x["flow_quality"])
    def test_missing_signal_no_false_approval(self):
        x=m.admission(trade(1000,2000),None,P,{})
        self.assertFalse(x["flow_quality"])
        self.assertFalse(x["short_quality"])
        self.assertFalse(x["combined"])
    def test_future_cutoff_excludes_predeployment(self):
        s=m.frozen_spec(2500)
        d=m.calculate([trade(1000,5000)],[],s,now=6000)
        self.assertEqual(d["baseline"]["trades"],0)
        self.assertFalse(d["sample_mature"])
    def test_actual_net_and_fees_retained(self):
        d=m.calculate([trade(1000,2000,net=1,exit="TP")],[sig(1000)],SPEC,now=5000)
        self.assertAlmostEqual(d["baseline"]["gross_usdt"],1.1)
        self.assertAlmostEqual(d["baseline"]["fees_usdt"],.1)
        self.assertAlmostEqual(d["baseline"]["net_pnl_usdt"],1)
        self.assertFalse(d["targets"]["daily_equity_target_verifiable_with_this_data"])
    def test_invalid_net_fee_rejected(self):
        x=trade(1000,2000)
        x["netPnl"]=50
        d=m.calculate([x],[sig(1000)],SPEC,now=5000)
        self.assertEqual(d["invalid_closed_trades"],1)

if __name__=="__main__":
    unittest.main(verbosity=2)
