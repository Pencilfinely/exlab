"""Independent resumable execution checkpoint lane; never produces result JSON."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import hashlib
import sqlite3
import threading

from . import common
from .execution_control import CHUNK, checkpoint


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS execution_lane_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS execution_checkpoint_outbox (
            job_id TEXT NOT NULL, generation INTEGER NOT NULL, attempt INTEGER NOT NULL,
            sha256 TEXT NOT NULL, manifest TEXT NOT NULL, path TEXT NOT NULL,
            offset INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(job_id,generation,attempt,sha256));
    """)
    db.commit()


def telemetry(db):
    values = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM execution_lane_meta")}
    count, size = db.execute("SELECT COUNT(*),COALESCE(SUM(offset),0) FROM execution_checkpoint_outbox WHERE complete=0").fetchone()
    return {**values, "pending_checkpoints": count, "confirmed_pending_bytes": size}


def describe(agent, record):
    value = {"schema_version": 1, "generation": record.get("generation", 1), "attempt": record["attempt"],
             "ever_started": bool(record.get("ever_started")),
             "stopped_confirmed": bool(record.get("execution_stopped")) or (
                 record["state"] in ("paused", "interrupted", "canceled") and not record.get("ever_started")),
             "checkpoint": None, "progress": record.get("metrics", {}), "release": record.get("resource_release")}
    try:
        manifest = common.read_json(agent._output(record) / "checkpoint.json")
        if manifest:
            file = common.safe_child(agent._output(record), manifest["path"])
            value["checkpoint"] = checkpoint({**manifest, "size": file.stat().st_size})
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return value


