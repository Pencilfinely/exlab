import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request

from expman.hub import APIError, Hub, make_server
from tests.support import temporary_directory


SPEC = {"name": "Labelled experiment", "backend": "demo", "params": {"steps": 2, "seed": 1},
        "tags": ["required-node"]}


class RecordTagsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.hub = Hub(self.root)
        self.node_token = self.hub.add_node("node-a")

    def tearDown(self):
        self.hub.close()
        self.temporary.__exit__(None, None, None)

    def tag(self, name="基线", color="#246657"):
        return self.hub.tag_save({"name": name, "color": color})

    def job(self, request="job", **changes):
        return self.hub.submit({"spec": copy.deepcopy(SPEC), "request_id": request, **changes})["ids"][0]

    def matrix(self, **changes):
        return self.hub.matrix_save({"name": "Comparison", "spec": copy.deepcopy(SPEC),
                                     "grid": {"seed": [1, 2]}, **changes})

    def assign(self, target, identity, tags):
        return self.hub.tag_assign({"target": target, "id": identity, "tag_ids": tags})

    def test_crud_updates_all_associations_without_mutating_task_or_dates(self):
        tag = self.tag(color="#AB12EF")
        job_id = self.job()
        before = self.hub.job(job_id)
        matrix = self.matrix(tag_ids=[tag["id"]])
        self.assign("job", job_id, [tag["id"], tag["id"]])
        edited = self.hub.tag_save({"id": tag["id"], "name": "论文基线", "color": "#3355aa"})
        self.assertEqual(edited["created"], tag["created"])
        job = self.hub.job(job_id)
        self.assertEqual(job["tags"], [edited])
        self.assertEqual(job["spec"], before["spec"])
        self.assertEqual((job["created"], job["updated"]), (before["created"], before["updated"]))
        self.assertEqual(job["spec"]["tags"], ["required-node"])
        self.assertEqual(self.hub.state()["jobs"][0]["tags"], [edited])
        self.assertEqual(self.hub.matrices()["matrices"][0]["tags"], [edited])
        self.assertEqual(self.hub.matrix_item(matrix["id"])["tags"], [edited])
        definition = self.hub.record_tags()["tags"][0]
        self.assertEqual((definition["job_count"], definition["matrix_count"]), (1, 1))
        self.hub.tag_delete({"id": tag["id"]})
        self.hub.tag_delete({"id": tag["id"]})
        self.assertEqual(self.hub.job(job_id)["tag_ids"], [])
        self.assertEqual(self.hub.matrix_item(matrix["id"])["tags"], [])
        self.assertEqual(self.hub.job(job_id)["spec"], before["spec"])

    def test_validation_and_failed_assignments_preserve_existing_labels(self):
        tag = self.tag()
        job_id = self.job(tag_ids=[tag["id"]])
        for payload in (None, {}, {"name": " "}, {"name": "x" * 41}, {"name": "line\nbreak"},
                        {"name": "valid", "color": "red"}, {"name": "valid", "color": "#fff"},
                        {"name": "valid", "id": False}, {"name": "valid", "id": "f" * 32}):
            with self.subTest(payload=payload), self.assertRaises(APIError):
                self.hub.tag_save(payload)
        for ids in (None, "not-a-list", [False], ["f" * 32], [tag["id"]] * 33):
            with self.subTest(ids=ids), self.assertRaises(APIError):
                self.assign("job", job_id, ids)
        self.assertEqual(self.hub.job(job_id)["tag_ids"], [tag["id"]])
        self.assign("job", job_id, [])
        self.assertEqual(self.hub.job(job_id)["tags"], [])
        english = self.tag("Baseline")
        with self.assertRaises(APIError) as duplicate:
            self.tag(" baseline ")
        self.assertEqual(duplicate.exception.status, 409)
        self.assertEqual(self.hub.record_tags()["tags"][0]["id"], english["id"])

    def test_matrices_inherit_labels_at_launch_and_invalid_save_is_atomic(self):
        first, second = self.tag("第一轮"), self.tag("第二轮", "#ee7733")
        matrix = self.matrix(tag_ids=[first["id"]])
        result = self.hub.matrix_start({"id": matrix["id"], "request_id": "first"})
        for identity in result["ids"]:
            self.assertEqual(self.hub.job(identity)["tag_ids"], [first["id"]])
            self.assertEqual(self.hub.job(identity)["spec"]["tags"], SPEC["tags"])
        self.assign("matrix", matrix["id"], [second["id"]])
        self.assertEqual(self.hub.matrix_item(matrix["id"])["revision"], matrix["revision"])
        again = self.hub.matrix_start({"id": matrix["id"], "request_id": "second"})
        self.assertEqual(self.hub.job(result["ids"][0])["tag_ids"], [first["id"]])
        self.assertEqual(self.hub.job(again["ids"][0])["tag_ids"], [second["id"]])
        self.assertEqual(self.hub.matrix_start({"id": matrix["id"], "request_id": "first"}), result)
        before = self.hub.matrix_item(matrix["id"])
        with self.assertRaises(APIError):
            self.matrix(id=matrix["id"], revision=matrix["revision"], tag_ids=["f" * 32], name="Bad edit")
        self.assertEqual(self.hub.matrix_item(matrix["id"]), before)
        self.hub.matrix_save({**before, "description": "Keep labels", "tag_ids": [second["id"]]})
        self.assertEqual(self.hub.matrix_item(matrix["id"])["tag_ids"], [second["id"]])

    def test_submission_retries_include_labels_and_survive_tag_deletion(self):
        tag = self.tag()
        first = self.job(tag_ids=[tag["id"]])
        self.assertEqual(self.job(tag_ids=[tag["id"], tag["id"]]), first)
        with self.assertRaises(APIError) as conflict:
            self.job(tag_ids=[])
        self.assertEqual(conflict.exception.status, 409)
        self.hub.tag_delete({"id": tag["id"]})
        self.assertEqual(self.job(tag_ids=[tag["id"]]), first)
        self.assertEqual(len(self.hub.state()["jobs"]), 1)
        with self.assertRaises(APIError):
            self.job("new", tag_ids=[tag["id"]])
        self.assertEqual(len(self.hub.state()["jobs"]), 1)

    def test_labels_survive_restart_and_legacy_databases_migrate_in_place(self):
        tag = self.tag()
        identity = self.job(tag_ids=[tag["id"]])
        matrix = self.matrix(tag_ids=[tag["id"]])
        self.hub.close()
        self.hub = Hub(self.root)
        self.assertEqual(self.hub.job(identity)["tags"], [tag])
        self.assertEqual(self.hub.matrix_item(matrix["id"])["tags"], [tag])
        self.hub.close()
        with closing(sqlite3.connect(self.root / "hub.sqlite3")) as db, db:
            for table in ("job_tags", "matrix_tags", "record_tags"):
                db.execute("DROP TABLE " + table)
        self.hub = Hub(self.root)
        self.assertEqual(self.hub.job(identity)["tags"], [])
        self.assertEqual(self.hub.job(identity)["spec"]["tags"], SPEC["tags"])
        self.assertEqual(self.hub.matrix_item(matrix["id"])["tags"], [])
        self.assertEqual(self.hub.matrix_item(matrix["id"])["created"], matrix["created"])

    def test_http_tag_routes_require_admin_and_return_persisted_associations(self):
        server = make_server(self.hub, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_address[1])
        def request(path, payload=None, token=None):
            headers = {"Authorization": "Bearer " + (token or self.hub.config["admin_token"]), "Content-Type": "application/json"}
            body = None if payload is None else json.dumps(payload).encode()
            with urllib.request.urlopen(urllib.request.Request(base + path, data=body, headers=headers), timeout=3) as response:
                return json.load(response)
        try:
            for script in ("records.js", "templates.js"):
                with urllib.request.urlopen(base + "/" + script, timeout=3) as response:
                    self.assertEqual(response.headers.get_content_type(), "text/javascript")
                    self.assertEqual(response.read(), (Path(__file__).resolve().parents[1] / "expman/static" / script).read_bytes())
            tag = request("/api/tags/save", {"name": "Test", "color": "#8844cc"})
            identity = self.job()
            request("/api/tags/assign", {"target": "job", "id": identity, "tag_ids": [tag["id"]]})
            self.assertEqual(request("/api/job?id=" + identity)["tag_ids"], [tag["id"]])
            self.assertEqual(request("/api/tags")["tags"][0]["job_count"], 1)
            for route, payload in (("/api/tags", None), ("/api/tags/save", {"name": "Bad"}),
                                   ("/api/tags/delete", {"id": tag["id"]}),
                                   ("/api/tags/assign", {"target": "job", "id": identity, "tag_ids": []})):
                with self.subTest(route=route), self.assertRaises(urllib.error.HTTPError) as error:
                    request(route, payload, self.node_token)
                self.assertEqual(error.exception.code, 403)
            request("/api/tags/delete", {"id": tag["id"]})
            self.assertEqual(request("/api/state")["jobs"][0]["tags"], [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
