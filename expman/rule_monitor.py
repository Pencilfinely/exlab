"""Local deterministic inspections and a bounded declarative rule language.

Rules are data, never Python/JavaScript. No eval, imports, loops, file access,
network access or shell commands. Effects use the existing guarded tool gateway.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid

from .common import now

FIELDS = {
    "node": {"id", "online", "mode", "active_jobs", "offline_seconds", "ac_power",
             "available", "availability_reason", "compute_phase", "resources_released", "resource_error", "delivery_error"},
    "job": {"id", "state", "node_id", "attempt", "generation", "queue_hold", "queue_priority",
            "queued_seconds", "command_pending_seconds", "handoff_status", "handoff_seconds",
            "progress_stalled_seconds", "resume_supported"},
}
COMPARISONS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "exists"}
EFFECTS = {"notify", "escalate", "queue", "node_mode", "pause", "handoff"}
ACTIVE = {"assigned", "preparing", "ready", "starting", "running"}
MAX_CUSTOM_RULES = 24


def _builtin(identity, name, target, when, after=30, severity="warning"):
    return {"id": identity, "revision": 0, "name": name, "enabled": True, "target": target,
            "when": when, "for_seconds": after, "cooldown_seconds": 300,
            "severity": severity, "effect": {"type": "escalate"}}


BUILTINS = [
    _builtin("builtin-node-offline", "运行中的节点离线", "node", {"all": [
        {"field": "online", "op": "eq", "value": False}, {"field": "active_jobs", "op": "gt", "value": 0}]}, severity="critical"),
    _builtin("builtin-resource-error", "节点资源策略未生效", "node", {"all": [
        {"field": "online", "op": "eq", "value": True}, {"field": "resource_error", "op": "exists", "value": True}]}),
    _builtin("builtin-compute-blocked", "停止或释放训练资源未完成", "node", {"all": [
        {"field": "online", "op": "eq", "value": True}, {"field": "compute_phase", "op": "eq", "value": "blocked"}]}),
    _builtin("builtin-delivery-error", "节点回传异常", "node", {"all": [
        {"field": "online", "op": "eq", "value": True}, {"field": "delivery_error", "op": "exists", "value": True}]}),
    _builtin("builtin-command-pending", "任务指令长时间未确认", "job", {"field": "command_pending_seconds", "op": "gte", "value": 120}),
    _builtin("builtin-queue-wait", "可运行任务等待超过十分钟", "job", {"all": [
        {"field": "queue_hold", "op": "eq", "value": False}, {"field": "queued_seconds", "op": "gte", "value": 600}]}),
    _builtin("builtin-handoff-wait", "检查点接手长时间等待", "job", {"field": "handoff_seconds", "op": "gte", "value": 600}),
    _builtin("builtin-job-failed", "实验执行失败", "job", {"field": "state", "op": "eq", "value": "failed"}),
]


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS agent_rules (
            id TEXT PRIMARY KEY,revision INTEGER NOT NULL,document TEXT NOT NULL,
            origin TEXT NOT NULL,actor TEXT NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_rule_findings (
            rule_id TEXT NOT NULL,target TEXT NOT NULL,revision INTEGER NOT NULL,episode TEXT NOT NULL,
            first_seen REAL NOT NULL,last_seen REAL NOT NULL,status TEXT NOT NULL,
            severity TEXT NOT NULL,title TEXT NOT NULL,escalate INTEGER NOT NULL,
            facts TEXT NOT NULL,action TEXT,PRIMARY KEY(rule_id,target));
        CREATE TABLE IF NOT EXISTS agent_rule_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,created REAL NOT NULL,kind TEXT NOT NULL,
            rule_id TEXT NOT NULL,target TEXT NOT NULL,title TEXT NOT NULL,detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_rule_observations (
            target TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_rule_cooldowns (
            rule_id TEXT NOT NULL,target TEXT NOT NULL,acted REAL NOT NULL,PRIMARY KEY(rule_id,target));
        CREATE INDEX IF NOT EXISTS agent_rule_findings_status ON agent_rule_findings(status,last_seen);
    """)
    for document in BUILTINS:
        db.execute("INSERT OR IGNORE INTO agent_rules(id,revision,document,origin,actor,updated) VALUES (?,?,?,?,?,?)",
                   (document["id"], 0, json.dumps(document, ensure_ascii=False), "builtin", "system", now()))