class ExecutionTransfer:
    def __init__(self, agent):
        self.root = agent.root
        self.config = agent.config
        self.stop = threading.Event()
        self.thread = None
        initialize(agent.db)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="execution-checkpoints", daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=6)

    @staticmethod
    def _meta(db, key, value):
        db.execute("INSERT OR REPLACE INTO execution_lane_meta VALUES (?,?)", (key, json.dumps(value, allow_nan=False)))
        db.commit()

    def _request(self, endpoint, payload):
        return common.api_request(self.config["hub_url"].rstrip("/") + endpoint, self.config["token"], payload, timeout=3)

    def _freeze(self, db, record):
        output = common.safe_child(self.root, "runs/" + record["id"])
        manifest = common.read_json(output / "checkpoint.json")
        if not manifest:
            return
        source = common.safe_child(output, manifest["path"])
        manifest = checkpoint({**manifest, "size": source.stat().st_size})
        key = (record["id"], record.get("generation", 1), record["attempt"], manifest["sha256"])
        if db.execute("SELECT 1 FROM execution_checkpoint_outbox WHERE job_id=? AND generation=? AND attempt=? AND sha256=?", key).fetchone():
            return
        target = common.safe_child(self.root, "execution-cache/" + record["id"] + "/" + manifest["sha256"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix(".copying")
            digest = hashlib.sha256()
            with source.open("rb") as reader, temporary.open("wb") as writer:
                for data in iter(lambda: reader.read(1024 * 1024), b""):
                    if self.stop.is_set():
                        return
                    writer.write(data)
                    digest.update(data)
            if temporary.stat().st_size != manifest["size"] or digest.hexdigest() != manifest["sha256"]:
                temporary.unlink()
                return  # The producer replaced a checkpoint while it was copied.
            os.replace(temporary, target)
        db.execute("INSERT OR IGNORE INTO execution_checkpoint_outbox(job_id,generation,attempt,sha256,manifest,path) VALUES (?,?,?,?,?,?)",
                   (*key, json.dumps(manifest), str(target)))
        db.commit()

    def _upload(self, db, record):
        row = db.execute("SELECT * FROM execution_checkpoint_outbox WHERE job_id=? AND generation=? AND attempt=? AND complete=0 ORDER BY rowid LIMIT 1",
                         (record["id"], record.get("generation", 1), record["attempt"])).fetchone()
        if not row:
            return
        manifest = json.loads(row["manifest"])
        body = {"job_id": row["job_id"], "generation": row["generation"], "attempt": row["attempt"],
                "manifest": manifest, "offset": 0, "data": ""}
        # Query the durable receiver offset on every cycle; a lost ACK is safe.
        response = self._request("/api/execution/checkpoints", body)
        offset = response["offset"]
        if type(offset) is not int or not 0 <= offset <= manifest["size"]:
            raise ValueError("Invalid checkpoint receiver offset")
        if not response.get("complete") and not self.stop.is_set():
            path = common.safe_child(self.root, "execution-cache/" + record["id"] + "/" + manifest["sha256"])
            with path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read(CHUNK)
            response = self._request("/api/execution/checkpoints", {**body, "offset": offset, "data": base64.b64encode(data).decode("ascii")})
            offset = response["offset"]
        if type(offset) is not int or not 0 <= offset <= manifest["size"] or (response.get("complete") and offset != manifest["size"]):
            raise ValueError("Invalid checkpoint upload acknowledgment")
        db.execute("UPDATE execution_checkpoint_outbox SET offset=?,complete=? WHERE job_id=? AND generation=? AND attempt=? AND sha256=?",
                   (offset, int(bool(response.get("complete"))), row["job_id"], row["generation"], row["attempt"], row["sha256"]))
        db.commit()
        if response.get("complete"):
            self._meta(db, "last_checkpoint_received_at", common.now())

    def _restore(self, db, record):
        manifest = record.get("resume_checkpoint")
        if not manifest or record.get("checkpoint_restored"):
            return
        manifest = checkpoint(manifest)
        output = common.safe_child(self.root, "runs/" + record["id"])
        output.mkdir(parents=True, exist_ok=True)
        marker = output / (".execution-resume-" + str(record["generation"]) + ".json")
        if common.read_json(marker) == manifest:
            return
        relative = "recovery/" + str(record["generation"]) + "/" + manifest["sha256"]
        target = common.safe_child(output, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".partial")
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > manifest["size"]:
            partial.unlink()
            offset = 0
        response = self._request("/api/execution/checkpoints/download", {"job_id": record["id"], "generation": record["generation"], "offset": offset})
        if response.get("manifest") != manifest or response.get("offset") != offset:
            raise ValueError("Assigned checkpoint identity changed during restore")
        data = base64.b64decode(response["data"], validate=True)
        if len(data) > CHUNK or offset + len(data) > manifest["size"] or (not data and offset != manifest["size"]):
            raise ValueError("Invalid checkpoint download chunk")
        with partial.open("ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self._meta(db, "restoring", {"job_id": record["id"], "confirmed_bytes": offset + len(data), "size": manifest["size"]})
        if offset + len(data) == manifest["size"]:
            if common.sha256_file(partial) != manifest["sha256"]:
                partial.unlink()
                raise ValueError("Restored checkpoint checksum mismatch")
            os.replace(partial, target)
            common.atomic_json(output / "checkpoint.json", {key: item for key, item in {**manifest, "path": relative}.items() if key != "size"})
            common.atomic_json(marker, manifest)
            self._meta(db, "restoring", None)

    def _run(self):
        db = sqlite3.connect(self.root / "node.sqlite3", timeout=5)
        db.row_factory = sqlite3.Row
        try:
            while not self.stop.is_set():
                try:
                    enabled = db.execute("SELECT value FROM metadata WHERE key='execution_protocol'").fetchone()
                    if enabled and json.loads(enabled[0]) == 1:
                        records = [json.loads(row[0]) for row in db.execute("SELECT record FROM tasks")]
                        errors = []
                        for record in records:
                            if self.stop.is_set():
                                break
                            if record.get("execution_retired"):
                                continue
                            try:
                                self._restore(db, record)
                                if record.get("resume_checkpoint") and not record.get("checkpoint_restored"):
                                    continue
                                self._freeze(db, record)
                                self._upload(db, record)
                            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                                # One broken publication must not starve other experiments.
                                errors.append(record["id"] + ": " + str(error))
                        self._meta(db, "last_error", "; ".join(errors)[:500] if errors else None)
                        self._meta(db, "next_retry_at", common.now() + 3 if errors else None)
                        if errors:
                            self.stop.wait(3)
                            continue
                    self.stop.wait(0.25)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as error:
                    # Errors stay in the execution lane and never block state or final results.
                    self._meta(db, "last_error", str(error)[:500])
                    self._meta(db, "next_retry_at", common.now() + 3)
                    self.stop.wait(3)
        finally:
            db.close()
