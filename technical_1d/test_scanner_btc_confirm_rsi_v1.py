#!/usr/bin/env python3
"""Offline tests: BTC must confirm first, daily candle only, no autonomous trades."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).with_name("scanner_btc_confirm_rsi_v1.py")
SPEC=importlib.util.spec_from_file_location("daily_btc_rsi",SOURCE)
m=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

def asset(sym):
    return {"symbol":sym, "baseAsset":sym.removesuffix("USDT"),"quoteAsset":"USDT"}

def outcome(meta,rsi):
    l,s=m.score_conditions({"rsi14":rsi})
    side="LONG" if rsi<40 else "SHORT" if rsi>80 else "NEUTRAL"
    return {"status":"OK","signal":{
        "signal_id":m.sha([meta["symbol"],rsi]),"base_asset":meta["baseAsset"],
        "source_symbol":meta["symbol"],"rsi14":rsi,
        "long_conditions":l,"short_conditions":s,
        "long_score":sum(x["points"] for x in l.values()),
        "short_score":sum(x["points"] for x in s.values()),
        "decision_bias":side, "direction":side+"_BIAS"}}

class BitcoinOneDayRsiTest(unittest.TestCase):
    def test_daily_wilder_thresholds(self):
        self.assertEqual(m.STEP,86400000)
        self.assertEqual(m.MAX_SCORE,25)
        self.assertTrue(m.score_conditions({"rsi14":39.999})[0]["rsi_below40"]["met"])
        self.assertFalse(m.score_conditions({"rsi14":40})[0]["rsi_below40"]["met"])
        self.assertFalse(m.score_conditions({"rsi14":80})[1]["rsi_above80"]["met"])
        self.assertTrue(m.score_conditions({"rsi14":80.001})[1]["rsi_above80"]["met"])

    def run_case(self,btc_rsi,expected,expected_calls):
        btc,eth,sol=asset("BTCUSDT"),asset("ETHUSDT"),asset("SOLUSDT")
        now=1800000000000
        asof=now-20000
        calls=[]
        def fake_analyze(meta,*unused):
            calls.append(meta["symbol"])
            value={"BTCUSDT":btc_rsi,"ETHUSDT":38.5,"SOLUSDT":83.5}[meta["symbol"]]
            return outcome(meta,value)
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,"discover",return_value=("FUTURES",m.FUTURES,"/fapi/v1",asof,[sol,btc,eth],[],3,[])),patch.object(m,"analyze",side_effect=fake_analyze),patch.object(m,"now_ms",return_value=now),patch.object(m,"log"):
            out=str(Path(tmp)/"latest.json")
            with patch.object(sys,"argv",["scanner","--out",out,"--workers","1"]):
                m.main()
            packet=json.loads(Path(out).read_text())
        self.assertEqual(packet["schema_version"],"SUPERMAN_BTC_1D_RSI_ADVISORY_V1")
        self.assertEqual(packet["btc_gate"]["rsi14"],btc_rsi)
        self.assertTrue(packet["btc_gate"]["btc_confirmed_first"])
        self.assertEqual(calls[0],"BTCUSDT")
        self.assertEqual(set(calls[1:]),set(expected_calls))
        self.assertEqual([s["source_symbol"] for s in packet["symbol_signals"]],expected)
        self.assertFalse(packet["execution_command"])
        self.assertFalse(packet["decision_input_allowed"])
        self.assertTrue(packet["shadow_test_required"])

    def test_btc_neutral_does_not_scan_alts(self):
        self.run_case(50,[],[])
    def test_btc_exact_40_is_neutral(self):
        self.run_case(40,[],[])
    def test_btc_exact_80_is_neutral(self):
        self.run_case(80,[],[])
    def test_btc_long_only_matching_alts(self):
        self.run_case(39.9,["ETHUSDT"],["ETHUSDT","SOLUSDT"])
    def test_btc_short_only_matching_alts(self):
        self.run_case(80.1,["SOLUSDT"],["ETHUSDT","SOLUSDT"])
    def test_no_ma_ema_code(self):
        source=SOURCE.read_text()
        for forbidden in ("def sma(", "def ema(", "sma_cross", "ema_cross", '"sma50"', '"ema50"'):
            self.assertNotIn(forbidden,source)

if __name__=="__main__":
    unittest.main(verbosity=2)
