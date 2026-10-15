"""Mutable execution state and checkpoint transport, separate from final results.

Ownership does not expire on a missed heartbeat. A new generation can be issued
only after the old generation explicitly reports a confirmed process exit.
"""
from __future__ import annotations

import base64
import binascii
import copy
import json
import os
import re
import threading

from .common import now, safe_child, sha256_file
from .compute_policy import number
from .scheduler import choose_assignment

CAPABILITY = "execution-state-v1"
CHUNK = 512 * 1024
MAX_CHECKPOINT = 64 * 1024**3


def checkpoint(value):
    if not isinstance(value, dict) or set(value) - {"path", "sha256", "size", "step", "time"}:
        raise ValueError("Invalid execution checkpoint")
    from .result_protocol import relative
    relative(value.get("path"))
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256", ""))):
        raise ValueError("Invalid checkpoint SHA-256")
    for key, maximum in (("size", MAX_CHECKPOINT), ("step", 2**53 - 1)):
        number(value.get(key), "checkpoint." + key, maximum)
        if type(value[key]) is not int:
            raise ValueError("Checkpoint size/step must be integers")
    number(value.get("time"), "checkpoint.time")
    return copy.deepcopy(value)


def validate_state(value, attempt, generation):
    fields = {"schema_version", "generation", "attempt", "ever_started", "stopped_confirmed",
              "checkpoint", "progress", "release", "resources", "diagnostics"}
    if not isinstance(value, dict) or set(value) - fields:
        raise ValueError("Invalid execution state fields")
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or type(value.get("generation")) is not int or value["generation"] != generation
            or type(value.get("attempt")) is not int or value["attempt"] != attempt):
        raise ValueError("Execution state identity mismatch")
    for key in ("ever_started", "stopped_confirmed"):
        if type(value.get(key)) is not bool:
            raise ValueError(key + " must be boolean")
    if value.get("checkpoint") is not None:
        checkpoint(value["checkpoint"])
    for key in ("progress", "resources", "diagnostics"):
        if key in value and not isinstance(value[key], dict):
            raise ValueError(key + " must be an object")
    release = value.get("release")
    if release is not None and (not isinstance(release, str) or len(release) > 300):
        raise ValueError("Invalid resource release reason")
    if len(json.dumps(value, allow_nan=False).encode("utf-8")) > 128 * 1024:
        raise ValueError("Execution state exceeds 128 KiB; transfer checkpoint bytes separately")
    return copy.deepcopy(value)


