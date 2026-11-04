"""History must not turn idle heartbeats or lifecycle probes into full-job scans."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import socket
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from expman.common import validate_task
from expman.hub import Hub, make_server
from expman.scheduler import workload_key
from tests.support import temporary_directory


SNAPSHOT = {"allow_demo": True, "policy": {"max_prefetch": 2, "cpu_budget": 4, "ram_budget_mb": 8192}}


class CenterResponsivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = Path(self.temp.__enter__())
        self.hub = Hub(self.root)
        self.hub.add_node("idle")
        self.hub.add_node("history")

    def tearDown(self):
        self.hub.close()
        self.temp.__exit__(None, None, None)

    def history(self, count=16):
        spec = validate_task({"backend": "demo", "resources": {"gpu_memory_mb": 0}})
        spec["command"] = ["python", "-c", "runner = " + "x" * 128_000]
        with self.hub.transaction():
            for i in range(count):
                spec["params"]["seed"] = i
                self.hub.db.execute(
                    "INSERT INTO jobs(id,spec,node_id,state,created,updated,attempt,timing,log_tail) "
                    "VALUES (?,?,?,'succeeded',?,?,1,?,?)",
                    (f"{i:032x}", json.dumps(spec), "history", i, i,
                     json.dumps({"complete": True, "elapsed_seconds": i + 1}), "log" * 5000))
        return spec

    @contextmanager
    def deny_program_reads(self):
        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and table == "jobs" and column in ("spec", "log_tail"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.hub.db.set_authorizer(authorize)
        try:
            yield
        finally:
            # Python 3.10 does not support disabling this callback with None.
            # Restore unrestricted access after the read-isolation assertion.
            self.hub.db.set_authorizer(lambda *_: sqlite3.SQLITE_OK)

    @contextmanager
    def server(self, **options):
        server = make_server(self.hub, port=0, **options)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def request(self, server, token=None):
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/health",
            headers={"Authorization": "Bearer " + (token or self.hub.config["admin_token"])})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
            return json.load(response)

    def test_idle_heartbeat_polling_and_rules_never_read_historical_programs(self):
        self.history()
        with self.deny_program_reads(), patch.object(self.hub, "_scheduling_nodes", side_effect=AssertionError("idle scheduler")):
            response = self.hub.sync("idle", {"node_id": "idle", "snapshot": SNAPSHOT, "reports": []})
            self.assertEqual(response["jobs"], [])
            state = self.hub.state(summary=True)
            self.assertEqual(len(state["jobs"]), 16)
            self.assertTrue(all("command" not in job["spec"] for job in state["jobs"]))
            self.hub.autonomy.rules.tick(force=True)
            self.assertEqual(self.hub.autonomy.rules.facts()["job"][0]["state"], "succeeded")

    def test_projection_tracks_updates_and_preserves_full_worker_definition(self):
        spec = self.history(1)
        spec.update(priority=9, command=["python", "-c", "changed_runner"])
        self.hub.db.execute("UPDATE jobs SET spec=?,state='running',metrics=?,runtime=?,execution_state=? WHERE id=?",
            (json.dumps(spec), '{"score":0.7}', '{"diagnostics":{"large":"details"},"version":1}',
             '{"progress":{"step":7},"diagnostics":{"large":"details"},"resources":{"gpu":1}}', "0" * 32))
        listed = self.hub.state(summary=True)["jobs"][0]
        self.assertEqual(listed["spec"]["priority"], 9)
        self.assertEqual(listed["metrics"], {"score": .7})
        self.assertEqual(listed["execution_state"], {"progress": {"step": 7}})
        self.assertNotIn("diagnostics", listed["runtime"])
        response = self.hub.sync("history", {"node_id": "history", "snapshot": SNAPSHOT, "reports": []})
        self.assertEqual(response["jobs"][0]["spec"]["command"], spec["command"])
        self.assertEqual(self.hub.job("0" * 32)["spec"], spec)

    def test_projection_rolls_back_with_the_canonical_job(self):
        self.history(1)
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.hub.transaction():
                self.hub.db.execute("UPDATE jobs SET state='failed' WHERE id=?", ("0" * 32,))
                self.assertEqual(self.hub.state(summary=True)["jobs"][0]["state"], "failed")
                raise RuntimeError("rollback")
        self.assertEqual(self.hub.job("0" * 32)["state"], "succeeded")
        self.assertEqual(self.hub.state(summary=True)["jobs"][0]["state"], "succeeded")

    def test_waiting_for_process_exit_does_not_rebuild_historical_performance(self):
        self.history(1)
        self.hub.db.execute("UPDATE jobs SET state='running',execution_state=? WHERE id=?",
                            ('{"ever_started":true,"stopped_confirmed":false}', "0" * 32))
        self.hub.db.execute("INSERT INTO execution_handoffs VALUES (?,1,NULL,'test','requested',0)", ("0" * 32,))
        with patch.object(self.hub, "_scheduling_nodes", side_effect=AssertionError("waiting handoff")):
            self.hub.sync("idle", {"node_id": "idle", "snapshot": SNAPSHOT, "reports": []})

    def test_workload_samples_are_cached_invalidated_and_keep_latest_ten(self):
        spec = self.history(16)
        key = workload_key(spec)
        first = next(node for node in self.hub._scheduling_nodes() if node["id"] == "history")
        self.assertEqual(first["performance"][key], 11.5)  # Latest ten durations: 7 through 16.
        with self.deny_program_reads():
            second = next(node for node in self.hub._scheduling_nodes() if node["id"] == "history")
        self.assertEqual(second["performance"], first["performance"])
        spec["command"] = ["different-runner"]
        self.hub.db.execute("UPDATE jobs SET spec=? WHERE id=?", (json.dumps(spec), f"{15:032x}"))
        changed = next(node for node in self.hub._scheduling_nodes() if node["id"] == "history")
        self.assertEqual(changed["performance"][workload_key(spec)], 16)
        self.hub.db.execute("UPDATE jobs SET generation=2 WHERE id=?", (f"{15:032x}",))
        self.assertNotIn(workload_key(spec), next(node for node in self.hub._scheduling_nodes() if node["id"] == "history")["performance"])

    def test_missing_projection_is_backfilled_after_restart_without_changing_jobs(self):
        spec = self.history(2)
        self.hub.close()
        with closing(sqlite3.connect(self.root / "hub.sqlite3")) as db:
            for name in ("job_summary_insert", "job_summary_update", "job_summary_delete", "job_workload_invalidate"):
                db.execute("DROP TRIGGER " + name)
            db.execute("DROP TABLE job_summaries")
            db.execute("DROP TABLE job_workload_keys")
            db.commit()
        self.hub = Hub(self.root)
        self.assertEqual(len(self.hub.state(summary=True)["jobs"]), 2)
        self.assertEqual(self.hub.job(f"{1:032x}")["spec"], spec)

    def test_observation_changes_use_one_transaction_and_unchanged_rows_are_not_written(self):
        self.history()
        observations = []
        def trace(sql):
            if sql.startswith("INSERT OR REPLACE INTO agent_rule_observations"):
                observations.append(self.hub.db.in_transaction)
        self.hub.db.set_trace_callback(trace)
        try:
            self.hub.autonomy.rules.tick(force=True)
            self.assertEqual(len(observations), 18)
            self.assertTrue(all(observations))
            observations.clear()
            self.hub.autonomy.rules.tick(force=True)
            self.assertEqual(observations, [])
        finally:
            self.hub.db.set_trace_callback(None)

    def test_health_authentication_and_counts_remain_available_while_scheduler_is_locked(self):
        self.history()
        with self.server() as server, self.hub.lock:
            health = self.request(server)
            self.assertEqual(health["jobs"], 16)
            self.assertEqual(health['workspace_id'], self.hub.config['workspace_id'])
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.request(server, "wrong-token")
            self.assertEqual(rejected.exception.code, 401)

    @unittest.skipUnless(os.name == 'nt', 'Windows exclusive socket semantics')
    def test_windows_port_is_exclusive_even_against_a_legacy_reuse_listener(self):
        with self.server(host='0.0.0.0') as server:
            self.assertFalse(server.allow_reuse_address)
            self.assertEqual(server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE), 1)
            for address in ('0.0.0.0', '127.0.0.1'):
                with self.subTest(address=address), socket.socket() as duplicate:
                    duplicate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    with self.assertRaises(OSError):
                        duplicate.bind((address, server.server_port))
            self.assertEqual(self.request(server)['workspace_id'], self.hub.config['workspace_id'])

    @unittest.skipUnless(os.name == 'nt', 'Windows exclusive socket semantics')
    def test_windows_center_refuses_a_port_already_owned_by_a_legacy_listener(self):
        with socket.socket() as legacy:
            legacy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            legacy.bind(('0.0.0.0', 0))
            legacy.listen()
            with self.assertRaises(OSError):
                make_server(self.hub, host='0.0.0.0', port=legacy.getsockname()[1])

    def test_formatting_history_does_not_block_a_worker_heartbeat(self):
        self.history()
        entered, release = threading.Event(), threading.Event()
        original = self.hub._job
        def delayed_format(*args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            return original(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(self.hub, "_job", side_effect=delayed_format):
            listing = pool.submit(self.hub.state, summary=True)
            try:
                self.assertTrue(entered.wait(timeout=2))
                heartbeat = pool.submit(self.hub.sync, "idle", {"node_id": "idle", "snapshot": SNAPSHOT, "reports": []})
                self.assertEqual(heartbeat.result(timeout=2)["jobs"], [])
            finally:
                release.set()
            self.assertEqual(len(listing.result(timeout=3)["jobs"]), 16)

    def test_history_snapshot_reads_without_the_writer_lock_and_keeps_related_rows_consistent(self):
        self.history(1)
        captured = threading.Event()
        original = self.hub._attach_record_tags
        def attached(*args, **kwargs):
            result = original(*args, **kwargs)
            captured.set()
            return result
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(self.hub, "_attach_record_tags", side_effect=attached):
            with self.hub.lock:
                listing = pool.submit(self.hub.state, summary=True)
                self.assertTrue(captured.wait(timeout=2))
                with self.hub.transaction():
                    self.hub.db.execute("UPDATE jobs SET state='failed' WHERE id=?", ("0" * 32,))
                    self.assertEqual(self.hub.state(summary=True)["jobs"][0]["state"], "failed")
            self.assertEqual(listing.result(timeout=3)["jobs"][0]["state"], "succeeded")

    def test_connection_limit_rejects_before_dispatch_and_recovers_slots(self):
        entered = threading.Barrier(3)
        release = threading.Event()
        original = self.hub.health
        def slow_health():
            entered.wait(timeout=3)
            release.wait(timeout=3)
            return original()
        with self.server(max_request_threads=2) as server, ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(self.hub, "health", side_effect=slow_health):
                requests = [pool.submit(self.request, server) for _ in range(2)]
                try:
                    entered.wait(timeout=3)
                    for _ in range(3):
                        with self.assertRaises(urllib.error.HTTPError) as rejected:
                            self.request(server)
                        self.assertEqual(rejected.exception.code, 503)
                        self.assertEqual(rejected.exception.headers["Retry-After"], "1")
                finally:
                    release.set()
                for request in requests:
                    self.assertEqual(request.result(timeout=3)["jobs"], 0)
            self.assertEqual(self.request(server)["jobs"], 0)

    def test_waiting_worker_precedes_repeated_background_work_and_lock_is_reentrant(self):
        order = []
        def worker():
            with self.hub.lock:
                order.append("worker")
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.hub.lock:
                future = pool.submit(worker)
                with self.hub.lock._condition:
                    self.assertTrue(self.hub.lock._condition.wait_for(lambda: bool(self.hub.lock._waiters), timeout=1))
                with self.hub.lock:
                    order.append("nested")
            with self.hub.lock:
                order.append("background")
            future.result(timeout=2)
        self.assertEqual(order, ["nested", "worker", "background"])

    def test_timed_out_waiter_and_wrong_owner_cannot_strand_the_lock(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.hub.lock:
                self.assertFalse(pool.submit(self.hub.lock.acquire, timeout=.01).result(timeout=1))
                self.assertFalse(pool.submit(self.hub.lock.acquire, False).result(timeout=1))
                with self.assertRaises(RuntimeError):
                    pool.submit(self.hub.lock.release).result(timeout=1)
            def probe():
                acquired = self.hub.lock.acquire(timeout=1)
                if acquired:
                    self.hub.lock.release()
                return acquired
            self.assertTrue(pool.submit(probe).result(timeout=2))

    def test_disconnect_while_sending_an_error_does_not_escape_handler(self):
        with self.server() as server, patch.object(server.RequestHandlerClass, "_send", side_effect=ConnectionAbortedError), \
                patch.object(server, "handle_error") as errors:
            with self.assertRaises((OSError, urllib.error.URLError)):
                self.request(server, "wrong-token")
            self.assertFalse(errors.called)
