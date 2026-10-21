"""One-shot diagnostic dispatch controls, using fake key/process callbacks."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import test_build_core as support
from fantasy_basketball.core.config import canonical
from fantasy_basketball.providers.qualification import synthetic_response
from test_build_research import action
from gemini_probe_entry import ProbeConfig, run


class ProbeEntryTests(support.BuildCase):
    def config_permit(self):
        root = Path(__file__).resolve().parents[1]
        cfg = json.loads((root/'workstreams/build/RESEARCH_CONTEXT_RC1_2026-10-18/package.json').read_text())["config"]
        profile = next(p for p in cfg["draft_plan"]["profiles"] if p["provider"] == "gemini")
        wire = json.loads((root/'workstreams/build/GEMINI_SCHEMA_DIAGNOSTIC_2026-10-18/candidate-wire.json').read_text())
        names = [name for name, _ in cfg["sources"]] + ["workstreams/build/gemini_probe_entry.py", "workstreams/build/gemini_action_probe.py"]
        value = dict(run_id="gemini-schema-test", max_requests=1, profile=profile, wire=wire,
                     legal_ids=["synthetic-player"], budget_micro_usd=4526984,
                     sources={name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names})
        config = ProbeConfig(canonical(value))
        permit = dict(config_hash=config.hash, paid_execution_authorized=True, max_requests=1,
                      budget_micro_usd=4526984, not_before_ms=10000, expires_ms=20000, owner_record="owner:Jeremy-fixture")
        return config, permit

    def test_success_and_cold_restart_use_one_call_and_preserve_bytes(self):
        cfg, permit = self.config_permit()
        calls = []
        def process(*args, **kwargs):
            with kwargs["launch_guard"]() as allowed:
                self.assertTrue(allowed)
                calls.append(1)
                self.assertIn(b"fixture-only-secret", args[1])
            return {"outcome":"RESPONSE", "http_status":200, "raw":synthetic_response(
                cfg.profile, "synthetic-player", text=canonical(action("search",query="synthetic availability")))}
        result = run(self.directory,cfg,permit,lambda _:"fixture-only-secret",clock=self.clock.now_ms,process=process)
        self.assertEqual(result["phase"],"COMPLETE")
        path=self.directory/(cfg.run_id+".sqlite3"); before=path.read_bytes()
        self.assertEqual(run(self.directory,cfg,permit,lambda _:self.fail("no key on restart"),clock=self.clock.now_ms,process=process),result)
        self.assertEqual(len(calls),1)
        self.assertEqual(path.read_bytes(),before)
        self.assertNotIn(b"fixture-only-secret",before)

    def test_disabled_expired_changed_binding_and_source_reject_before_key(self):
        cfg, permit = self.config_permit()
        for key,value in (("paid_execution_authorized",False),("expires_ms",10000),("config_hash","0"*64),("budget_micro_usd",1)):
            changed=dict(permit);changed[key]=value
            with self.assertRaises(ValueError):run(self.directory,cfg,changed,lambda _:self.fail("no key"),clock=self.clock.now_ms)
        source=cfg.value; name=next(iter(source["sources"]));source["sources"][name]="0"*64
        changed_cfg=ProbeConfig(canonical(source));changed=dict(permit,config_hash=changed_cfg.hash)
        with self.assertRaises(ValueError):run(self.directory,changed_cfg,changed,lambda _:self.fail("no key"),clock=self.clock.now_ms)
        self.assertFalse((self.directory/(cfg.run_id+".sqlite3")).exists())

    def test_unknown_after_send_never_resends(self):
        cfg,permit=self.config_permit();calls=[]
        def process(*args,**kwargs):calls.append(1);return {"outcome":"WORKER_TIMEOUT"}
        def crash(boundary):
            if boundary=="after_send":raise RuntimeError("synthetic loss")
        with self.assertRaises(RuntimeError):run(self.directory,cfg,permit,lambda _:"fixture-key",clock=self.clock.now_ms,process=process,fault=crash)
        state=run(self.directory,cfg,permit,lambda _:self.fail("no resend key"),clock=self.clock.now_ms,process=process)
        self.assertEqual(state["pause"],"UNKNOWN_DISPATCH")
        self.assertEqual(len(calls),1)
        self.assertEqual(state["retained_micro_usd"],4526984)

    def test_saved_response_replays_without_dispatch_and_bad_usage_stays_unknown(self):
        cfg,permit=self.config_permit()
        raw=synthetic_response(cfg.profile,"synthetic-player",text=canonical(action("search",query="test")))
        raw["usageMetadata"].pop("candidatesTokenCount")
        def process(*args,**kwargs):return {"outcome":"RESPONSE","raw":raw,"http_status":200}
        def crash(boundary):
            if boundary=="after_result":raise RuntimeError("synthetic loss")
        with self.assertRaises(RuntimeError):run(self.directory,cfg,permit,lambda _:"fixture-key",clock=self.clock.now_ms,process=process,fault=crash)
        state=run(self.directory,cfg,permit,lambda _:self.fail("no dispatch"),clock=self.clock.now_ms)
        self.assertEqual(state["pause"],"MISSING_OR_INVALID_USAGE")
        self.assertIsNone(state["receipt"]["usage"])
        self.assertEqual(state["requests"],1)


if __name__=="__main__":
    import unittest
    unittest.main()
