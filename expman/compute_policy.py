"""Availability is independent of connectivity and of resource authorization."""
from __future__ import annotations

import copy
import ctypes
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import statistics

from .common import now

CAPABILITY = "dynamic-compute-v1"


def number(value, name, maximum=10**12, minimum=0):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise ValueError("Invalid " + name)
    return value


def validate(value):
    allowed = {"revision", "kind", "mode", "until", "reason", "require_ac", "reliability",
               "maintenance", "risk_windows"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Invalid compute policy fields")
    value = copy.deepcopy(value)
    revision = number(value.setdefault("revision", 0), "revision", 2**53 - 2)
    if type(revision) is not int:
        raise ValueError("revision must be an integer")
    if value.setdefault("kind", "shared") not in ("stable", "shared", "portable"):
        raise ValueError("Unknown node kind")
    if value.setdefault("mode", "available") not in ("available", "drain", "release"):
        raise ValueError("mode must be available, drain or release")
    if value.setdefault("until", None) is not None:
        number(value["until"], "until")
    reason = value.setdefault("reason", "")
    if not isinstance(reason, str) or len(reason) > 300:
        raise ValueError("reason must contain at most 300 characters")
    if type(value.setdefault("require_ac", False)) is not bool:
        raise ValueError("require_ac must be boolean")
    number(value.setdefault("reliability", 1), "reliability", 1)
    entries = value.setdefault("maintenance", [])
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError("maintenance must contain at most 100 windows")
    for item in entries:
        if not isinstance(item, dict) or set(item) - {"start", "end", "reason"}:
            raise ValueError("Invalid maintenance window")
        number(item.get("start"), "maintenance.start")
        number(item.get("end"), "maintenance.end")
        if item["start"] >= item["end"]:
            raise ValueError("Maintenance end must follow start")
        if not isinstance(item.setdefault("reason", "维护"), str) or len(item["reason"]) > 300:
            raise ValueError("Invalid maintenance reason")
    entries = value.setdefault("risk_windows", [])
    if not isinstance(entries, list) or len(entries) > 32:
        raise ValueError("risk_windows must contain at most 32 windows")
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"window", "risk"}:
            raise ValueError("Risk windows require window and risk")
        if not isinstance(item["window"], str) or not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", item["window"]):
            raise ValueError("Risk windows use HH:MM-HH:MM")
        number(item["risk"], "risk", 1)
    return value


def evaluate(policy, snapshot=None, timestamp=None):
    snapshot, timestamp = snapshot or {}, now() if timestamp is None else timestamp
    mode = policy.get("mode", "available")
    expired = policy.get("until") is not None and timestamp >= policy["until"]
    until = None if expired else policy.get("until")
    if expired:
        mode = "available"
    manual_block = mode != "available"
    reason = (policy.get("reason") if not expired else "") or {"release": "临时释放算力", "drain": "等待当前实验结束"}.get(mode, "可提供算力")
    stop = mode == "release"
    available_seconds = None
    for window in policy.get("maintenance", []):
        if window["start"] <= timestamp < window["end"]:
            mode, stop, reason = "release", True, window.get("reason", "维护")
            until = (max(until, window["end"]) if until is not None else None) if manual_block else max(until or 0, window["end"])
        elif window["start"] > timestamp:
            seconds = window["start"] - timestamp
            available_seconds = min(available_seconds, seconds) if available_seconds is not None else seconds
    if policy.get("require_ac") and snapshot.get("ac_power") is not True:
        mode, stop, reason = "release", True, "未接电源" if snapshot.get("ac_power") is False else "供电状态未知，等待确认"
        until = None
    risk = 1 - policy.get("reliability", 1)
    clock = snapshot.get("local_time", "")
    if re.fullmatch(r"\d{2}:\d{2}", str(clock)):
        point = int(clock[:2]) * 60 + int(clock[3:])
        for item in policy.get("risk_windows", []):
            start, end = item["window"].split("-")
            lo, hi = int(start[:2]) * 60 + int(start[3:]), int(end[:2]) * 60 + int(end[3:])
            if (lo < hi and lo <= point < hi) or (lo > hi and (point >= lo or point < hi)) or lo == hi:
                risk = max(risk, item["risk"])
    return {"available": mode == "available", "stopping": stop, "mode": mode,
            "reason": reason, "until": until, "risk": risk,
            "available_seconds": available_seconds, "revision": policy.get("revision", 0)}


def apply(snapshot, policy):
    snapshot["compute_policy"] = policy
    availability = evaluate(policy, snapshot)
    local = snapshot.get("local_compute_policy")
    if local is not None:
        local_availability = evaluate(validate(local), snapshot)
        current = availability
        if (not local_availability["available"] and
                (not availability["stopping"] or local_availability["stopping"])):
            availability = local_availability
        availability["risk"] = max(current["risk"], local_availability["risk"])
        horizons = [item for item in (current["available_seconds"], local_availability["available_seconds"]) if item is not None]
        availability["available_seconds"] = min(horizons) if horizons else None
    snapshot["availability"] = availability
    if not snapshot["availability"]["available"]:
        snapshot.setdefault("policy", {})["run_enabled"] = False
    return snapshot


