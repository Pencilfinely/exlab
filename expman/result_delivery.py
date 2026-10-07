"""Two durable lanes: result discovery/sending and on-demand evidence/control.

Neither lane controls execution. Network retries only resend immutable bytes.
The result lane never copies or hashes a checkpoint and has its own HTTP request.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import urllib.error

from . import __version__, common
from . import result_protocol as protocol


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS node_results (
            job_id TEXT NOT NULL, attempt INTEGER NOT NULL, experiment_id TEXT NOT NULL,
            source_hash TEXT NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued', received REAL, retries INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '', error_at REAL, next_retry REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(job_id,attempt,experiment_id,sha256));
        CREATE TABLE IF NOT EXISTS node_evidence (
            job_id TEXT NOT NULL, attempt INTEGER NOT NULL, experiment_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
            name TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER, sha256 TEXT, source TEXT NOT NULL, path TEXT,
            retention TEXT NOT NULL DEFAULT 'pending', availability TEXT NOT NULL DEFAULT 'pending',
            transfer_status TEXT NOT NULL DEFAULT 'retained', confirmed_bytes INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '', retries INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
            error_at REAL,
            dirty INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(job_id,attempt,experiment_id,evidence_id));
        CREATE TABLE IF NOT EXISTS legacy_upload_policy (
            job_id TEXT NOT NULL, name TEXT NOT NULL, sha TEXT NOT NULL, disposition TEXT NOT NULL,
            PRIMARY KEY(job_id,name,sha));
        CREATE TABLE IF NOT EXISTS node_delivery_overrides (job_id TEXT PRIMARY KEY, config TEXT NOT NULL, request_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS node_delivery_commands (request_id TEXT PRIMARY KEY, status TEXT NOT NULL, report TEXT NOT NULL, dirty INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS node_delivery_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS node_progress (
            job_id TEXT NOT NULL, attempt INTEGER NOT NULL, experiment_id TEXT NOT NULL, value TEXT NOT NULL,
            dirty INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(job_id,attempt,experiment_id));
    """)
    db.commit()


def lightweight(db, record):
    override = db.execute("SELECT config FROM node_delivery_overrides WHERE job_id=?", (record["id"],)).fetchone()
    if override:
        return json.loads(override[0])
    config = record["spec"].get("result_delivery", {"mode": "legacy"})
    return config if config.get("mode") == "lightweight" else None


def telemetry(db, active=None):
    def dictionaries(query):
        cursor = db.execute(query)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor]
    rows = dictionaries("SELECT * FROM node_results")
    results = [row for row in rows if row["status"] in ("queued", "retrying", "sending")]
    evidence = dictionaries("SELECT * FROM node_evidence WHERE transfer_status IN ('queued','uploading','retrying')")
    legacy = db.execute("SELECT COUNT(*),COALESCE(SUM(MAX(0,u.size-u.offset)),0) FROM uploads u "
                        "LEFT JOIN legacy_upload_policy p ON p.job_id=u.job_id AND p.name=u.name AND p.sha=u.sha "
                        "WHERE u.complete=0 AND COALESCE(p.disposition,'automatic')!='retained'").fetchone()
    errors = [row for row in rows if row["error"]] + [row for row in dictionaries("SELECT * FROM node_evidence") if row["error"]]
    latest = max(errors, key=lambda row: row["error_at"] or 0) if errors else None
    meta = {row[0]: json.loads(row[1]) for row in db.execute("SELECT key,value FROM node_delivery_meta")}
    if meta.get("legacy_error_at", 0) > ((latest["error_at"] or 0) if latest else 0):
        latest = {"error": meta["legacy_error"], "error_at": meta["legacy_error_at"],
                  "retries": meta.get("legacy_retry_count", 0), "next_retry": meta.get("legacy_next_retry_at", 0)}
    return {"agent_version": __version__, "protocol_version": 1,
            "pending_results": len(results), "pending_result_bytes": sum(row["size"] for row in results),
            "pending_evidence": len(evidence), "pending_evidence_bytes": None if any(row["size"] is None for row in evidence) else sum(max(0, row["size"] - row["confirmed_bytes"]) for row in evidence),
            "legacy_pending_files": legacy[0], "legacy_pending_bytes": legacy[1],
            "blocked_results": sum(row["status"] in ("conflict", "unsupported", "invalid") for row in rows),
            "current_result": meta.get("current_result"), "current_evidence": meta.get("current_evidence"),
            "current_legacy": active, "last_byte_progress_at": meta.get("last_byte_progress_at"),
            "last_file_completed_at": meta.get("last_file_completed_at"),
            "last_error": latest["error"] if latest else meta.get("control_error") or meta.get("scan_error"),
            "last_error_at": latest["error_at"] if latest else meta.get("control_error_at"),
            "retry_count": latest["retries"] if latest else 0,
            "next_retry_at": latest["next_retry"] if latest and latest["next_retry"] else None,
            "result_worker_at": meta.get("result_worker_at"), "evidence_worker_at": meta.get("evidence_worker_at"),
            "hub_version": meta.get("hub_version"), "hub_capabilities": meta.get("hub_capabilities", [])}


