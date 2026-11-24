import time
import unittest
from unittest.mock import patch

from expman import common, execution_lease, lease_guard
from expman.hub import Hub
from tests import test_dynamic_compute as dynamic


class LeaseTests(unittest.TestCase):
    setUp = dynamic.DynamicComputeTests.setUp
    tearDown = dynamic.DynamicComputeTests.tearDown
    sync = dynamic.DynamicComputeTests.sync
    report = dynamic.DynamicComputeTests.report
    manifest = dynamic.DynamicComputeTests.manifest
    upload = dynamic.DynamicComputeTests.upload

    def job(self):
        env = {"profile": "gpu", "image": "example@sha256:" + "b" * 64}
        repo = "https://example.test/repo.git"
        self.snap = {**dynamic.snapshot(), "gpus": [{"uuid": "GPU-one", "profiles": ["gpu"], "total_mb": 12000}],
                     "allowed_repos": [repo], "profiles": {"gpu": env["image"]}}
        self.snap["capabilities"].append(execution_lease.CAPABILITY)
        spec = {"backend": "docker", "source": {"repo": repo, "commit": "a" * 40}, "command": ["python", "train.py"],
                "environments": [env], "resume_supported": True, "planning": {"lease_seconds": 60}}
        job = self.hub.submit({"spec": spec, "request_id": "leased"})["ids"][0]
        self.assertIsNone(self.hub.job(job)["node_id"])
        self.assertFalse(self.sync("a", snap=self.snap)["jobs"][0]["lease_granted"])
        self.sync("b", snap=self.snap)
        granted = self.hub.sync("a", {"node_id": "a", "snapshot": self.snap, "reports": [self.report(job)],
                                      "lease_requests": {job: [1, 1]}})
        self.assertTrue(granted["jobs"][0]["lease_granted"])
        return job

    def expire(self, job):
        with self.hub.transaction():
            self.hub.db.execute("UPDATE execution_leases SET deadline=0 WHERE job_id=?", (job,))

    def test_only_complete_checkpoint_moves_to_a_new_generation(self):
        job = self.job()
        self.upload(job)
        newer = {**self.manifest(b"more progress"), "step": 10}
        self.upload(job, data=b"more", manifest=newer)
        self.expire(job)
        assigned, = self.sync("b", snap=self.snap)["jobs"]
        self.assertEqual((assigned["id"], assigned["generation"], assigned["attempt"]), (job, 2, 2))
        self.assertEqual(assigned["resume_checkpoint"], self.manifest())
        reply = self.sync("a", [self.report(job, seq=2, state="succeeded")], self.snap)
        self.assertFalse(reply["jobs"])
        self.assertIn({"id": job, "generation": 1}, reply["retired"])
        self.assertEqual(self.hub.job(job)["node_id"], "b")

    def test_missing_checkpoint_waits_and_human_stop_cancels_handoff(self):
        job = self.job()
        self.expire(job)
        self.sync("b", snap=self.snap)
        self.assertEqual(self.hub.job(job)["handoff"]["status"], "waiting_checkpoint")
        self.hub.action({"job_id": job, "action": "stop"})
        self.upload(job)
        self.assertFalse(self.sync("b", snap=self.snap)["jobs"])
        self.assertEqual(self.hub.job(job)["handoff"]["status"], "canceled")

    def test_restart_waits_full_grace_and_pending_cancel_prevents_transfer(self):
        job = self.job()
        self.upload(job)
        self.expire(job)
        self.hub.close()
        self.hub = Hub(self.root / "hub")
        self.assertFalse(self.sync("b", snap=self.snap)["jobs"])
        self.assertGreater(self.hub.db.execute("SELECT deadline FROM execution_leases").fetchone()[0], time.monotonic()+60)
        self.hub.action({"job_id": job, "action": "cancel"})
        self.expire(job)
        self.sync("b", snap=self.snap)
        self.assertEqual(self.hub.job(job)["state"], "canceled")

    def test_stale_challenge_cannot_start_or_extend_training(self):
        import json
        watch, grants = self.root / "watch", self.root / "grants"
        watch.mkdir()
        grants.mkdir()
        t = [0.0]
        def advance(delay):
            t[0] += 1
            # First grant arrives in time; repeated stale grants then expire.
            if t[0] == 1:
                common.atomic_json(grants / "grant.json", (watch / "challenge").read_text())
        with patch.object(lease_guard.time, "sleep", side_effect=advance), patch.object(lease_guard.subprocess, "Popen") as spawn:
            spawn.return_value.poll.return_value = None
            self.assertEqual(lease_guard.supervise(watch, grants, 4, ["train"], lambda: t[0]), 75)
            spawn.assert_called_once_with(["train"])
            spawn.return_value.kill.assert_called_once()
        self.assertNotEqual(json.loads((grants / "grant.json").read_text()), (watch / "challenge").read_text())
        with patch.object(lease_guard.time, "sleep", side_effect=advance), patch.object(lease_guard.subprocess, "Popen") as spawn:
            lease_guard.supervise(watch, grants, 4, ["train"], lambda: t[0])
            spawn.assert_not_called()
