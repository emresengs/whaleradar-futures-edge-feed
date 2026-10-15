#!/usr/bin/env python3
"""Offline contract regression for inbox, no execution and no writing."""
import copy
import datetime as dt
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).with_name("consumer_inbox_v1.py")
spec=importlib.util.spec_from_file_location("daily_inbox_v1",SOURCE)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

DAY=86400000
NOW=1800001200000
BOUNDARY=(NOW//DAY)*DAY
def z(ts):
    return dt.datetime.fromtimestamp(ts/1000,dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
def packet(rsi=50,rows=None):
    side="LONG" if rsi<40 else "SHORT" if rsi>80 else "NEUTRAL"
    p={
      "schema_version":"SUPERMAN_BTC_1D_RSI_ADVISORY_V1",
      "producer":"futures_1d_btc_confirm_rsi_v1",
      "rule_version":m.RULE,"execution_command":False,"decision_input_allowed":False,
      "shadow_test_required":True,"timeframe":"1d","source_mode":"FUTURES",
      "as_of_exchange_utc":z(BOUNDARY+5000),
      "generated_at_utc":z(BOUNDARY+12000),
      "evidence_timestamp_utc":z(BOUNDARY-1),
      "valid_until_utc":z(BOUNDARY+DAY),
      "btc_gate":{
         "rsi14":rsi, "source_market":"FUTURES","side":side,
         "symbol":"BTCUSDT","btc_confirmed_first":True,
         "candle_close_utc":z(BOUNDARY-1)
      },
      "symbol_signals":rows or []
    }
    p["scan_id"]=m.sha(p)
    return p
def row(sym,rsi,side):
    return {"source_symbol":sym,"base_asset":sym.removesuffix("USDT"),"rsi14":rsi,
      "source_market":"FUTURES","source_quote_asset":"USDT",
      "decision_bias":side,"source_candle_close_utc":z(BOUNDARY-1),
      "signal_id":"a"*64}

class InboxContracts(unittest.TestCase):
    def test_neutral_approves_empty(self):
        d=m.validate(packet(50),NOW)
        self.assertEqual(d["candidate_count"],0)
        self.assertEqual(d["status"],"NO_BTC_CONFIRMATION")
        self.assertFalse(d["orders_allowed"])
    def test_btc_long_matching_alt_only(self):
        d=m.validate(packet(39.5,[row("ETHUSDT",34,"LONG")]),NOW)
        self.assertEqual(d["btc"]["side"],"LONG")
        self.assertEqual(d["candidates"][0]["recommendation"],"LONG")
        self.assertFalse(d["candidates"][0]["futures_mapping_verified"])
    def test_btc_short_matching_alt_only(self):
        d=m.validate(packet(82,[row("SOLUSDT",81,"SHORT")]),NOW)
        self.assertEqual(d["candidates"][0]["recommendation"],"SHORT")
    def test_neutral_cannot_claim_advice(self):
        x=packet(50,[row("ETHUSDT",38,"LONG")])
        with self.assertRaises(ValueError):m.validate(x,NOW)
    def test_btc_direction_mismatch(self):
        x=packet(80)
        x["btc_gate"]["side"]="LONG"
        x["scan_id"]=m.sha({k:v for k,v in x.items() if k!="scan_id"})
        with self.assertRaises(ValueError):m.validate(x,NOW)
    def test_wrong_altcoin_rsi(self):
        x=packet(39,[row("ETHUSDT",41,"LONG")])
        with self.assertRaises(ValueError):m.validate(x,NOW)
    def test_corrupted_scan_hash(self):
        x=packet(39,[row("ETHUSDT",37,"LONG")])
        x["scan_id"]="0"*64
        with self.assertRaises(ValueError):m.validate(x,NOW)
    def test_ma_ema_bypass_rejected(self):
        x=packet(39,[dict(row("ETHUSDT",37,"LONG"),sma50=1.2)])
        with self.assertRaises(ValueError):m.validate(x,NOW)
    def test_stale_packet_fails(self):
        with self.assertRaises(ValueError):m.validate(packet(39),NOW+DAY)
    def test_execution_switch_refused(self):
        x=packet(39)
        x["execution_command"]=True
        x["scan_id"]=m.sha({k:v for k,v in x.items() if k!="scan_id"})
        with self.assertRaises(ValueError):m.validate(x,NOW)

if __name__=="__main__":
    unittest.main(verbosity=2)
