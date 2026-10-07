"""Authenticated result receipts, evidence catalog and reviewed queue commands."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os

from . import __version__
from .common import now, safe_child
from . import result_protocol as protocol


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS experiment_results (
            job_id TEXT NOT NULL REFERENCES jobs(id), attempt INTEGER NOT NULL, experiment_id TEXT NOT NULL,
            project_id TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, name TEXT NOT NULL,
            received REAL NOT NULL, summary TEXT NOT NULL, independent TEXT NOT NULL,
            PRIMARY KEY(job_id,attempt,experiment_id));
        CREATE TABLE IF NOT EXISTS result_conflicts (
            job_id TEXT NOT NULL, attempt INTEGER NOT NULL, experiment_id TEXT NOT NULL,
            expected_sha256 TEXT NOT NULL, received_sha256 TEXT NOT NULL, received REAL NOT NULL, summary TEXT NOT NULL,
            PRIMARY KEY(job_id,attempt,experiment_id,received_sha256));
        CREATE TABLE IF NOT EXISTS experiment_progress (
            job_id TEXT NOT NULL, attempt INTEGER NOT NULL, experiment_id TEXT NOT NULL,
            phase TEXT NOT NULL, complete INTEGER NOT NULL, updated REAL NOT NULL, detail TEXT NOT NULL,
            PRIMARY KEY(job_id,attempt,experiment_id));
        CREATE TABLE IF NOT EXISTS evidence_catalog (
            job_id TEXT NOT NULL REFERENCES jobs(id), attempt INTEGER NOT NULL, experiment_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER, sha256 TEXT,
            retention TEXT NOT NULL, availability TEXT NOT NULL, transfer_status TEXT NOT NULL,
            confirmed_bytes INTEGER, error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
            PRIMARY KEY(job_id,attempt,experiment_id,evidence_id));
        CREATE TABLE IF NOT EXISTS delivery_commands (
            request_id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id), kind TEXT NOT NULL,
            payload TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL, report TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS node_delivery_status (
            node_id TEXT PRIMARY KEY REFERENCES nodes(id), value TEXT NOT NULL, updated REAL NOT NULL);
    """)


def error(status, message):
    from .hub import APIError
    return APIError(status, message)


def independent_status(summary):
    value = summary["verification"].get("independent", "pending")
    if isinstance(value, dict):
        value = value.get("status")
    return value if value in ("quarantined", "failed") else "pending"


