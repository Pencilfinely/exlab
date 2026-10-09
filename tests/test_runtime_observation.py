import copy
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from expman import runtime_observation as runtime
from tests import test_agent, test_hub


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.record = {"id": "a"*32, "attempt": 1}
        self.calls = []
        self.container = {"Id": "b"*64, "State": {"Status": "running", "Running": True, "Pid": 12}}
        def execute(argv, **kwargs):
            self.calls.append(argv)
            return SimpleNamespace(stdout="epoch 92\n", stderr="")
        self.agent = SimpleNamespace(_inspect=lambda r: self.container, _exec=execute)

    def test_stale_log_has_fresh_read_and_readonly_process_snapshot(self):
        first, _ = runtime.observe(self.agent,self.record,1000)
        self.record["runtime"]=first
        second, tail=runtime.observe(self.agent,self.record,1300)
        self.assertEqual(tail,"epoch 92\n")
        self.assertEqual(second["log"]["changed_at"],1000)
        self.assertEqual(second["log"]["last_success_at"],1300)
        self.assertIn("processes",second["diagnostics"])
        self.assertTrue(all(argv[1] in ("logs","top","stats") for argv in self.calls))
        self.assertTrue(all("b"*64 in argv for argv in self.calls))
        self.assertNotIn("args",str(self.calls))
        runtime.validate(second,1)

    def test_timeout_keeps_log_and_reports_failure_without_leaking_command(self):
        first,_=runtime.observe(self.agent,self.record,1000)
        self.record["runtime"]=first
        def timeout(*a,**k):
            raise subprocess.TimeoutExpired("secret argument must not leak",5)
        self.agent._exec=timeout
        value,tail=runtime.observe(self.agent,self.record,1300)
        self.assertIsNone(tail)
        self.assertEqual(value["log"]["error"],"timeout")
        self.assertEqual(value["log"]["last_success_at"],1000)
        self.assertNotIn("secret",json.dumps(value))
        self.assertEqual(value["diagnostics"]["errors"],{"top":"timeout","stats":"timeout"})

    def test_paused_is_distinct_from_running_and_not_automatically_resumed(self):
        self.container["State"]["Paused"]=True
        value,_=runtime.observe(self.agent,self.record,1000)
        self.assertEqual(value["container"]["status"],"paused")
        self.assertTrue(all(x[1] in ("logs","top","stats") for x in self.calls))

    def test_ownership_failure_prevents_logs_and_process_queries(self):
        def fail(record):
            raise ValueError("wrong job labels")
        self.agent._inspect=fail
        value,tail=runtime.observe(self.agent,self.record,1000)
        self.assertEqual(value["container"]["error"],"read_failed")
        self.assertEqual(self.calls,[])
        self.assertIsNone(tail)

    def test_identity_missing_or_malformed_fails_closed(self):
        for identity in ("", "name", "b"*64+";stop"):
            self.container["Id"]=identity
            value,_=runtime.observe(self.agent,self.record,1000)
            self.assertEqual(value["container"]["status"],"unknown")
            self.assertEqual(self.calls,[])

    def test_gpu_probe_is_scoped_to_valid_assigned_uuid_and_failure_is_explicit(self):
        first, _ = runtime.observe(self.agent, self.record, 1000)
        self.record["runtime"] = first
        self.record["gpu_uuid"] = "GPU-11111111-1111-1111-1111-111111111111"
        value, _ = runtime.observe(self.agent, self.record, 1300)
        gpu_calls = [c for c in self.calls if c[0] == "nvidia-smi"]
        self.assertEqual(len(gpu_calls), 1)
        self.assertEqual(gpu_calls[0][1], "--id=" + self.record["gpu_uuid"])
        self.assertIn("gpu", value["diagnostics"])
        runtime.validate(value, 1)
        self.calls = []
        self.record["gpu_uuid"] = "GPU-bad;command"
        runtime.observe(self.agent, self.record, 1500)
        self.assertFalse(any(c[0] == "nvidia-smi" for c in self.calls))
        self.record["gpu_uuid"] = "GPU-11111111-1111-1111-1111-111111111111"
        original = self.agent._exec
        def fail_gpu(argv, **kwargs):
            if argv[0] == "nvidia-smi":
                raise subprocess.TimeoutExpired(argv, 3)
            return original(argv, **kwargs)
        self.agent._exec = fail_gpu
        value, _ = runtime.observe(self.agent, self.record, 1600)
        self.assertEqual(value["diagnostics"]["errors"]["gpu"], "timeout")
        self.assertIn("processes", value["diagnostics"])
        runtime.validate(value, 1)

    def test_new_attempt_does_not_inherit_staleness_or_diagnostics(self):
        self.record["runtime"]={"attempt":0,"log":{"changed_at":1},"diagnostics":{"checked_at":500}}
        value,_=runtime.observe(self.agent,self.record,1000)
        self.assertEqual(value["log"]["changed_at"],1000)
        self.assertNotIn("diagnostics",value)

    def test_slow_diagnostics_are_throttled_and_errors_do_not_mask_log(self):
        first,_=runtime.observe(self.agent,self.record,1000)
        self.record["runtime"]=first
        second,_=runtime.observe(self.agent,self.record,1300)
        self.record["runtime"]=second
        self.calls=[]
        third,_=runtime.observe(self.agent,self.record,1310)
        self.assertEqual([c[1] for c in self.calls],["logs"])
        self.assertEqual(third["diagnostics"]["checked_at"],1300)


