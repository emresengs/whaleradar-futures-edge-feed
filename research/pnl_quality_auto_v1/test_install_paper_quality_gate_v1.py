#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest

p=Path(__file__).with_name("install_paper_quality_gate_v1.py")
sp=importlib.util.spec_from_file_location("quality_installer",p)
x=importlib.util.module_from_spec(sp);sp.loader.exec_module(x)
SOURCE="""class PaperTrader {
  constructor(config) {
    if (config.executionMode === 'paper' && process.env.PAPER_DAILY_CYCLE_ENABLED === 'true') {
      this.dailyCycle.start();
    }
  }

  _positionNotional() { }
  onSignal(signal) {
    if (this.dailyStopped) return;
    if (this.open.length >= this.config.maxOpenPositions) return;
  }
  report() {
    return {
      dailyCycle: this.dailyCycle ? this.dailyCycle.status() : null,
    };
  }
}
"""
class InstallSourceContracts(unittest.TestCase):
    def test_paper_only_gate_and_no_duplicate_patch(self):
        s=x.patch_paper_source(SOURCE)
        self.assertIn("config.executionMode === 'paper'",s)
        self.assertIn("this.qualityGate.allow(signal, this.closed)",s)
        self.assertIn("qualityAutoGateEnabled: !!this.qualityGate",s)
        with self.assertRaises(ValueError):x.patch_paper_source(s)
    def test_refuse_unknown_source(self):
        with self.assertRaises(ValueError):x.patch_paper_source("not the verified PaperTrader")
    def test_prevent_other_order_logic_changes(self):
        s=x.patch_paper_source(SOURCE)
        self.assertIn("if (this.open.length >= this.config.maxOpenPositions) return;",s)
        self.assertIn("this.dailyCycle.start();",s)
if __name__=="__main__":
    unittest.main(verbosity=2)