def initialize(db):
    columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
    for name, declaration in (("generation", "INTEGER NOT NULL DEFAULT 1"), ("execution_state", "TEXT"),
                              ("resume_checkpoint", "TEXT")):
        if name not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN " + name + " " + declaration)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS execution_owners (
            job_id TEXT NOT NULL REFERENCES jobs(id), generation INTEGER NOT NULL, node_id TEXT NOT NULL,
            first_attempt INTEGER NOT NULL, last_attempt INTEGER,
            PRIMARY KEY(job_id,generation));
        CREATE TABLE IF NOT EXISTS execution_handoffs (
            job_id TEXT PRIMARY KEY REFERENCES jobs(id), generation INTEGER NOT NULL,
            target_node_id TEXT, reason TEXT NOT NULL, status TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS execution_queue_controls (
            job_id TEXT PRIMARY KEY REFERENCES jobs(id), priority INTEGER NOT NULL,
            hold INTEGER NOT NULL, revision INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS execution_checkpoint_uploads (
            job_id TEXT NOT NULL REFERENCES jobs(id), attempt INTEGER NOT NULL, generation INTEGER NOT NULL,
            sha256 TEXT NOT NULL, manifest TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0,
            received REAL, PRIMARY KEY(job_id,attempt,sha256));
    """)
    db.execute("INSERT OR IGNORE INTO execution_owners SELECT id,generation,node_id,1,NULL FROM jobs WHERE node_id IS NOT NULL")


class ExecutionControlHubMixin:
    def _record_execution_owner(self, job_id, node_id, generation=1, first_attempt=1):
        self.db.execute("INSERT OR IGNORE INTO execution_owners VALUES (?,?,?,?,NULL)",
                        (job_id, generation, node_id, first_attempt))

    def _execution_result_owner(self, node_id, job_id, attempt):
        from .hub import APIError
        row = self._find_job(job_id)
        history = self.db.execute("SELECT 1 FROM execution_owners WHERE job_id=? AND node_id=? AND first_attempt<=? "
                                  "AND (last_attempt IS NULL OR last_attempt>=?)", (job_id, node_id, attempt, attempt)).fetchone()
        if not history and not (row["generation"] == 1 and row["node_id"] == node_id):
            raise APIError(403, "Result/evidence does not belong to this node's execution attempts")
        return row

    def _archive_owner(self, job_id, node_id):
        from .hub import APIError
        row = self._find_job(job_id)
        if row["node_id"] != node_id and not self.db.execute("SELECT 1 FROM execution_owners WHERE job_id=? AND node_id=?", (job_id, node_id)).fetchone():
            raise APIError(403, "Archive does not belong to this node")
        return row

    def _execution_fence(self, row, node_id, generation):
        from .hub import APIError
        if type(generation) is not int or generation < 1:
            raise ValueError("Invalid execution generation")
        if row["node_id"] == node_id and row["generation"] == generation:
            return True
        old = self.db.execute("SELECT 1 FROM execution_owners WHERE job_id=? AND node_id=? AND generation=? AND last_attempt IS NOT NULL",
                              (row["id"], node_id, generation)).fetchone()
        if old:
            return False
        raise APIError(403, "Execution generation does not belong to this node")

    def _execution_public(self, job, *, full=False):
        value = job.get("execution_state")
        if isinstance(value, str):
            value = json.loads(value)
        if value and not full:
            value = {key: item for key, item in value.items() if key not in ("diagnostics", "resources")}
        job["execution_state"] = value
        pending = self.db.execute("SELECT * FROM execution_handoffs WHERE job_id=?", (job["id"],)).fetchone()
        job["handoff"] = dict(pending) if pending else None
        control = self.db.execute("SELECT priority,hold,revision FROM execution_queue_controls WHERE job_id=?", (job["id"],)).fetchone()
        job["queue_control"] = {**dict(control), "hold": bool(control["hold"])} if control else {"priority": job["spec"].get("priority", 0), "hold": False, "revision": 0}
        return job

    def set_queue_control(self, payload):
        from .hub import APIError, _integer
        if not isinstance(payload, dict) or set(payload) != {"job_id", "priority", "hold", "revision"}:
            raise ValueError("Queue control requires job_id, priority, hold and revision")
        priority = _integer(payload["priority"], "priority", -100, 100)
        revision = _integer(payload["revision"], "revision")
        if type(payload["hold"]) is not bool:
            raise ValueError("hold must be boolean")
        with self.transaction():
            row = self._find_job(payload["job_id"])
            old = self.db.execute("SELECT * FROM execution_queue_controls WHERE job_id=?", (row["id"],)).fetchone()
            if old and old["priority"] == priority and bool(old["hold"]) == payload["hold"] and revision in (old["revision"], old["revision"] - 1):
                return {**dict(old), "hold": bool(old["hold"])}
            if row["node_id"] is not None or row["state"] != "queued":
                raise APIError(409, "任务已被节点领取；请使用停止或交接操作")
            if revision != (old["revision"] if old else 0):
                raise APIError(409, "排队策略已变化，请刷新后重试")
            self.db.execute("INSERT OR REPLACE INTO execution_queue_controls VALUES (?,?,?,?)", (row["id"], priority, int(payload["hold"]), revision + 1))
            self._event(row["id"], "queue_control", {"priority": priority, "hold": payload["hold"]})
            self._assign()
            return {"job_id": row["id"], "priority": priority, "hold": payload["hold"], "revision": revision + 1}

    def _queue_handoff(self, row, target=None, reason="手动交接"):
        spec = json.loads(row["spec"])
        if spec.get("result_delivery", {}).get("mode") != "lightweight":
            return False  # Legacy archives keep their original ownership contract.
        if spec.get("planning", {}).get("migrate", True) is False:
            return False
        node = self.db.execute("SELECT snapshot FROM nodes WHERE id=?", (row["node_id"],)).fetchone()
        if node is None or CAPABILITY not in json.loads(node[0]).get("capabilities", []):
            return False
        old = self.db.execute("SELECT * FROM execution_handoffs WHERE job_id=?", (row["id"],)).fetchone()
        if old and old["generation"] == row["generation"] and old["status"] in ("requested", "waiting_checkpoint", "waiting_capacity"):
            return True
        self.db.execute("INSERT OR REPLACE INTO execution_handoffs VALUES (?,?,?,?,?,?)",
                        (row["id"], row["generation"], target, reason, "requested", now()))
        if row["state"] not in ("paused", "interrupted", "failed"):
            self.db.execute("UPDATE jobs SET action='stop',command_id=command_id+1,updated=? WHERE id=?", (now(), row["id"]))
        self._event(row["id"], "handoff_requested", {"target_node_id": target, "reason": reason, "generation": row["generation"]})
        return True

    def request_handoff(self, payload):
        from .hub import APIError
        if not isinstance(payload, dict) or set(payload) - {"job_id", "generation", "target_node_id"}:
            raise ValueError("Invalid handoff request")
        with self.transaction():
            row = self._find_job(payload.get("job_id"))
            if type(payload.get("generation")) is not int or payload["generation"] != row["generation"]:
                raise APIError(409, "实验归属已变化，请刷新后重试")
            if row["node_id"] is None or row["state"] in ("succeeded", "canceled"):
                raise APIError(409, "此实验当前不能交接")
            target = payload.get("target_node_id")
            if target is not None and (not isinstance(target, str) or target == row["node_id"] or
                    not self.db.execute("SELECT 1 FROM nodes WHERE id=?", (target,)).fetchone()):
                raise ValueError("Select a different, registered target node")
            if not self._queue_handoff(row, target):
                raise APIError(409, "交接需要新版 Worker、轻量结果协议和允许迁移的实验")
            self._assign()
            return dict(self.db.execute("SELECT * FROM execution_handoffs WHERE job_id=?", (row["id"],)).fetchone())

    def _policy_handoffs(self, node_id, snapshot):
        availability = snapshot.get("availability", {})
        # Stop-and-hold preserves local ownership. Only release hands work off.
        stopping = availability.get("mode") == "release"
        for row in self.db.execute("SELECT * FROM jobs WHERE node_id=?", (node_id,)).fetchall():
            state = json.loads(row["execution_state"]) if row["execution_state"] else {}
            handoff = self.db.execute("SELECT generation,status FROM execution_handoffs WHERE job_id=?", (row["id"],)).fetchone()
            if (row["action"] == "resume" and row["command_ack"] < row["command_id"]) or (
                    row["action"] == "stop" and handoff and handoff["generation"] == row["generation"]
                    and handoff["status"] == "canceled"):
                continue  # A manual stop cancels automatic recovery until explicitly resumed/handed off.
            # A previously manually paused task must never become an automatic resume.
            if row["state"] not in ("succeeded", "canceled", "failed") and (state.get("release") or
                    (stopping and row["state"] not in ("paused", "interrupted"))):
                self._queue_handoff(row, reason=state.get("release") or availability.get("reason", "释放资源"))

    def _maintain_handoffs(self, nodes, counts):
        pending = self.db.execute("SELECT h.*,j.state AS job_state FROM execution_handoffs h JOIN jobs j ON j.id=h.job_id "
                                  "WHERE h.status IN ('requested','waiting_checkpoint','waiting_capacity')").fetchall()
        for handoff in pending:
            row = self._find_job(handoff["job_id"])
            state = json.loads(row["execution_state"]) if row["execution_state"] else {}
            if row["state"] in ("succeeded", "canceled"):
                self.db.execute("UPDATE execution_handoffs SET status='finished',updated=? WHERE job_id=?", (now(), row["id"]))
                continue
            if row["state"] not in ("paused", "interrupted", "failed") or not state.get("stopped_confirmed"):
                continue
            spec, resume = json.loads(row["spec"]), None
            if state.get("ever_started"):
                manifest = state.get("checkpoint")
                if not manifest or (spec.get("resume_supported") is not True and spec["backend"] != "demo"):
                    self.db.execute("UPDATE execution_handoffs SET status='waiting_checkpoint',updated=? WHERE job_id=?", (now(), row["id"]))
                    continue
                received = self.db.execute("SELECT manifest FROM execution_checkpoint_uploads WHERE job_id=? AND attempt=? AND generation=? AND sha256=? AND complete=1",
                    (row["id"], row["attempt"], row["generation"], manifest["sha256"])).fetchone()
                if not received:
                    self.db.execute("UPDATE execution_handoffs SET status='waiting_checkpoint',updated=? WHERE job_id=?", (now(), row["id"]))
                    continue
                resume = json.loads(received[0])
            destinations = [n for n in nodes if CAPABILITY in n["snapshot"].get("capabilities", [])]
            if handoff["target_node_id"]:
                destinations = [n for n in destinations if n["id"] == handoff["target_node_id"]]
            elif handoff["reason"] == "手动交接":
                destinations = [n for n in destinations if n["id"] != row["node_id"]]
            assignment = choose_assignment(spec, destinations, counts)
            if not assignment:
                self.db.execute("UPDATE execution_handoffs SET status='waiting_capacity',updated=? WHERE job_id=?", (now(), row["id"]))
                continue
            target, generation, attempt = assignment["node_id"], row["generation"] + 1, row["attempt"] + 1
            self.db.execute("UPDATE execution_owners SET last_attempt=? WHERE job_id=? AND generation=?", (row["attempt"], row["id"], row["generation"]))
            self._record_execution_owner(row["id"], target, generation, attempt)
            self.db.execute("UPDATE jobs SET node_id=?,spec=?,state='assigned',generation=?,attempt=?,seq=0,execution_state=NULL,"
                            "resume_checkpoint=?,action=NULL,command_ack=command_id,resume_after_attempt=NULL,updated=?,detail=? WHERE id=?",
                            (target, json.dumps(assignment["spec"]), generation, attempt, json.dumps(resume) if resume else None,
                             now(), "已确认原进程停止，正在交接至 " + target, row["id"]))
            self.db.execute("UPDATE execution_handoffs SET status='assigned',updated=? WHERE job_id=?", (now(), row["id"]))
            self._event(row["id"], "ownership_transferred", {"from": row["node_id"], "to": target,
                "generation": generation, "attempt": attempt, "checkpoint": resume})
            counts[target] = counts.get(target, 0) + 1
            from .scheduler import duration_estimate
            for node in nodes:
                if node["id"] == target:
                    node["queued_work_seconds"] = node.get("queued_work_seconds", 0) + duration_estimate(spec, node)

    def upload_execution_checkpoint(self, node_id, payload):
        from .hub import APIError, _integer
        if not isinstance(payload, dict) or set(payload) != {"job_id", "attempt", "generation", "manifest", "offset", "data"}:
            raise ValueError("Invalid checkpoint upload")
        manifest = checkpoint(payload["manifest"])
        attempt = _integer(payload["attempt"], "attempt", 1)
        offset = _integer(payload["offset"], "offset")
        if not isinstance(payload["data"], str) or len(payload["data"]) > 4 * ((CHUNK + 2) // 3):
            raise ValueError("Checkpoint chunks are limited to 512 KiB")
        try:
            data = base64.b64decode(payload["data"], validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("Invalid checkpoint bytes") from error
        if len(data) > CHUNK or offset + len(data) > manifest["size"]:
            raise ValueError("Checkpoint chunk exceeds declared size")
        key = (payload["job_id"], attempt, manifest["sha256"])
        with self.lock:
            if not hasattr(self, "execution_upload_locks"):
                self.execution_upload_locks = {}
            guard = self.execution_upload_locks.setdefault(key, threading.Lock())
        with guard:
            with self.lock:
                row = self._find_job(payload["job_id"])
                current_execution = self._execution_fence(row, node_id, payload["generation"])
                previous = self.db.execute("SELECT * FROM execution_checkpoint_uploads WHERE job_id=? AND attempt=? AND sha256=?", key).fetchone()
                if previous and previous["generation"] != payload["generation"]:
                    raise APIError(403, "Checkpoint receipt belongs to a different execution generation")
                if previous and json.loads(previous["manifest"]) != manifest:
                    raise APIError(409, "Checkpoint metadata changed")
                if previous and previous["complete"]:
                    return {"offset": manifest["size"], "complete": True, "manifest": manifest}
                if not current_execution or row["attempt"] != attempt:
                    raise APIError(409, "Checkpoint execution is no longer current")
                self.db.execute("INSERT OR IGNORE INTO execution_checkpoint_uploads(job_id,attempt,generation,sha256,manifest) VALUES (?,?,?,?,?)",
                                (key[0], attempt, row["generation"], key[2], json.dumps(manifest)))
            target = safe_child(self.root, "execution-checkpoints/" + row["id"] + "/" + manifest["sha256"])
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(".partial")
            # Recover a crash between publishing immutable bytes and committing its receipt.
            if target.is_file() and target.stat().st_size == manifest["size"] and sha256_file(target) == manifest["sha256"]:
                with self.transaction():
                    self.db.execute("UPDATE execution_checkpoint_uploads SET complete=1,received=? WHERE job_id=? AND attempt=? AND sha256=?", (now(), *key))
                    self._assign()
                return {"offset": manifest["size"], "complete": True, "manifest": manifest}
            current = partial.stat().st_size if partial.exists() else 0
            if data and offset == current:
                with partial.open("ab") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                current += len(data)
            elif data:
                return {"offset": current, "complete": False}
            if not partial.exists() and manifest["size"] == 0:
                partial.touch()
            if current == manifest["size"]:
                if sha256_file(partial) != manifest["sha256"]:
                    partial.unlink()
                    raise APIError(409, "Checkpoint checksum mismatch; retry from offset zero")
                os.replace(partial, target)
                with self.transaction():
                    self.db.execute("UPDATE execution_checkpoint_uploads SET complete=1,received=? WHERE job_id=? AND attempt=? AND sha256=?", (now(), *key))
                    self._event(row["id"], "checkpoint_received", {"attempt": attempt, "sha256": key[2], "size": manifest["size"]})
                    self._assign()
                return {"offset": current, "complete": True, "manifest": manifest}
            return {"offset": current, "complete": False}

    def download_execution_checkpoint(self, node_id, payload):
        from .hub import APIError, _integer
        if not isinstance(payload, dict) or set(payload) != {"job_id", "generation", "offset"}:
            raise ValueError("Invalid checkpoint download")
        offset = _integer(payload["offset"], "offset")
        with self.lock:
            row = self._find_job(payload["job_id"])
            if not self._execution_fence(row, node_id, payload["generation"]) or not row["resume_checkpoint"]:
                raise APIError(403, "No checkpoint assigned to this execution")
            manifest = json.loads(row["resume_checkpoint"])
        if offset > manifest["size"]:
            raise ValueError("Checkpoint offset is beyond file size")
        path = safe_child(self.root, "execution-checkpoints/" + row["id"] + "/" + manifest["sha256"])
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(CHUNK)
        return {"manifest": manifest, "offset": offset, "data": base64.b64encode(data).decode("ascii"), "complete": offset + len(data) == manifest["size"]}
