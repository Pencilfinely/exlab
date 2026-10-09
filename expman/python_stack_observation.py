"""Bounded nonblocking Python stack sampling for an unchanged, CPU-busy job.

The sampler has access only to the exact owned container's private PID namespace.
It cannot issue a remote command, mount experiments, or signal the target.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

CAPABILITY = "python-stack-observation-v1"
BINARY_SHA256 = "9b4d1f39b2a47ae44f4c6a46f615dcc0287d7755beba5065f32391951e07d594"
INTERVAL = 600
QUIET_SECONDS = 300
MARKER = "\nPYTHON_STACK_OBSERVATION "


def eligible(value, timestamp):
    if value.get("container", {}).get("status") != "running":
        return False
    log = value.get("log", {})
    if "error" in log or timestamp - log.get("changed_at", timestamp) < QUIET_SECONDS:
        return False
    diag = value.get("diagnostics", {})
    if diag.get("errors"):
        return False
    try:
        stats = json.loads(diag["stats"])
        cpu = float(stats["CPUPerc"].rstrip("%"))
        gpu_rows = [s.split(",") for s in diag["gpu"].splitlines() if s.strip()]
        return cpu >= 50 and len(gpu_rows) == 1 and float(gpu_rows[0][1]) == 0
    except (ValueError, KeyError, TypeError, IndexError):
        return False


def _files(agent):
    source = Path(__file__).parent / "vendor" / "py-spy-0.4.2-linux-x64.bin"
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != BINARY_SHA256:
        raise ValueError("Sampler hash mismatch")
    cache = agent.root / "runtime-diagnostics"
    cache.mkdir(mode=0o700, exist_ok=True)
    binary = cache / ("py-spy-" + BINARY_SHA256)
    if binary.is_symlink():
        raise ValueError("Sampler cannot be a symlink")
    if not binary.exists():
        temporary = cache / ("probe-" + uuid.uuid4().hex)
        with temporary.open("xb") as stream:
            stream.write(raw)
        temporary.chmod(0o555)
        os.replace(temporary, binary)
    if hashlib.sha256(binary.read_bytes()).hexdigest() != BINARY_SHA256:
        raise ValueError("Cached sampler hash mismatch")
    binary.chmod(0o555)
    helper_raw = (Path(__file__).parent / "stack_probe_helper.py").read_bytes()
    helper_digest = hashlib.sha256(helper_raw).hexdigest()
    helper = cache / ("helper-" + helper_digest + ".py")
    if helper.is_symlink():
        raise ValueError("Helper cannot be a symlink")
    if not helper.exists():
        with helper.open("xb") as stream:
            stream.write(helper_raw)
    if hashlib.sha256(helper.read_bytes()).hexdigest() != helper_digest:
        raise ValueError("Cached helper hash mismatch")
    helper.chmod(0o444)
    return binary.resolve(), helper.resolve()


def probe(agent, record, identity):
    diagnostic = uuid.uuid4().hex
    name = "expman-stack-" + diagnostic
    created = None
    labels = {"expman.diagnostic": diagnostic, "expman.job": record["id"],
              "expman.node": agent.config["node_id"]}
    try:
        container = agent._inspect(record)
        if (not container or container.get("Id") != identity
                or not re.fullmatch(r"[a-f0-9]{64}", identity)
                or container.get("HostConfig", {}).get("PidMode") not in ("", None)
                or container.get("State", {}).get("Status") != "running"
                or container.get("State", {}).get("Paused")
                or container.get("State", {}).get("Restarting")):
            raise ValueError("Target identity or namespace changed")
        image = container.get("Image", "")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("Pinned local image required")
        binary, helper = _files(agent)
        if any("," in str(p) for p in (binary, helper)):
            raise ValueError("Unsupported mount path")
        argv = ["docker", "create", "--name", name, "--pull=never", "--restart=no",
                "--network=none", "--read-only", "--cap-drop=ALL", "--cap-add=SYS_PTRACE",
                "--security-opt=no-new-privileges", "--pid=container:" + identity,
                "--user=0:0", "--cpus=0.25", "--memory=256m", "--pids-limit=16",
                "--tmpfs=/tmp:rw,nosuid,noexec,size=2m", "--workdir=/",
                "--mount", "type=bind,src=" + str(binary) + ",dst=/probe/py-spy,readonly",
                "--mount", "type=bind,src=" + str(helper) + ",dst=/probe/helper.py,readonly"]
        for key, value in labels.items():
            argv += ["--label", key + "=" + value]
        argv += ["--entrypoint=python", image, "-I", "-B", "/probe/helper.py"]
        created = agent._exec(argv, timeout=5).stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{64}", created) or created == identity:
            raise ValueError("Invalid diagnostic identity")
        reply = agent._exec(["docker", "start", "--attach", created], timeout=12)
        if len(reply.stdout.encode()) > 3500:
            raise ValueError("Oversized diagnostic result")
        result = json.loads(reply.stdout)
        if result.get("mode") != "python-stack-nonblocking-v1":
            raise ValueError("Unsupported sampler output")
        return result
    except subprocess.TimeoutExpired:
        return {"mode": "python-stack-nonblocking-v1", "error": "timeout"}
    except (OSError, ValueError, RuntimeError):
        return {"mode": "python-stack-nonblocking-v1", "error": "sampling_unavailable"}
    finally:
        # A timeout can leave only OUR auxiliary container, never stop the job.
        try:
            checked = agent._exec(["docker", "inspect", created or name], timeout=3, check=False)
            if checked.returncode == 0:
                found = json.loads(checked.stdout)[0]
                actual = found.get("Id", "")
                have = found.get("Config", {}).get("Labels", {}) or {}
                if (re.fullmatch(r"[a-f0-9]{64}", actual) and actual != identity
                        and all(have.get(k) == v for k, v in labels.items())):
                    agent._exec(["docker", "rm", "--force", actual], timeout=3)
        except (OSError, ValueError, RuntimeError, IndexError, subprocess.TimeoutExpired):
            pass


def observe(agent, record, value, timestamp):
    if not eligible(value, timestamp):
        return
    previous = record.get("_python_stack_observation") or {}
    identity = value["container"]["id"]
    same = previous.get("attempt") == record["attempt"] and previous.get("container") == identity
    if not same or timestamp - previous.get("checked_at", 0) >= INTERVAL:
        result = probe(agent, record, identity)
        previous = {"attempt": record["attempt"], "container": identity,
                    "checked_at": timestamp, "result": result}
        record["_python_stack_observation"] = previous
    diag = value["diagnostics"]
    top = diag.get("processes", "").split(MARKER, 1)[0].encode()[:4000].decode(errors="ignore")
    # Fits the already-deployed v1 protocol (processes <= 8000 bytes).
    small = {"checked_at": previous["checked_at"], "result": previous["result"]}
    diag["processes"] = top + MARKER + json.dumps(small, ensure_ascii=True, separators=(",", ":"))