def validate_document(value):
    if not isinstance(value, dict) or len(json.dumps(value, allow_nan=False).encode()) > 16384:
        raise ValueError("规则必须是 16 KiB 以内的 JSON 对象")
    required = {"id", "name", "enabled", "target", "when", "for_seconds", "cooldown_seconds", "severity", "effect"}
    if not required <= value.keys() or value.keys() - required - {"revision"}:
        raise ValueError("规则字段不完整或包含不支持的脚本能力")
    if not isinstance(value["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value["id"]):
        raise ValueError("规则 ID 只能包含 1–64 个英文字母、数字、下划线或连字符")
    if not isinstance(value["name"], str) or not 1 <= len(value["name"].strip()) <= 100 or type(value["enabled"]) is not bool:
        raise ValueError("请填写规则名称和启用状态")
    if value["target"] not in FIELDS or value["severity"] not in ("info", "warning", "critical"):
        raise ValueError("不支持的巡检对象或严重级别")
    for key, minimum, maximum in (("for_seconds", 0, 86400), ("cooldown_seconds", 60, 86400)):
        if type(value[key]) is not int or not minimum <= value[key] <= maximum:
            raise ValueError(key + " 超出允许范围")
    atoms = 0
    def condition(part, depth=0):
        nonlocal atoms
        atoms += 1
        if atoms > 48 or depth > 5 or not isinstance(part, dict):
            raise ValueError("规则条件最多 48 个节点、6 层深度")
        if set(part) in ({"all"}, {"any"}):
            children = next(iter(part.values()))
            if not isinstance(children, list) or not 1 <= len(children) <= 12:
                raise ValueError("组合条件需要 1–12 个子条件")
            for child in children:
                condition(child, depth + 1)
            return
        if set(part) != {"field", "op", "value"} or part["field"] not in FIELDS[value["target"]] or part["op"] not in COMPARISONS:
            raise ValueError("规则只能读取公开的巡检字段并使用受限比较运算")
        literal = part["value"]
        literals = literal if isinstance(literal, list) else [literal]
        if len(literals) > 20 or any(type(x) not in (str, int, float, bool, type(None)) or
            isinstance(x, str) and len(x) > 256 or type(x) in (float, int) and (not math.isfinite(x) or abs(x) > 2**53) for x in literals):
            raise ValueError("条件仅支持有限长度的常量")
        if part["op"] == "in" and not isinstance(literal, list) or part["op"] == "exists" and type(literal) is not bool:
            raise ValueError("in 需要常量列表；exists 需要布尔值")
        if part["op"] in ("gt", "gte", "lt", "lte") and type(literal) not in (int, float):
            raise ValueError("数值比较需要数值常量")
        if part["op"] == "contains" and not isinstance(literal, str):
            raise ValueError("contains 需要文本常量")
    condition(value["when"])
    effect = value["effect"]
    if not isinstance(effect, dict) or effect.get("type") not in EFFECTS:
        raise ValueError("规则只能通知、请求 AI 研判或使用指定控制操作")
    kind = effect["type"]
    allowed = {"type"} | ({"priority", "hold"} if kind == "queue" else {"target_node_id"} if kind == "handoff" else set())
    if effect.keys() - allowed:
        raise ValueError("规则效果包含不支持的参数；不能执行系统脚本")
    if kind == "queue":
        if set(effect) != allowed or type(effect["priority"]) is not int or not -100 <= effect["priority"] <= 100 or type(effect["hold"]) is not bool:
            raise ValueError("队列规则需要 priority（-100–100）及 hold 布尔值")
    if kind == "handoff" and effect.get("target_node_id") is not None and (not isinstance(effect["target_node_id"], str) or not 1 <= len(effect["target_node_id"]) <= 100):
        raise ValueError("接手节点标识无效")
    if kind == "node_mode" and value["target"] != "node" or kind in ("queue", "pause", "handoff") and value["target"] != "job":
        raise ValueError("控制操作与巡检对象不匹配")
    return json.loads(json.dumps(value, allow_nan=False))


def matches(condition, facts):
    if "all" in condition:
        return all(matches(child, facts) for child in condition["all"])
    if "any" in condition:
        return any(matches(child, facts) for child in condition["any"])
    left, right, op = facts.get(condition["field"]), condition["value"], condition["op"]
    if op == "exists":
        return bool(left is not None and left != "") == right
    if op == "eq":
        return type(left) is type(right) and left == right or type(left) in (int, float) and type(right) in (int, float) and left == right
    if op == "ne":
        return not matches({**condition, "op": "eq"}, facts)
    if op == "in":
        return any(matches({**condition, "op": "eq", "value": value}, facts) for value in right)
    if op == "contains":
        return isinstance(left, str) and right in left
    if type(left) not in (int, float) or not math.isfinite(left):
        return False
    return {"gt": lambda: left > right, "gte": lambda: left >= right,
            "lt": lambda: left < right, "lte": lambda: left <= right}[op]()


class RuleMonitor:
    def __init__(self, manager):
        self.manager, self.hub = manager, manager.hub

    def listing(self, offset=0, limit=32):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError("规则库分页参数无效")
        with self.hub.lock:
            rules = [{**json.loads(row["document"]), "origin": row["origin"]}
                     for row in self.hub.db.execute("SELECT * FROM agent_rules WHERE deleted=0 ORDER BY origin='builtin' DESC,id LIMIT ? OFFSET ?", (limit, offset))]
            total = self.hub.db.execute("SELECT COUNT(*) FROM agent_rules WHERE deleted=0").fetchone()[0]
            return self.manager.gateway.clean({"rules": rules, "fields": {k: sorted(v) for k, v in FIELDS.items()},
                "total": total, "offset": offset, "next_offset": offset+limit if offset+limit < total else None,
                "operators": sorted(COMPARISONS), "effects": sorted(EFFECTS), "limits": {
                    "custom_rules": MAX_CUSTOM_RULES, "condition_nodes": 48, "bytes": 16384,
                    "actions_per_scan": 4, "actions_per_hour": 60}, "language": "exlab-rules-v1"})

    def save(self, payload, actor, automatic=False):
        from .hub import APIError
        if set(payload) != {"revision", "document"} or type(payload["revision"]) is not int or payload["revision"] < 0:
            raise ValueError("保存规则需要 revision 和 document")
        document = validate_document(payload["document"])
        with self.hub.transaction():
            row = self.hub.db.execute("SELECT * FROM agent_rules WHERE id=?", (document["id"],)).fetchone()
            if automatic:
                if not self.manager.settings()["allow_rule_authoring"]:
                    raise APIError(403, "AI 编写规则未开启")
                if row and row["origin"] != "ai":
                    raise APIError(403, "AI 只能维护 AI 创建的规则，不能改写人工或内置规则")
                if document["effect"]["type"] not in ("notify", "escalate") and document["effect"]["type"] not in self.manager.settings()["operations"]:
                    raise APIError(403, "规则操作不在托管允许范围内")
            if not row and document["id"].startswith("builtin-"):
                raise ValueError("内置规则前缀保留")
            old = json.loads(row["document"]) if row else None
            comparable = {k: v for k, v in document.items() if k != "revision"}
            if row and not row["deleted"] and comparable == {k: v for k, v in old.items() if k != "revision"} and payload["revision"] in (row["revision"], row["revision"]-1):
                return {**old, "origin": row["origin"]}
            if payload["revision"] != (row["revision"] if row else 0):
                raise APIError(409, "规则已变化，请刷新后再保存")
            if (not row or row["deleted"]) and self.hub.db.execute("SELECT COUNT(*) FROM agent_rules WHERE origin!='builtin' AND deleted=0").fetchone()[0] >= MAX_CUSTOM_RULES:
                raise APIError(409, "自定义规则数量已达上限")
            document["revision"] = (row["revision"] if row else 0) + 1
            origin = "builtin" if row and row["origin"] == "builtin" else "ai" if automatic else "manual"
            self.hub.db.execute("INSERT OR REPLACE INTO agent_rules VALUES (?,?,?,?,?,0,?)", (
                document["id"], document["revision"], json.dumps(document, ensure_ascii=False), origin, actor["id"], now()))
            self._event("edited", document["id"], "", document["name"], "规则已保存；下一次规则巡检生效")
        return self.manager.gateway.clean({**document, "origin": origin})

    def delete(self, payload, actor, automatic=False):
        from .hub import APIError
        if set(payload) != {"id", "revision"} or type(payload["revision"]) is not int:
            raise ValueError("删除规则需要 id 和 revision")
        with self.hub.transaction():
            row = self.hub.db.execute("SELECT * FROM agent_rules WHERE id=?", (payload["id"],)).fetchone()
            if not row or row["deleted"]:
                return {"deleted": payload["id"]}
            if row["origin"] == "builtin":
                raise APIError(409, "内置规则请使用停用开关")
            if automatic and (row["origin"] != "ai" or not self.manager.settings()["allow_rule_authoring"]):
                raise APIError(403, "AI 只能删除 AI 创建的规则")
            if payload["revision"] != row["revision"]:
                raise APIError(409, "规则已变化，请刷新后再删除")
            self.hub.db.execute("UPDATE agent_rules SET deleted=1,revision=revision+1,updated=? WHERE id=?", (now(), payload["id"]))
            self.hub.db.execute("UPDATE agent_rule_findings SET status='resolved' WHERE rule_id=?", (payload["id"],))
            self._event("deleted", payload["id"], "", json.loads(row["document"])["name"], "规则已删除")
        return {"deleted": payload["id"]}

    def _age(self, observation, key, value, timestamp):
        prior = observation.get(key)
        if value is None:
            observation.pop(key, None)
            return 0
        if not prior or prior["value"] != value or prior["since"] > timestamp:
            prior = observation[key] = {"value": value, "since": timestamp}
        return timestamp-prior["since"]

    def facts(self, persist=False):
        state, timestamp = self.hub.state(), now()
        result = {"job": [], "node": []}
        existing = {row["target"]: json.loads(row["value"]) for row in self.hub.db.execute("SELECT * FROM agent_rule_observations")}
        observations = {}
        for job in state["jobs"]:
            key = "job:" + job["id"]
            observation = existing.get(key, {})
            queue, execution, handoff = job.get("queue_control") or {}, job.get("execution_state") or {}, job.get("handoff") or {}
            progress = execution.get("progress") or {}
            pending = job.get("command_id", 0) > job.get("command_ack", 0)
            active_handoff = handoff.get("status") not in (None, "finished", "canceled")
            facts = {"id": job["id"], "state": job["state"], "node_id": job.get("node_id"),
                "attempt": job.get("attempt", 0), "generation": job.get("generation", 0),
                "queue_hold": bool(queue.get("hold")), "queue_priority": queue.get("priority", 0),
                "resume_supported": job["spec"].get("resume_supported") is True or job["spec"].get("backend") == "demo",
                "queued_seconds": self._age(observation, "queue", [job.get("attempt"), job.get("generation")] if job["state"] == "queued" and not queue.get("hold") else None, timestamp),
                "command_pending_seconds": self._age(observation, "command", job["command_id"] if pending else None, timestamp),
                "handoff_status": handoff.get("status"),
                "handoff_seconds": self._age(observation, "handoff", [job.get("generation"), handoff.get("status"), handoff.get("target_node_id")] if active_handoff else None, timestamp),
                "progress_stalled_seconds": self._age(observation, "progress", [job.get("attempt"), job.get("generation"), progress.get("step"), progress.get("epoch")] if job["state"] == "running" and any(k in progress for k in ("step", "epoch")) else None, timestamp)}
            observations[key] = observation
            result["job"].append(facts)
        for node in state["nodes"]:
            key, snap = "node:" + node["id"], node["snapshot"]
            observation = existing.get(key, {})
            availability = snap.get("availability") or {}
            from .compute_policy import control_matches
            control = (snap.get("compute_control") or {}) if control_matches(snap) else {}
            delivery = snap.get("delivery") or {}
            result["node"].append({"id": node["id"], "online": node["online"], "mode": node["mode"],
                "active_jobs": sum(job["node_id"] == node["id"] and job["state"] in ACTIVE for job in result["job"]),
                "offline_seconds": self._age(observation, "offline", True if not node["online"] else None, timestamp),
                "ac_power": snap.get("ac_power"), "available": availability.get("available"),
                "availability_reason": availability.get("reason"), "resource_error": snap.get("resource_policy_error") or None,
                "compute_phase": control.get("phase"), "resources_released": control.get("resources_released"),
                "delivery_error": delivery.get("last_error") or None})
            observations[key] = observation
        if persist:
            self.hub.db.executemany("INSERT OR REPLACE INTO agent_rule_observations VALUES (?,?)", [(key, json.dumps(value)) for key, value in observations.items()])
            for key in existing.keys() - observations.keys():
                self.hub.db.execute("DELETE FROM agent_rule_observations WHERE target=?", (key,))
        return result

    def preview(self, document):
        value = validate_document(document)
        with self.hub.lock:
            found = [entity for entity in self.facts()[value["target"]] if matches(value["when"], entity)]
            return self.manager.gateway.clean({"valid": True, "language": "exlab-rules-v1", "matched": len(found),
                "examples": found[:10], "effect": value["effect"], "note": "仅预览；实际执行仍需满足持续时间、冷却、权限和人工限制。"})

    def _event(self, kind, rule, target, title, detail):
        self.hub.db.execute("INSERT INTO agent_rule_events(created,kind,rule_id,target,title,detail) VALUES (?,?,?,?,?,?)",
            (now(), kind, rule, target, title, self.manager.gateway.clean(detail)[:1000]))

    def _action(self, document, facts, finding, config):
        effect, kind = document["effect"], document["effect"]["type"]
        if kind in ("notify", "escalate") or not config["enabled"] or kind not in config["operations"] or finding["action"]:
            return False
        recent = self.hub.db.execute("SELECT COUNT(*) FROM agent_tool_receipts WHERE actor LIKE 'rule:%' AND created>?", (now()-3600,)).fetchone()[0]
        if recent >= 60:
            return False
        prior = self.hub.db.execute("SELECT acted FROM agent_rule_cooldowns WHERE rule_id=? AND target=?", (document["id"], facts["id"])).fetchone()
        previous = prior[0] if prior else None
        if previous and now()-previous < document["cooldown_seconds"]:
            return False
        if kind == "node_mode":
            args = {"node_id": facts["id"], "mode": "drain", "expected_mode": facts["mode"]}
        else:
            job = self.hub.job(facts["id"])
            if kind == "queue":
                args = {"job_id": facts["id"], "revision": job["queue_control"]["revision"], "priority": effect["priority"], "hold": effect["hold"]}
            elif kind == "pause":
                args = {"job_id": facts["id"], "expected_command_id": job["command_id"]}
            else:
                args = {"job_id": facts["id"], "generation": job["generation"], "target_node_id": effect.get("target_node_id")}
        actor = {"id": "rule:"+document["id"], "scopes": ["read", "control"]}
        request_id = "rule-"+finding["episode"]
        def fence():
            if self.manager.stop.is_set() or self.hub.updating or self.hub.center_state["mode"] != "primary" or self.manager.settings()["revision"] != config["revision"]:
                raise ValueError("规则巡检授权或主控状态已变化")
        try:
            receipt = self.manager.gateway.operate(kind, args, request_id, actor, automatic=True, check=fence)
        except Exception as error:
            receipt = {"status": "rejected", "error": self.manager.gateway.clean(str(error))[:500]}
        self.hub.db.execute("UPDATE agent_rule_findings SET action=? WHERE rule_id=? AND target=?", (json.dumps(receipt, ensure_ascii=False), document["id"], facts["id"]))
        self.hub.db.execute("INSERT OR REPLACE INTO agent_rule_cooldowns VALUES (?,?,?)", (document["id"], facts["id"], now()))
        self._event("action", document["id"], facts["id"], document["name"], "规则操作已记录；执行结果以节点回传为准")
        return True

    def tick(self, force=False):
        with self.hub.lock:
            if self.manager.stop.is_set() or self.hub.updating or self.hub.center_state["mode"] != "primary":
                return
            timestamp, config, runtime = now(), self.manager.settings(), self.manager._runtime()
            self.manager.reconcile_feedback()
            elapsed = timestamp-runtime.get("last_rule_attempt_at", runtime.get("last_rule_scan_at", 0))
            if not force and 0 <= elapsed < config["rule_interval_seconds"]:
                return
            self.manager._runtime(last_rule_attempt_at=timestamp)
            facts = self.facts(persist=True)
            seen, action_count, evaluations = set(), 0, 0
            for row in self.hub.db.execute("SELECT * FROM agent_rules WHERE deleted=0").fetchall():
                document = validate_document(json.loads(row["document"]))
                if not document["enabled"]:
                    continue
                for entity in facts[document["target"]]:
                    evaluations += 1
                    if not matches(document["when"], entity):
                        continue
                    key = (document["id"], entity["id"])
                    seen.add(key)
                    finding = self.hub.db.execute("SELECT * FROM agent_rule_findings WHERE rule_id=? AND target=?", key).fetchone()
                    if not finding or finding["status"] == "resolved" or finding["revision"] != row["revision"]:
                        self.hub.db.execute("INSERT OR REPLACE INTO agent_rule_findings VALUES (?,?,?,?,?,?,'watching',?,?,?,?,NULL)",
                            (*key, row["revision"], uuid.uuid4().hex, timestamp, timestamp, document["severity"], document["name"], int(document["effect"]["type"] == "escalate"), json.dumps(entity, ensure_ascii=False)))
                        finding = self.hub.db.execute("SELECT * FROM agent_rule_findings WHERE rule_id=? AND target=?", key).fetchone()
                    self.hub.db.execute("UPDATE agent_rule_findings SET last_seen=?,facts=? WHERE rule_id=? AND target=?", (timestamp, json.dumps(entity, ensure_ascii=False), *key))
                    if timestamp-finding["first_seen"] < document["for_seconds"]:
                        continue
                    if finding["status"] != "open":
                        self.hub.db.execute("UPDATE agent_rule_findings SET status='open' WHERE rule_id=? AND target=?", key)
                        self._event("opened", *key, document["name"], "规则条件持续满足，已记录异常")
                    if action_count < 4:
                        action_count += self._action(document, entity, finding, config)
            for row in self.hub.db.execute("SELECT * FROM agent_rule_findings WHERE status!='resolved'").fetchall():
                if (row["rule_id"], row["target"]) not in seen:
                    self.hub.db.execute("UPDATE agent_rule_findings SET status='resolved',last_seen=? WHERE rule_id=? AND target=?", (timestamp, row["rule_id"], row["target"]))
                    if row["status"] == "open":
                        self._event("resolved", row["rule_id"], row["target"], row["title"], "条件已恢复或规则已停用")
            self.hub.db.execute("DELETE FROM agent_rule_events WHERE id <= (SELECT COALESCE(MAX(id),0)-2000 FROM agent_rule_events)")
            self.hub.db.execute("DELETE FROM agent_rule_cooldowns WHERE acted<?", (timestamp-86400,))
            self.manager._runtime(last_rule_scan_at=timestamp, rule_evaluations=evaluations, rule_scan_error=None)
            self.manager.reconcile_feedback()

    def summary(self):
        with self.hub.lock:
            rows = self.hub.db.execute("SELECT * FROM agent_rule_findings WHERE status='open' ORDER BY severity='critical' DESC,last_seen DESC").fetchall()
            signature = hashlib.sha256(json.dumps(sorted([row["rule_id"], row["target"], row["episode"], row["severity"]] for row in rows if row["escalate"])).encode()).hexdigest() if any(row["escalate"] for row in rows) else None
            runtime = self.manager._runtime()
            return self.manager.gateway.clean({"last_scan_at": runtime.get("last_rule_scan_at"), "scan_error": runtime.get("rule_scan_error"),
                "evaluations": runtime.get("rule_evaluations", 0), "open_count": len(rows), "escalation_signature": signature,
                "findings": [{k: row[k] for k in ("rule_id", "target", "severity", "title", "first_seen", "last_seen", "escalate")} for row in rows[:5]],
                "enabled_rules": sum(json.loads(row[0])["enabled"] for row in self.hub.db.execute("SELECT document FROM agent_rules WHERE deleted=0")),
                "recent_events": [dict(row) for row in self.hub.db.execute("SELECT * FROM agent_rule_events ORDER BY id DESC LIMIT 5")]})

    def report(self, offset=0, limit=10):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("巡检报告分页参数无效")
        with self.hub.lock:
            rows = self.hub.db.execute("SELECT * FROM agent_rule_findings WHERE status='open' ORDER BY severity='critical' DESC,rule_id,target LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            total = self.hub.db.execute("SELECT COUNT(*) FROM agent_rule_findings WHERE status='open'").fetchone()[0]
            items = []
            for row in rows:
                action = json.loads(row["action"]) if row["action"] else None
                feedback = self.manager._outcomes("rule:"+row["rule_id"], action["request_id"]) if action and action.get("request_id") else []
                items.append({**dict(row), "facts": json.loads(row["facts"]), "action": action,
                    "feedback": feedback[0] if feedback else None})
            return self.manager.gateway.clean({"items": items,
                "total": total, "offset": offset, "next_offset": offset+limit if offset+limit < total else None,
                "events": [dict(row) for row in self.hub.db.execute("SELECT * FROM agent_rule_events ORDER BY id DESC LIMIT 10")]})