class ResultDelivery:
    def __init__(self, agent):
        self.agent = agent
        initialize(agent.db)
        self.root = agent.root
        self.result_db = self._connect()
        self.control_db = self._connect()
        self.result_lock = threading.RLock()
        self.stop = threading.Event()
        self.threads = []
        self.catalogued = set()

    def _connect(self):
        db = sqlite3.connect(self.root / "node.sqlite3", check_same_thread=False, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def start(self):
        if self.threads:
            return
        for function in (self._result_loop, self._control_loop):
            thread = threading.Thread(target=function, daemon=True, name=function.__name__)
            self.threads.append(thread)
            thread.start()

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join()
        self.result_db.close()
        self.control_db.close()

    def _meta(self, db, key, value):
        with db:
            db.execute("INSERT INTO node_delivery_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, json.dumps(value, allow_nan=False)))

    def _error(self, exc):
        message = str(exc)
        if isinstance(exc, urllib.error.HTTPError):
            try:
                message += ": " + exc.read(2000).decode("utf-8", errors="replace")
            except OSError:
                pass
            finally:
                exc.close()
        message = message.replace(self.agent.config["token"], "[redacted]")
        message = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[redacted]", message)
        message = re.sub(r"(?i)(token|password|api[_-]?key)([=:]\s*)[^\s&,\"']+", r"\1\2[redacted]", message)
        return message[:1000]

    def _request(self, endpoint, payload, timeout=2):
        return common.api_request(self.agent.config["hub_url"].rstrip("/") + endpoint,
                                  self.agent.config["token"], payload, timeout=timeout)

    def _records(self, db):
        return [json.loads(row[0]) for row in db.execute("SELECT record FROM tasks ORDER BY id")]

    def discover_record(self, record):
        with self.result_lock:
            config = lightweight(self.result_db, record)
        if config is None:
            return
        output = common.safe_child(self.root / "runs", record["id"])
        registered = common.safe_child(output, f".exlab/results/attempt-{record['attempt']}")
        dynamic = []
        if registered.is_dir():
            for path in sorted(registered.glob("*.json")):
                common.safe_child(output, path.relative_to(output).as_posix())
                if path.is_file():
                    dynamic.append((protocol.identifier(path.stem), path))
        for identity, path in dynamic:
            try:
                self._register(record, identity, path, registered=True)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                if record["state"] not in protocol.TERMINAL:
                    raise
                self._register_failure(record, identity, path, exc)
        single_default = len(config.get("files", [])) == 1 and config["files"][0]["path"] == "result.json"
        if dynamic and single_default:
            return  # A batch never gets an extra implicit job-level result.
        for item in config.get("files", []):
            identity = item["experiment_id"]
            if any(pair[0] == identity for pair in dynamic):
                continue
            path = common.safe_child(output, protocol.relative(item["path"]))
            if not item.get("stream") and record["state"] not in protocol.TERMINAL:
                continue
            if path.is_file():
                # A resumed attempt must not publish the previous attempt's result.
                started = record.get("_timing", {}).get("segment_started_at")
                if record["attempt"] > 1 and started and path.stat().st_mtime < started:
                    continue
                try:
                    self._register(record, identity, path)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    if record["state"] not in protocol.TERMINAL:
                        raise
                    self._register_failure(record, identity, path, exc)
            elif record["state"] in protocol.TERMINAL and self.result_db.execute("SELECT 1 FROM node_delivery_overrides WHERE job_id=?", (record["id"],)).fetchone():
                cached = self.result_db.execute("SELECT path FROM uploads WHERE job_id=? AND name=? ORDER BY complete DESC LIMIT 1", (record["id"], item["path"])).fetchone()
                if cached and Path(cached[0]).is_file() and Path(cached[0]).resolve().is_relative_to(self.root / "upload_cache" / record["id"]):
                    self._register(record, identity, Path(cached[0]))
                else:
                    raise ValueError("Migrated result source and immutable cache are unavailable; no rerun")
            elif record["state"] in protocol.TERMINAL:
                producer = {"status": "failed", "failure_stage": "result_generation",
                            "error": "Declared result file is missing; scientific metrics are unknown.",
                            "execution": {"status": "failed", "exit_code": record.get("exit_code"),
                                          "stages": {"training": {"status": "unknown", "exit_code": None},
                                                     "test": {"status": "not_run", "exit_code": None}}}}
                self._register(record, identity, None, producer=producer)

    def _register_failure(self, record, identity, path, exc):
        output = common.safe_child(self.root / "runs", record["id"])
        name = path.relative_to(output).as_posix()
        producer = {"status": "failed", "failure_stage": "result_registration", "error": self._error(exc),
                    "evidence": [{"path": name, "kind": "invalid-result-source", "size": path.stat().st_size}],
                    "execution": {"status": "failed", "exit_code": record.get("exit_code"),
                        "stages": {"training": {"status": "unknown", "exit_code": None},
                                   "test": {"status": "unknown", "exit_code": None}}}}
        self._register(record, identity, None, producer=producer)

    def _register(self, record, identity, path, registered=False, producer=None):
        if path is not None:
            if path.stat().st_size > protocol.MAX_RESULT_BYTES:
                raise ValueError("Result producer file exceeds 1 MiB; retain arrays as evidence, do not rerun")
            raw = path.read_bytes()
            source_hash = hashlib.sha256(raw).hexdigest()
            parsed = json.loads(raw.decode("utf-8-sig"))
            if registered:
                if parsed.get("attempt") != record["attempt"] or parsed.get("experiment_id") != identity:
                    raise ValueError("Registered experiment identity disagrees with its file")
                producer, completed = parsed["result"], parsed["completed_at"]
            else:
                producer, completed = parsed, path.stat().st_mtime
        else:
            source_hash = hashlib.sha256(protocol.encoded(producer)).hexdigest()
            completed = record.get("updated", common.now())
        with self.result_lock:
            old = self.result_db.execute("SELECT source_hash FROM node_results WHERE job_id=? AND attempt=? AND experiment_id=?",
                                        (record["id"], record["attempt"], identity)).fetchall()
            if any(row[0] == source_hash for row in old):
                return
        summary = protocol.envelope(record, producer, identity, self.agent.config["node_id"], completed, source_hash)
        protocol.validate_envelope(summary)
        raw = protocol.encoded(summary)
        if len(raw) > protocol.MAX_RESULT_BYTES:
            raise ValueError("Result envelope exceeds 1 MiB; preserve source and move arrays to evidence without rerunning")
        digest = hashlib.sha256(raw).hexdigest()
        destination = common.safe_child(self.root, f"result_cache/{record['id']}/{digest}.json")
        common.atomic_json(destination, summary)
        # atomic_json formatting differs from the canonical wire encoding: hash the exact cached bytes.
        wire = destination.read_bytes()
        digest = hashlib.sha256(wire).hexdigest()
        final = destination.with_name(digest + ".json")
        os.replace(destination, final)
        with self.result_lock, self.result_db:
            self.result_db.execute("INSERT OR IGNORE INTO node_results(job_id,attempt,experiment_id,source_hash,sha256,path,size) VALUES(?,?,?,?,?,?,?)",
                (record["id"], record["attempt"], identity, source_hash, digest, str(final), len(wire)))

    def results_once(self):
        db = self.result_db
        with self.result_lock:
            records = self._records(db)
        for record in records:
            if self.stop.is_set():
                return
            try:
                self.discover_record(record)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                with self.result_lock:
                    self._meta(db, "scan_error", self._error(exc))
        with self.result_lock:
            queue = [dict(row) for row in db.execute("SELECT * FROM node_results WHERE status IN ('queued','retrying','sending') AND next_retry<=? ORDER BY retries,rowid DESC LIMIT 100", (common.now(),))]
        sending_started = time.monotonic()
        for item in queue:
            if self.stop.is_set():
                return
            key = (item["job_id"], item["attempt"], item["experiment_id"], item["sha256"])
            with self.result_lock:
                self._meta(db, "current_result", {"job_id": item["job_id"], "experiment_id": item["experiment_id"],
                          "confirmed_bytes": 0, "total_bytes": item["size"], "started_at": common.now()})
            try:
                raw = Path(item["path"]).read_bytes()
                if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                    raise ValueError("Immutable result cache changed")
                response = self._request("/api/results", {"sha256": item["sha256"], "data": base64.b64encode(raw).decode()})
                if response.get("sha256") != item["sha256"] or response.get("size") != len(raw) or response.get("complete") is not True:
                    raise ValueError("Result receipt does not match the sent bytes")
                with self.result_lock, db:
                    db.execute("UPDATE node_results SET status='delivered',received=?,next_retry=0 WHERE job_id=? AND attempt=? AND experiment_id=? AND sha256=?", (response["received_at"], *key))
                with self.result_lock:
                    self._meta(db, "last_file_completed_at", common.now())
                    self._meta(db, "last_byte_progress_at", common.now())
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                status = {409: "conflict", 404: "unsupported", 400: "invalid", 403: "invalid", 413: "invalid"}.get(getattr(exc, "code", None), "retrying")
                retries = item["retries"] + 1
                with self.result_lock, db:
                    db.execute("UPDATE node_results SET status=?,retries=?,error=?,error_at=?,next_retry=? WHERE job_id=? AND attempt=? AND experiment_id=? AND sha256=?",
                        (status, retries, self._error(exc), common.now(), common.now() + min(5, 0.5 * 2**min(retries, 4)) if status == "retrying" else 0, *key))
            finally:
                with self.result_lock:
                    self._meta(db, "current_result", None)
            if time.monotonic() - sending_started >= 2:
                break  # Bound each sending pass so discovery runs while old results retry.
        with self.result_lock:
            self._meta(db, "result_worker_at", common.now())

    def _evidence(self, db, record, experiment, item, source=None):
        name = protocol.relative(item["name"])
        identity = protocol.evidence_id(record["id"], record["attempt"], experiment, name)
        source = source or common.safe_child(self.root / "runs" / record["id"], name)
        resolved = Path(source).resolve()
        if not (resolved.is_relative_to(self.root / "runs" / record["id"]) or resolved.is_relative_to(self.root / "upload_cache" / record["id"])):
            raise ValueError("Evidence source must be an owned output or upload cache")
        with db:
            db.execute("INSERT OR IGNORE INTO node_evidence(job_id,attempt,experiment_id,evidence_id,name,kind,size,sha256,source) VALUES(?,?,?,?,?,?,?,?,?)",
                       (record["id"], record["attempt"], experiment, identity, name, item.get("kind", "file"), item.get("size"), item.get("sha256"), str(source)))

    def _catalog(self):
        db = self.control_db
        records = self._records(db)
        for record in records:
            config = lightweight(db, record)
            if config is None:
                continue
            results = db.execute("SELECT experiment_id,path FROM node_results WHERE job_id=? AND attempt=?", (record["id"], record["attempt"])).fetchall()
            for result in results:
                summary = common.read_json(result["path"])
                for item in summary["evidence"]:
                    self._evidence(db, record, result["experiment_id"], item)
            output = common.safe_child(self.root / "runs", record["id"])
            progress_dir = common.safe_child(output, f".exlab/progress/attempt-{record['attempt']}")
            if progress_dir.is_dir():
                for path in progress_dir.glob("*.json"):
                    common.safe_child(output, path.relative_to(output).as_posix())
                    item = common.read_json(path)
                    if item.get("attempt") != record["attempt"] or item.get("experiment_id") != path.stem:
                        raise ValueError("Progress identity disagrees with its path")
                    protocol.identifier(path.stem)
                    raw = protocol.encoded({"job_id": record["id"], **item}).decode()
                    with db:
                        db.execute("INSERT INTO node_progress VALUES (?,?,?,?,1) ON CONFLICT(job_id,attempt,experiment_id) DO UPDATE SET value=excluded.value,dirty=CASE WHEN node_progress.value=excluded.value THEN node_progress.dirty ELSE 1 END",
                                   (record["id"], record["attempt"], path.stem, raw))
            key = (record["id"], record["attempt"])
            if record["state"] not in protocol.TERMINAL or key in self.catalogued or not results:
                continue
            experiments = {row["experiment_id"] for row in results}
            # Batch evidence must be explicitly associated with each experiment by its producer.
            if len(experiments) == 1:
                experiment = experiments.pop()
                result_names = {item["path"] for item in config["files"]}
                for parent, directories, files in os.walk(output, followlinks=False):
                    directories[:] = [d for d in directories if d != ".exlab" and not (Path(parent) / d).is_symlink()]
                    for name in files:
                        path = Path(parent) / name
                        relative = path.relative_to(output).as_posix()
                        if path.is_symlink() or not path.is_file() or name == "STOP" or name.endswith(".tmp") or name.startswith(".write-") or relative in result_names:
                            continue
                        common.safe_child(output, relative)
                        self._evidence(db, record, experiment, {"name": relative, "size": path.stat().st_size})
            self.catalogued.add(key)

    def _retain_one(self):
        db = self.control_db
        row = db.execute("SELECT * FROM node_evidence WHERE retention='pending' LIMIT 1").fetchone()
        if row is None:
            return
        key = (row["job_id"], row["attempt"], row["experiment_id"], row["evidence_id"])
        source = common.safe_child(self.root, Path(row["source"]).relative_to(self.root).as_posix())
        destination = common.safe_child(self.root, f"retained_evidence/{row['job_id']}/{row['attempt']}/{row['experiment_id']}/{row['evidence_id']}")
        temporary = destination.with_suffix(".tmp")
        try:
            if source.is_symlink() or not source.is_file():
                raise FileNotFoundError("Evidence source is missing or is a symbolic link")
            before = source.stat()
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                temporary.unlink(missing_ok=True)
                if source.is_relative_to(self.root / "upload_cache" / row["job_id"]):
                    try:
                        os.link(source, temporary)  # Existing content-addressed snapshots are immutable.
                    except OSError:
                        shutil.copyfile(source, temporary)
                else:
                    # Algorithms may overwrite a named checkpoint on a later attempt.
                    # A live-output hardlink would mutate the retained historical evidence.
                    digest_value = hashlib.sha256()
                    with source.open("rb") as incoming, temporary.open("wb") as outgoing:
                        for block in iter(lambda: incoming.read(1024 * 1024), b""):
                            if self.stop.is_set():
                                raise InterruptedError("Evidence retention deferred for Agent shutdown; source preserved")
                            outgoing.write(block)
                            digest_value.update(block)
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                    digest = digest_value.hexdigest()
                if source.is_relative_to(self.root / "upload_cache" / row["job_id"]):
                    digest = common.sha256_file(temporary)
                after = source.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("Evidence changed while being retained")
                os.replace(temporary, destination)
            else:
                digest = common.sha256_file(destination)
            size = destination.stat().st_size
            if row["sha256"] is not None and digest != row["sha256"] or row["size"] is not None and size != row["size"]:
                raise ValueError("Evidence differs from the declared size/SHA-256; no upload or rerun")
            with db:
                db.execute("UPDATE node_evidence SET path=?,size=?,sha256=?,retention='retained',availability='available',dirty=1 WHERE job_id=? AND attempt=? AND experiment_id=? AND evidence_id=?", (str(destination), size, digest, *key))
        except (OSError, ValueError) as exc:
            status = "missing" if isinstance(exc, FileNotFoundError) else "changed" if isinstance(exc, ValueError) else "pending"
            with db:
                db.execute("UPDATE node_evidence SET retention=?,availability=?,error=?,error_at=?,dirty=1 WHERE job_id=? AND attempt=? AND experiment_id=? AND evidence_id=?", (status, status, self._error(exc), common.now(), *key))
        finally:
            temporary.unlink(missing_ok=True)

    def _migration_plan(self, payload):
        db = self.control_db
        records = {record["id"]: record for record in self._records(db)}
        jobs = payload["job_ids"]
        mappings = payload.get("result_files", [])
        configured = {}
        for job in jobs:
            record = records.get(job)
            if record is None or record["spec"].get("project_id", record["spec"]["algorithm"]) != payload["project_id"]:
                raise ValueError("Migration scope does not match local owned project/job")
            if record["attempt"] != 1:
                raise NotImplementedError("Safe migration unsupported: this legacy upload queue has no per-file attempt metadata for a resumed job; preserve its files and use producer attempt manifests for manual compatibility review")
            config = record["spec"].get("result_delivery", protocol.delivery_config(None, record["spec"].get("experiment_id", "default")))
            configured[job] = config["files"]
        for mapping in mappings:
            job = mapping.get("job_id")
            if job not in jobs:
                raise ValueError("Result mapping targets a job outside the migration scope")
        for job in jobs:
            selected = [mapping for mapping in mappings if mapping.get("job_id") == job]
            if selected:
                configured[job] = protocol.delivery_config({"files": [{"experiment_id": mapping.get("experiment_id"),
                    "path": mapping.get("path"), "stream": True} for mapping in selected]})["files"]
        rows, unresolved, identities = [], [], []
        for job in jobs:
            record = records[job]
            identities.append({"job_id": job, "attempt": record["attempt"], "spec_sha256": hashlib.sha256(protocol.encoded(record["spec"])).hexdigest(),
                               "command_ack": record.get("command_ack"), "result_files": configured[job]})
            for row in db.execute("SELECT u.*,COALESCE(p.disposition,'automatic') AS disposition FROM uploads u LEFT JOIN legacy_upload_policy p ON p.job_id=u.job_id AND p.name=u.name AND p.sha=u.sha WHERE u.job_id=? ORDER BY u.name,u.sha", (job,)):
                item = dict(row)
                if any(prior["job_id"] == job and prior["name"] == row["name"] and prior["sha"] != row["sha"] for prior in rows):
                    raise NotImplementedError("Safe migration unsupported: multiple legacy versions of the same file lack experiment/attempt bindings; no queue changes made")
                cache = common.safe_child(self.root, Path(row["path"]).relative_to(self.root).as_posix())
                item["cache_status"] = "available" if cache.is_file() and cache.stat().st_size == row["size"] else "missing_or_changed"
                item["kind"] = "result" if any(entry["path"] == row["name"] for entry in configured[job]) else "evidence"
                candidates = [entry for entry in configured[job] if row["name"] == entry["path"] or
                    (Path(entry["path"]).parent.as_posix() != "." and row["name"].startswith(Path(entry["path"]).parent.as_posix() + "/"))]
                item["experiment_id"] = max(candidates, key=lambda entry: len(entry["path"]))["experiment_id"] if candidates else configured[job][0]["experiment_id"] if len(configured[job]) == 1 else "job-evidence"
                item["confirmed_bytes"] = row["offset"]
                item["remaining_bytes"] = max(0, row["size"] - row["offset"]) if not row["complete"] else 0
                active = getattr(self.agent, "active_legacy_upload", None)
                item["in_flight"] = bool(active and (active["job_id"], active["name"], active["sha256"]) == (job, row["name"], row["sha"]))
                if Path(row["name"]).name == "000_result.json" and item["kind"] != "result":
                    unresolved.append({"job_id": job, "path": row["name"], "reason": "Explicit experiment_id mapping required"})
                item.pop("path")  # Internal paths are never a remotely writable file selector.
                rows.append(item)
        signature = hashlib.sha256(protocol.encoded({"identities": identities, "files": [{key: row[key] for key in ("job_id", "name", "sha", "size", "kind")} for row in rows]})).hexdigest()
        reduction = sum(row["remaining_bytes"] for row in rows if row["kind"] == "evidence" and row["disposition"] != "retained")
        plan = {"plan_sha256": signature, "project_id": payload["project_id"], "identities": identities,
                "files": rows, "unresolved_results": unresolved, "estimated_reduced_bytes": reduction,
                "source_files_preserved": True, "execution_unchanged": True}
        if len(protocol.encoded(plan)) > 512 * 1024:
            raise ValueError("Migration review exceeds 512 KiB; select fewer jobs to keep before/after reports reviewable")
        return plan

    def _migration(self, command):
        db = self.control_db
        payload = command["payload"]
        plan = self._migration_plan(payload)
        if payload["action"] == "dry-run":
            return plan
        reviewed = payload["plan"]
        if plan["plan_sha256"] != reviewed.get("plan_sha256") or plan["unresolved_results"]:
            raise ValueError("Queue identities changed or experiment mappings are unresolved; generate a new dry-run")
        if any(row["cache_status"] != "available" for row in plan["files"]):
            raise ValueError("Immutable upload cache is missing or changed; cannot safely migrate, no rerun")
        records = {record["id"]: record for record in self._records(db)}
        with db:
            for identity in plan["identities"]:
                config = protocol.delivery_config({"mode": "lightweight", "files": identity["result_files"]})
                db.execute("INSERT INTO node_delivery_overrides VALUES (?,?,?) ON CONFLICT(job_id) DO UPDATE SET config=excluded.config,request_id=excluded.request_id", (identity["job_id"], json.dumps(config), command["request_id"]))
            for row in plan["files"]:
                # Preserve the original cache, SHA, offset and completion bit. The next chunk yields.
                db.execute("INSERT INTO legacy_upload_policy VALUES (?,?,?,'retained') ON CONFLICT(job_id,name,sha) DO UPDATE SET disposition='retained'", (row["job_id"], row["name"], row["sha"]))
        for row in plan["files"]:
            record = records[row["job_id"]]
            experiment = row["experiment_id"]
            source = db.execute("SELECT path FROM uploads WHERE job_id=? AND name=? AND sha=?", (row["job_id"], row["name"], row["sha"])).fetchone()[0]
            self._evidence(db, record, experiment, {"name": row["name"], "size": row["size"], "sha256": row["sha"], "kind": row["kind"]}, source=source)
        for job in payload["job_ids"]:
            self.discover_record(records[job])
        return {"before": reviewed, "after": self._migration_plan(payload), "reduced_bytes": plan["estimated_reduced_bytes"],
                "in_flight_yield_pending": any(row["in_flight"] for row in plan["files"]),
                "source_files_preserved": True, "execution_unchanged": True, "plan_id": payload["plan_id"]}

    def _commands(self, commands):
        db = self.control_db
        for command in commands:
            old = db.execute("SELECT * FROM node_delivery_commands WHERE request_id=?", (command["request_id"],)).fetchone()
            if old:
                with db:
                    db.execute("UPDATE node_delivery_commands SET dirty=1 WHERE request_id=?", (command["request_id"],))
                continue
            try:
                if command["kind"] == "migration":
                    report = self._migration(command)
                    status = "completed"
                elif command["kind"] == "evidence":
                    payload = command["payload"]
                    records = {record["id"]: record for record in self._records(db)}
                    if payload["job_id"] not in records:
                        raise ValueError("Evidence job is not owned locally")
                    for identity in payload["evidence_ids"]:
                        row = db.execute("SELECT * FROM node_evidence WHERE job_id=? AND attempt=? AND experiment_id=? AND evidence_id=?",
                            (payload["job_id"], payload["attempt"], payload["experiment_id"], identity)).fetchone()
                        if row is None:
                            raise ValueError("Evidence ID is not registered locally")
                        with db:
                            db.execute("UPDATE node_evidence SET transfer_status=CASE WHEN transfer_status='delivered' THEN transfer_status ELSE 'queued' END,dirty=1,next_retry=0 WHERE job_id=? AND attempt=? AND experiment_id=? AND evidence_id=?",
                                       (payload["job_id"], payload["attempt"], payload["experiment_id"], identity))
                    report, status = {"evidence_ids": payload["evidence_ids"], "payload": payload}, "running"
                else:
                    report, status = {"error": "Unsupported transfer command; upgrade the Agent"}, "unsupported"
            except NotImplementedError as exc:
                report, status = {"error": self._error(exc)}, "unsupported"
            except (OSError, ValueError, TypeError, KeyError) as exc:
                report, status = {"error": self._error(exc)}, "failed"
            with db:
                db.execute("INSERT INTO node_delivery_commands VALUES (?,?,?,1) ON CONFLICT(request_id) DO UPDATE SET status=excluded.status,report=excluded.report,dirty=1", (command["request_id"], status, json.dumps(report)))

    def _report(self):
        db = self.control_db
        evidence_rows = db.execute("SELECT * FROM node_evidence WHERE dirty=1 LIMIT 100").fetchall()
        fields = ("job_id", "attempt", "experiment_id", "evidence_id", "name", "kind", "size", "sha256", "retention", "availability", "transfer_status", "confirmed_bytes", "error")
        evidence = [{key: row[key] for key in fields} for row in evidence_rows]
        progress = [dict(row) for row in db.execute("SELECT * FROM node_progress WHERE dirty=1 LIMIT 100")]
        commands = [dict(row) for row in db.execute("SELECT * FROM node_delivery_commands WHERE dirty=1 LIMIT 100")]
        if not (evidence or progress or commands):
            return
        response = self._request("/api/delivery/report", {"evidence": evidence,
                    "progress": [json.loads(row["value"]) for row in progress],
                    "commands": [{"request_id": row["request_id"], "status": row["status"], "report": json.loads(row["report"])} for row in commands]})
        if response.get("ack") is not True:
            raise ValueError("Delivery report was not acknowledged")
        with db:
            for row in evidence_rows:
                db.execute("UPDATE node_evidence SET dirty=0 WHERE job_id=? AND attempt=? AND experiment_id=? AND evidence_id=?", tuple(row[key] for key in fields[:4]))
            for row in progress:
                db.execute("UPDATE node_progress SET dirty=0 WHERE job_id=? AND attempt=? AND experiment_id=?", (row["job_id"], row["attempt"], row["experiment_id"]))
            for row in commands:
                db.execute("UPDATE node_delivery_commands SET dirty=0 WHERE request_id=?", (row["request_id"],))

    def _upload_one(self):
        db = self.control_db
        row = db.execute("SELECT * FROM node_evidence WHERE transfer_status IN ('queued','uploading','retrying') AND retention='retained' AND next_retry<=? ORDER BY job_id,attempt,experiment_id LIMIT 1", (common.now(),)).fetchone()
        if row is None:
            return
        key = (row["job_id"], row["attempt"], row["experiment_id"], row["evidence_id"])
        base = {field: row[field] for field in ("job_id", "attempt", "experiment_id", "evidence_id", "sha256", "size")}
        self._meta(db, "current_evidence", {**base, "confirmed_bytes": row["confirmed_bytes"], "total_bytes": row["size"]})
        try:
            path = common.safe_child(self.root, Path(row["path"]).relative_to(self.root).as_posix())
            if not path.is_file():
                raise FileNotFoundError("Retained evidence is missing; no experiment will be restarted")
            # Verify once when starting/restarting a request, before transferring any bytes.
            if row["transfer_status"] != "uploading" and (path.stat().st_size != row["size"] or common.sha256_file(path) != row["sha256"]):
                raise ValueError("Retained evidence changed; refusing transfer")
            response = self._request("/api/evidence/upload", {**base, "offset": 0, "data": ""})
            offset = response["offset"]
            if type(offset) is not int or not 0 <= offset <= row["size"]:
                raise ValueError("Invalid acknowledged evidence offset")
            if not response.get("complete"):
                with path.open("rb") as stream:
                    stream.seek(offset)
                    block = stream.read(512 * 1024)
                response = self._request("/api/evidence/upload", {**base, "offset": offset, "data": base64.b64encode(block).decode()})
                if type(response.get("offset")) is not int or not offset <= response["offset"] <= row["size"] or not response.get("complete") and response["offset"] == offset:
                    raise ValueError("Evidence upload made no acknowledged progress")
            if response.get("complete") and response["offset"] != row["size"]:
                raise ValueError("Incomplete evidence must not be marked complete")
            with db:
                db.execute("UPDATE node_evidence SET transfer_status=?,confirmed_bytes=?,dirty=1 WHERE job_id=? AND attempt=? AND experiment_id=? AND evidence_id=?",
                           ("delivered" if response.get("complete") else "uploading", response["offset"], *key))
            self._meta(db, "last_byte_progress_at", common.now())
            if response.get("complete"):
                self._meta(db, "last_file_completed_at", common.now())
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            permanent = isinstance(exc, (FileNotFoundError, ValueError)) or getattr(exc, "code", None) in (400, 403, 409, 410)
            with db:
                db.execute("UPDATE node_evidence SET transfer_status=?,error=?,retries=retries+1,next_retry=?,dirty=1,availability=?,error_at=? WHERE job_id=? AND attempt=? AND experiment_id=? AND evidence_id=?",
                           ("unavailable" if permanent else "retrying", self._error(exc), common.now() + min(30, 2**min(row["retries"] + 1, 5)),
                            "missing" if isinstance(exc, FileNotFoundError) else "changed" if isinstance(exc, ValueError) else row["availability"], common.now(), *key))
        finally:
            self._meta(db, "current_evidence", None)

    def _complete_requests(self):
        db = self.control_db
        for row in db.execute("SELECT * FROM node_delivery_commands WHERE status='running'").fetchall():
            report = json.loads(row["report"])
            payload = report.get("payload")
            if not payload:
                continue
            items = [db.execute("SELECT transfer_status,error FROM node_evidence WHERE job_id=? AND attempt=? AND experiment_id=? AND evidence_id=?",
                (payload["job_id"], payload["attempt"], payload["experiment_id"], identity)).fetchone() for identity in payload["evidence_ids"]]
            if all(item and item[0] == "delivered" for item in items):
                status = "completed"
            elif any(item is None or item[0] == "unavailable" for item in items):
                status = "failed"
            else:
                continue
            report["files"] = [dict(item) if item else {"error": "Evidence disappeared"} for item in items]
            with db:
                db.execute("UPDATE node_delivery_commands SET status=?,report=?,dirty=1 WHERE request_id=?", (status, json.dumps(report), row["request_id"]))

    def control_once(self):
        db = self.control_db
        self._catalog()
        self._retain_one()
        self._report()
        response = self._request("/api/delivery/poll", {"status": telemetry(db, getattr(self.agent, "active_legacy_upload", None))})
        self._meta(db, "hub_version", response.get("hub_version"))
        self._meta(db, "hub_capabilities", response.get("capabilities", []))
        if protocol.CAPABILITY in response.get("capabilities", []):
            with db:
                db.execute("UPDATE node_results SET status='queued',next_retry=0 WHERE status='unsupported'")
        self._commands(response.get("commands", []))
        self._report()  # Publish hash/size/requests before starting a chunk.
        self._upload_one()
        self._complete_requests()
        self._report()
        self._meta(db, "evidence_worker_at", common.now())
        self._meta(db, "control_error", None)

    def _result_loop(self):
        while not self.stop.is_set():
            try:
                self.results_once()
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                with self.result_lock:
                    self._meta(self.result_db, "scan_error", self._error(exc))
            self.stop.wait(0.25)

    def _control_loop(self):
        while not self.stop.is_set():
            try:
                self.control_once()
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
                self._meta(self.control_db, "control_error", self._error(exc))
                self._meta(self.control_db, "control_error_at", common.now())
            self.stop.wait(0.5)