class AgentRuntimeTests(unittest.TestCase):
    setUp=test_agent.AgentTests.setUp
    tearDown=test_agent.AgentTests.tearDown
    record=test_agent.AgentTests.record
    docker_spec=test_agent.AgentTests.docker_spec

    def test_timeout_persists_evidence_and_existing_log_survives(self):
        record=self.record("running",self.docker_spec())
        record.update(container_name="owned",log_tail="epoch92")
        container={"Id":"b"*64,"State":{"Status":"running"}}
        with patch.object(self.agent,"_inspect",return_value=container), patch.object(self.agent,"_exec",side_effect=subprocess.TimeoutExpired("docker",5)):
            self.agent._metrics(record)
        saved=self.agent.records()[0]
        self.assertEqual(saved["log_tail"],"epoch92")
        self.assertEqual(saved["runtime"]["log"]["error"],"timeout")
        self.assertEqual(self.agent._report(saved)["runtime"],saved["runtime"])
        self.assertEqual(saved["state"],"running")

    def test_wrong_labels_rejected_by_real_inspect(self):
        record=self.record("running",self.docker_spec())
        record["container_name"]="owned"
        wrong={"Id":"b"*64,"State":{"Status":"running"},"Config":{"Labels":{"expman.job":self.job_id,"expman.node":"other"}}}
        response=subprocess.CompletedProcess([],0,json.dumps([wrong]),"")
        with patch.object(self.agent,"_exec",return_value=response) as command:
            self.agent._metrics(record)
        self.assertEqual(command.call_count,1)
        self.assertEqual(record["runtime"]["container"]["error"],"read_failed")

    def test_null_runtime_from_legacy_state_is_safe(self):
        record = self.record("running")
        record["runtime"] = None
        self.assertNotIn("runtime", self.agent._report(record))

    def test_old_attempt_not_reported_after_resume(self):
        record=self.record("running")
        record["runtime"]={"attempt":0}
        self.assertNotIn("runtime",self.agent._report(record))


class HubRuntimeTests(unittest.TestCase):
    setUp=test_hub.HubTests.setUp
    tearDown=test_hub.HubTests.tearDown
    sync=test_hub.HubTests.sync
    assigned=test_hub.HubTests.assigned
    submit=test_hub.HubTests.submit
    report=test_hub.HubTests.report

    def value(self):
        return {"version":1,"attempt":1,"observed_at":1000,"container":{"status":"paused"},"log":{}}

    def test_roundtrip_replay_and_terminal_fence(self):
        identity=self.assigned()
        value=self.value()
        report=self.report(identity,1,"running",runtime=value)
        self.sync([report])
        saved=self.hub.job(identity)["runtime"]
        self.assertEqual(saved["container"]["status"],"paused")
        self.sync([report])
        self.assertEqual(self.hub.job(identity)["runtime"],saved)
        self.sync([self.report(identity,2,"succeeded")])
        self.sync([self.report(identity,3,"running",runtime=value)])
        self.assertIsNone(self.hub.job(identity)["runtime"])
        self.assertEqual(self.hub.job(identity)["state"],"succeeded")

    def test_invalid_batch_does_not_advance_heartbeat_or_job(self):
        identity=self.assigned()
        before=self.hub.job(identity)["seq"]
        for bad in (dict(self.value(),attempt=2), dict(self.value(),observed_at=float("nan")),
                    dict(self.value(),container={"status":"running","command":"sh"}),
                    dict(self.value(),diagnostics={"checked_at":1,"processes":"x"*17000})):
            with self.assertRaises(test_hub.APIError):
                self.sync([self.report(identity,1,"running",runtime=bad)])
        self.assertEqual(self.hub.job(identity)["seq"],before)

    def test_foreign_node_cannot_overwrite_runtime(self):
        identity=self.assigned()
        with self.assertRaises(test_hub.APIError):
            self.sync([self.report(identity,1,"running",runtime=self.value())],node="node-b")
        self.assertIsNone(self.hub.job(identity)["runtime"])

    def test_legacy_report_clears_current_runtime_not_forged_as_live(self):
        identity=self.assigned()
        self.sync([self.report(identity,1,"running",runtime=self.value())])
        self.sync([self.report(identity,2,"running")])
        self.assertIsNone(self.hub.job(identity)["runtime"])


if __name__=="__main__":
    unittest.main()