class ResultHubMixin:
    def _result_identity(self, node_id, job_id, attempt, experiment_id):
        from .hub import _integer
        row = self._owned(job_id, node_id)
        _integer(attempt, "attempt", 1)
        protocol.identifier(experiment_id)
        maximum = max(1, row["attempt"]) + int(row["resume_after_attempt"] is not None)
        if attempt > maximum:
            raise error(409, "Result/evidence attempt does not belong to this execution")
        return row

    def receive_result(self, node_id, payload):
        from .hub import _object
        _object(payload, "request")
        try:
            raw = base64.b64decode(payload.get("data", ""), validate=True)
        except (TypeError, ValueError, binascii.Error) as exc:
            raise error(400, "Result data must be base64 JSON") from exc
        if len(raw) > protocol.MAX_RESULT_BYTES:
            raise error(413, "Result exceeds 1 MiB transport limit; retain arrays as evidence, without rerunning")
        digest = hashlib.sha256(raw).hexdigest()
        if payload.get("sha256") != digest:
            raise error(422, "Result SHA-256 mismatch")
        try:
            summary = json.loads(raw.decode("utf-8"))
            warnings = protocol.validate_envelope(summary)
        except (ValueError, UnicodeError, TypeError) as exc:
            raise error(400, "Invalid result: " + str(exc)) from exc
        identity = summary["identity"]
        job, attempt, experiment = identity.get("job_id"), identity["attempt"], identity["experiment_id"]
        conflict = False
        with self.transaction():
            row = self._result_identity(node_id, job, attempt, experiment)
            spec = json.loads(row["spec"])
            if identity.get("node_id") != node_id or identity["project_id"] != spec.get("project_id", spec["algorithm"]):
                raise error(403, "Result project/node identity disagrees with the owned job")
            old = self.db.execute("SELECT * FROM experiment_results WHERE job_id=? AND attempt=? AND experiment_id=?",
                                  (job, attempt, experiment)).fetchone()
            if old and old["sha256"] == digest:
                return {"receipt": digest, "sha256": digest, "size": old["size"], "received_at": old["received"],
                        "complete": True, "warnings": warnings}
            if old:
                self.db.execute("INSERT OR IGNORE INTO result_conflicts VALUES (?,?,?,?,?,?,?)",
                                (job, attempt, experiment, old["sha256"], digest, now(), raw.decode("utf-8")))
                self._event(job, "result_conflict", {"attempt": attempt, "experiment_id": experiment,
                            "expected_sha256": old["sha256"], "received_sha256": digest})
                conflict = True
            else:
                name = "result.json" if experiment == spec.get("experiment_id", "default") else f"results/attempt-{attempt}/{experiment}.json"
                timestamp = now()
                path = safe_child(self.root, f"archives/{job}/{digest}")
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".result-part")
                with temporary.open("wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                self.db.execute("INSERT INTO experiment_results VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (job, attempt, experiment, identity["project_id"], digest, len(raw), name, timestamp,
                     raw.decode("utf-8"), independent_status(summary)))
                self.db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?)", (job, name, digest, len(raw), timestamp))
                self.db.execute("INSERT INTO experiment_progress VALUES (?,?,?,?,?,?,?) ON CONFLICT(job_id,attempt,experiment_id) "
                    "DO UPDATE SET phase=excluded.phase,complete=1,updated=excluded.updated",
                    (job, attempt, experiment, summary["execution"]["status"], 1, timestamp, ""))
                for item in summary["evidence"]:
                    self._store_evidence(job, attempt, experiment, item)
                self._event(job, "result_received", {"experiment_id": experiment, "attempt": attempt, "sha256": digest,
                            "independent": independent_status(summary), "warnings": warnings})
        if conflict:
            raise error(409, "Final result identity already has different content; conflict recorded, original preserved")
        return {"receipt": digest, "sha256": digest, "size": len(raw), "received_at": timestamp,
                "complete": True, "warnings": warnings}

    def _store_evidence(self, job, attempt, experiment, item):
        identity = protocol.identifier(item.get("evidence_id"))
        name = protocol.relative(item.get("name"))
        if identity != protocol.evidence_id(job, attempt, experiment, name):
            raise error(400, "Evidence ID is not bound to its job/attempt/experiment/file")
        old = self.db.execute("SELECT * FROM evidence_catalog WHERE job_id=? AND attempt=? AND experiment_id=? AND evidence_id=?",
                              (job, attempt, experiment, identity)).fetchone()
        digest, size = item.get("sha256"), item.get("size")
        if digest is not None and (not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
            raise error(400, "Invalid evidence SHA-256")
        if size is not None and (type(size) is not int or not 0 <= size <= 16 * 1024**4):
            raise error(400, "Invalid evidence size")
        if old and ((old["sha256"] is not None and digest is not None and old["sha256"] != digest)
                    or (old["size"] is not None and size is not None and old["size"] != size)):
            raise error(409, "Evidence content identity changed; retained record is immutable")
        if old and item.get("retention", "pending") == "pending" and old["retention"] != "pending":
            item = {**item, **{key: old[key] for key in ("retention", "availability", "transfer_status", "confirmed_bytes", "error")}}
        retention, available, transfer = item.get("retention", "pending"), item.get("availability", "pending"), item.get("transfer_status", "retained")
        if retention not in ("pending", "retained", "missing", "expired", "changed") or available not in ("pending", "available", "missing", "expired", "changed"):
            raise error(400, "Invalid evidence availability/retention")
        if transfer not in ("retained", "queued", "uploading", "delivered", "retrying", "unavailable"):
            raise error(400, "Invalid evidence transfer state")
        confirmed = item.get("confirmed_bytes")
        if confirmed is not None and (type(confirmed) is not int or confirmed < 0 or size is not None and confirmed > size):
            raise error(400, "Invalid confirmed evidence bytes")
        self.db.execute("INSERT INTO evidence_catalog VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_id,attempt,experiment_id,evidence_id) DO UPDATE SET "
            "sha256=COALESCE(excluded.sha256,evidence_catalog.sha256),size=COALESCE(excluded.size,evidence_catalog.size),"
            "retention=excluded.retention,availability=excluded.availability,transfer_status=excluded.transfer_status,"
            "confirmed_bytes=excluded.confirmed_bytes,error=excluded.error,updated=excluded.updated",
            (job, attempt, experiment, identity, name, str(item.get("kind", "file"))[:100], size, digest,
             retention, available, transfer, confirmed, str(item.get("error", ""))[:1000], now()))

    def result_details(self, job_id):
        with self.lock:
            results = []
            for row in self.db.execute("SELECT * FROM experiment_results WHERE job_id=? ORDER BY attempt,experiment_id", (job_id,)):
                item = dict(row)
                item["summary"] = json.loads(item["summary"])
                item["result_delivered"] = True
                item["warnings"] = ["结果超过 64 KiB；大数组应移到按需证据中。"] if item["size"] > protocol.SOFT_LIMIT else []
                results.append(item)
            progress = [dict(row) for row in self.db.execute("SELECT * FROM experiment_progress WHERE job_id=? ORDER BY attempt,experiment_id", (job_id,))]
            evidence = [dict(row) for row in self.db.execute("SELECT * FROM evidence_catalog WHERE job_id=? ORDER BY attempt,experiment_id,name", (job_id,))]
            conflicts = [dict(row) for row in self.db.execute("SELECT attempt,experiment_id,expected_sha256,received_sha256,received FROM result_conflicts WHERE job_id=?", (job_id,))]
            return {"experiment_results": results, "experiment_progress": progress, "evidence": evidence,
                    "result_conflicts": conflicts}

    def verify_result(self, payload):
        from .hub import _object, _integer
        _object(payload, "request")
        job, experiment = payload.get("job_id"), protocol.identifier(payload.get("experiment_id"))
        attempt = _integer(payload.get("attempt"), "attempt", 1)
        decision, references = payload.get("status"), payload.get("references", [])
        if decision not in ("passed", "failed", "quarantined", "pending") or not isinstance(references, list):
            raise error(400, "Invalid independent verification")
        if decision == "passed" and (not references or any(not isinstance(r, str) or not r.strip() for r in references)):
            raise error(400, "Independent acceptance requires explicit review/evidence references")
        with self.transaction():
            self._find_job(job)
            row = self.db.execute("SELECT sha256 FROM experiment_results WHERE job_id=? AND attempt=? AND experiment_id=?", (job, attempt, experiment)).fetchone()
            if row is None or payload.get("sha256") != row["sha256"]:
                raise error(409, "Review must reference the received result SHA-256")
            self.db.execute("UPDATE experiment_results SET independent=? WHERE job_id=? AND attempt=? AND experiment_id=?", (decision, job, attempt, experiment))
            self._event(job, "independent_verification", payload)
        return {"independent": decision}

    def _capable_node(self, node_id, capability):
        row = self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if row is None:
            raise error(404, "Unknown node")
        snapshot = json.loads(row["snapshot"])
        if capability not in snapshot.get("capabilities", []):
            raise error(409, "Operation not supported by the running Agent; upgrade Worker and confirm its advertised capabilities")
        if now() - row["last_seen"] > 45:
            raise error(409, "Node offline; evidence/queue availability cannot be confirmed, no execution will be restarted")

    def _delivery_command(self, request_id, node_id, kind, payload):
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise error(400, "request_id must be a nonempty string of at most 200 characters")
        raw = protocol.encoded(payload).decode()
        fingerprint = hashlib.sha256(protocol.encoded([node_id, kind, payload])).hexdigest()
        old = self.db.execute("SELECT * FROM delivery_commands WHERE request_id=?", (request_id,)).fetchone()
        if old:
            if old["fingerprint"] != fingerprint:
                raise error(409, "request_id already identifies another transfer request")
            return self.transfer_request(request_id)
        self.db.execute("INSERT INTO delivery_commands VALUES (?,?,?,?,?,?,?,?,?)",
                        (request_id, node_id, kind, raw, fingerprint, "pending", "{}", now(), now()))
        return self.transfer_request(request_id)

    def request_evidence(self, payload):
        from .hub import _object, _integer
        _object(payload, "request")
        job, experiment = payload.get("job_id"), protocol.identifier(payload.get("experiment_id"))
        attempt = _integer(payload.get("attempt"), "attempt", 1)
        identities = payload.get("evidence_ids")
        if not isinstance(identities, list) or not 1 <= len(identities) <= 100 or len(set(map(str, identities))) != len(identities):
            raise error(400, "Select 1..100 explicit evidence IDs")
        with self.transaction():
            row = self._find_job(job)
            self._capable_node(row["node_id"], protocol.EVIDENCE_CAPABILITY)
            for identity in identities:
                item = self.db.execute("SELECT * FROM evidence_catalog WHERE job_id=? AND attempt=? AND experiment_id=? AND evidence_id=?", (job, attempt, experiment, protocol.identifier(identity))).fetchone()
                if item is None:
                    raise error(404, "Evidence ID does not belong to the selected execution")
                if item["availability"] not in ("available", "pending"):
                    raise error(410, "Evidence unavailable: " + item["availability"])
            result = self._delivery_command(payload.get("request_id"), row["node_id"], "evidence",
                       {"job_id": job, "attempt": attempt, "experiment_id": experiment, "evidence_ids": identities})
            for identity in identities:
                self.db.execute("UPDATE evidence_catalog SET transfer_status=CASE WHEN transfer_status='delivered' THEN transfer_status ELSE 'queued' END WHERE job_id=? AND attempt=? AND experiment_id=? AND evidence_id=?", (job, attempt, experiment, identity))
            return result

    def migrate_uploads(self, payload):
        from .hub import _object
        _object(payload, "request")
        action = payload.get("action", "dry-run")
        with self.transaction():
            if action == "apply":
                plan = self.transfer_request(payload.get("plan_id"))
                if plan["kind"] != "migration" or plan["payload"].get("action") != "dry-run" or plan["status"] != "completed":
                    raise error(409, "Apply requires a completed, reviewed dry-run")
                command = {**plan["payload"], "action": "apply", "plan_id": plan["request_id"],
                           "plan": plan["report"]}
                node_id = plan["node_id"]
            elif action == "dry-run":
                project, jobs = payload.get("project_id"), payload.get("job_ids")
                if not isinstance(project, str) or not project or not isinstance(jobs, list) or not 1 <= len(jobs) <= 100:
                    raise error(400, "Queue migration requires project_id and explicit job_ids (1..100)")
                owners = set()
                for job in jobs:
                    row = self._find_job(job)
                    spec = json.loads(row["spec"])
                    if spec.get("project_id", spec["algorithm"]) != project or not row["node_id"]:
                        raise error(403, "Migration scope disagrees with the assigned project/job")
                    owners.add(row["node_id"])
                if len(owners) != 1:
                    raise error(400, "Review one node's queue at a time")
                node_id = owners.pop()
                command = {"action": action, "project_id": project, "job_ids": jobs,
                           "result_files": payload.get("result_files", [])}
                if not isinstance(command["result_files"], list):
                    raise error(400, "result_files must be explicit job/experiment/path mappings")
            else:
                raise error(400, "Migration action must be dry-run or apply")
            self._capable_node(node_id, protocol.MIGRATION_CAPABILITY)
            return self._delivery_command(payload.get("request_id"), node_id, "migration", command)

    def transfer_request(self, request_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM delivery_commands WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise error(404, "Unknown transfer request")
            item = dict(row)
            item["payload"], item["report"] = json.loads(item["payload"]), json.loads(item["report"])
            item.pop("fingerprint")
            return item

    def delivery_poll(self, node_id, payload):
        from .hub import _object
        _object(payload, "request")
        status = _object(payload.get("status", {}), "status")
        if len(protocol.encoded(status)) > 64 * 1024:
            raise error(413, "Delivery telemetry exceeds 64 KiB")
        with self.transaction():
            secret = self.config["nodes"][node_id]
            if isinstance(secret, dict):
                secret = secret["token"]
            status = json.loads(protocol.encoded(status).decode().replace(secret, "[redacted]"))
            self.db.execute("INSERT INTO node_delivery_status VALUES (?,?,?) ON CONFLICT(node_id) DO UPDATE SET value=excluded.value,updated=excluded.updated",
                            (node_id, protocol.encoded(status).decode(), now()))
            commands = []
            for row in self.db.execute("SELECT * FROM delivery_commands WHERE node_id=? AND status NOT IN ('completed','failed','unsupported') ORDER BY created LIMIT 20", (node_id,)):
                item = dict(row)
                item["payload"] = json.loads(item["payload"])
                commands.append(item)
            return {"commands": commands, "hub_version": __version__, "capabilities": [protocol.CAPABILITY, protocol.EVIDENCE_CAPABILITY, protocol.MIGRATION_CAPABILITY]}

    def delivery_report(self, node_id, payload):
        from .hub import _object
        _object(payload, "request")
        evidence, progress, commands = (payload.get(key, []) for key in ("evidence", "progress", "commands"))
        if any(not isinstance(items, list) or len(items) > 1000 for items in (evidence, progress, commands)):
            raise error(400, "Delivery report lists must have at most 1000 entries")
        with self.transaction():
            secret = self.config["nodes"][node_id]
            if isinstance(secret, dict):
                secret = secret["token"]
            for item in evidence:
                _object(item, "evidence")
                self._result_identity(node_id, item.get("job_id"), item.get("attempt"), item.get("experiment_id"))
                clean = {**item, "error": str(item.get("error", "")).replace(secret, "[redacted]")}
                self._store_evidence(item["job_id"], item["attempt"], item["experiment_id"], clean)
            for item in progress:
                _object(item, "progress")
                self._result_identity(node_id, item.get("job_id"), item.get("attempt"), item.get("experiment_id"))
                if type(item.get("complete")) is not bool or not isinstance(item.get("phase"), str) or len(item["phase"]) > 100:
                    raise error(400, "Invalid experiment progress")
                self.db.execute("INSERT INTO experiment_progress VALUES (?,?,?,?,?,?,?) ON CONFLICT(job_id,attempt,experiment_id) DO UPDATE SET "
                    "phase=CASE WHEN experiment_progress.complete=1 THEN experiment_progress.phase ELSE excluded.phase END,"
                    "complete=MAX(experiment_progress.complete,excluded.complete),updated=excluded.updated,detail=excluded.detail",
                    (item["job_id"], item["attempt"], item["experiment_id"], item["phase"], int(item["complete"]), now(), str(item.get("detail", ""))[:1000]))
            for item in commands:
                _object(item, "command report")
                row = self.db.execute("SELECT * FROM delivery_commands WHERE request_id=? AND node_id=?", (item.get("request_id"), node_id)).fetchone()
                if row is None:
                    raise error(403, "Transfer request belongs to another node")
                if item.get("status") not in ("running", "completed", "failed", "unsupported"):
                    raise error(400, "Invalid transfer request status")
                if row["status"] not in ("completed", "failed", "unsupported"):
                    report = protocol.encoded(item.get("report", {})).decode().replace(secret, "[redacted]")
                    self.db.execute("UPDATE delivery_commands SET status=?,report=?,updated=? WHERE request_id=?", (item["status"], report, now(), row["request_id"]))
        return {"ack": True}

    def upload_evidence(self, node_id, payload):
        with self.lock:
            job, attempt, experiment = payload.get("job_id"), payload.get("attempt"), payload.get("experiment_id")
            self._result_identity(node_id, job, attempt, experiment)
            item = self.db.execute("SELECT * FROM evidence_catalog WHERE job_id=? AND attempt=? AND experiment_id=? AND evidence_id=?",
                                  (job, attempt, experiment, payload.get("evidence_id"))).fetchone()
            if item is None or item["transfer_status"] not in ("queued", "uploading", "retrying", "delivered"):
                raise error(403, "Evidence was not explicitly requested")
            if item["sha256"] != payload.get("sha256") or item["size"] != payload.get("size"):
                raise error(409, "Evidence upload does not match the registered size/hash")
            upload = {**payload, "name": f"evidence/attempt-{attempt}/{experiment}/{item['name']}"}
        response = self.upload(node_id, upload)
        with self.transaction():
            self.db.execute("UPDATE evidence_catalog SET transfer_status=?,confirmed_bytes=?,updated=? WHERE job_id=? AND attempt=? AND experiment_id=? AND evidence_id=?",
                            ("delivered" if response["complete"] else "uploading", response["offset"], now(), job, attempt, experiment, item["evidence_id"]))
        return response
