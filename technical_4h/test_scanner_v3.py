"""Behavioral checks for the requested closed-candle scoring contract."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import scanner_v3 as s

ASOF = 1791463500000 // s.STEP * s.STEP + 60_000
BOUNDARY = ASOF // s.STEP * s.STEP
META = {'symbol': 'BTCUSDT', 'baseAsset': 'BTC', 'quoteAsset': 'USDT'}

def averages(sma50=99, sma200=100, ema50=99, ema200=100, rsi14=50):
    return dict(sma50=sma50, sma200=sma200, ema50=ema50, ema200=ema200, rsi14=rsi14)

def raw_candles(values):
    return [[BOUNDARY - (len(values) - i) * s.STEP, v, v + 1, v - 1, v, 10,
             BOUNDARY - (len(values) - i - 1) * s.STEP - 1] for i, v in enumerate(values)]

def analyze(values):
    with patch.object(s, 'get_json', return_value=raw_candles(values)) as request:
        result = s.analyze(META, 'SPOT', s.SPOT, '/api/v3', ASOF, BOUNDARY)
    assert request.call_args.args[2]['endTime'] == BOUNDARY - 1
    return result

class ScoringTests(unittest.TestCase):
    def test_new_upward_crosses_only(self):
        for key in ('sma', 'ema'):
            for prior in (99, 100):
                previous, latest = averages(), averages()
                previous[key + '50'], latest[key + '50'] = prior, 101
                long, short = s.score_conditions(previous, latest)
                self.assertEqual(long[key + '_cross']['points'], 25)
                self.assertEqual(short[key + '_cross']['points'], 0)

    def test_already_above_does_not_score(self):
        long, _ = s.score_conditions(averages(101, 100, 101, 100), averages(102, 100, 102, 100))
        self.assertEqual(long['sma_cross']['points'] + long['ema_cross']['points'], 0)

    def test_new_downward_crosses_only(self):
        for key in ('sma', 'ema'):
            for prior in (100, 101):
                previous, latest = averages(), averages()
                previous[key + '50'], latest[key + '50'] = prior, 99
                long, short = s.score_conditions(previous, latest)
                self.assertEqual(short[key + '_cross']['points'], 25)
                self.assertEqual(long[key + '_cross']['points'], 0)

    def test_already_below_does_not_score(self):
        _, short = s.score_conditions(averages(), averages(98, 100, 98, 100))
        self.assertEqual(short['sma_cross']['points'] + short['ema_cross']['points'], 0)

    def test_current_equality_does_not_score(self):
        for prior in (averages(), averages(101, 100, 101, 100)):
            long, short = s.score_conditions(prior, averages(100, 100, 100, 100))
            self.assertEqual(long['sma_cross']['points'] + long['ema_cross']['points'], 0)
            self.assertEqual(short['sma_cross']['points'] + short['ema_cross']['points'], 0)

    def test_rsi_strict_boundaries_and_sides(self):
        for value, lp, sp in [(0,25,0),(39.999,25,0),(40,0,0),(50,0,0),(80,0,0),(80.001,0,25),(90,0,25),(100,0,25)]:
            long, short = s.score_conditions(averages(), averages(rsi14=value))
            self.assertEqual(long['rsi_below40']['points'], lp)
            self.assertEqual(short['rsi_above80']['points'], sp)
            self.assertEqual(len(long), 3)
            self.assertEqual(len(short), 3)

    def test_maximum75_without_trend(self):
        for value, side in [(35,0),(85,1)]:
            previous = averages() if side == 0 else averages(101,100,101,100)
            latest = averages(101,100,101,100,value) if side == 0 else averages(rsi14=value)
            conditions = s.score_conditions(previous, latest)
            self.assertEqual(sum(c['points'] for c in conditions[side].values()), 75)
            self.assertNotIn('trend_break', conditions[side])

    def test_real_indicator_pipeline_long25(self):
        result = analyze([200 - i * 0.1 for i in range(1000)])
        self.assertEqual(result['status'], 'OK')
        r = result['signal']
        self.assertEqual((r['long_score'], r['short_score'], r['direction']), (25,0,'LONG_BIAS'))
        self.assertEqual(r['bias_strength'], 25/75)
        self.assertEqual(r['source_candle_close_utc'], s.iso(BOUNDARY-1))

    def test_real_indicator_pipeline_short25(self):
        r = analyze([100 + i * 0.1 for i in range(1000)])['signal']
        self.assertEqual((r['long_score'], r['short_score'], r['direction']), (0,25,'SHORT_BIAS'))

    def test_conflicting25_scores_are_neutral(self):
        r = analyze([100] * 999 + [110])['signal']
        self.assertEqual((r['long_score'], r['short_score']), (50,25))
        self.assertTrue(r['conflict'])
        self.assertEqual(r['decision_bias'], 'NEUTRAL')

    def test_below25_is_not_published(self):
        result = analyze([100] * 1000)
        with patch.object(s, 'now_ms', return_value=ASOF):
            p = s.make_packet([result], [], 'SPOT', s.SPOT, '/api/v3', ASOF, BOUNDARY, 1, 1, [])
        self.assertEqual(p['symbol_signals'], [])

    def test_25_is_published_and_old_id_retracted(self):
        result = analyze([200 - i * 0.1 for i in range(1000)])
        with patch.object(s, 'now_ms', return_value=ASOF):
            p = s.make_packet([result], [], 'SPOT', s.SPOT, '/api/v3', ASOF, BOUNDARY, 1, 1, [], {'symbol_signals':[{'signal_id':'a'*64}]})
        self.assertEqual(len(p['symbol_signals']), 1)
        self.assertEqual(p['max_score'], 75)
        self.assertEqual(p['minimum_candidate_score'], 25)
        self.assertEqual(p['expired_or_retracted_signal_ids'], ['a'*64])
        self.assertEqual(p['symbol_signals'][0]['signal_id'], analyze([200-i*.1 for i in range(1000)])['signal']['signal_id'])

    def test_history_minimum(self):
        self.assertEqual(analyze([100]*599)['status'], 'INSUFFICIENT_HISTORY')
        self.assertEqual(analyze([100]*600)['status'], 'OK')

    def test_candle_data_rejections(self):
        for bad in ('open', 'gap', 'ohlc'):
            raw = raw_candles([100]*600)
            if bad == 'open': raw[-1][6] += s.STEP
            elif bad == 'gap': raw[20][0] += s.STEP
            else: raw[-1][2] = 99
            with self.assertRaises(ValueError): s.candles_from_raw(raw, ASOF, BOUNDARY)

if __name__ == '__main__':
    unittest.main(verbosity=2)
