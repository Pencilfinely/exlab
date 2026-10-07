"""Simulated experiments over real HTTP/SQLite; no scientific training is run."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

from expman import common, result_protocol as protocol
from expman.agent import Agent
from expman.hub import APIError, Hub, make_server
from expman.sdk import Run
from tests.support import temporary_directory


class ResultDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.hub = Hub(self.root / "hub")
        self.token = self.hub.add_node("worker")
        self.other_token = self.hub.add_node("other")
        self.server = make_server(self.hub, port=0)
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        config = self.root / "node.json"
        common.atomic_json(config, {"node_id": "worker", "hub_url": self.url, "token": self.token,
            "root": str(self.root / "node"), "allow_demo": True, "policy": {"max_prefetch": 32}})
        self.agent = Agent(config)
        self.delivery = self.agent.result_delivery
        self.counter = 0

    def tearDown(self):
        self.agent.close()
        self.server.shutdown()
        self.server.server_close()
        self.hub.close()
        self.temporary.__exit__(None, None, None)

    def job(self, *, mode="lightweight", state="running", project="research"):
        self.counter += 1
        spec = {"backend": "demo", "project_id": project, "params": {"seed": 42},
                "result_delivery": {"mode": mode}}
        identity = self.hub.submit({"spec": spec, "request_id": "job-" + str(self.counter)})["ids"][0]
        snapshot = self.agent.snapshot()
        response = self.hub.sync("worker", {"node_id": "worker", "snapshot": snapshot, "reports": []})
        owned = next(job for job in response["jobs"] if job["id"] == identity)
        record = {"id": identity, "spec": owned["spec"], "state": state, "attempt": 1,
                  "seq": 0, "command_ack": 0, "prepared": True, "metrics": {}, "exit_code": 0}
        self.agent._save(record)
        self.agent._output(record).mkdir(parents=True, exist_ok=True)
        self.hub.sync("worker", {"node_id": "worker", "snapshot": snapshot,
            "reports": [{"id": identity, "seq": 1, "attempt": 1, "state": state, "metrics": {}}]})
        return record, Run(output=self.agent._output(record), params={"seed": 42})

    def publish(self, run, experiment="trial", **changes):
        value = {"status": "succeeded", "metrics": {"validation": {"score": 0.123456789012345},
                 "test": {"score": 0.987654321098765}, "protocol": "example-v1"},
                 "execution": {"status": "succeeded", "stages": {"training": {"status": "succeeded", "exit_code": 0},
                    "test": {"status": "succeeded", "exit_code": 0}}}, **changes}
        return run.publish_result(experiment, value)

    def request(self, endpoint, payload, token=None):
        return common.api_request(self.url + endpoint, token or self.hub.config["admin_token"], payload)

    def assert_eventually(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.025)
        self.fail("Timed out waiting for result delivery")

    def test_ten_experiments_have_one_default_result_each_and_no_checkpoint_upload(self):
        record, run = self.job()
        record["environment"] = {"image": "fixture-image:actual"}
        record["cached_environments"] = [{"image": "fixture-image:actual", "image_id": "sha256:fixture"}]
        self.agent._save(record)
        for index in range(10):
            checkpoint = run.output / f"checkpoint-{index}.bin"
            checkpoint.write_bytes(b"weights" * 1000)
            self.publish(run, "trial-" + str(index), evidence=[run.evidence(checkpoint, "checkpoint")],
                         provenance={"platform": {"agent_version": "producer-claim"}})
        self.delivery.results_once()
        detail = self.hub.job(record["id"])
        self.assertEqual(len(detail["experiment_results"]), 10)
        self.assertEqual(len(detail["artifacts"]), 10)
        self.assertTrue(all(item["name"].endswith(".json") for item in detail["artifacts"]))
        self.assertEqual(self.agent.db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0], 0)
        self.assertEqual(len(list(run.output.glob("checkpoint-*.bin"))), 10)
        self.assertEqual(detail["experiment_results"][0]["summary"]["metrics"]["test"]["score"], 0.987654321098765)
        platform = detail["experiment_results"][0]["summary"]["provenance"]["platform"]
        self.assertEqual(platform["agent_version"], self.agent.snapshot()["agent_version"])
        self.assertEqual(platform["job_config_sha256"], hashlib.sha256(protocol.encoded(record["spec"]["params"])).hexdigest())
        self.assertEqual(platform["container_image_id"], "sha256:fixture")
        self.assertEqual(platform["environment"], record["environment"])

    def test_first_of_five_sequential_experiments_is_visible_during_the_batch(self):
        record, run = self.job()
        self.publish(run, "trial-1")
        self.delivery.results_once()
        first = self.hub.job(record["id"])
        self.assertEqual(first["state"], "running")
        self.assertEqual(len(first["experiment_results"]), 1)
        for index in range(2, 6):
            self.publish(run, "trial-" + str(index))
            self.delivery.results_once()
        run.finish({"status": "succeeded", "batch": True})
        record["state"] = "succeeded"
        self.agent._save(record)
        self.agent._snapshot_files(record)
        self.delivery.results_once()
        self.assertEqual(len(self.hub.job(record["id"])["artifacts"]), 5)

    def test_result_enters_independent_lane_within_five_seconds_while_large_hash_is_blocked(self):
        record, run = self.job()
        big, _ = self.job(mode="legacy")
        data = b"checkpoint" * 10000
        digest = hashlib.sha256(data).hexdigest()
        hashing, release = threading.Event(), threading.Event()
        original = common.sha256_file
        errors = []
        def delayed_hash(path):
            if Path(path).name == digest + ".part":
                hashing.set()
                if not release.wait(8):
                    raise TimeoutError("Controlled checkpoint delay")
            return original(path)
        def upload():
            try:
                common.api_request(self.url + "/api/upload", self.token, {"job_id": big["id"], "name": "best.bin",
                    "sha256": digest, "size": len(data), "offset": 0, "data": base64.b64encode(data).decode()})
            except Exception as exc:
                errors.append(exc)
        with patch("expman.hub.sha256_file", side_effect=delayed_hash):
            thread = threading.Thread(target=upload)
            thread.start()
            try:
                self.assertTrue(hashing.wait(3))
                self.delivery.start()
                started = time.monotonic()
                self.publish(run)
                self.assert_eventually(lambda: len(self.hub.job(record["id"])["experiment_results"]) == 1)
                self.assertLess(time.monotonic() - started, 5)
                self.assertTrue(thread.is_alive())
            finally:
                release.set()
                thread.join(timeout=5)
        self.assertEqual(errors, [])

    def test_lost_receipt_resends_same_bytes_and_persists_one_result(self):
        record, run = self.job()
        self.publish(run)
        original = common.api_request
        dropped = False
        def lose_ack(url, token, payload=None, timeout=10):
            nonlocal dropped
            response = original(url, token, payload, timeout)
            if url.endswith("/api/results") and not dropped:
                dropped = True
                raise TimeoutError("Lost receipt after commit")
            return response
        with patch("expman.result_delivery.common.api_request", side_effect=lose_ack):
            self.delivery.results_once()
            with self.agent.db:
                self.agent.db.execute("UPDATE node_results SET next_retry=0")
            self.delivery.results_once()
        self.assertTrue(dropped)
        self.assertEqual(len(self.hub.job(record["id"])["artifacts"]), 1)
        self.assertEqual(self.agent.db.execute("SELECT status,retries FROM node_results").fetchone(), ("delivered", 1))
        self.assertEqual(self.agent.records()[0]["attempt"], 1)

    def test_conflicting_final_content_is_recorded_without_overwriting(self):
        record, run = self.job()
        path = self.publish(run)
        self.delivery.results_once()
        original = self.hub.job(record["id"])["experiment_results"][0]["sha256"]
        value = common.read_json(path)
        value["result"]["metrics"]["test"]["score"] = 0.1
        common.atomic_json(path, value)
        self.delivery.results_once()
        detail = self.hub.job(record["id"])
        self.assertEqual(detail["experiment_results"][0]["sha256"], original)
        self.assertEqual(len(detail["result_conflicts"]), 1)
        self.assertEqual(len(detail["artifacts"]), 1)
        self.assertEqual(self.agent.db.execute("SELECT COUNT(*) FROM node_results WHERE status='conflict'").fetchone()[0], 1)

    def test_disconnect_reconnect_keeps_attempt_and_prioritizes_small_results(self):
        record, run = self.job()
        self.publish(run)
        with patch.object(self.delivery, "_request", side_effect=OSError("Hub offline")):
            self.delivery.results_once()
        self.assertEqual(self.agent.db.execute("SELECT status FROM node_results").fetchone()[0], "retrying")
        with self.agent.db:
            self.agent.db.execute("UPDATE node_results SET next_retry=0")
        with patch.object(self.agent, "_start", side_effect=AssertionError("Transport must never start execution")):
            self.delivery.results_once()
        self.assertEqual(self.hub.job(record["id"])["experiment_results"][0]["attempt"], 1)
        self.assertEqual(self.agent.records()[0]["state"], "running")

    def test_request_one_checkpoint_only_and_keep_evidence_after_output_removal(self):
        record, run = self.job()
        best, last = run.output / "best.bin", run.output / "last.bin"
        best.write_bytes(b"best weights" * 80000)
        last.write_bytes(b"last weights" * 10000)
        self.publish(run, evidence=[run.evidence(best, "best-checkpoint"), run.evidence(last, "last-checkpoint")])
        self.delivery.results_once()
        for _ in range(2):
            self.delivery.control_once()
        detail = self.hub.job(record["id"])
        item = next(item for item in detail["evidence"] if item["name"] == "best.bin")
        best.unlink()
        response = self.request("/api/evidence/request", {"request_id": "best-request", "job_id": record["id"],
            "attempt": 1, "experiment_id": "trial", "evidence_ids": [item["evidence_id"]]})
        for _ in range(5):
            self.delivery.control_once()
        response = self.hub.transfer_request(response["request_id"])
        self.assertEqual(response["status"], "completed", response)
        detail = self.hub.job(record["id"])
        self.assertEqual(len(detail["artifacts"]), 2)
        transferred = next(item for item in detail["artifacts"] if item["name"].endswith("best.bin"))
        self.assertEqual(transferred["sha256"], item["sha256"])
        self.assertEqual(common.sha256_file(self.hub.artifact(record["id"], transferred["sha256"])), item["sha256"])
        self.assertTrue(last.exists())
        self.assertEqual(next(item for item in detail["evidence"] if item["name"] == "last.bin")["transfer_status"], "retained")

    def test_missing_retained_evidence_reports_unavailable_without_rerun(self):
        record, run = self.job()
        file = run.output / "best.bin"
        file.write_bytes(b"best")
        self.publish(run, evidence=[run.evidence(file)])
        self.delivery.results_once()
        self.delivery.control_once()
        item = self.hub.job(record["id"])["evidence"][0]
        retained = self.agent.db.execute("SELECT path FROM node_evidence").fetchone()[0]
        Path(retained).unlink()
        self.hub.request_evidence({"request_id": "missing", "job_id": record["id"], "attempt": 1,
            "experiment_id": "trial", "evidence_ids": [item["evidence_id"]]})
        self.delivery.control_once()
        detail = self.hub.job(record["id"])
        self.assertEqual(detail["evidence"][0]["availability"], "missing")
        self.assertEqual(self.hub.transfer_request("missing")["status"], "failed")
        self.assertEqual(self.agent.records()[0]["attempt"], 1)

    def test_remote_audit_cannot_establish_independent_acceptance(self):
        record, run = self.job()
        self.publish(run, verification={"remote": "passed", "independent": "passed"})
        self.delivery.results_once()
        item = self.hub.job(record["id"])["experiment_results"][0]
        self.assertEqual(item["independent"], "pending")
        with self.assertRaises(APIError):
            self.hub.verify_result({"job_id": record["id"], "attempt": 1, "experiment_id": "trial",
                                   "sha256": item["sha256"], "status": "passed"})
        self.hub.verify_result({"job_id": record["id"], "attempt": 1, "experiment_id": "trial",
            "sha256": item["sha256"], "status": "passed", "references": ["independent tensor review record"]})
        self.assertEqual(self.hub.job(record["id"])["experiment_results"][0]["independent"], "passed")

    def test_quarantined_initialization_stays_quarantined_when_result_arrives(self):
        record, run = self.job()
        self.publish(run, verification={"remote": "passed", "independent": "quarantined"})
        self.delivery.results_once()
        self.assertEqual(self.hub.job(record["id"])["experiment_results"][0]["independent"], "quarantined")
        self.assertEqual(self.hub.state()["jobs"][0]["delivery"]["independently_accepted"], 0)

    def test_failure_has_no_fabricated_test_metrics(self):
        record, run = self.job(state="failed")
        record["exit_code"] = 9
        self.agent._save(record, detail="Training command failed")
        self.agent._snapshot_files(record)
        self.delivery.results_once()
        result = self.hub.job(record["id"])["experiment_results"][0]["summary"]
        self.assertEqual(result["execution"]["exit_code"], 9)
        self.assertEqual(result["metrics"]["test"], {})
        self.assertEqual(result["execution"]["stages"]["test"]["status"], "not_run")

    def test_progress_is_metadata_and_no_implicit_result_is_uploaded(self):
        record, run = self.job()
        run.progress("trial", "test", detail="Training finished")
        self.delivery.control_once()
        detail = self.hub.job(record["id"])
        self.assertEqual(detail["experiment_progress"][0]["phase"], "test")
        self.assertEqual(detail["artifacts"], [])

    def test_oversized_summary_warns_and_preserves_fields(self):
        record, run = self.job()
        self.publish(run, explanation="x" * (70 * 1024))
        self.delivery.results_once()
        item = self.hub.job(record["id"])["experiment_results"][0]
        raw = Path(self.agent.db.execute("SELECT path FROM node_results").fetchone()[0]).read_bytes()
        response = self.request("/api/results", {"sha256": item["sha256"], "data": base64.b64encode(raw).decode()}, self.token)
        self.assertTrue(response["warnings"])
        self.assertEqual(len(item["summary"]["summary"]["explanation"]), 70 * 1024)

    def test_queue_migration_dry_run_and_apply_preserve_offsets_sources_and_other_projects(self):
        record, run = self.job(mode="legacy", state="succeeded")
        other, other_run = self.job(mode="legacy", state="succeeded", project="other-project")
        for current, producer in ((record, run), (other, other_run)):
            producer.finish({"status": "succeeded", "test_metrics": {"score": 0.75}})
            (producer.output / "best.bin").write_bytes(b"weights" * 100000)
            self.agent._snapshot_files(current)
        checkpoint = self.agent.db.execute("SELECT * FROM uploads WHERE job_id=? AND name='best.bin'", (record["id"],)).fetchone()
        original_offsets = checkpoint[5]
        before_tasks = copy.deepcopy(self.agent.records())
        before_other = self.agent.db.execute("SELECT * FROM uploads WHERE job_id=?", (other["id"],)).fetchall()
        self.hub.migrate_uploads({"request_id": "preview", "project_id": "research", "job_ids": [record["id"]]})
        self.delivery.control_once()
        plan = self.hub.transfer_request("preview")
        self.assertEqual(plan["status"], "completed", plan)
        self.assertEqual(plan["report"]["estimated_reduced_bytes"], len(b"weights" * 100000))
        self.assertEqual(self.agent.db.execute("SELECT COUNT(*) FROM legacy_upload_policy").fetchone()[0], 0)
        self.hub.migrate_uploads({"request_id": "apply", "action": "apply", "plan_id": "preview"})
        self.delivery.control_once()
        self.delivery.results_once()
        applied = self.hub.transfer_request("apply")
        self.assertEqual(applied["status"], "completed", applied)
        self.assertEqual(self.agent.records(), before_tasks)
        self.assertEqual(self.agent.db.execute("SELECT * FROM uploads WHERE job_id=?", (other["id"],)).fetchall(), before_other)
        self.assertEqual(self.agent.db.execute("SELECT offset,complete FROM uploads WHERE job_id=? AND name='best.bin'", (record["id"],)).fetchone(), (original_offsets, 0))
        self.assertTrue(Path(checkpoint[4]).is_file())
        self.assertTrue((run.output / "best.bin").is_file())
        self.assertEqual(len(self.hub.job(record["id"])["experiment_results"]), 1)
        duplicate = self.hub.migrate_uploads({"request_id": "apply", "action": "apply", "plan_id": "preview"})
        self.assertEqual(duplicate["report"], applied["report"])

    def test_legacy_queue_yields_at_acknowledged_chunk_boundary(self):
        record, run = self.job(mode="legacy", state="succeeded")
        data = b"w" * (2 * 512 * 1024)
        (run.output / "best.bin").write_bytes(data)
        self.agent._snapshot_files(record)
        chunks = []
        def request(url, token, payload, **kwargs):
            block = base64.b64decode(payload["data"])
            if not block:
                return {"offset": 0, "complete": False}
            chunks.append(block)
            with self.agent.db:
                self.agent.db.execute("INSERT INTO legacy_upload_policy VALUES (?,?,?,'retained')", (record["id"], "best.bin", payload["sha256"]))
            return {"offset": len(block), "complete": False}
        self.agent.online = True
        with patch("expman.agent.common.api_request", side_effect=request):
            self.agent._uploads(chunks=10)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(self.agent.db.execute("SELECT offset,complete FROM uploads").fetchone(), (512 * 1024, 0))
        self.assertTrue((run.output / "best.bin").is_file())

    def test_old_agent_or_offline_node_returns_explicit_unsupported_unavailable(self):
        record, run = self.job()
        self.hub.sync("worker", {"node_id": "worker", "snapshot": {"allow_demo": True}, "reports": []})
        with self.assertRaisesRegex(APIError, "not supported"):
            self.hub.migrate_uploads({"request_id": "old", "project_id": "research", "job_ids": [record["id"]]})
        with self.hub.transaction():
            self.hub.db.execute("UPDATE nodes SET snapshot=?,last_seen=0 WHERE id='worker'", (json.dumps(self.agent.snapshot()),))
        with self.assertRaisesRegex(APIError, "offline"):
            self.hub.migrate_uploads({"request_id": "offline", "project_id": "research", "job_ids": [record["id"]]})

    def test_new_lightweight_job_waits_for_capable_node(self):
        identity = self.hub.submit({"spec": {"backend": "demo"}, "request_id": "new"})["ids"][0]
        self.hub.sync("worker", {"node_id": "worker", "snapshot": {"allow_demo": True}, "reports": []})
        self.assertIsNone(self.hub.job(identity)["node_id"])

    def test_authenticated_roles_and_evidence_scope_are_enforced(self):
        record, run = self.job()
        self.publish(run)
        self.delivery.results_once()
        raw = Path(self.agent.db.execute("SELECT path FROM node_results").fetchone()[0]).read_bytes()
        payload = {"sha256": hashlib.sha256(raw).hexdigest(), "data": base64.b64encode(raw).decode()}
        with self.assertRaises(urllib.error.HTTPError) as wrong_node:
            self.request("/api/results", payload, self.other_token)
        self.assertEqual(wrong_node.exception.code, 403)
        wrong_node.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as wrong_role:
            self.request("/api/results", payload)
        self.assertEqual(wrong_role.exception.code, 403)
        wrong_role.exception.close()
        with self.assertRaises(ValueError):
            self.hub.request_evidence({"request_id": "arbitrary", "job_id": record["id"], "attempt": 1,
                "experiment_id": "trial", "evidence_ids": ["../../credentials"]})

    def test_result_protocol_rejects_recursive_paths_and_nonfinite_metrics(self):
        with self.assertRaises(ValueError):
            protocol.delivery_config({"files": [{"experiment_id": "x", "path": "run_output/**/*"}]})
        record, run = self.job()
        with self.assertRaises(ValueError):
            self.publish(run, metrics={"validation": {}, "test": {"bad": float("nan")}})
        with self.assertRaises(ValueError):
            run.publish_result("../escape", {})

    def test_malformed_terminal_json_yields_a_failure_result_and_retains_the_source(self):
        record, run = self.job(state="succeeded")
        (run.output / "result.json").write_text("{bad json", encoding="utf-8")
        self.delivery.results_once()
        value = self.hub.job(record["id"])["experiment_results"][0]["summary"]
        self.assertEqual(value["execution"]["status"], "failed")
        self.assertEqual(value["execution"]["failure_stage"], "result_registration")
        self.assertEqual(value["metrics"]["test"], {})
        self.assertTrue((run.output / "result.json").is_file())

    def test_batch_migration_mapping_has_no_extra_implicit_result(self):
        record, run = self.job(mode="legacy", state="succeeded")
        mappings = []
        for identity in ("beauty", "sports"):
            path = run.output / identity / "000_result.json"
            common.atomic_json(path, {"status": "succeeded", "test_metrics": {"score": 0.75}})
            (path.parent / "best.bin").write_bytes(identity.encode())
            mappings.append({"job_id": record["id"], "experiment_id": identity, "path": identity + "/000_result.json"})
        self.agent._snapshot_files(record)
        self.hub.migrate_uploads({"request_id": "batch-preview", "project_id": "research", "job_ids": [record["id"]], "result_files": mappings})
        self.delivery.control_once()
        self.hub.migrate_uploads({"request_id": "batch-apply", "action": "apply", "plan_id": "batch-preview"})
        self.delivery.control_once()
        self.delivery.results_once()
        detail = self.hub.job(record["id"])
        self.assertEqual({result["experiment_id"] for result in detail["experiment_results"]}, {"beauty", "sports"})
        self.assertEqual(len(detail["artifacts"]), 2)
        self.assertEqual(next(item for item in detail["evidence"] if item["name"] == "sports/best.bin")["experiment_id"], "sports")

    def test_retained_snapshot_survives_in_place_source_changes_on_a_later_attempt(self):
        record, run = self.job()
        source = run.output / "best.bin"
        source.write_bytes(b"original checkpoint")
        expected = run.evidence(source)
        self.publish(run, evidence=[expected])
        self.delivery.results_once()
        self.delivery.control_once()
        source.write_bytes(b"next attempt overwrites this checkpoint in place")
        row = self.agent.db.execute("SELECT path,sha256 FROM node_evidence").fetchone()
        self.assertEqual(Path(row[0]).read_bytes(), b"original checkpoint")
        self.assertEqual(row[1], expected["sha256"])

    def test_evidence_ack_loss_resumes_at_server_confirmed_offset(self):
        record, run = self.job()
        source = run.output / "best.bin"
        source.write_bytes(b"b" * (512 * 1024 + 123))
        self.publish(run, evidence=[run.evidence(source)])
        self.delivery.results_once()
        self.delivery.control_once()
        item = self.hub.job(record["id"])["evidence"][0]
        self.hub.request_evidence({"request_id": "evidence-ack", "job_id": record["id"], "attempt": 1,
            "experiment_id": "trial", "evidence_ids": [item["evidence_id"]]})
        original = self.delivery._request
        sent_offsets = []
        dropped = False
        def lose_ack(endpoint, payload, **kwargs):
            nonlocal dropped
            response = original(endpoint, payload, **kwargs)
            if endpoint == "/api/evidence/upload" and payload["data"]:
                sent_offsets.append(payload["offset"])
                if not dropped:
                    dropped = True
                    raise TimeoutError("Evidence ACK lost after append")
            return response
        with patch.object(self.delivery, "_request", side_effect=lose_ack):
            self.delivery.control_once()
            with self.agent.db:
                self.agent.db.execute("UPDATE node_evidence SET next_retry=0")
            self.delivery.control_once()
        self.assertEqual(sent_offsets, [0, 512 * 1024])
        self.assertEqual(self.hub.transfer_request("evidence-ack")["status"], "completed")

    def test_result_retry_survives_agent_restart_without_rerunning(self):
        record, run = self.job(state="succeeded")
        self.publish(run)
        with patch.object(self.delivery, "_request", side_effect=OSError("offline")):
            self.delivery.results_once()
        config = self.agent.config_path
        self.agent.close()
        self.agent = Agent(config)
        self.delivery = self.agent.result_delivery
        with self.agent.db:
            self.agent.db.execute("UPDATE node_results SET next_retry=0")
        self.delivery.results_once()
        self.assertEqual(len(self.hub.job(record["id"])["experiment_results"]), 1)
        self.assertEqual(self.agent.records()[0]["attempt"], 1)

    def test_legacy_attempt_without_file_bindings_is_explicitly_unsupported(self):
        record, run = self.job(mode="legacy", state="succeeded")
        run.finish({"status": "succeeded"})
        self.agent._snapshot_files(record)
        record["attempt"] = 2
        self.agent._save(record)
        self.hub.migrate_uploads({"request_id": "unknown-attempt", "project_id": "research", "job_ids": [record["id"]]})
        self.delivery.control_once()
        report = self.hub.transfer_request("unknown-attempt")
        self.assertEqual(report["status"], "unsupported")
        self.assertIn("attempt", report["report"]["error"])
        self.assertEqual(self.agent.db.execute("SELECT COUNT(*) FROM legacy_upload_policy").fetchone()[0], 0)
        self.assertEqual(self.agent.records()[0]["attempt"], 2)

    def test_missing_queue_cache_prevents_apply_before_any_queue_change(self):
        record, run = self.job(mode="legacy", state="succeeded")
        run.finish({"status": "succeeded"})
        self.agent._snapshot_files(record)
        self.hub.migrate_uploads({"request_id": "cache-preview", "project_id": "research", "job_ids": [record["id"]]})
        self.delivery.control_once()
        cache = self.agent.db.execute("SELECT path FROM uploads").fetchone()[0]
        Path(cache).unlink()
        self.hub.migrate_uploads({"request_id": "cache-apply", "action": "apply", "plan_id": "cache-preview"})
        self.delivery.control_once()
        self.assertEqual(self.hub.transfer_request("cache-apply")["status"], "failed")
        self.assertEqual(self.agent.db.execute("SELECT COUNT(*) FROM legacy_upload_policy").fetchone()[0], 0)
        self.assertTrue((run.output / "result.json").is_file())

    def test_partial_ack_cannot_masquerade_as_a_completed_legacy_file(self):
        record, run = self.job(mode="legacy", state="succeeded")
        (run.output / "best.bin").write_bytes(b"weights")
        self.agent._snapshot_files(record)
        self.agent.online = True
        with patch("expman.agent.common.api_request", return_value={"offset": 1, "complete": True}):
            self.agent._uploads()
        self.assertEqual(self.agent.db.execute("SELECT complete FROM uploads").fetchone()[0], 0)
        self.assertIn("Incomplete", self.agent.snapshot()["delivery"]["last_error"])

    def test_spliced_checkpoint_selection_is_not_accepted_as_a_scientific_result(self):
        record, run = self.job(state="succeeded")
        path = self.publish(run, selection={"checkpoint": ["best-for-hr", "best-for-ndcg"]})
        self.delivery.results_once()
        result = self.hub.job(record["id"])["experiment_results"][0]
        self.assertEqual(result["summary"]["execution"]["status"], "failed")
        self.assertEqual(result["independent"], "pending")
        self.assertEqual(result["summary"]["metrics"]["test"], {})
        self.assertTrue(path.is_file())

    def test_transport_errors_are_redacted_and_do_not_change_execution(self):
        record, run = self.job()
        self.publish(run)
        before = copy.deepcopy(self.agent.records())
        with patch.object(self.delivery, "_request", side_effect=OSError("Bearer " + self.token + " password=secret")):
            self.delivery.results_once()
        snapshot = self.agent.snapshot()["delivery"]
        self.assertNotIn(self.token, snapshot["last_error"])
        self.assertNotIn("secret", snapshot["last_error"])
        self.assertEqual(snapshot["pending_results"], 1)
        self.assertEqual(snapshot["retry_count"], 1)
        self.assertGreater(snapshot["next_retry_at"], time.time())
        self.assertEqual(self.agent.records(), before)
