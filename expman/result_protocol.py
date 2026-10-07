"""Model-independent, immutable experiment results and safe evidence identities."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

from . import __version__

CAPABILITY = "experiment-results-v1"
EVIDENCE_CAPABILITY = "evidence-on-demand-v1"
MIGRATION_CAPABILITY = "upload-migration-v1"
SOFT_LIMIT = 64 * 1024
MAX_RESULT_BYTES = 1024 * 1024
TERMINAL = {"succeeded", "failed", "paused", "interrupted", "canceled"}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value):
        raise ValueError("Experiment/evidence ID must use letters, digits, _, . or -")
    return value


def relative(value):
    if (not isinstance(value, str) or len(value) > 1024 or any(ord(c) < 32 for c in value)
            or any(p in ("", ".", "..") or ":" in p for p in value.replace("\\", "/").split("/"))):
        raise ValueError("Result and evidence paths must be explicit files inside the run output")
    if any(c in value for c in "*?[]"):
        raise ValueError("Result files cannot use recursive/glob artifact rules")
    return value.replace("\\", "/")


def delivery_config(value, experiment_id="default"):
    if value is None:
        value = {"mode": "lightweight"}
    if not isinstance(value, dict) or set(value) - {"mode", "files"}:
        raise ValueError("result_delivery accepts mode and explicit files")
    value = copy.deepcopy(value)
    if value.setdefault("mode", "lightweight") not in ("lightweight", "legacy"):
        raise ValueError("result_delivery.mode must be lightweight or legacy")
    files = value.setdefault("files", [{"experiment_id": identifier(experiment_id), "path": "result.json"}])
    if not isinstance(files, list) or not 1 <= len(files) <= 1000:
        raise ValueError("result_delivery.files must list 1..1000 experiment results")
    ids, paths = set(), set()
    for item in files:
        if not isinstance(item, dict) or set(item) - {"experiment_id", "path", "stream"}:
            raise ValueError("Each result file requires experiment_id, path and optional stream")
        identity, path = identifier(item.get("experiment_id")), relative(item.get("path"))
        if identity in ids or path in paths or not path.endswith(".json"):
            raise ValueError("Result IDs and JSON paths must be unique")
        if type(item.setdefault("stream", path != "result.json")) is not bool:
            raise ValueError("result file stream must be boolean (publish atomically when true)")
        ids.add(identity)
        paths.add(path)
    return value


def evidence_id(job_id, attempt, experiment_id, path):
    return hashlib.sha256(encoded([job_id, attempt, experiment_id, relative(path)])).hexdigest()[:32]


def evidence_entries(producer, record, experiment_id):
    result = []
    values = producer.get("evidence", [])
    if not isinstance(values, list) or len(values) > 1000:
        raise ValueError("evidence must be a list of at most 1000 file references")
    seen = set()
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("Evidence reference must be an object")
        path = relative(item.get("path", item.get("name")))
        identity = evidence_id(record["id"], record["attempt"], experiment_id, path)
        if identity in seen:
            raise ValueError("Duplicate evidence reference")
        seen.add(identity)
        digest, size = item.get("sha256"), item.get("size")
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("Evidence SHA-256 must be lowercase hex, or null when unknown")
        if size is not None and (type(size) is not int or size < 0):
            raise ValueError("Evidence size must be nonnegative, or null when unknown")
        result.append({"evidence_id": identity, "name": path, "kind": str(item.get("kind", "file"))[:100],
                       "size": size, "sha256": digest, "retention": "pending",
                       "availability": "pending", "transfer_status": "retained"})
    return result


def envelope(record, producer, experiment_id, node_id, completed_at, producer_sha256=None):
    if not isinstance(producer, dict):
        raise ValueError("An experiment result must be a JSON object")
    spec = record["spec"]
    final = record["state"] in TERMINAL
    execution = copy.deepcopy(producer.get("execution", {}))
    if not isinstance(execution, dict):
        raise ValueError("execution must be an object")
    execution.setdefault("status", producer.get("status", record["state"] if final else "unknown"))
    execution.setdefault("exit_code", producer.get("exit_code", record.get("exit_code") if final else None))
    execution.setdefault("failure_stage", producer.get("failure_stage"))
    execution.setdefault("error", producer.get("error", producer.get("harness_error")) or
                         (record.get("detail") if final and record["state"] == "failed" else None))
    execution.setdefault("stages", {"training": {"status": "unknown", "exit_code": None},
                                     "test": {"status": "unknown", "exit_code": None}})
    selection = copy.deepcopy(producer.get("selection", {}))
    if not isinstance(selection, dict):
        raise ValueError("selection must describe one complete checkpoint")
    for key, default in (("checkpoint", None), ("sha256", None), ("epoch", None), ("rule", "unspecified")):
        selection.setdefault(key, default)
    metrics = copy.deepcopy(producer.get("metrics", {}))
    if not isinstance(metrics, dict):
        raise ValueError("metrics must be an object")
    metrics.setdefault("validation", producer.get("validation_metrics", {}))
    metrics.setdefault("test", producer.get("test_metrics", {}))
    metrics.setdefault("protocol", producer.get("metric_protocol", spec.get("metric_protocol", "unspecified")))
    if not metrics["validation"] and not metrics["test"] and record.get("metrics"):
        metrics.setdefault("reported", record["metrics"])
    verification = producer.get("verification", {})
    if not isinstance(verification, dict):
        raise ValueError("verification must be an object")
    independent = verification.get("independent", "pending")
    if isinstance(independent, dict):
        independent = independent.get("status", "pending")
    # Only an authenticated controller-side review can establish independent acceptance.
    independent = independent if independent in ("quarantined", "failed") else "pending"
    known = {"execution", "status", "exit_code", "failure_stage", "error", "metrics", "validation_metrics",
             "test_metrics", "metric_protocol", "selection", "provenance", "timing", "verification", "evidence"}
    platform_provenance = {"agent_version": __version__, "source": spec.get("source"),
        "job_config_sha256": hashlib.sha256(encoded(spec["params"])).hexdigest(), "inputs": spec.get("assets", []),
        "environment": record.get("environment"), "container_image_id": next((item.get("image_id")
            for item in record.get("cached_environments", []) if item.get("image") == (record.get("environment") or {}).get("image")), None)}
    return {"schema_version": 1, "identity": {"project_id": spec.get("project_id", spec["algorithm"]),
            "job_id": record["id"], "attempt": record["attempt"], "experiment_id": identifier(experiment_id),
            "node_id": node_id, "gpu_uuid": record.get("gpu_uuid"), "completed_at": completed_at},
            "execution": execution, "metrics": metrics, "selection": selection,
            "provenance": {"source": spec.get("source"), "config_sha256": hashlib.sha256(encoded(spec["params"])).hexdigest(),
                "inputs": spec.get("assets", []), "seed": producer.get("seed", spec["params"].get("seed")),
                "environment": record.get("environment"), "producer_sha256": producer_sha256,
                **producer.get("provenance", {}), "platform": platform_provenance},
            "timing": producer.get("timing", {"stages": {}, "job_timing": record.get("_timing"),
                "note": "Job timing includes this experiment; do not add nested durations."}),
            "verification": {"remote": verification.get("remote", "unknown"), "independent": independent,
                             "producer_independent_claim": verification.get("independent")},
            "evidence": evidence_entries(producer, record, experiment_id),
            "summary": {key: value for key, value in producer.items() if key not in known}}


def validate_envelope(value):
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("Result requires schema_version: 1")
    identity = value.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("Result identity is required")
    identifier(identity.get("experiment_id"))
    if not isinstance(identity.get("project_id"), str) or not 1 <= len(identity["project_id"]) <= 200:
        raise ValueError("Result project_id is required")
    if type(identity.get("attempt")) is not int or identity["attempt"] < 1:
        raise ValueError("Result attempt must be positive")
    timestamp = identity.get("completed_at")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("Result completion time must be a finite timestamp")
    for key in ("execution", "metrics", "selection", "provenance", "timing", "verification"):
        if not isinstance(value.get(key), dict):
            raise ValueError("Result requires an object: " + key)
    for key in ("validation", "test"):
        if not isinstance(value["metrics"].get(key), dict):
            raise ValueError("Validation and test metrics must be separate objects")
    if not isinstance(value["execution"].get("status"), str) or not value["execution"]["status"]:
        raise ValueError("Result execution status is required")
    selection = value["selection"]
    if any(key in selection for key in ("per_metric", "by_metric", "checkpoints")) or isinstance(selection.get("checkpoint"), list):
        raise ValueError("Select one complete checkpoint; do not splice different models per metric")
    if selection.get("checkpoint") is not None:
        if not isinstance(selection["checkpoint"], (str, dict)) or not selection["checkpoint"]:
            raise ValueError("Invalid selected checkpoint identity")
        if not re.fullmatch(r"[0-9a-f]{64}", str(selection.get("sha256", ""))):
            raise ValueError("Selected checkpoint requires its complete SHA-256")
        if not isinstance(selection.get("rule"), str) or not selection["rule"]:
            raise ValueError("Selected checkpoint requires the selection rule")
    if not isinstance(value.get("evidence"), list) or len(value["evidence"]) > 1000:
        raise ValueError("Invalid evidence index")
    seen = set()
    for item in value["evidence"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid evidence reference")
        identifier(item.get("evidence_id"))
        relative(item.get("name"))
        if item["evidence_id"] in seen:
            raise ValueError("Duplicate evidence ID")
        seen.add(item["evidence_id"])
        if item.get("sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}", str(item["sha256"])):
            raise ValueError("Invalid evidence hash")
        if item.get("size") is not None and (type(item["size"]) is not int or item["size"] < 0):
            raise ValueError("Invalid evidence size")
    # Serializing also rejects NaN/Infinity at every depth, without rounding any metric.
    raw = encoded(value)
    return ["Result exceeds 64 KiB; move large arrays to on-demand evidence."] if len(raw) > SOFT_LIMIT else []
