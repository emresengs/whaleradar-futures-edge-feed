'use strict';
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const {verifyPolicy,gateDecision,SCHEMA,PaperQualityAutoGateV1}=require('./paper_quality_auto_gate_v1');
const now=Date.parse('2026-10-30T12:00:00Z');
const parameters={
  cooldown_after_realized_sl_minutes:45,
  min_trade_flow_usdt:300000,
  max_spread_ratio:0.0003,
  min_book_imbalance_abs:0.08,
  min_short_sell_ratio:0.75,
  max_short_book_imbalance:-0.1
};
const policy={
 schema:SCHEMA,status:'PAPER_PILOT_ENABLED',arm:'short_quality',executionMode:'paper',
 liveTradingEnabled:false,researchCohort:'future_only_holdout',scienceResultValidated:true,
 frozenSpecSha256:'a'.repeat(64),killSwitch:false,
 activatedUtc:'2026-10-29T12:00:00Z',issuedUtc:'2026-10-30T11:58:00Z',
 expiresUtc:'2026-10-30T12:28:00Z',parameters,authorization:'USER_STANDING_APPROVAL_2026-10-15_PAPER_ONLY'
};
const wrapper={schema:SCHEMA,policy,sha256:crypto.createHash('sha256').update(JSON.stringify(policy)).digest('hex')};
assert.ok(verifyPolicy(wrapper,now));
assert.equal(verifyPolicy({...wrapper,sha256:'b'.repeat(64)},now),null);
assert.equal(verifyPolicy(wrapper,now+45*60*1000),null);
assert.equal(verifyPolicy({...wrapper,policy:{...policy,liveTradingEnabled:true}},now),null);
assert.throws(()=>new PaperQualityAutoGateV1({executionMode:'live'}),/PAPER_ONLY/);
function signal(side,ts,spread=.0001,volume=500000,book=side==='SHORT'?-0.2:0.2,sell=.80){
 return {symbol:'STRKUSDT',side,ts,metrics:{spreadPct:spread,totalUsd:volume,orderBook:{avg:book},sellRatio:sell}};
}
assert.equal(gateDecision(signal('SHORT',now),[],policy,now).allow,true);
assert.equal(gateDecision(signal('SHORT',now,.0001,500000,-.2,.7),[],policy,now).allow,false);
assert.equal(gateDecision(signal('LONG',now),[],policy,now).allow,true);
assert.equal(gateDecision(signal('SHORT',now),[],null,now).allow,true);
const c={...policy,arm:'cooldown'};
const prior=[{symbol:'STRKUSDT',exitReason:'SL',closedAt:now-10*60*1000}];
assert.equal(gateDecision(signal('SHORT',now),prior,c,now).allow,false);
assert.equal(gateDecision(signal('SHORT',now-3600*1000),prior,c,now-3600*1000).allow,true);
const future=[{symbol:'STRKUSDT',exitReason:'SL',closedAt:now+1000}];
assert.equal(gateDecision(signal('SHORT',now),future,c,now).allow,true);
const flow={...policy,arm:'flow_quality'};
assert.equal(gateDecision(signal('SHORT',now,.005),[],flow,now).allow,false);
assert.equal(gateDecision(signal('SHORT',now,.0001,500000,-.3),[],flow,now).allow,true);
const combo={...policy,arm:'combined'};
assert.equal(gateDecision(signal('SHORT',now,.0001,500000,-.2,.79),[],combo,now).allow,true);
assert.equal(gateDecision(signal('SHORT',now,.0001,500000,-.2,.70),[],combo,now).allow,false);
console.log('PAPER_QUALITY_AUTO_GATE_15_CHECKS_OK');
