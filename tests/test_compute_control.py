"""Intent, execution confirmation, and bounded resource cleanup transitions."""
import copy
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from expman import common, compute_policy
from expman.agent import Agent
from expman.compute_control import observe


def availability(mode, **extra):
    snap = {}
    compute_policy.apply(snap, compute_policy.validate({"mode": mode, **extra}))
    return snap["availability"]


def task(state="running", backend="docker", **extra):
    return {"id": "a"*32, "state": state, "spec": {"backend": backend},
            "command_ack": 0, "container_name": "owned-experiment", "ever_started": True,
            "execution_stopped": state == "paused", **extra}


class ComputeControlTests(unittest.TestCase):
    def setUp(self):
        self.tasks, self.meta = [], {}
        self.agent = SimpleNamespace(config={"stop_grace_seconds": 60, "node_id": "70ti", "allow_demo": True}, last_error="", preparation=None,
            prep_mutex=threading.Lock(), prep_process=None, project_delivery=SimpleNamespace(installing=None),
            processes={}, records=lambda: self.tasks, _inspect=Mock(return_value=None), _exec=Mock(return_value=SimpleNamespace(stdout="")),
            _set_meta=lambda key, value: self.meta.update({key: value}),
            _save=lambda record, **fields: record.update(fields))
        self.agent._command = Mock(side_effect=lambda record, *_: record.update(stop_at=common.now()))
        self.agent._cancel_preparation = lambda: Agent._cancel_preparation(self.agent)

    def tick(self, mode):
        snap = {"availability": availability(mode)}
        Agent._availability_tick(self.agent, snap)
        return snap["compute_control"]

    def test_admission_pause_neither_stops_nor_releases_running_work(self):
        self.tasks.append(task())
        receipt = self.tick("drain")
        self.assertEqual(receipt["phase"], "admission_paused")
        self.assertFalse(receipt["resources_released"])
        self.agent._command.assert_not_called()
        self.agent._exec.assert_not_called()
        self.assertNotIn("resource_release", self.tasks[0])

    def test_stop_is_cooperative_and_holds_local_work_without_cleanup(self):
        self.tasks.extend([task(), task(state="ready", ever_started=False)])
        receipt = self.tick("stop")
        self.assertEqual(receipt["phase"], "stopping")
        self.assertFalse(receipt["experiments_stopped"])
        self.agent._command.assert_called_once()
        self.assertEqual(self.tasks[1]["state"], "paused")
        self.assertTrue(all(not t.get("resource_release") for t in self.tasks))
        self.agent._inspect.return_value = {"State": {"Status": "exited"}}
        self.tasks[0].update(state="paused", execution_stopped=True)
        receipt = self.tick("stop")
        self.assertEqual(receipt["phase"], "stopped")
        self.assertTrue(receipt["experiments_stopped"])
        self.assertFalse(receipt["resources_released"])
        self.assertFalse(any(call.args[0][1] == "rm" for call in self.agent._exec.call_args_list))
        self.assertEqual(self.tasks[0]["container_name"], "owned-experiment")

    def test_release_waits_for_exit_and_removes_only_verified_stopped_container(self):
        self.tasks.append(task())
        self.assertEqual(self.tick("release")["phase"], "stopping")
        self.assertTrue(self.tasks[0]["resource_release"])
        self.agent._exec.assert_not_called()
        self.tasks[0].update(state="paused", execution_stopped=True)
        self.agent._inspect.side_effect = [{"Id": "b"*64, "State": {"Status": "exited"}}, None]
        receipt = self.tick("release")
        self.agent._exec.assert_any_call(["docker", "rm", "b"*64], timeout=30)
        self.assertEqual(self.agent._exec.call_count, 2)
        self.assertEqual(receipt["phase"], "released")
        self.assertTrue(receipt["resources_released"])
        self.assertTrue(receipt["runtime_retained"])
        self.assertIsNone(self.tasks[0]["container_name"])

    def test_terminal_label_does_not_prove_processes_released(self):
        self.tasks.append(task(state="paused"))
        for state in ({"Running": True}, {"Restarting": True}, {"Paused": True}, {"Status": "unknown"}):
            with self.subTest(state=state):
                self.agent._inspect.return_value = {"Id": "b"*64, "State": state}
                receipt = self.tick("release")
                self.assertEqual(receipt["phase"], "blocked")
                self.assertFalse(receipt["resources_released"])
        self.agent._exec.assert_not_called()

    def test_failed_or_foreign_inspection_never_reports_release(self):
        self.tasks.append(task(state="paused"))
        for error in (ValueError("ownership mismatch"), RuntimeError("Docker unavailable"),
                      subprocess.TimeoutExpired(["docker"], 5)):
            self.agent._inspect.side_effect = error
            receipt = self.tick("release")
            self.assertEqual(receipt["phase"], "blocked")
            self.assertFalse(receipt["resources_released"])
        self.agent._exec.assert_not_called()

    def test_remove_failure_and_reappearing_container_are_incomplete(self):
        self.tasks.append(task(state="paused"))
        self.agent._inspect.return_value = {"Id": "b"*64, "State": {"Status": "exited"}}
        self.agent._exec.side_effect = RuntimeError("remove denied")
        self.assertEqual(self.tick("release")["phase"], "blocked")
        self.agent._exec.side_effect = None
        self.assertEqual(self.tick("release")["phase"], "blocked")
        self.assertEqual(self.tasks[0]["container_name"], "owned-experiment")

    def test_preparation_canceled_but_not_assumed_finished(self):
        cancel = threading.Event()
        process = Mock()
        process.poll.return_value = None
        self.agent.preparation = {"cancel": cancel}
        self.agent.prep_process = process
        receipt = self.tick("release")
        self.assertTrue(cancel.is_set())
        process.terminate.assert_called_once()
        self.assertEqual(receipt["phase"], "releasing")
        self.assertFalse(receipt["resources_released"])
        self.agent.preparation = None
        self.agent.project_delivery.installing = {"thread": "still working"}
        self.assertEqual(self.tick("release")["phase"], "releasing")

    def test_unknown_process_or_missing_handle_prevents_release(self):
        self.tasks.append(task(state="paused", container_name=None, execution_stopped=False))
        self.assertEqual(self.tick("release")["phase"], "blocked")
        self.tasks[0] = task(state="paused", backend="demo", worker_pid=123)
        process = Mock()
        process.poll.return_value = None
        self.agent.processes[self.tasks[0]["id"]] = process
        self.assertEqual(self.tick("release")["phase"], "blocked")

    def test_unrecorded_owned_container_blocks_release_without_killing_it(self):
        self.tasks.append(task(state="paused", execution_stopped=True, container_name=None))
        self.agent._exec.return_value = SimpleNamespace(stdout="orphan-container\n")
        receipt = self.tick("release")
        self.assertEqual(receipt["phase"], "blocked")
        self.assertFalse(receipt["resources_released"])
        self.assertEqual(self.agent._exec.call_args.args[0][1], "ps")

    def test_receipt_is_bound_to_effective_local_policy_and_expiration(self):
        snap = {"local_compute_policy": compute_policy.validate({"mode": "release", "reason": "游戏"})}
        compute_policy.apply(snap, compute_policy.validate({"revision": 4, "mode": "stop"}))
        self.assertEqual(snap["availability"]["mode"], "release")
        self.assertEqual(snap["availability"]["source"], "local")
        snap["compute_control"] = observe(self.agent, snap["availability"])
        self.assertTrue(compute_policy.control_matches(snap))
        previous = copy.deepcopy(snap)
        snap["local_compute_policy"] = compute_policy.validate({"mode": "drain"})
        compute_policy.apply(snap, compute_policy.validate({"revision": 4, "mode": "stop"}))
        self.assertEqual(snap["availability"]["mode"], "stop")
        self.assertFalse(compute_policy.control_matches(snap))
        snap["availability"] = compute_policy.evaluate({"mode": "release", "until": 10}, timestamp=11)
        self.assertFalse(compute_policy.control_matches(snap))
        self.assertTrue(compute_policy.control_matches(previous))

    def test_legacy_node_admission_switch_cannot_claim_release_or_available(self):
        snap = {"node_mode": "drain"}
        compute_policy.apply(snap, compute_policy.validate({}))
        self.assertEqual(snap["availability"]["mode"], "drain")
        self.assertFalse(snap["availability"]["release_resources"])
        self.assertEqual(observe(self.agent, snap["availability"])["phase"], "admission_paused")
        compute_policy.apply(snap, compute_policy.validate({"mode": "release"}))
        self.assertEqual(snap["availability"]["mode"], "release")

    def test_invalid_release_receipts_are_rejected_at_sync_boundary(self):
        snap = {"availability": availability("release")}
        receipt = observe(self.agent, snap["availability"])
        compute_policy.validate_snapshot({**snap, "compute_control": receipt})
        for changes in ({"resources_released": False}, {"experiments_stopped": False},
                        {"intent": "x"*64}, {"phase": "available"}, {"policy": {}}, {"observed_at": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                compute_policy.validate_snapshot({**snap, "compute_control": {**receipt, **changes}})


if __name__ == "__main__":
    unittest.main()
