"""Frequent deterministic checks, bounded AI rule authoring and round budgeting."""
import json
from pathlib import Path
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

from expman.ai_assist import AIAssist
from expman.agent_mcp import dispatch
from expman.autonomy import initialize
from expman.hub import APIError, Hub, make_server
from expman.rule_monitor import matches, validate_document, BUILTINS
from tests.support import temporary_directory


def document(identity="custom", **changes):
    return {"id": identity, "name": "队列检查", "enabled": True, "target": "job",
        "when": {"field": "state", "op": "eq", "value": "queued"}, "for_seconds": 0,
        "cooldown_seconds": 60, "severity": "warning", "effect": {"type": "escalate"}, **changes}


def answer(calls=()):
    return {"text": "已检查", "history": [], "calls": list(calls)}


class RuleMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = Path(self.temp.__enter__())
        self.hub = Hub(self.root / "hub")
        self.manager = self.hub.autonomy
        self.rules = self.manager.rules
        self.gateway = self.manager.gateway
        self.actor = self.gateway.authenticate("Bearer " + self.hub.config["admin_token"])

    def tearDown(self):
        self.hub.close()
        self.temp.__exit__(None, None, None)

    def job(self):
        return self.hub.submit({"request_id": uuid.uuid4().hex,
            "spec": {"backend": "demo", "resources": {"gpu_memory_mb": 0}}})["ids"][0]

    def enable(self, **values):
        AIAssist(self.hub).settings({"enabled": True})
        return self.manager.settings({"revision": self.manager.settings()["revision"], "enabled": True, **values})

    def save(self, value, automatic=False, revision=0):
        return self.rules.save({"revision": revision, "document": value}, self.actor, automatic)

    def test_local_rules_run_with_ai_disabled_and_exhausted_call_budget(self):
        self.job()
        self.save(document())
        with patch("expman.rule_monitor.now", return_value=1000), patch.object(AIAssist, "tool_turn") as model:
            self.manager.tick()
            first = self.rules.summary()
            self.assertEqual(first["open_count"], 1)
            self.assertEqual(first["last_scan_at"], 1000)
            self.enable(max_calls_per_hour=1)
            self.hub.db.execute("INSERT INTO agent_model_calls VALUES ('spent',?)", (time.time(),))
            with patch("expman.rule_monitor.now", return_value=1011):
                self.manager.tick()
                self.assertEqual(self.rules.summary()["last_scan_at"], 1011)
            model.assert_not_called()

    def test_sustained_findings_coalesce_and_resolve_without_report_pileup(self):
        identity = self.job()
        self.save(document(for_seconds=30))
        for stamp in (1000, 1010, 1030, 1040, 1100):
            with patch("expman.rule_monitor.now", return_value=stamp):
                self.rules.tick(force=True)
        self.assertEqual(self.rules.summary()["open_count"], 1)
        self.assertEqual(self.hub.db.execute("SELECT COUNT(*) FROM agent_rule_events WHERE kind='opened'").fetchone()[0], 1)
        first = self.rules.summary()["escalation_signature"]
        self.hub.set_queue_control({"job_id": identity, "revision": 0, "priority": 1, "hold": False})
        self.rules.tick(force=True)
        self.assertEqual(self.rules.summary()["escalation_signature"], first)
        self.hub.db.execute("UPDATE jobs SET state='succeeded' WHERE id=?", (identity,))
        self.rules.tick(force=True)
        self.assertEqual(self.rules.report()["total"], 0)
        self.assertEqual(self.hub.db.execute("SELECT COUNT(*) FROM agent_rule_events WHERE kind='resolved'").fetchone()[0], 1)

    def test_preview_validates_but_does_not_store_observations_or_execute(self):
        identity = self.job()
        preview = self.rules.preview(document(effect={"type": "queue", "priority": 10, "hold": False}))
        self.assertEqual(preview["matched"], 1)
        self.assertEqual(self.hub.job(identity)["queue_control"]["priority"], 0)
        self.assertEqual(self.hub.db.execute("SELECT COUNT(*) FROM agent_rule_observations").fetchone()[0], 0)
        self.assertEqual(len(self.rules.listing()["rules"]), len(BUILTINS))

    def test_blocked_release_escalates_with_rules_and_ignores_superseded_receipt(self):
        from expman import compute_policy
        from expman.compute_control import observe
        from types import SimpleNamespace
        self.hub.add_node("70ti")
        snap = {"capabilities": [compute_policy.CAPABILITY, compute_policy.CONTROL_CAPABILITY]}
        def sync():
            self.hub.sync("70ti", {"node_id": "70ti", "snapshot": snap, "reports": []})
        sync()
        policy = self.hub.set_compute_policy({"node_id": "70ti", "policy": {"mode": "release"}})["policy"]
        compute_policy.apply(snap, policy)
        empty = SimpleNamespace(records=lambda: [], config={"allow_demo": True}, preparation=None,
                                project_delivery=SimpleNamespace(installing=None))
        snap["compute_control"] = {**observe(empty, snap["availability"]), "phase": "blocked", "resources_released": False}
        sync()
        with patch("expman.rule_monitor.now", return_value=1000), patch.object(AIAssist, "tool_turn") as model:
            self.rules.tick(force=True)
            with patch("expman.rule_monitor.now", return_value=1031):
                self.rules.tick(force=True)
            model.assert_not_called()
        findings = self.rules.report()["items"]
        self.assertTrue(any(item["rule_id"] == "builtin-compute-blocked" for item in findings))
        self.hub.set_compute_policy({"node_id": "70ti", "policy": {**policy, "mode": "available"}})
        self.rules.tick(force=True)
        self.assertFalse(any(item["rule_id"] == "builtin-compute-blocked" for item in self.rules.report()["items"]))

    def test_language_rejects_code_unbounded_conditions_and_unauthorized_fields(self):
        invalid = [document(script="import os"), document(when={"field": "api_key", "op": "exists", "value": True}),
            document(effect={"type": "shell", "command": "something"}), document(effect={"type": "node_mode", "mode": "run"}),
            document(when={"field": "state", "op": "eval", "value": "True"}), document(for_seconds=True),
            document(when={"field": "state", "op": "in", "value": [1]*21}),
            document(when={"field": "queued_seconds", "op": "gte", "value": float("nan")}),
            document(when={"field": "queued_seconds", "op": "gte", "value": 2**54}),
            document(when={"any": [{"field": "state", "op": "eq", "value": "queued"}]*13}),
            document(name="x"*17000)]
        nested = {"field": "state", "op": "eq", "value": "queued"}
        for _ in range(7):
            nested = {"all": [nested]}
        invalid.append(document(when=nested))
        for value in invalid:
            with self.subTest(value=str(value)[:80]), self.assertRaises(ValueError):
                validate_document(value)
        safe = document(when={"any": [{"field": "state", "op": "eq", "value": "__import__('os')"},
            {"field": "queued_seconds", "op": "gte", "value": 100}]})
        self.assertFalse(matches(validate_document(safe)["when"], {"state": "queued", "queued_seconds": 0}))
        self.assertFalse(matches({"field": "state", "op": "in", "value": [1]}, {"state": True}))

    def test_rule_save_is_versioned_and_uncertain_retry_is_idempotent(self):
        first = self.save(document())
        self.assertEqual(first["revision"], 1)
        self.assertEqual(self.save(document())["revision"], 1)
        with self.assertRaises(APIError) as error:
            self.save(document(name="changed"))
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.save(document(name="changed"), revision=1)["revision"], 2)
        self.rules.delete({"id": "custom", "revision": 2}, self.actor)
        self.assertFalse(any(r["id"] == "custom" for r in self.rules.listing()["rules"]))

    def test_ai_authoring_is_scoped_and_cannot_rewrite_human_or_builtin_rules(self):
        self.save(document("human"))
        self.save(document("ai"), automatic=True)
        self.assertEqual(next(r for r in self.rules.listing()["rules"] if r["id"] == "ai")["origin"], "ai")
        for value in (document("human", name="AI edit"), next(r for r in self.rules.listing()["rules"] if r["origin"] == "builtin")):
            value = {k: v for k, v in value.items() if k != "origin"}
            with self.assertRaises(APIError):
                self.save(value, automatic=True, revision=value.get("revision", 1))
        with self.assertRaises(APIError):
            self.gateway.call("exlab_rule_save", {"revision": 0, "document": document("no-scope")}, {"id": "reader", "scopes": ["read"]})
        with self.assertRaises(APIError):
            self.gateway.call("exlab_automation", {"revision": 0, "enabled": False, "request_id": "self-edit"}, self.actor, automatic=True)
        self.manager.settings({"revision": 0, "allow_rule_authoring": False})
        with self.assertRaises(APIError):
            self.save(document("ai-off"), automatic=True)

    def test_ai_can_preview_and_save_rule_in_agent_loop_without_self_authorization(self):
        self.enable()
        value = document("ai-managed")
        calls = [{"id": "preview", "name": "exlab_rule_preview", "arguments": {"document": value}},
            {"id": "save", "name": "exlab_rule_save", "arguments": {"revision": 0, "document": value}},
            {"id": "self", "name": "exlab_automation", "arguments": {"revision": 1, "enabled": False, "request_id": "self"}}]
        with patch.object(AIAssist, "tool_turn", side_effect=[answer(calls), answer()]):
            self.manager.tick()
        self.assertEqual(next(r for r in self.rules.listing()["rules"] if r["id"] == "ai-managed")["origin"], "ai")
        detail = self.manager.run_detail(self.manager.status()["runs"][0]["id"])
        self.assertEqual(detail["steps"][-1]["result"]["status"], "rejected")
        self.assertTrue(self.manager.settings()["enabled"])

    def test_automatic_rule_action_once_per_episode_and_never_unholds_human_work(self):
        one, two = self.job(), self.job()
        self.hub.set_queue_control({"job_id": two, "revision": 0, "hold": True, "priority": 0})
        self.save(document(effect={"type": "queue", "priority": 10, "hold": False}))
        self.rules.tick(force=True)
        self.assertEqual(self.hub.job(one)["queue_control"]["revision"], 0)
        self.enable()
        for _ in range(3):
            self.rules.tick(force=True)
        self.assertEqual(self.hub.job(one)["queue_control"]["priority"], 10)
        self.assertEqual(self.hub.job(one)["queue_control"]["revision"], 1)
        one_report = next(item for item in self.rules.report()["items"] if item["target"] == one)
        self.assertEqual(one_report["feedback"]["status"], "verified")
        self.assertTrue(self.hub.job(two)["queue_control"]["hold"])
        self.assertEqual(self.hub.db.execute("SELECT COUNT(*) FROM agent_tool_receipts WHERE actor='rule:custom'").fetchone()[0], 1)
        self.assertEqual(len([e for e in self.rules.report()["events"] if e["kind"] == "action"]), 2)

    def test_actions_bounded_per_scan_and_cooldown_applies_per_target(self):
        identities = [self.job() for _ in range(6)]
        self.enable()
        self.save(document(effect={"type": "queue", "priority": 10, "hold": False}))
        self.rules.tick(force=True)
        self.assertEqual(sum(self.hub.job(j)["queue_control"]["priority"] == 10 for j in identities), 4)
        self.rules.tick(force=True)
        self.assertEqual(sum(self.hub.job(j)["queue_control"]["priority"] == 10 for j in identities), 6)
        self.assertEqual(self.hub.db.execute("SELECT COUNT(*) FROM agent_tool_receipts WHERE actor='rule:custom'").fetchone()[0], 6)

    def test_rule_actions_respect_operation_allowlist_and_lifecycle(self):
        identity = self.job()
        self.enable(operations=[])
        self.save(document(effect={"type": "queue", "priority": 5, "hold": False}))
        self.rules.tick(force=True)
        self.assertEqual(self.hub.job(identity)["queue_control"]["priority"], 0)
        before = self.rules.summary()["last_scan_at"]
        self.hub.center_state["mode"] = "replica"
        with patch("expman.rule_monitor.now", return_value=before+100):
            self.rules.tick(force=True)
        self.assertEqual(self.rules.summary()["last_scan_at"], before)

    def test_deleted_rule_restore_cannot_bypass_active_rule_limit(self):
        first = self.save(document("deleted"))
        self.rules.delete({"id": "deleted", "revision": first["revision"]}, self.actor)
        for number in range(24):
            self.save(document("r"+str(number)))
        with self.assertRaises(APIError) as error:
            self.save(document("deleted"), revision=2)
        self.assertEqual(error.exception.status, 409)
        first_page = self.gateway.call("exlab_rules", {}, {"id": "reader", "scopes": ["read"]})
        self.assertEqual(len(first_page["rules"]), 5)
        self.assertEqual(first_page["next_offset"], 5)
        self.assertEqual(first_page["total"], 24+len(BUILTINS))

    def test_new_incident_during_model_io_remains_unreviewed_until_next_round(self):
        self.job()
        self.enable(min_interval_seconds=10)
        def model(*_):
            self.save(document())
            self.rules.tick(force=True)
            return answer()
        with patch.object(AIAssist, "tool_turn", side_effect=model):
            self.manager.tick()
        signature = self.rules.summary()["escalation_signature"]
        self.assertTrue(signature)
        self.assertNotEqual(self.manager._runtime().get("reviewed_rule_signature"), signature)
        self.manager._runtime(last_attempt_at=0)
        with patch.object(AIAssist, "tool_turn", return_value=answer()) as later:
            self.manager.tick()
        self.assertEqual(later.call_count, 1)
        self.assertEqual(self.manager._runtime()["reviewed_rule_signature"], signature)

    def test_rule_failure_is_throttled_and_does_not_disable_ai_planning(self):
        self.enable()
        with patch.object(self.rules, "facts", side_effect=ValueError("fault")) as facts, patch.object(AIAssist, "tool_turn", return_value=answer()) as model:
            self.manager.tick()
            self.manager.tick()
            self.assertEqual(facts.call_count, 1)
            self.assertEqual(model.call_count, 1)
        self.assertTrue(self.rules.summary()["scan_error"])
        self.rules.tick(force=True)
        self.assertIsNone(self.rules.summary()["scan_error"])

    def test_per_target_cooldown_survives_resolution_and_event_retention(self):
        identity = self.job()
        self.enable()
        self.save(document(effect={"type": "queue", "priority": 5, "hold": False}))
        with patch("expman.rule_monitor.now", return_value=1000):
            self.rules.tick(force=True)
        self.hub.db.execute("UPDATE jobs SET state='succeeded' WHERE id=?", (identity,))
        with patch("expman.rule_monitor.now", return_value=1010):
            self.rules.tick(force=True)
        self.hub.db.execute("DELETE FROM agent_rule_events")
        self.hub.db.execute("UPDATE jobs SET state='queued' WHERE id=?", (identity,))
        with patch("expman.rule_monitor.now", return_value=1030):
            self.rules.tick(force=True)
        self.assertEqual(self.hub.db.execute("SELECT COUNT(*) FROM agent_tool_receipts WHERE actor='rule:custom'").fetchone()[0], 1)
        with patch("expman.rule_monitor.now", return_value=1060):
            self.rules.tick(force=True)
        self.assertEqual(self.hub.db.execute("SELECT COUNT(*) FROM agent_tool_receipts WHERE actor='rule:custom'").fetchone()[0], 2)

    def test_persistent_pending_command_timer_does_not_reset_on_heartbeat(self):
        identity = self.job()
        self.hub.db.execute("UPDATE jobs SET state='running',command_id=2,command_ack=1 WHERE id=?", (identity,))
        for stamp in (1000, 1100):
            with patch("expman.rule_monitor.now", return_value=stamp):
                self.rules.tick(force=True)
        self.hub.db.execute("UPDATE jobs SET updated=? WHERE id=?", (time.time()+100, identity))
        with patch("expman.rule_monitor.now", return_value=1120):
            facts = self.rules.facts()["job"][0]
        self.assertEqual(facts["command_pending_seconds"], 120)
        other = Hub(self.root / "hub")
        try:
            with patch("expman.rule_monitor.now", return_value=1150):
                self.assertEqual(other.autonomy.rules.facts()["job"][0]["command_pending_seconds"], 150)
        finally:
            other.close()
        self.hub.db.execute("UPDATE jobs SET command_ack=2 WHERE id=?", (identity,))
        self.rules.tick(force=True)
        self.assertEqual(self.rules.facts()["job"][0]["command_pending_seconds"], 0)

    def test_rule_thread_keeps_inspecting_during_slow_model_call(self):
        self.enable(rule_interval_seconds=5)
        entered, release = threading.Event(), threading.Event()
        def slow_model(*args):
            entered.set()
            release.wait(8)
            return answer()
        try:
            with patch.object(AIAssist, "tool_turn", side_effect=slow_model):
                self.manager.start()
                self.assertTrue(entered.wait(3))
                self.job()
                self.save(document())
                with self.hub.lock:
                    self.manager._runtime(last_rule_scan_at=0, last_rule_attempt_at=0)
                deadline = time.monotonic()+3
                while self.rules.summary()["open_count"] == 0 and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(self.rules.summary()["open_count"], 1)
                self.assertTrue(self.manager.current_run)
                release.set()
                self.manager.close()
        finally:
            release.set()

    def test_round_budget_allows_complete_turns_and_leaves_excess_queued(self):
        config = self.enable(max_runs_per_hour=1, max_turns=3)
        self.assertEqual(config["max_calls_per_hour"], 3)
        call = {"id": "observe", "name": "exlab_topology", "arguments": {}}
        with patch.object(AIAssist, "tool_turn", side_effect=[answer([call]), answer([call]), answer()]) as model:
            self.manager.tick()
            queued = self.manager.enqueue("next", self.actor["id"], "next")
            self.manager.tick()
            self.assertEqual(model.call_count, 3)
        self.assertEqual(self.manager.run_detail(queued["run_id"])["state"], "queued")
        self.assertEqual(self.manager.status()["runs_last_hour"], 1)
        self.assertIn("额度", self.manager.status()["blocked_reason"])

    def test_failed_round_does_not_consume_topology_or_incident_review(self):
        self.job()
        self.save(document())
        self.enable()
        with patch.object(AIAssist, "tool_turn", side_effect=ValueError("temporarily unavailable")):
            self.manager.tick()
        self.assertIsNone(self.manager._runtime().get("reviewed_rule_signature"))
        self.assertIsNone(self.manager._runtime().get("last_signature"))
        self.manager._runtime(last_attempt_at=0, next_retry_at=0)
        with patch.object(AIAssist, "tool_turn", return_value=answer()) as retry:
            self.manager.tick()
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(self.manager._runtime()["reviewed_rule_signature"], self.rules.summary()["escalation_signature"])

    def test_upgrade_legacy_budget_once_without_raising_maximum_cost(self):
        old = {"revision": 7, "enabled": False, "min_interval_seconds": 60, "review_seconds": 300,
            "max_turns": 6, "max_calls_per_hour": 13}
        self.hub.db.execute("INSERT OR REPLACE INTO agent_settings VALUES (1,?)", (json.dumps(old),))
        initialize(self.hub.db)
        c = self.manager.settings()
        self.assertEqual((c["revision"], c["min_interval_seconds"], c["review_seconds"]), (8, 900, 3600))
        self.assertEqual((c["max_runs_per_hour"], c["max_turns"], c["max_calls_per_hour"]), (2, 6, 12))
        initialize(self.hub.db)
        self.assertEqual(self.manager.settings()["revision"], 8)

    def test_history_is_bounded_and_full_report_is_loaded_separately(self):
        AIAssist(self.hub).settings({"enabled": True})
        identities = [self.manager.enqueue("check", self.actor["id"], str(i))["run_id"] for i in range(13)]
        self.hub.db.execute("UPDATE agent_runs SET state='completed',summary=?", ("long text "*2000,))
        first = self.manager.status()
        self.assertEqual(first["run_total"], 13)
        self.assertEqual(len(first["runs"]), 5)
        self.assertTrue(all(len(r["summary"]) <= 240 for r in first["runs"]))
        second = self.manager.history(5, 5)
        self.assertFalse({r["id"] for r in first["runs"]} & {r["id"] for r in second["runs"]})
        self.assertGreater(len(self.manager.run_detail(identities[0])["summary"]), 240)

    def test_gpu_memory_jitter_does_not_trigger_ai_but_permission_changes_do(self):
        self.hub.add_node("gpu")
        self.hub.sync("gpu", {"node_id": "gpu", "snapshot": {"gpus": [{"uuid": "gpu1", "free_mb": 1000}]}, "reports": []})
        first = self.manager.signature(self.gateway.topology({"limit": 10}))
        self.hub.sync("gpu", {"node_id": "gpu", "snapshot": {"gpus": [{"uuid": "gpu1", "free_mb": 6000}]}, "reports": []})
        self.assertEqual(self.manager.signature(self.gateway.topology({"limit": 10})), first)
        self.hub.sync("gpu", {"node_id": "gpu", "snapshot": {"gpus": [{"uuid": "gpu1", "free_mb": 6000, "local_enabled": False}]}, "reports": []})
        self.assertNotEqual(self.manager.signature(self.gateway.topology({"limit": 10})), first)

    def test_monitor_and_mcp_read_reports_and_control_permission_edits_rules(self):
        reader = {"id": "reader", "scopes": ["read"]}
        listed = dispatch(self.hub, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, reader)
        self.assertIn("exlab_inspection", [t["name"] for t in listed["result"]["tools"]])
        self.assertNotIn("exlab_rule_save", [t["name"] for t in listed["result"]["tools"]])
        for permission in ("monitor", "control"):
            device = self.hub.mobile_enroll({"name": permission, "permission": permission})
            header = "Bearer " + device["token"]
            for suffix in ("history", "inspection", "rules"):
                self.assertIsInstance(self.hub.mobile_request(header, "GET", "/api/mobile/agent/"+suffix), dict)
            payload = {"revision": 0, "document": document(permission)}
            if permission == "monitor":
                with self.assertRaises(APIError):
                    self.hub.mobile_request(header, "POST", "/api/mobile/agent/rules/save", payload)
            else:
                self.assertEqual(self.hub.mobile_request(header, "POST", "/api/mobile/agent/rules/save", payload)["revision"], 1)

    def test_http_static_style_and_paginated_endpoints(self):
        server = make_server(self.hub, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:"+str(server.server_address[1])
        try:
            for path in ("/", "/mobile/", "/autonomy.css"):
                with urllib.request.urlopen(base+path, timeout=3) as response:
                    text = response.read().decode()
                    self.assertIn('autonomy.css' if path != '/autonomy.css' else 'agent-overlay', text)
            for path in ("history?offset=0&limit=5", "inspection?offset=0&limit=10", "rules"):
                request = urllib.request.Request(base+"/api/agent/"+path, headers={"Authorization": "Bearer "+self.hub.config["admin_token"]})
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertIsInstance(json.load(response), dict)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
