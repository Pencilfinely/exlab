"""Revocable, scoped mobile access to the existing controller."""
from __future__ import annotations

import hashlib
import secrets
import uuid

from .common import now


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS mobile_devices (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, permission TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL,
        expires REAL NOT NULL, revoked REAL)""")


def _pick(value, keys):
    return {key: value[key] for key in keys if key in value}


def _job(value):
    result = _pick(value, ("id", "state", "node_id", "detail", "created", "updated",
                          "attempt", "metrics", "timing", "command_id", "command_ack", "action", "generation",
                          "execution_state", "handoff", "queue_control", "delivery"))
    result["spec"] = _pick(value["spec"], ("name", "algorithm", "group", "params", "resume_supported", "result_delivery", "planning", "priority"))
    return result


class MobileHubMixin:
    def mobile_devices(self):
        with self.lock:
            return {"devices": [dict(row) for row in self.db.execute(
                "SELECT id,name,permission,created,expires,revoked FROM mobile_devices ORDER BY created DESC")],
                "time": now()}

    def mobile_enroll(self, payload):
        from .hub import APIError, _object
        _object(payload, "request")
        name, permission = payload.get("name"), payload.get("permission", "monitor")
        days = payload.get("days", 0)
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise APIError(400, "请输入 1–80 字的设备名称")
        if permission not in ("monitor", "control"):
            raise APIError(400, "权限必须为 monitor 或 control")
        if type(days) is not int or not 0 <= days <= 90:
            raise APIError(400, "有效期必须为 1–90 天，或选择长期有效")
        token, identity, timestamp = secrets.token_urlsafe(32), uuid.uuid4().hex, now()
        with self.transaction():
            count = self.db.execute("SELECT COUNT(*) FROM mobile_devices WHERE revoked IS NULL AND (expires=0 OR expires>?)", (timestamp,)).fetchone()[0]
            if count >= 100:
                raise APIError(409, "有效移动凭证已达 100 个，请先撤销不用的凭证")
            self.db.execute("INSERT INTO mobile_devices VALUES (?,?,?,?,?,?,NULL)",
                            (identity, name.strip(), permission, hashlib.sha256(token.encode()).hexdigest(),
                             timestamp, timestamp + days * 86400 if days else 0))
        return {"id": identity, "name": name.strip(), "permission": permission,
                "expires": timestamp + days * 86400 if days else 0, "token": token}

    def mobile_renew(self, payload):
        from .hub import APIError, _object
        _object(payload, "request")
        identity, days = payload.get("id"), payload.get("days", 0)
        if not isinstance(identity, str) or not identity or type(days) is not int or not 0 <= days <= 90:
            raise APIError(400, "请选择设备和有效期")
        timestamp = now()
        expires = timestamp + days * 86400 if days else 0
        with self.transaction():
            row = self.db.execute("SELECT revoked,expires FROM mobile_devices WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise APIError(404, "找不到移动设备")
            if row["revoked"] is not None:
                raise APIError(409, "已撤销的设备需要重新配对")
            if row["expires"] and row["expires"] <= timestamp:
                count = self.db.execute("SELECT COUNT(*) FROM mobile_devices WHERE revoked IS NULL AND (expires=0 OR expires>?)", (timestamp,)).fetchone()[0]
                if count >= 100:
                    raise APIError(409, "有效移动凭证已达 100 个，请先撤销不用的凭证")
            self.db.execute("UPDATE mobile_devices SET expires=? WHERE id=?", (expires, identity))
        return {"id": identity, "expires": expires}

    def mobile_revoke(self, payload):
        from .hub import APIError, _object
        _object(payload, "request")
        identity = payload.get("id")
        if not isinstance(identity, str):
            raise APIError(400, "缺少设备 ID")
        with self.transaction():
            if self.db.execute("UPDATE mobile_devices SET revoked=COALESCE(revoked,?) WHERE id=?", (now(), identity)).rowcount != 1:
                raise APIError(404, "找不到移动设备")
        return {"id": identity, "revoked": True}

    def mobile_authenticate(self, header):
        from .hub import APIError
        if not isinstance(header, str) or not header.startswith("Bearer ") or not 1 <= len(header[7:]) <= 512:
            raise APIError(401, "请输入管理端签发的移动访问凭证")
        digest = hashlib.sha256(header[7:].encode()).hexdigest()
        with self.lock:
            row = self.db.execute("SELECT id,name,permission,expires FROM mobile_devices "
                                  "WHERE token_hash=? AND revoked IS NULL AND (expires=0 OR expires>?)", (digest, now())).fetchone()
            if row is None:
                raise APIError(401, "移动凭证无效、已过期或已撤销，请重新连接")
            return dict(row)

    def mobile_state(self):
        state = self.state()
        nodes = []
        for value in state["nodes"]:
            node = _pick(value, ("id", "last_seen", "online", "mode", "display_name", "compute_policy", "resource_policy"))
            snapshot = value["snapshot"]
            node["snapshot"] = _pick(snapshot, ("free_ram_mb", "disk_free_mb", "policy", "availability", "compute_control", "local_compute_policy", "ac_power", "capabilities", "execution_delivery", "delivery", "compute_policy_revision"))
            node["snapshot"]["gpus"] = [_pick(gpu, ("uuid", "name", "total_mb", "free_mb", "local_enabled"))
                                        for gpu in snapshot.get("gpus", [])]
            nodes.append(node)
        return {"jobs": [_job(value) for value in state["jobs"]], "nodes": nodes,
                "time": state["time"], "version": state["version"], "agent": state["agent"]}

    def mobile_job(self, identity):
        value = self.job(identity)
        return {**_job(value), "time": value["time"], "log_tail": value["log_tail"],
                "events": value["events"], "artifacts": value["artifacts"],
                **_pick(value, ("execution_checkpoints", "experiment_results", "experiment_progress", "evidence"))}

    def mobile_request(self, header, method, path, payload=None, identity=None, query=None):
        """Keep revocation, authorization and execution under the same hub lock."""
        from .hub import APIError, _integer, _object
        with self.lock:
            session = self.mobile_authenticate(header)
            if method == "GET":
                if path == "/api/mobile/state":
                    return {**self.mobile_state(), "session": session}
                if path == "/api/mobile/job":
                    return self.mobile_job(identity)
                if path == "/api/mobile/agent/status":
                    return self.autonomy.status()
                if path == "/api/mobile/agent/run":
                    return self.autonomy.run_detail(identity)
                if path == "/api/mobile/agent/history":
                    values = query or {}
                    return self.autonomy.history(int(values.get("offset", [0])[0]), int(values.get("limit", [5])[0]))
                if path == "/api/mobile/agent/inspection":
                    values = query or {}
                    return self.autonomy.rules.report(int(values.get("offset", [0])[0]), int(values.get("limit", [10])[0]))
                if path == "/api/mobile/agent/rules":
                    values = query or {}
                    return self.autonomy.rules.listing(int(values.get("offset", [0])[0]), min(8, int(values.get("limit", [5])[0])))
                raise APIError(404, "找不到移动接口")
            if method != "POST":
                raise APIError(405, "不支持的请求方法")
            if path not in ("/api/mobile/action", "/api/mobile/node-mode", "/api/mobile/agent/settings", "/api/mobile/agent/delegate",
                            "/api/mobile/agent/interrupt", "/api/mobile/agent/rules/preview", "/api/mobile/agent/rules/save", "/api/mobile/agent/rules/delete",
                            "/api/mobile/compute-policy", "/api/mobile/execution/handoff", "/api/mobile/planning/queue"):
                raise APIError(404, "找不到移动接口")
            if session["permission"] != "control":
                raise APIError(403, "此设备仅有查看权限")
            _object(payload, "request")
            if path.startswith("/api/mobile/agent/"):
                from .agent_tools import validate, obj, TEXT
                actor = {"id": "mobile:" + session["id"], "scopes": ["read", "control", "delegate", "automation"]}
                if "/rules/" in path:
                    suffix = path.rsplit("/", 1)[-1]
                    return self.autonomy.gateway.call("exlab_rule_"+suffix, payload, actor)
                if path.endswith("settings"):
                    return self.autonomy.settings(payload)
                if path.endswith("delegate"):
                    return self.autonomy.gateway.call("exlab_delegate", payload, actor)
                validate(payload, obj({"run_id": TEXT}, ("run_id",)))
                return self.autonomy.cancel(payload["run_id"])
            routes = {"/api/mobile/compute-policy": self.set_compute_policy, "/api/mobile/execution/handoff": self.request_handoff,
                      "/api/mobile/planning/queue": self.set_queue_control}
            if path in routes:
                return routes[path](payload)
            if path == "/api/mobile/action":
                expected = _integer(payload.get("expected_command_id"), "expected_command_id")
                return self.action({"job_id": payload.get("job_id"), "action": payload.get("action"),
                                    "expected_command_id": expected})
            if payload.get("expected_mode") not in ("run", "drain"):
                raise APIError(400, "请先刷新节点状态")
            return self.set_mode({"node_id": payload.get("node_id"), "mode": payload.get("mode"),
                                  "expected_mode": payload["expected_mode"]})
