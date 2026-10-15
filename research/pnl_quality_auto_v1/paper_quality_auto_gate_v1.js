'use strict';
/*
 * Superman Paper Quality Auto Gate V1.
 * Reads *local* time-limited scientific promotion only. Does not place orders.
 * Never runs in LiveTrader; never modifies existing open positions.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const SCHEMA = 'SUPERMAN_PAPER_QUALITY_AUTOPILOT_V1';
const ALLOWED_ARMS = new Set(['cooldown','flow_quality','short_quality','combined']);
const MAX_TTL_MS = 45 * 60 * 1000;
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

function verifyPolicy(raw, now = Date.now()) {
  if (!raw || raw.schema !== SCHEMA || typeof raw.sha256 !== 'string') return null;
  const obj = raw.policy;
  if (!obj || obj.schema !== SCHEMA || obj.status !== 'PAPER_PILOT_ENABLED'
      || !ALLOWED_ARMS.has(obj.arm) || obj.executionMode !== 'paper'
      || obj.liveTradingEnabled !== false || obj.researchCohort !== 'future_only_holdout'
      || obj.scienceResultValidated !== true || obj.killSwitch !== false
      || !/^[a-f0-9]{64}$/.test(obj.frozenSpecSha256 || '')) return null;
  const hash = crypto.createHash('sha256').update(JSON.stringify(obj)).digest('hex');
  if (hash !== raw.sha256) return null;
  const start = Date.parse(obj.activatedUtc), made = Date.parse(obj.issuedUtc), until = Date.parse(obj.expiresUtc);
  if (![start, made, until].every(Number.isFinite)
      || start > now || made > now + 120000 || made < now - MAX_TTL_MS
      || until < now || until <= made || until - made > MAX_TTL_MS) return null;
  const params = obj.parameters;
  if (!params || !Number.isSafeInteger(params.cooldown_after_realized_sl_minutes)
      || params.cooldown_after_realized_sl_minutes < 10 || params.cooldown_after_realized_sl_minutes > 120
      || !(num(params.min_trade_flow_usdt) > 0)
      || !(num(params.max_spread_ratio) > 0 && num(params.max_spread_ratio) < 0.01)
      || !(num(params.min_book_imbalance_abs) >= 0 && num(params.min_book_imbalance_abs) <= 1)
      || !(num(params.min_short_sell_ratio) > 0.5 && num(params.min_short_sell_ratio) <= 1)
      || !(num(params.max_short_book_imbalance) >= -1 && num(params.max_short_book_imbalance) <= 0)) return null;
  return obj;
}

function gateDecision(signal, closed, policy, now) {
  if (!policy) return { allow: true, reason: 'NO_VALID_POLICY' };
  if (!signal || !['LONG','SHORT'].includes(signal.side) || !signal.symbol || !Number.isFinite(now)) {
    return { allow: false, reason: 'BAD_PAPER_SIGNAL' };
  }
  const p = policy.parameters;
  const filtered = {cooldown: false, flow_quality: false, short_quality: false, combined: false};
  if (policy.arm === 'cooldown' || policy.arm === 'combined') {
    // Only fully settled historical closes at or before the signal timestamp.
    const recentSL = (closed || []).some(t => t && t.symbol === signal.symbol
      && t.exitReason === 'SL' && Number.isFinite(t.closedAt)
      && t.closedAt <= now && now - t.closedAt < p.cooldown_after_realized_sl_minutes * 60000);
    filtered.cooldown = recentSL;
  }
  const m = signal.metrics || {};
  const spread = num(m.spreadPct);
  const volume = num(m.totalUsd);
  const book = num(m.orderBook?.avg);
  const sell = num(m.sellRatio);
  if (policy.arm === 'flow_quality' || policy.arm === 'combined') {
    const signBook = signal.side === 'LONG' ? book : book === null ? null : -book;
    filtered.flow_quality = !(spread !== null && volume !== null && book !== null && signBook !== null
      && spread >= 0 && spread <= p.max_spread_ratio
      && volume >= p.min_trade_flow_usdt && signBook >= p.min_book_imbalance_abs);
  }
  if (policy.arm === 'short_quality' || policy.arm === 'combined') {
    filtered.short_quality = signal.side === 'SHORT' && !(sell !== null && book !== null
      && sell >= p.min_short_sell_ratio && book <= p.max_short_book_imbalance);
  }
  const denied = Object.entries(filtered).filter(([, reject]) => reject).map(([name]) => name);
  return denied.length ? { allow: false, reason: denied.join('+') } : { allow: true, reason: 'PAPER_PROMOTED_GATE_PASS' };
}

class PaperQualityAutoGateV1 {
  constructor(config) {
    if (!config || config.executionMode !== 'paper') throw new Error('QUALITY_GATE_PAPER_ONLY');
    this.config = config;
    this.policyFile = path.join(config.dataDir, 'paper_quality_auto_v1', 'active_policy.json');
    this.auditFile = path.join(config.dataDir, 'paper_quality_auto_v1', 'decisions.jsonl');
    this.lastPolicy = null;
    this.lastReadAt = 0;
  }
  currentPolicy() {
    const now = Date.now();
    if (now - this.lastReadAt > 10000) {
      this.lastReadAt = now;
      try { this.lastPolicy = verifyPolicy(JSON.parse(fs.readFileSync(this.policyFile, 'utf8')), now); }
      catch (_) { this.lastPolicy = null; }
    }
    // Expired policy cannot survive the cache interval.
    if (this.lastPolicy && Date.parse(this.lastPolicy.expiresUtc) < now) this.lastPolicy = null;
    return this.lastPolicy;
  }
  allow(signal, closed) {
    const policy = this.currentPolicy();
    const result = gateDecision(signal, closed, policy, Number(signal?.ts) || Date.now());
    if (policy && !result.allow) {
      try {
        fs.appendFileSync(this.auditFile, JSON.stringify({
          ts: Date.now(),symbol: signal.symbol,side: signal.side,arm: policy.arm,
          policyActivatedUtc: policy.activatedUtc,reason:result.reason,
          orderPlaced:false,mode:'paper'
        })+'\n',{mode:0o600});
      } catch (_) {}
    }
    return result.allow;
  }
}
module.exports = { PaperQualityAutoGateV1, verifyPolicy, gateDecision, SCHEMA };
