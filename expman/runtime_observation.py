"""Bounded, read-only runtime evidence; container liveness is not training progress."""
import copy
import hashlib
import json
import math
import re
import subprocess

CAPABILITY = "runtime-observation-v1"
MAX_BYTES = 16 * 1024
QUIET_SECONDS = 120
DIAGNOSTIC_INTERVAL = 60
STATUS = {"running", "paused", "restarting", "created", "exited", "dead", "missing", "unknown"}


def error_code(error):
    # Do not send subprocess arguments, environment, stderr or credentials.
    if isinstance(error, subprocess.TimeoutExpired):
        return "timeout"
    if isinstance(error, OSError):
        return "unavailable"
    return "read_failed"


def validate(value, attempt):
    if not isinstance(value, dict) or len(json.dumps(value, allow_nan=False).encode()) > MAX_BYTES:
        raise ValueError("runtime must be an object of at most 16 KiB")
    allowed = {"version", "attempt", "observed_at", "container", "log", "diagnostics"}
    if set(value) - allowed or type(value.get("version")) is not int or value.get("version") != 1 or type(value.get("attempt")) is not int or value.get("attempt") != attempt:
        raise ValueError("runtime version or attempt mismatch")
    def stamp(number):
        if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or number < 0:
            raise ValueError("Invalid runtime timestamp")
    stamp(value.get("observed_at"))
    container = value.get("container", {})
    if not isinstance(container, dict) or set(container) - {"id", "status", "paused", "restarting", "oom_killed", "pid", "restart_count", "error"}:
        raise ValueError("Invalid runtime container fields")
    if container.get("status") not in STATUS:
        raise ValueError("Invalid runtime container status")
    if "id" in container and not re.fullmatch(r"[a-f0-9]{64}", str(container["id"])):
        raise ValueError("Invalid container identity")
    for key in ("paused", "restarting", "oom_killed"):
        if key in container and type(container[key]) is not bool:
            raise ValueError("Invalid runtime container flag")
    for key in ("pid", "restart_count"):
        if key in container and (type(container[key]) is not int or container[key] < 0):
            raise ValueError("Invalid runtime container count")
    log = value.get("log", {})
    if not isinstance(log, dict) or set(log) - {"checked_at", "last_success_at", "changed_at", "sha256", "error"}:
        raise ValueError("Invalid runtime log")
    for key in ("checked_at", "last_success_at", "changed_at"):
        if key in log:
            stamp(log[key])
    if "sha256" in log and not re.fullmatch(r"[a-f0-9]{64}", str(log["sha256"])):
        raise ValueError("Invalid log digest")
    for item in (container, log):
        if "error" in item and item["error"] not in ("timeout", "unavailable", "read_failed"):
            raise ValueError("Invalid runtime error")
    diag = value.get("diagnostics")
    if diag is not None:
        if not isinstance(diag, dict) or set(diag) - {"checked_at", "processes", "stats", "gpu", "errors"}:
            raise ValueError("Invalid runtime diagnostics")
        stamp(diag.get("checked_at"))
        for key, limit in (("processes", 8000), ("stats", 2000), ("gpu", 2000)):
            if key in diag and (not isinstance(diag[key], str) or len(diag[key].encode()) > limit):
                raise ValueError("Runtime diagnostic text too large")
        errors = diag.get("errors", {})
        if not isinstance(errors, dict) or set(errors) - {"top", "stats", "gpu"} or any(v not in ("timeout", "unavailable", "read_failed") for v in errors.values()):
            raise ValueError("Invalid runtime diagnostic errors")
    return copy.deepcopy(value)


def bounded(text, size):
    return text.encode("utf-8")[:size].decode("utf-8", errors="ignore")


def observe(agent, record, timestamp):
    """Only inspect/logs/top/stats on the exact owned container ID; never exec/signal."""
    old = record.get("runtime") or {}
    if old.get("attempt") != record["attempt"]:
        old = {}
    value = {"version": 1, "attempt": record["attempt"], "observed_at": timestamp,
             "container": {"status": "unknown"}, "log": copy.deepcopy(old.get("log", {}))}
    if "diagnostics" in old:
        value["diagnostics"] = copy.deepcopy(old["diagnostics"])
    tail = None
    try:
        container = agent._inspect(record)  # checks both node and job labels
        if container is None:
            value["container"]["status"] = "missing"
            return value, tail
        identity = container.get("Id", "")
        if not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise ValueError("Missing immutable Docker identity")
        state = container.get("State", {})
        status = "paused" if state.get("Paused") else "restarting" if state.get("Restarting") else state.get("Status", "unknown")
        value["container"] = {
            "id": identity, "status": status if status in STATUS else "unknown",
            "paused": bool(state.get("Paused")), "restarting": bool(state.get("Restarting")),
            "oom_killed": bool(state.get("OOMKilled")), "pid": int(state.get("Pid", 0)),
            "restart_count": int(container.get("RestartCount", 0))}
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        value["container"]["error"] = error_code(error)
        return value, tail
    value["log"]["checked_at"] = timestamp
    try:
        result = agent._exec(["docker", "logs", "--tail", "40", identity], timeout=5)
        tail = bounded((result.stdout + result.stderr)[-16000:], 16000)
        digest = hashlib.sha256(tail.encode("utf-8")).hexdigest()
        if digest != value["log"].get("sha256"):
            value["log"]["changed_at"] = timestamp
        value["log"].update(last_success_at=timestamp, sha256=digest)
        value["log"].pop("error", None)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        value["log"]["error"] = error_code(error)
    quiet = timestamp - value["log"].get("changed_at", timestamp) >= QUIET_SECONDS
    suspect = quiet or "error" in value["log"] or value["container"]["status"] in {"paused", "restarting"}
    due = timestamp - value.get("diagnostics", {}).get("checked_at", 0) >= DIAGNOSTIC_INTERVAL
    if suspect and due:
        diag = {"checked_at": timestamp, "errors": {}}
        commands = [
            ("top", "processes", ["docker", "top", identity, "-eo", "pid,ppid,stat,etime,time,wchan:32,comm"], 8000),
            ("stats", "stats", ["docker", "stats", "--no-stream", "--format", "{{json .}}", identity], 2000)]
        gpu = record.get("gpu_uuid")
        if isinstance(gpu, str) and re.fullmatch(r"GPU-[a-fA-F0-9-]{36}", gpu):
            commands.append(("gpu", "gpu", [
                "nvidia-smi", "--id=" + gpu,
                "--query-gpu=uuid,utilization.gpu,memory.used,memory.total,pstate",
                "--format=csv,noheader,nounits"], 2000))
        for name, key, command, limit in commands:
            try:
                result = agent._exec(command, timeout=3)
                # top contains names, no command arguments or environments.
                diag[key] = bounded(result.stdout, limit)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                diag["errors"][name] = error_code(error)
        value["diagnostics"] = diag
        from .python_stack_observation import observe as observe_stack
        observe_stack(agent, record, value, timestamp)
    return value, tail
