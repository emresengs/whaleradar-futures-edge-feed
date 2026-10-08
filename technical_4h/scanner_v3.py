#!/usr/bin/env python3
"""Public Binance 4H scanner; canonical SUPERMAN_TECHNICAL_BIAS_V1 producer.

No orders, keys, or account endpoints. CLI output is a packet, not a trade.
"""
import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

STEP = 14_400_000
RULE = "4h_equal25_max75_rsi40_80_last_closed_v2"
MAX_SCORE = 75
MIN_SCORE = 25
SPOT = "https://data-api.binance.vision"
FUTURES = "https://fapi.binance.com"
QUOTES = ["USDT", "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "RLUSD", "PYUSD"]
STABLE = set(QUOTES + ["USDS", "AEUR", "EUR", "EURI"])
_gate = threading.Lock()
_next_request = {}
_retry_until = {}


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_ms():
    return time.time_ns() // 1_000_000


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def log(**values):
    print(json.dumps(values, ensure_ascii=False), flush=True)


def throttle(origin):
    while True:
        with _gate:
            now = time.monotonic()
            wait = max(_next_request.get(origin, 0), _retry_until.get(origin, 0)) - now
            if wait <= 0:
                _next_request[origin] = now + 0.25
                return
        time.sleep(min(wait, 1))


def get_json(base, endpoint, params=None):
    url = base + endpoint
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        throttle(base)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Futures-4H-Scanner/2.0", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=20) as response:
                limit = (64 if endpoint.endswith("/exchangeInfo") else 8) * 1024 * 1024
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise ValueError("RESPONSE_TOO_LARGE")
            return json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code in (403, 451):
                raise
            if error.code in (418, 429):
                value = error.headers.get("Retry-After", "300")
                try:
                    seconds = float(value)
                except ValueError:
                    from email.utils import parsedate_to_datetime
                    try:
                        seconds = parsedate_to_datetime(value).timestamp() - time.time()
                    except (ValueError, TypeError):
                        seconds = 300
                if not math.isfinite(seconds):
                    seconds = 300
                with _gate:
                    _retry_until[base] = max(_retry_until.get(base, 0), time.monotonic() + max(0, seconds))
                # Stop this symbol; all subsequent requests honor the origin-wide deadline.
                raise
            if error.code < 500 or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(1 + attempt)
    raise RuntimeError("REQUEST_FAILED")


def sma(values, length, end):
    return sum(values[end - length + 1:end + 1]) / length


def ema(values, length):
    out = [None] * len(values)
    if len(values) < length:
        return out
    value = sum(values[:length]) / length
    out[length - 1] = value
    alpha = 2 / (length + 1)
    for index in range(length, len(values)):
        value += alpha * (values[index] - value)
        out[index] = value
    return out


def rsi(values, length=14):
    out = [None] * len(values)
    if len(values) <= length:
        return out
    delta = [values[i] - values[i - 1] for i in range(1, length + 1)]
    gain = sum(max(d, 0) for d in delta) / length
    loss = sum(max(-d, 0) for d in delta) / length

    def score():
        if gain == loss == 0:
            return 50.0
        if loss == 0:
            return 100.0
        return 100 - 100 / (1 + gain / loss)

    out[length] = score()
    for index in range(length + 1, len(values)):
        d = values[index] - values[index - 1]
        gain = (gain * (length - 1) + max(d, 0)) / length
        loss = (loss * (length - 1) + max(-d, 0)) / length
        out[index] = score()
    return out


def candles_from_raw(raw, asof, boundary):
    if not isinstance(raw, list):
        raise ValueError("KLINES_NOT_ARRAY")
    # Internal fields: openTime, closeTime, open, high, low, close, volume.
    candles = [[int(k[0]), int(k[6]), *map(float, k[1:6])] for k in raw]
    if not candles:
        return candles
    opens = [c[0] for c in candles]
    if any(t % STEP for t in opens) or any(opens[i] - opens[i - 1] != STEP for i in range(1, len(opens))):
        raise ValueError("CANDLE_GAP_OR_DUPLICATE_OR_ALIGNMENT")
    for candle in candles:
        start, end, o, h, low, close, volume = candle
        if end != start + STEP - 1 or end >= asof or end >= boundary:
            raise ValueError("OPEN_OR_INVALID_CLOSE_TIME")
        if not all(math.isfinite(x) for x in (o, h, low, close, volume)) or min(o, h, low, close) <= 0 or volume < 0:
            raise ValueError("INVALID_OHLCV")
        if h < max(o, low, close) or low > min(o, h, close):
            raise ValueError("INCONSISTENT_OHLC")
    if candles[-1][1] != boundary - 1:
        raise ValueError("LAST_CLOSED_CANDLE_MISMATCH")
    return candles


def condition(met):
    return {"met": bool(met), "points": 25 if met else 0}


def score_conditions(previous, latest):
    """Only a new cross on the last closed candle earns MA/EMA points."""
    lc = {"sma_cross": condition(previous["sma50"] <= previous["sma200"] and latest["sma50"] > latest["sma200"]),
          "ema_cross": condition(previous["ema50"] <= previous["ema200"] and latest["ema50"] > latest["ema200"]),
          "rsi_below40": condition(latest["rsi14"] < 40)}
    sc = {"sma_cross": condition(previous["sma50"] >= previous["sma200"] and latest["sma50"] < latest["sma200"]),
          "ema_cross": condition(previous["ema50"] >= previous["ema200"] and latest["ema50"] < latest["ema200"]),
          "rsi_above80": condition(latest["rsi14"] > 80)}
    return lc, sc


def analyze(meta, market, base, prefix, asof, boundary):
    raw = get_json(base, prefix + "/klines", {"symbol": meta["symbol"], "interval": "4h", "limit": 1000, "endTime": boundary - 1})
    candles = candles_from_raw(raw, asof, boundary)
    if len(candles) < 600:
        return {"status": "INSUFFICIENT_HISTORY", "symbol": meta["symbol"], "candles": len(candles)}
    values = [c[5] for c in candles]
    last, previous = len(values) - 1, len(values) - 2
    a50, a200, strength = ema(values, 50), ema(values, 200), rsi(values)
    prev = {"close": values[previous], "sma50": sma(values, 50, previous), "sma200": sma(values, 200, previous), "ema50": a50[previous], "ema200": a200[previous]}
    sm50, sm200 = sma(values, 50, last), sma(values, 200, last)
    lc, sc = score_conditions(prev, {"sma50": sm50, "sma200": sm200,
                                    "ema50": a50[last], "ema200": a200[last], "rsi14": strength[last]})
    lscore, sscore = sum(c["points"] for c in lc.values()), sum(c["points"] for c in sc.values())
    conflict = lscore >= MIN_SCORE and sscore >= MIN_SCORE
    side = "NEUTRAL" if conflict else "LONG" if lscore >= MIN_SCORE else "SHORT" if sscore >= MIN_SCORE else "BELOW_THRESHOLD"
    booleans = [c["met"] for c in [*lc.values(), *sc.values()]]
    row = {"source_market": market, "source_symbol": meta["symbol"], "source_quote_asset": meta["quoteAsset"], "base_asset": meta["baseAsset"],
           "execution_market": "FUTURES", "futures_symbol": meta["symbol"] if market == "FUTURES" else None,
           "mapping_status": "REQUIRES_CONSUMER_VALIDATION", "stablecoin_pair": meta["baseAsset"] in STABLE,
           "source_candle_close_utc": iso(boundary - 1), "source_close": values[last], "candles": len(candles),
           "sma50": sm50, "sma200": sm200, "ema50": a50[last], "ema200": a200[last], "rsi14": strength[last], "previous": prev,
           "long_conditions": lc, "short_conditions": sc, "long_score": lscore, "short_score": sscore,
           "direction": "CONFLICT_NEUTRAL" if conflict else side + "_BIAS" if side in ("LONG", "SHORT") else "BELOW_THRESHOLD",
           "decision_bias": side if side in ("LONG", "SHORT") else "NEUTRAL", "conflict": conflict,
           "bias_strength": max(lscore, sscore) / MAX_SCORE, "signal_id": sha([market, meta["symbol"], boundary - 1, RULE, *booleans]),
           "valid_until_utc": iso(boundary + STEP), "source_url": base + prefix + "/klines",
           "spot_derived_futures_bias": market == "SPOT"}
    return {"status": "OK", "symbol": meta["symbol"], "signal": row}


def discover(mode):
    notes = []
    if mode != "spot":
        try:
            info = get_json(FUTURES, "/fapi/v1/exchangeInfo")
            asof = int(get_json(FUTURES, "/fapi/v1/time")["serverTime"])
            selected = [s for s in info["symbols"] if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"]
            if not selected:
                raise ValueError("EMPTY_FUTURES_UNIVERSE")
            return "FUTURES", FUTURES, "/fapi/v1", asof, selected, [], len(selected), notes
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as error:
            notes.append("USD-M public data unavailable; authorized spot fallback: " + type(error).__name__ + ":" + str(error)[:120])
    info = get_json(SPOT, "/api/v3/exchangeInfo")
    asof = int(get_json(SPOT, "/api/v3/time")["serverTime"])
    active = [s for s in info["symbols"] if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", True)]
    selected, unsupported = [], []
    for asset in sorted({s["baseAsset"] for s in active}):
        options = [s for s in active if s["baseAsset"] == asset and s["quoteAsset"] in QUOTES]
        if options:
            selected.append(min(options, key=lambda s: (QUOTES.index(s["quoteAsset"]), s["symbol"])))
        else:
            unsupported.append({"symbol": asset, "status": "UNSUPPORTED_QUOTE"})
    if not selected:
        raise ValueError("EMPTY_SPOT_UNIVERSE")
    return "SPOT", SPOT, "/api/v3", asof, selected, unsupported, len({s["baseAsset"] for s in active}), notes


def make_packet(results, failures, market, base, prefix, asof, boundary, selected_count, base_count, notes, previous=None):
    generated = now_ms()
    if generated // STEP != boundary // STEP or generated - asof > 7_200_000 or abs(asof - generated) > 7_200_000:
        raise ValueError("SCAN_CROSSED_BOUNDARY_OR_TOO_OLD")
    calculated = [r for r in results if r["status"] == "OK"]
    exclusions = [r for r in results if r["status"] != "OK"]
    candidates = [r["signal"] for r in calculated if max(r["signal"]["long_score"], r["signal"]["short_score"]) >= MIN_SCORE]
    candidates.sort(key=lambda r: (-max(r["long_score"], r["short_score"]), r["direction"], r["source_symbol"]))
    current = {r["signal_id"] for r in candidates}
    previous_ids = {r.get("signal_id") for r in (previous or {}).get("symbol_signals", [])}
    errors = [f for f in failures if f["status"] == "DATA_UNAVAILABLE"]
    status = "PARTIAL" if errors else "COMPLETE_WITH_EXCLUSIONS" if exclusions or failures else "COMPLETE"
    packet = {"schema_version": "SUPERMAN_TECHNICAL_BIAS_V1", "producer": "futures_4h_edge_scanner_v1", "rule_version": RULE,
              "scanner_implementation_version": "3.0.0", "max_score": MAX_SCORE, "minimum_candidate_score": MIN_SCORE,
              "generated_at_utc": iso(generated), "fresh_scan_timestamp": iso(generated),
              "as_of_exchange_utc": iso(asof), "evidence_timestamp_utc": iso(boundary - 1), "valid_until_utc": iso(boundary + STEP),
              "scan_status": status, "source_mode": market, "decision_input_allowed": True, "shadow_test_required": False, "execution_command": False,
              "symbol_signals": candidates, "failures": sorted([*failures, *exclusions], key=lambda f: f["symbol"]),
              "expired_or_retracted_signal_ids": sorted(s for s in previous_ids - current if isinstance(s, str) and len(s) == 64),
              "universe_counts": {"active_base_assets": base_count, "selected_pairs": selected_count, "attempted": len(results) + len(errors),
                                  "calculated": len(calculated), "insufficient_history": len(exclusions), "data_errors": len(errors),
                                  "unsupported_quote": sum(f["status"] == "UNSUPPORTED_QUOTE" for f in failures)},
              "coverage": {"selected_pairs_calculated_pct": round(100 * len(calculated) / selected_count, 4),
                           "source_market": market, "futures_coverage_verified": market == "FUTURES",
                           "scope": "ALL_ACTIVE_USDT_PERPETUALS" if market == "FUTURES" else "ALL_ACTIVE_SPOT_BASES_WITH_AVAILABLE_USD_STABLE_QUOTE"},
              "source_urls": [base + prefix + p for p in ("/exchangeInfo", "/time", "/klines")], "notes": notes,
              "delivery_status": "DELIVERY_UNCONFIRMED", "decision_effect_status": "DECISION_EFFECT_UNCONFIRMED", "integration_status": "RULE_UPDATE_REQUIRES_CONSUMER_V3"}
    packet["scan_id"] = sha(packet)
    return packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--previous")
    parser.add_argument("--mode", choices=("auto", "spot"), default="auto")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    market, base, prefix, asof, selected, unsupported, base_count, notes = discover(args.mode)
    if abs(now_ms() - asof) > 120_000:
        raise ValueError("EXCHANGE_OR_LOCAL_CLOCK_SKEW")
    boundary = asof // STEP * STEP
    log(phase="universe", source_market=market, asof=iso(asof), selected=len(selected), unsupported=len(unsupported))
    results, failures = [], list(unsupported)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, args.workers))) as pool:
        jobs = {pool.submit(analyze, s, market, base, prefix, asof, boundary): s for s in selected}
        for count, job in enumerate(concurrent.futures.as_completed(jobs), 1):
            meta = jobs[job]
            try:
                results.append(job.result())
            except Exception as error:
                failures.append({"symbol": meta["symbol"], "status": "DATA_UNAVAILABLE", "reason": type(error).__name__ + ":" + str(error)[:180]})
            if count % 50 == 0 or count == len(jobs):
                log(phase="progress", done=count, total=len(jobs), data_errors=sum(f["status"] == "DATA_UNAVAILABLE" for f in failures))
    previous = None
    if args.previous:
        previous = json.loads(Path(args.previous).read_text())
    packet = make_packet(results, failures, market, base, prefix, asof, boundary, len(selected), base_count, notes, previous)
    target = Path(args.out).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + "." + str(os.getpid()) + ".tmp")
    try:
        temp.write_text(json.dumps(packet, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    log(phase="complete", packet=str(target), scan_id=packet["scan_id"], status=packet["scan_status"], counts=packet["universe_counts"],
        candidates=[{"symbol":r["source_symbol"],"direction":r["direction"],"long":r["long_score"],"short":r["short_score"]} for r in packet["symbol_signals"]])


if __name__ == "__main__":
    main()