def validate_snapshot(snapshot):
    if snapshot.get("ac_power") is not None and type(snapshot["ac_power"]) is not bool:
        raise ValueError("ac_power must be boolean or unknown")
    if "local_compute_policy" in snapshot:
        validate(snapshot["local_compute_policy"])
    if "compute_policy_revision" in snapshot:
        revision = number(snapshot["compute_policy_revision"], "compute_policy_revision", 2**53 - 2)
        if type(revision) is not int:
            raise ValueError("compute_policy_revision must be an integer")
    availability = snapshot.get("availability")
    if availability is not None:
        if not isinstance(availability, dict):
            raise ValueError("availability must be an object")
        for key in ("available", "stopping"):
            if key in availability and type(availability[key]) is not bool:
                raise ValueError("availability." + key + " must be boolean")
        if "risk" in availability:
            number(availability["risk"], "availability.risk", 1)
        if availability.get("available_seconds") is not None:
            number(availability["available_seconds"], "available_seconds")
        if "reason" in availability and (not isinstance(availability["reason"], str) or len(availability["reason"]) > 300):
            raise ValueError("Invalid availability reason")


def ac_power():
    """Query the host, including Windows when the agent is inside WSL."""
    try:
        if os.name == "nt":
            class Power(ctypes.Structure):
                _fields_ = [("ac", ctypes.c_ubyte), ("battery", ctypes.c_ubyte),
                            ("percent", ctypes.c_ubyte), ("reserved", ctypes.c_ubyte),
                            ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]
            power = Power()
            return bool(power.ac) if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power)) and power.ac in (0, 1) else None
        if sys.platform == "linux":
            for source in Path("/sys/class/power_supply").glob("*"):
                if (source / "type").read_text().strip() in ("Mains", "USB", "USB_C"):
                    return (source / "online").read_text().strip() == "1"
            if "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower():
                command = ("Add-Type -AssemblyName System.Windows.Forms; "
                           "[System.Windows.Forms.SystemInformation]::PowerStatus.PowerLineStatus.ToString()")
                result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                        capture_output=True, text=True, timeout=3, shell=False)
                return {"Online": True, "Offline": False}.get(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS node_compute_policies (node_id TEXT PRIMARY KEY REFERENCES nodes(id), value TEXT NOT NULL)")


class ComputePolicyHubMixin:
    def _compute_policy(self, node_id):
        row = self.db.execute("SELECT value FROM node_compute_policies WHERE node_id=?", (node_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_compute_policy(self, payload):
        from .hub import APIError
        if not isinstance(payload, dict) or set(payload) != {"node_id", "policy"}:
            raise ValueError("node_id and policy are required")
        desired = validate(payload["policy"])
        with self.transaction():
            row = self.db.execute("SELECT snapshot FROM nodes WHERE id=?", (payload["node_id"],)).fetchone()
            if row is None:
                raise APIError(404, "Unknown node")
            if CAPABILITY not in json.loads(row[0]).get("capabilities", []):
                raise APIError(409, "请先升级 Worker 并等待连接，再设置可用策略")
            previous = self._compute_policy(payload["node_id"])
            revision = previous["revision"] if previous else 0
            if previous and all(desired[k] == previous[k] for k in desired if k != "revision") and desired["revision"] in (revision, revision - 1):
                return {"policy": previous}
            if desired["revision"] != revision:
                raise APIError(409, "可用策略已变化，请重新载入")
            desired["revision"] += 1
            self.db.execute("INSERT OR REPLACE INTO node_compute_policies VALUES (?,?)", (payload["node_id"], json.dumps(desired, allow_nan=False)))
            self._assign()
            return {"policy": desired}

    def _scheduling_nodes(self):
        from .scheduler import workload_key, duration_estimate
        nodes = []
        for row in self.db.execute("SELECT * FROM nodes"):
            node = dict(row)
            node["snapshot"] = json.loads(node["snapshot"])
            policy = self._compute_policy(node["id"])
            if policy:
                apply(node["snapshot"], policy)
            nodes.append(node)
        by_id = {node["id"]: node for node in nodes}
        samples = {}
        rows = self.db.execute("SELECT spec,node_id,state,timing,generation FROM jobs WHERE node_id IS NOT NULL ORDER BY updated DESC").fetchall()
        for row in rows:
            if row["state"] != "succeeded" or row["generation"] != 1 or not row["timing"]:
                continue
            timing = json.loads(row["timing"])
            if timing.get("complete") and timing.get("elapsed_seconds", 0) > 0:
                values = samples.setdefault((row["node_id"], workload_key(json.loads(row["spec"]))), [])
                if len(values) < 10:
                    values.append(timing["elapsed_seconds"])
        for node in nodes:
            node["performance"] = {key: statistics.median(values) for (identity, key), values in samples.items() if identity == node["id"]}
            node["queued_work_seconds"] = 0
        for row in rows:
            if row["state"] not in ("succeeded", "failed", "paused", "interrupted", "canceled"):
                node = by_id[row["node_id"]]
                elapsed = json.loads(row["timing"]).get("elapsed_seconds", 0) if row["timing"] else 0
                node["queued_work_seconds"] += max(1, duration_estimate(json.loads(row["spec"]), node) - elapsed)
        return nodes
