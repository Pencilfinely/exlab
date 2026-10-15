"""Autonomous execution, provider protocols, scoped external tools and MCP wiring."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

from expman import common
from expman.ai_assist import AIAssist
from expman.agent_mcp import dispatch, http_request
from expman.agent_tools import definitions, SCOPES
from expman.hub import APIError, Hub, make_server
from tests.support import temporary_directory


def answer(calls=(), text="本轮核验完成", history=None):
    return {"history": history or [], "calls": list(calls), "text": text}


def control(operation, arguments, request_id="intent-1"):
    return {"id": request_id, "name": "exlab_control", "arguments": {
        "operation": operation, "arguments": arguments, "request_id": request_id}}


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = Path(self.temp.__enter__())
        self.hub = Hub(self.root / "hub")
        self.ai, self.manager = AIAssist(self.hub), self.hub.autonomy
        self.gateway = self.manager.gateway
        self.actor = self.gateway.authenticate("Bearer " + self.hub.config["admin_token"])

    def tearDown(self):
        self.hub.close()
        self.temp.__exit__(None, None, None)

    def job(self):
        return self.hub.submit({"request_id": "new-" + uuid.uuid4().hex, "spec": {"backend": "demo",
            "resources": {"gpu_memory_mb": 0}}})["ids"][0]

    def enable(self, **extra):
        self.ai.settings({"enabled": True, "api_key": "private-provider-key"})
        return self.manager.settings({"revision": self.manager.settings()["revision"], "enabled": True, **extra})

    def queue_args(self, identity, priority=5, revision=0, hold=False):
        return {"job_id": identity, "priority": priority, "revision": revision, "hold": hold}

    def test_no_model_call_until_both_switches_enabled(self):
        with patch.object(AIAssist, "tool_turn") as model:
            self.manager.tick()
            with self.assertRaises(APIError):
                self.manager.settings({"revision": 0, "enabled": True})
            self.ai.settings({"enabled": True})
            self.manager.tick()
            model.assert_not_called()

    def test_automatic_change_detection_execution_and_feedback_without_click(self):
        identity = self.job()
        self.enable()
        with patch.object(AIAssist, "tool_turn", side_effect=[answer([control("queue", self.queue_args(identity))]), answer()]) as model:
            self.manager.tick()
            self.assertEqual(model.call_count, 2)
            self.manager.tick()  # same topology: no paid call or repeated action
            self.assertEqual(model.call_count, 2)
        self.assertEqual(self.hub.job(identity)["queue_control"]["priority"], 5)
        latest = self.manager.status()["runs"][0]
        self.assertEqual(latest["state"], "completed")
        self.assertEqual(latest["outcomes"][0]["status"], "verified")
        self.assertEqual(len(self.manager.run_detail(latest["id"])["steps"]), 1)

    def test_automatic_service_loop_wakes_itself(self):
        self.enable()
        done = threading.Event()
        def model(*_):
            done.set()
            return answer()
        with patch.object(AIAssist, "tool_turn", side_effect=model):
            self.manager.start()
            self.assertTrue(done.wait(4), "No UI request is needed for autonomous execution")
            deadline = time.monotonic() + 3
            while self.manager.current_run and time.monotonic() < deadline:
                time.sleep(.01)
            self.manager.close()
        self.assertEqual(self.manager.status()["runs"][0]["state"], "completed")

    def test_read_only_automation_and_invalid_tool_replans(self):
        self.enable(operations=[])
        with patch.object(AIAssist, "tool_turn", side_effect=[answer([control("cancel", {"job_id": "missing", "expected_command_id": 0}),
            {"id": "bad", "name": "shell_exec", "arguments": {"command": "delete"}}]), answer()]) as model:
            self.manager.tick()
        self.assertNotIn("exlab_control", [t["name"] for t in model.call_args_list[0].args[3]])
        detail = self.manager.run_detail(self.manager.status()["runs"][0]["id"])
        self.assertEqual(detail["state"], "completed")
        self.assertTrue(all(step["result"]["status"] == "rejected" for step in detail["steps"]))

    def test_model_io_does_not_hold_scheduler_lock(self):
        self.enable()
        queried = threading.Event()
        def model(*_):
            reader = threading.Thread(target=lambda: (self.hub.state(), queried.set()))
            reader.start()
            self.assertTrue(queried.wait(2))
            reader.join()
            return answer()
        with patch.object(AIAssist, "tool_turn", side_effect=model):
            self.manager.tick()

    def test_disabling_settings_provider_or_cancel_discards_inflight_plan(self):
        for mode in ("automation", "provider", "cancel", "model_config"):
            identity = self.job()
            self.enable()
            # Force a fresh run without waiting for the automatic cadence.
            run = self.manager.enqueue("检查", self.actor["id"], mode)
            def model(*_):
                if mode == "automation":
                    self.manager.settings({"revision": self.manager.settings()["revision"], "enabled": False})
                elif mode == "provider":
                    self.ai.settings({"enabled": False})
                elif mode == "cancel":
                    self.manager.cancel(run["run_id"])
                else:
                    self.ai.settings({"model": "changed-model"})
                return answer([control("queue", self.queue_args(identity))])
            with patch.object(AIAssist, "tool_turn", side_effect=model):
                self.manager.execute(run["run_id"])
            self.assertEqual(self.hub.job(identity)["queue_control"]["revision"], 0, mode)
            self.assertEqual(self.manager.run_detail(run["run_id"])["state"], "interrupted", mode)

    def test_action_and_hourly_call_budgets_are_enforced(self):
        one, two = self.job(), self.job()
        self.enable(max_actions=1, max_calls_per_hour=2)
        with patch.object(AIAssist, "tool_turn", side_effect=[answer([control("queue", self.queue_args(one), "one"),
                control("queue", self.queue_args(two), "two")]), answer()]):
            self.manager.tick()
        self.assertEqual(self.hub.job(one)["queue_control"]["priority"], 5)
        self.assertEqual(self.hub.job(two)["queue_control"]["priority"], 0)
        self.assertEqual(self.manager.status()["calls_last_hour"], 2)
        with patch.object(AIAssist, "tool_turn") as model:
            self.manager._runtime(last_attempt_at=0)
            self.job()
            self.manager.tick()
            model.assert_not_called()

    def test_failed_provider_backs_off_and_redacts_credentials(self):
        self.enable()
        with patch.object(AIAssist, "tool_turn", side_effect=ValueError("private-provider-key failure")) as model:
            self.manager.tick()
            self.manager.tick()
        self.assertEqual(model.call_count, 1)
        status = self.manager.status()
        self.assertEqual(status["runs"][0]["state"], "failed")
        self.assertGreater(status["runtime"]["next_retry_at"], time.time())
        self.assertNotIn("private-provider-key", json.dumps(status))

    def test_manual_release_ac_maintenance_and_queue_hold_survive_ai(self):
        self.hub.add_node("rog")
        self.hub.sync("rog", {"node_id": "rog", "snapshot": {"capabilities": ["dynamic-compute-v1"]}, "reports": []})
        from expman.compute_policy import validate
        policy = validate({"mode": "release", "require_ac": True, "reason": "游戏"})
        self.hub.set_compute_policy({"node_id": "rog", "policy": policy})
        current = self.hub.state()["nodes"][0]["compute_policy"]
        for changes in ({"mode": "available"}, {"require_ac": False}, {"until": time.time()+7200}):
            with self.assertRaises(APIError):
                self.gateway.operate("availability", {"node_id": "rog", "policy": {**current, **changes}}, "policy-"+str(changes), self.actor, automatic=True)
        self.hub.set_mode({"node_id": "rog", "mode": "drain"})
        with self.assertRaises(APIError):
            self.gateway.operate("node_mode", {"node_id": "rog", "mode": "run", "expected_mode": "drain"}, "drain", self.actor, automatic=True)
        identity = self.job()
        self.hub.set_queue_control(self.queue_args(identity, hold=True))
        with self.assertRaises(APIError):
            self.gateway.operate("queue", self.queue_args(identity, revision=1), "unhold", self.actor, automatic=True)

    def test_direct_external_control_works_without_builtin_ai_and_is_idempotent(self):
        credential = self.gateway.credentials({"name": "Codex", "scopes": ["control"]})
        actor = self.gateway.authenticate("Bearer " + credential["credential"])
        identity = self.job()
        call = control("queue", self.queue_args(identity))["arguments"]
        first = self.gateway.call("exlab_control", call, actor)
        self.assertEqual(first["status"], "requested")
        self.assertEqual(self.gateway.call("exlab_control", call, actor), first)
        with self.assertRaises(APIError):
            self.gateway.call("exlab_control", {**call, "arguments": self.queue_args(identity, 7)}, actor)
        self.assertFalse(self.ai.settings()["enabled"])
        self.gateway.revoke({"id": credential["id"]})
        with self.assertRaises(APIError):
            self.gateway.authenticate("Bearer " + credential["credential"])

    def test_ai_can_reverse_its_own_hold_but_not_subsequent_human_intent(self):
        identity = self.job()
        first = self.gateway.operate("queue", self.queue_args(identity, hold=True), "hold", self.actor, automatic=True)
        self.assertEqual(first["status"], "requested")
        undone = self.gateway.operate("queue", self.queue_args(identity, revision=1), "undo", self.actor, automatic=True)
        self.assertEqual(undone["status"], "requested")
        self.hub.set_queue_control(self.queue_args(identity, revision=2, hold=True))
        with self.assertRaises(APIError):
            self.gateway.operate("queue", self.queue_args(identity, revision=3), "human-hold", self.actor, automatic=True)
        self.hub.add_node("a")
        for mode, expected, request in (("drain", "run", "d1"), ("run", "drain", "r1"), ("drain", "run", "d2")):
            result = self.gateway.operate("node_mode", {"node_id": "a", "mode": mode, "expected_mode": expected}, request, self.actor, automatic=True)
            self.assertEqual(result["status"], "requested")
        self.hub.set_mode({"node_id": "a", "mode": "drain"})  # human reasserts the same mode
        with self.assertRaises(APIError):
            self.gateway.operate("node_mode", {"node_id": "a", "mode": "run", "expected_mode": "drain"}, "human-drain", self.actor, automatic=True)

    def test_ai_policy_changes_preserve_the_original_human_ac_baseline(self):
        from expman.compute_policy import validate
        self.hub.add_node("rog")
        self.hub.sync("rog", {"node_id": "rog", "snapshot": {"capabilities": ["dynamic-compute-v1"]}, "reports": []})
        self.hub.set_compute_policy({"node_id": "rog", "policy": validate({"require_ac": True})})
        current = self.hub._compute_policy("rog")
        result = self.gateway.operate("availability", {"node_id": "rog", "policy": {**current, "mode": "release"}}, "ai-release", self.actor, automatic=True)
        self.assertEqual(result["status"], "requested")
        current = self.hub._compute_policy("rog")
        result = self.gateway.operate("availability", {"node_id": "rog", "policy": {**current, "mode": "available"}}, "ai-restore", self.actor, automatic=True)
        self.assertEqual(result["status"], "requested")
        with self.assertRaises(APIError):
            self.gateway.operate("availability", {"node_id": "rog", "policy": {**self.hub._compute_policy("rog"), "require_ac": False}}, "ac", self.actor, automatic=True)

    def test_automatic_resume_recognizes_own_pause_but_not_manual_stop(self):
        self.hub.add_node("a")
        self.hub.sync("a", {"node_id": "a", "snapshot": {"allow_demo": True, "capabilities": ["experiment-results-v1"]}, "reports": []})
        identity = self.job()
        paused = self.gateway.operate("pause", {"job_id": identity, "expected_command_id": 0}, "pause", self.actor, automatic=True)
        self.assertEqual(paused["status"], "requested")
        # The guard requires actual process-exit feedback even for an AI-owned stop.
        with self.assertRaises(APIError):
            self.gateway._automatic_guard("resume", {"job_id": identity, "expected_command_id": 1})
        self.hub.db.execute("UPDATE jobs SET state='paused',command_ack=1,execution_state=? WHERE id=?",
            (json.dumps({"stopped_confirmed": True, "ever_started": False}), identity))
        self.gateway._automatic_guard("resume", {"job_id": identity, "expected_command_id": 1})
        # A later human command removes the AI pause ownership.
        self.hub.action({"job_id": identity, "action": "cancel", "expected_command_id": 1})
        self.assertIsNone(self.hub.db.execute("SELECT * FROM agent_managed_stops WHERE job_id=?", (identity,)).fetchone())
        with self.assertRaises(APIError):
            self.gateway._automatic_guard("resume", {"job_id": identity, "expected_command_id": 2})

    def test_worker_heartbeat_receives_small_hosting_status_without_other_plans(self):
        self.hub.add_node("a")
        self.enable()
        with patch.object(AIAssist, "tool_turn", return_value=answer(text="private project parameters")):
            self.manager.tick()
        response = self.hub.sync("a", {"node_id": "a", "snapshot": {}, "reports": []})
        self.assertTrue(response["controller_agent"]["enabled"])
        self.assertNotIn("private project parameters", json.dumps(response))
        self.assertNotIn("private-provider-key", json.dumps(response))

    def test_crash_reservation_never_replays_unconfirmed_operation(self):
        identity = self.job()
        args = self.queue_args(identity)
        with patch.object(self.hub, "set_queue_control", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.gateway.operate("queue", args, "uncertain", self.actor)
        with patch.object(self.hub, "set_queue_control") as mutation:
            result = self.gateway.operate("queue", args, "uncertain", self.actor)
            self.assertEqual(result["status"], "uncertain")
            mutation.assert_not_called()

    def test_scopes_revocation_expiry_and_credentials_are_not_admin(self):
        credential = self.gateway.credentials({"name": "reader", "scopes": ["read"], "days": 1})
        header = "Bearer " + credential["credential"]
        actor = self.gateway.authenticate(header)
        self.assertFalse(actor["admin"])
        self.assertNotIn("token_hash", json.dumps(self.gateway.credentials()))
        for name, args in (("exlab_control", control("queue", self.queue_args("x"))["arguments"]),
                            ("exlab_delegate", {"instructions": "check", "request_id": "1"}),
                            ("exlab_automation", {"revision": 0, "request_id": "1"})):
            with self.assertRaises(APIError):
                self.gateway.call(name, args, actor)
        with self.assertRaises(APIError):
            http_request(self.hub, header, "POST", "/api/agent/credentials", {"name": "evil", "scopes": ["control"]}, {})
        self.hub.db.execute("UPDATE agent_credentials SET expires=1")
        with self.assertRaises(APIError):
            self.gateway.authenticate(header)

    def test_delegation_is_durable_and_revocation_blocks_inflight_actions(self):
        identity = self.job()
        self.ai.settings({"enabled": True})
        credential = self.gateway.credentials({"name": "GPT", "scopes": ["delegate"]})
        actor = self.gateway.authenticate("Bearer " + credential["credential"])
        args = {"instructions": "检查并安排优先级", "request_id": "d1"}
        run = self.gateway.call("exlab_delegate", args, actor)
        self.assertEqual(self.gateway.call("exlab_delegate", args, actor), run)
        def provider(*_):
            self.gateway.revoke({"id": credential["id"]})
            return answer([control("queue", self.queue_args(identity))])
        with patch.object(AIAssist, "tool_turn", side_effect=provider):
            self.manager.execute(run["run_id"])
        self.assertEqual(self.manager.run_detail(run["run_id"])["state"], "interrupted")
        self.assertEqual(self.hub.job(identity)["queue_control"]["revision"], 0)

    def test_topology_pagination_events_results_and_secrets(self):
        identities = [self.job() for _ in range(3)]
        self.ai.settings({"api_key": "private-provider-key"})
        topology = self.gateway.call("exlab_topology", {"limit": 1}, self.actor)
        self.assertEqual(topology["job_total"], 3)
        self.assertEqual(topology["next_offset"], 1)
        self.assertNotEqual(self.gateway.topology({"offset": 1, "limit": 1})["jobs"][0]["id"], topology["jobs"][0]["id"])
        self.assertEqual(len(self.gateway.call("exlab_events", {}, self.actor)["events"]), 3)
        self.assertEqual(self.gateway.call("exlab_catalog", {"kind": "results"}, self.actor)["items"], [])
        cleaned = self.gateway.clean({"source": self.hub.config["admin_token"], "api_key": "other", "line": "private-provider-key"})
        self.assertNotIn("private-provider-key", json.dumps(cleaned))
        self.assertNotIn(self.hub.config["admin_token"], json.dumps(cleaned))
        self.assertEqual(cleaned["api_key"], "[redacted]")
        # A change outside the first page still changes the autonomous trigger signature.
        old = self.manager.signature(topology)
        self.hub.set_queue_control(self.queue_args(identities[0]))
        self.assertNotEqual(old, self.manager.signature(self.gateway.topology({"limit": 1})))

    def test_mobile_control_and_monitor_support_agent_and_split_state_results(self):
        identity = self.job()
        monitor = self.hub.mobile_enroll({"name": "Monitor", "permission": "monitor"})
        control_device = self.hub.mobile_enroll({"name": "Phone", "permission": "control"})
        for device in (monitor, control_device):
            token = "Bearer " + device["token"]
            self.assertIn("settings", self.hub.mobile_request(token, "GET", "/api/mobile/agent/status"))
            detail = self.hub.mobile_request(token, "GET", "/api/mobile/job", identity=identity)
            self.assertIn("execution_state", detail)
            self.assertIn("experiment_results", detail)
        with self.assertRaises(APIError) as error:
            self.hub.mobile_request("Bearer "+monitor["token"], "POST", "/api/mobile/agent/settings", {"revision": 0, "enabled": True})
        self.assertEqual(error.exception.status, 403)
        self.ai.settings({"enabled": True})
        self.hub.mobile_request("Bearer "+control_device["token"], "POST", "/api/mobile/agent/settings", {"revision": 0, "enabled": True})
        run = self.hub.mobile_request("Bearer "+control_device["token"], "POST", "/api/mobile/agent/delegate", {"instructions": "检查", "request_id": "mobile"})
        with patch.object(AIAssist, "tool_turn", return_value=answer()):
            self.manager.execute(run["run_id"])
        self.assertEqual(self.manager.run_detail(run["run_id"])["state"], "completed")  # long-lived expiry=0

    def test_replica_does_not_plan_or_mutate(self):
        self.enable()
        self.hub.center_state["mode"] = "replica"
        with patch.object(AIAssist, "tool_turn") as provider:
            self.manager.tick()
            provider.assert_not_called()

    def test_progress_heartbeat_order_does_not_spend_model_budget_and_objectives_wake(self):
        one, two = self.job(), self.job()
        self.enable()
        with patch.object(AIAssist, "tool_turn", return_value=answer()) as model:
            self.manager.tick()
            original = self.manager.signature(self.gateway.topology({"limit": 1}))
            self.hub.db.execute("UPDATE jobs SET updated=?,metrics=? WHERE id=?", (time.time()+100, '{"step":99}', one))
            self.assertEqual(self.manager.signature(self.gateway.topology({"limit": 1})), original)
            self.manager._runtime(last_attempt_at=time.time()-61)
            self.manager.tick()
            self.assertEqual(model.call_count, 1)
            self.manager.settings({"revision": self.manager.settings()["revision"], "instructions": "新的托管目标"})
            self.manager.tick()
            self.assertEqual(model.call_count, 2)

    def test_large_execution_diagnostics_remain_queryable_without_bloating_topology(self):
        identities = [self.job() for _ in range(6)]
        for identity in identities:
            self.hub.db.execute("UPDATE jobs SET execution_state=? WHERE id=?", (json.dumps({"ever_started": True,
                "stopped_confirmed": False, "progress": {"step": 5}, "diagnostics": {"log": "x"*100000}}), identity))
        topology = self.gateway.call("exlab_topology", {}, self.actor)
        self.assertLess(len(json.dumps(topology)), 50000)
        detail = self.gateway.call("exlab_job", {"job_id": identities[0], "fields": ["execution_state"]}, self.actor)
        self.assertEqual(len(detail["execution_state"]["diagnostics"]["log"]), 100000)
        events = self.gateway.call("exlab_job", {"job_id": identities[0], "fields": ["events"], "limit": 1}, self.actor)
        self.assertEqual(len(events["events"]), 1)
        self.assertNotIn("execution_state", events)


class ProviderProtocols(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = Path(self.temp.__enter__())
        self.hub = Hub(self.root / "hub")
        self.ai = AIAssist(self.hub)
        self.ai.settings({"enabled": True, "api_key": "private-key"})

    def tearDown(self):
        self.hub.close()
        self.temp.__exit__(None, None, None)

    def request(self, reply, **kwargs):
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(reply).encode())
            answer_value = self.ai.tool_turn("Plan with tools", {"admin_token": "private", "note": self.hub.config["admin_token"]}, None, definitions(["read"]), **kwargs)
            request = opener.return_value.open.call_args.args[0]
        self.assertNotIn(self.hub.config["admin_token"], request.data.decode())
        return answer_value, request

    def test_chat_tools_normalization_and_tool_reply(self):
        value, request = self.request({"choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "exlab_topology", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]})
        self.assertTrue(request.full_url.endswith("/chat/completions"))
        self.assertEqual(json.loads(request.data)["tools"][0]["function"]["name"], "exlab_topology")
        self.ai.tool_result(value["history"], value["calls"][0], {"nodes": []}, self.ai._config())
        self.assertEqual(value["history"][-1]["tool_call_id"], "c1")

    def test_responses_tools_reasoning_and_function_call_output(self):
        self.ai.settings({"api_mode": "responses"})
        value, request = self.request({"status": "completed", "output": [{"type": "reasoning", "id": "r1", "summary": []},
            {"type": "function_call", "call_id": "call_1", "id": "fc1", "name": "exlab_topology", "arguments": "{}"}]})
        self.assertTrue(request.full_url.endswith("/responses"))
        self.assertFalse(json.loads(request.data)["store"])
        self.assertEqual(json.loads(request.data)["tools"][0]["name"], "exlab_topology")
        self.ai.tool_result(value["history"], value["calls"][0], {"nodes": []}, self.ai._config())
        self.assertEqual(value["history"][-1]["type"], "function_call_output")
        self.assertTrue(any(item.get("type") == "reasoning" for item in value["history"]))

    def test_incomplete_or_duplicate_call_ids_never_execute(self):
        self.ai.settings({"api_mode": "responses"})
        with self.assertRaises(ValueError):
            self.request({"status": "incomplete", "output": []})
        call = {"type": "function_call", "call_id": "same", "name": "exlab_topology", "arguments": "{}"}
        with self.assertRaises(ValueError):
            self.request({"status": "completed", "output": [call, call]})

    def test_responses_legacy_json_draft_and_disabled_transport(self):
        self.ai.settings({"api_mode": "responses"})
        with patch.object(self.ai, "_request_model", return_value={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}]}):
            self.assertEqual(self.ai._complete("Return JSON", {}), {"ok": True})
        self.ai.settings({"enabled": False})
        with patch("urllib.request.build_opener") as opener:
            with self.assertRaises(ValueError):
                self.ai.tool_turn("plan", {}, None, [])
            opener.assert_not_called()


class AgentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = Path(self.temp.__enter__())
        self.hub = Hub(self.root / "hub")
        self.server = make_server(self.hub, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.admin = self.hub.config["admin_token"]
        self.credential = self.hub.autonomy.gateway.credentials({"name": "Codex", "scopes": ["read", "control", "delegate"]})

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.hub.close()
        self.temp.__exit__(None, None, None)

    def request(self, path, body=None, token=None, headers=None):
        request = urllib.request.Request(self.url+path, data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "Authorization": "Bearer "+(token or self.credential["credential"]), **(headers or {})})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
            data = response.read()
            return response.status, json.loads(data) if data else None

    def test_mcp_initialize_tools_resources_notifications_and_no_shell(self):
        self.assertEqual(self.request("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})[1]["result"]["protocolVersion"], "2025-06-18")
        tools = self.request("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]["result"]["tools"]
        self.assertIn("exlab_delegate", [tool["name"] for tool in tools])
        self.assertNotIn("exlab_automation", [tool["name"] for tool in tools])
        self.assertEqual(self.request("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}), (202, None))
        result = self.request("/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "exlab_topology"}})[1]["result"]
        self.assertEqual(result["structuredContent"]["job_total"], 0)
        result = self.request("/mcp", {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "shell_exec"}})[1]["result"]
        self.assertTrue(result["isError"])
        result = self.request("/mcp", {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": "exlab://agent"}})[1]["result"]
        self.assertEqual(result["contents"][0]["mimeType"], "application/json")

    def test_mcp_origin_protocol_and_auth_boundaries(self):
        for headers, code in (({"Origin": "https://evil.invalid"}, 403), ({"MCP-Protocol-Version": "9999-01-01"}, 400)):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers)
            self.assertEqual(error.exception.code, code)
        self.assertEqual(self.request("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": self.url})[0], 200)
        for path in ("/api/state", "/api/ai/settings", "/api/mobile/state"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code, 401)
        self.hub.autonomy.gateway.revoke({"id": self.credential["id"]})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(error.exception.code, 401)

    def test_external_http_api_direct_control_and_delegation(self):
        identity = self.hub.submit({"request_id": "api", "spec": {"backend": "demo"}})["ids"][0]
        result = self.request("/api/agent/call", {"name": "exlab_control", "arguments": control("queue", {
            "job_id": identity, "revision": 0, "priority": 9, "hold": False})["arguments"]})[1]
        self.assertEqual(result["status"], "requested")
        self.assertEqual(self.hub.job(identity)["queue_control"]["priority"], 9)
        self.assertTrue(self.request("/api/agent/tools")[1]["tools"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/agent/credentials", {"name": "escape", "scopes": ["automation"]})
        self.assertEqual(error.exception.code, 403)

    def test_stdio_bridge_interoperates_using_env_credential(self):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "exlab_topology", "arguments": {}}}]
        environment = {**os.environ, "EXLAB_AGENT_TOKEN": self.credential["credential"]}
        result = subprocess.run([sys.executable, "-m", "expman.agent_mcp", "--url", self.url],
            input="\n".join(json.dumps(r) for r in requests)+"\n", text=True, capture_output=True, timeout=15,
            env=environment, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([line["id"] for line in lines], [1, 2])
        self.assertEqual(lines[1]["result"]["structuredContent"]["job_total"], 0)
        self.assertNotIn(self.credential["credential"], result.stdout+result.stderr)


if __name__ == "__main__":
    unittest.main()
