#!/usr/bin/env python3
"""Offline verification for the RSI-only advisory candidate."""
import importlib.util
import pathlib
import unittest
from unittest.mock import patch

PATH = pathlib.Path(__file__).with_name("scanner_rsi_only_v1.py")
spec = importlib.util.spec_from_file_location("scanner_rsi_candidate", PATH)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class RSIOnlyContractTest(unittest.TestCase):
    def test_rsi_long_threshold(self):
        lo, sh = scanner.score_conditions({"rsi14": 39.9})
        self.assertEqual(lo["rsi_below40"]["points"], 25)
        self.assertEqual(sh["rsi_above80"]["points"], 0)
        self.assertEqual(set(lo), {"rsi_below40"})
        self.assertEqual(set(sh), {"rsi_above80"})

    def test_equal_40_no_long(self):
        lo, _ = scanner.score_conditions({"rsi14": 40})
        self.assertEqual(lo["rsi_below40"]["points"], 0)

    def test_equal_80_no_short(self):
        _, sh = scanner.score_conditions({"rsi14": 80})
        self.assertEqual(sh["rsi_above80"]["points"], 0)

    def test_above_80_short(self):
        lo, sh = scanner.score_conditions({"rsi14": 80.1})
        self.assertEqual(lo["rsi_below40"]["points"], 0)
        self.assertEqual(sh["rsi_above80"]["points"], 25)

    def test_neutral_middle(self):
        lo, sh = scanner.score_conditions({"rsi14": 52.0})
        self.assertEqual(sum(v["points"] for v in lo.values()), 0)
        self.assertEqual(sum(v["points"] for v in sh.values()), 0)

    def test_no_ma_ema_logic_in_source(self):
        s = PATH.read_text(encoding="utf-8")
        for forbidden in ('def sma(', 'def ema(', '"sma_cross"', '"ema_cross"', '"sma50"', '"ema50"'):
            self.assertNotIn(forbidden, s)

    def test_advisory_packet_not_production_authorized(self):
        now=1735711200000
        boundary=(now // scanner.STEP) * scanner.STEP
        asof=boundary+60_000
        result={"status":"OK", "signal":{
            "signal_id":"a"*64, "source_symbol":"TESTUSDT",
            "long_score":25, "short_score":0, "rsi14":39.9,
            "long_conditions":{"rsi_below40":{"met":True,"points":25}},
            "short_conditions":{"rsi_above80":{"met":False,"points":0}},
            "direction":"LONG_BIAS",
        }}
        with patch.object(scanner, "now_ms", return_value=asof):
            packet=scanner.make_packet(
                [result], [], "FUTURES", scanner.FUTURES, "/fapi/v1",
                asof, boundary, 1, 1, [],
            )
        self.assertFalse(packet["decision_input_allowed"])
        self.assertTrue(packet["shadow_test_required"])
        self.assertFalse(packet["execution_command"])
        self.assertEqual(packet["rule_version"],"4h_rsi14_40_80_last_closed_advisory_v1")
        self.assertEqual(packet["max_score"],75)
        self.assertEqual(len(packet["symbol_signals"]),1)

if __name__ == "__main__":
    unittest.main()
