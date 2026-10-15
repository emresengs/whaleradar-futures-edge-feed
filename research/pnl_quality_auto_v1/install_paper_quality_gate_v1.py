#!/usr/bin/env python3
"""Guarded one-time installer: connect PaperTrader to a time-limited Shadow gate.

Runs at most once, only with zero open Paper positions. Stops only Paper service,
checks durable checkpoint after stop, preserves original files and restores on error.
No live/testnet promotion or changes to risk sizing, Guardian, R&D or other timers.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT=Path("/opt/futures-flow-bot")
SRC=ROOT/"src/engine/paper_trader.js"
STAGED=ROOT/"tools/pnl_quality_auto_v1_20261015/paper_quality_auto_gate_v1.js"
TARGET=ROOT/"src/engine/paper_quality_auto_gate_v1.js"
PATCH_MARK=ROOT/"data/paper_quality_auto_v1/ACTIVATED.json"
DAY_STATE=ROOT/"data/paper_daily_v1/state.json"
ENVFILES=[ROOT/".env",Path("/etc/futures-flow-bot-paper.env")]
BACKUP_ROOT=Path("/root/superman_backups")
MARK="PAPER_QUALITY_AUTO_GATE_ENABLED=true"

def once(s,old,new):
    if s.count(old)!=1:
        raise ValueError("paper source anchor mismatch: "+old[:75])
    return s.replace(old,new,1)

def patch_paper_source(source):
    source=once(source,
        "      this.dailyCycle.start();\n    }\n  }\n\n  _positionNotional()",
        "      this.dailyCycle.start();\n    }\n"
        "    this.qualityGate = null;\n"
        "    if (config.executionMode === 'paper' && process.env.PAPER_QUALITY_AUTO_GATE_ENABLED === 'true') {\n"
        "      const { PaperQualityAutoGateV1 } = require('./paper_quality_auto_gate_v1');\n"
        "      this.qualityGate = new PaperQualityAutoGateV1(config);\n"
        "    }\n  }\n\n  _positionNotional()")
    source=once(source,
        "    if (this.dailyStopped) return;\n    if (this.open.length >= this.config.maxOpenPositions) return;",
        "    if (this.dailyStopped) return;\n"
        "    if (this.qualityGate && !this.qualityGate.allow(signal, this.closed)) return;\n"
        "    if (this.open.length >= this.config.maxOpenPositions) return;")
    source=once(source,
        "      dailyCycle: this.dailyCycle ? this.dailyCycle.status() : null,",
        "      dailyCycle: this.dailyCycle ? this.dailyCycle.status() : null,\n"
        "      qualityAutoGateEnabled: !!this.qualityGate,")
    return source

def run(*args,timeout=20):
    return subprocess.run(args,check=True,stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,timeout=timeout,text=True).stdout.strip()

def health():
    with urllib.request.urlopen("http://127.0.0.1:8797/healthz",timeout=4) as response:
        h=json.load(response)
    if h.get("mode")!="paper" or h.get("ok") is not True:
        raise ValueError("Paper health not verified")
    return h

def env_safe():
    current=[x.read_text() for x in ENVFILES]
    for body in current:
        if "EXECUTION_MODE=paper" not in body or "LIVE_TRADING_ENABLED=false" not in body:
            raise ValueError("paper environment guard")
        if "PAPER_QUALITY_AUTO_GATE_ENABLED=" in body:
            raise ValueError("Quality gate env already present")
    return current

def sha_file(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def flat_checkpoint(equity):
    data=json.loads(DAY_STATE.read_text())
    opens=data.get("open")
    if not isinstance(opens,list) or opens:return False
    e=float(data.get("balance",float("nan")))+float(data.get("reserveProfit",float("nan")))
    return abs(e-equity)<=0.03

def main():
    if "--self-test" in sys.argv:
        print("RUN_TEST_IMPORT_PATCH_SOURCE_FUNCTION")
        return
    if PATCH_MARK.exists():
        print("PAPER_QUALITY_AUTO_GATE_ALREADY_ACTIVATED")
        return
    if not STAGED.is_file() or not SRC.is_file() or not DAY_STATE.is_file():
        raise ValueError("Missing staged gate, source or ledger")
    body=SRC.read_text(encoding="utf8")
    patched=patch_paper_source(body)
    current=env_safe()
    run("node","--check",str(STAGED))
    before=health()
    b=before["report"]
    if int(b["open"])!=0:
        print("PAPER_QUALITY_INSTALL_WAIT_OPEN_POSITIONS",b["open"])
        return
    equity=float(b["totalEquity"])
    original_pid=run("systemctl","show","futures-flow-bot","-p","MainPID","--value")
    # Stopping is essential to close the entry race before validating durable open[].
    run("systemctl","stop","futures-flow-bot")
    stopped=True
    patched_files=False
    backup=None
    try:
        if not flat_checkpoint(equity):
            print("PAPER_QUALITY_INSTALL_DEFER_CHECKPOINT_CHANGED")
            return
        stamp=dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup=BACKUP_ROOT/("quality_auto_v1_"+stamp)
        backup.mkdir(parents=True,mode=0o700,exist_ok=False)
        shutil.copy2(SRC,backup/"paper_trader.js")
        shutil.copy2(ENVFILES[0],backup/"project.env")
        shutil.copy2(ENVFILES[1],backup/"service.env")
        PATCH_MARK.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        shutil.copy2(STAGED,TARGET)
        SRC.write_text(patched,encoding="utf8")
        for path,text in zip(ENVFILES,current):
            path.write_text(text.rstrip("\n")+"\n"+MARK+"\n",encoding="utf8")
        patched_files=True
        run("node","--check",str(SRC))
        run("node","--check",str(TARGET))
        run("systemctl","start","futures-flow-bot",timeout=25)
        time.sleep(3)
        after=health()
        r=after["report"]
        if r.get("qualityAutoGateEnabled") is not True:
            raise ValueError("Paper process did not load gate")
        if abs(float(r["totalEquity"])-equity)>.03:
            raise ValueError("Capital changed during passive gate installation")
        if int(r["open"])!=0:
            raise ValueError("Unexpected open positions immediately after restart")
        receipt={
            "status":"PASSIVE_GATE_ACTIVATED","mode":"paper",
            "liveTradingEnabled":False,"installedUtc":dt.datetime.now(dt.timezone.utc).isoformat(),
            "pidBefore":original_pid,"pidAfter":run("systemctl","show","futures-flow-bot","-p","MainPID","--value"),
            "equityBefore":equity,"equityAfter":float(r["totalEquity"]),
            "backup":str(backup),"sourceSha256":sha_file(SRC),
            "gateSha256":sha_file(TARGET),
            "noPromotionYet":True,"guardianRestarted":False,"scientificHistoryDeleted":False,
            "maxRiskChanged":False,"authorization":"USER_STANDING_APPROVAL_2026-10-15_PAPER_ONLY"
        }
        PATCH_MARK.write_text(json.dumps(receipt,indent=2)+"\n",encoding="utf8")
        os.chmod(PATCH_MARK,0o600)
        print("PAPER_QUALITY_SAFE_GATE_ACTIVATED",json.dumps(receipt,sort_keys=True))
    except Exception as error:
        if patched_files and backup is not None:
            shutil.copy2(backup/"paper_trader.js",SRC)
            shutil.copy2(backup/"project.env",ENVFILES[0])
            shutil.copy2(backup/"service.env",ENVFILES[1])
            try:TARGET.unlink(missing_ok=True)
            except Exception:pass
        print("PAPER_QUALITY_INSTALL_ROLLED_BACK",str(error),file=sys.stderr)
        raise
    finally:
        # Maintain original running Paper bot even if a checkpoint race occurs.
        if run("systemctl","is-active","futures-flow-bot")!="active":
            run("systemctl","start","futures-flow-bot",timeout=25)

if __name__=="__main__":
    try:main()
    except Exception as e:
        print("PAPER_QUALITY_INSTALL_ERROR",str(e)[:500],file=sys.stderr)
        sys.exit(1)
