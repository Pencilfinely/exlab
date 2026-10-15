"""Shared typed tools for managed AI, external models, MCP and trusted clients."""
from __future__ import annotations

import copy
import hashlib
import json
import secrets
import uuid

from .ai_assist import AIAssist, _redact, _text
from .common import now


def obj(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1, "maxLength": 200}
INTEGER = {"type": "integer", "minimum": 0, "maximum": 2**53 - 2}
ANY_OBJECT = {"type": "object"}
PAGE = {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}
OPERATIONS = {
    "queue": obj({"job_id": TEXT, "priority": {"type": "integer", "minimum": -100, "maximum": 100},
                  "hold": {"type": "boolean"}, "revision": INTEGER}, ("job_id", "priority", "hold", "revision")),
    "handoff": obj({"job_id": TEXT, "generation": INTEGER, "target_node_id": {"type": ["string", "null"]}}, ("job_id", "generation")),
    "pause": obj({"job_id": TEXT, "expected_command_id": INTEGER}, ("job_id", "expected_command_id")),
    "resume": obj({"job_id": TEXT, "expected_command_id": INTEGER}, ("job_id", "expected_command_id")),
    "cancel": obj({"job_id": TEXT, "expected_command_id": INTEGER}, ("job_id", "expected_command_id")),
    "availability": obj({"node_id": TEXT, "policy": ANY_OBJECT}, ("node_id", "policy")),
    "node_mode": obj({"node_id": TEXT, "mode": {"enum": ["run", "drain"]}, "expected_mode": {"enum": ["run", "drain"]}}, ("node_id", "mode", "expected_mode")),
    "resources": obj({"node_id": TEXT, "revision": INTEGER, "policy": ANY_OBJECT, "gpu_policy": ANY_OBJECT}, ("node_id", "revision", "policy", "gpu_policy")),
    "submit": obj({"spec": ANY_OBJECT, "grid": ANY_OBJECT}, ("spec",)),
    "matrix_start": obj({"id": TEXT, "revision": INTEGER}, ("id", "revision")),
    "project_deploy": obj({"digest": TEXT, "node_ids": {"type": "array", "items": TEXT, "minItems": 1, "maxItems": 100}}, ("digest", "node_ids")),
}
DEFAULT_OPERATIONS = ["queue", "handoff", "resume", "availability", "node_mode"]
SCOPES = {"read", "control", "delegate", "automation"}


def validate(value, schema, name="arguments"):
    """Validate our deliberately small JSON-schema vocabulary before dispatch."""
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    expected = schema.get("type")
    if expected and not any(type(value) is types[t] for t in (expected if isinstance(expected, list) else [expected])):
        raise ValueError(name + " has the wrong type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(name + " is not an allowed value")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value) or (schema.get("additionalProperties") is False and set(value) - set(properties)):
            raise ValueError(name + " has missing or unknown fields")
        for key in value.keys() & properties.keys():
            validate(value[key], properties[key], name + "." + key)
    elif isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000):
            raise ValueError(name + " has too many/few items")
        for item in value:
            validate(item, schema.get("items", {}), name + "[]")
    elif isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 1000000):
            raise ValueError(name + " has an invalid length")
    elif type(value) is int:
        if not schema.get("minimum", -2**53) <= value <= schema.get("maximum", 2**53):
            raise ValueError(name + " is out of range")


TOOLS = [
    ("exlab_topology", "Read current Center topology, node availability/resources, execution state and queue. Paginate jobs; no credentials or checkpoint bytes.", obj(PAGE), "read"),
    ("exlab_job", "Inspect one experiment: fixed spec, progress, checkpoints, runtime/log tail, events, lightweight results and evidence references. Use fields to inspect large state separately; array fields are paginated (events newest first).",
     obj({"job_id": TEXT, "fields": {"type": "array", "maxItems": 18, "items": {"enum": ["spec", "execution_state", "resume_checkpoint", "handoff", "queue_control", "execution_checkpoints", "runtime", "metrics", "timing", "events", "log_tail", "experiment_results", "experiment_progress", "evidence", "result_conflicts", "artifacts"]}},
          **PAGE, "log_chars": {"type": "integer", "minimum": 0, "maximum": 32000}}, ("job_id",)), "read"),
    ("exlab_catalog", "List projects, matrices or lightweight results. Use fixed definitions before submitting or starting work.", obj({"kind": {"enum": ["projects", "matrices", "results"]}, **PAGE}, ("kind",)), "read"),
    ("exlab_events", "Read durable experiment events after a cursor; useful to verify prior commands and detect changes.", obj({"after": INTEGER, "job_id": TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}), "read"),
    ("exlab_agent_status", "Inspect AI automation settings, recent plans/actions, provider availability, circuit breaker and active run.", obj(), "read"),
    ("exlab_agent_run", "Inspect a delegated or automatic agent run and its tool observations/actions; poll until completed/failed/interrupted.", obj({"run_id": TEXT}, ("run_id",)), "read"),
    ("exlab_agent_history", "Read bounded pages of AI reports; full reports are fetched only when opened.", obj(PAGE), "read"),
    ("exlab_rules", "Inspect a page of deterministic rules (default 5, max 8) and allowed exlab-rules-v1 fields/operators/effects. Follow next_offset for remaining rules. Rules never execute general code.", obj(PAGE), "read"),
    ("exlab_inspection", "Read a bounded page of rule findings and recent state transitions. Deterministic checks do not consume model calls.", obj(PAGE), "read"),
    ("exlab_rule_preview", "Validate a declarative rule and preview matching current observations without saving or executing actions.", obj({"document": ANY_OBJECT}, ("document",)), "read"),
    ("exlab_rule_save", "Create/update a bounded exlab-rules-v1 JSON rule using its revision. Fields: id/name/enabled/target(node|job)/when/for_seconds/cooldown_seconds/severity(info|warning|critical)/effect. Conditions: {all:[...]}, {any:[...]} or {field,op,value}. Read exlab_rules and preview first. AI can maintain only AI-authored rules. No arbitrary code, files, shell or network.",
     obj({"revision": INTEGER, "document": ANY_OBJECT}, ("revision", "document")), "automation"),
    ("exlab_rule_delete", "Delete a custom rule by id and revision. AI cannot modify human or built-in rules.", obj({"id": TEXT, "revision": INTEGER}, ("id", "revision")), "automation"),
    ("exlab_control", "Directly control ExLab using a durable request_id (reuse it after an uncertain response). Operations and exact payload schemas: " + json.dumps(OPERATIONS, separators=(",", ":")) + ". Commands are requests; inspect actual worker feedback. Never bypass stopped-process/checkpoint checks.",
     obj({"operation": {"enum": list(OPERATIONS)}, "arguments": ANY_OBJECT, "request_id": TEXT}, ("operation", "arguments", "request_id")), "control"),
    ("exlab_delegate", "Ask the system AI to investigate/plan/act and verify results autonomously. Queues a durable run; use exlab_agent_run to inspect it. Does not require automatic intervention to be enabled, but requires AI provider enabled.",
     obj({"instructions": {"type": "string", "minLength": 1, "maxLength": 8000}, "request_id": TEXT}, ("instructions", "request_id")), "delegate"),
    ("exlab_automation", "Enable/disable autonomous intervention or update its objectives, cadence and operation allowlist using the current revision. No per-action approval when enabled.",
     obj({"revision": INTEGER, "enabled": {"type": "boolean"}, "instructions": {"type": "string", "maxLength": 8000},
          "min_interval_seconds": {"type": "integer", "minimum": 10, "maximum": 86400},
          "review_seconds": {"type": "integer", "minimum": 60, "maximum": 86400},
          "rule_interval_seconds": {"type": "integer", "minimum": 5, "maximum": 3600},
          "allow_rule_authoring": {"type": "boolean"},
          "max_runs_per_hour": {"type": "integer", "minimum": 1, "maximum": 24},
          "budget_mode": {"enum": ["rounds", "calls"]},
          "max_turns": {"type": "integer", "minimum": 1, "maximum": 20},
          "max_actions": {"type": "integer", "minimum": 1, "maximum": 50},
          "max_calls_per_hour": {"type": "integer", "minimum": 1, "maximum": 480},
          "operations": {"type": "array", "items": {"enum": list(OPERATIONS)}, "maxItems": len(OPERATIONS)},
          "request_id": TEXT}, ("revision", "request_id")), "automation"),
]


def definitions(scopes):
    return [{"name": name, "description": description, "inputSchema": copy.deepcopy(schema),
             "annotations": {"readOnlyHint": scope == "read", "destructiveHint": scope != "read", "openWorldHint": False}}
            for name, description, schema, scope in TOOLS if scope in scopes]


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS agent_credentials (
            id TEXT PRIMARY KEY,name TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,scopes TEXT NOT NULL,
            created REAL NOT NULL,expires REAL,revoked REAL);
        CREATE TABLE IF NOT EXISTS agent_tool_receipts (
            actor TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,operation TEXT NOT NULL,
            arguments TEXT NOT NULL,state TEXT NOT NULL,result TEXT,created REAL NOT NULL,updated REAL NOT NULL,
            PRIMARY KEY(actor,request_id));
        CREATE TABLE IF NOT EXISTS agent_managed_policies (
            kind TEXT NOT NULL,target TEXT NOT NULL,baseline TEXT NOT NULL,current TEXT NOT NULL,
            PRIMARY KEY(kind,target));
        CREATE TABLE IF NOT EXISTS agent_managed_stops (
            job_id TEXT PRIMARY KEY,command_id INTEGER NOT NULL,generation INTEGER NOT NULL);
    """)


class AgentToolGateway:
    def __init__(self, hub):
        self.hub = hub

    def clean(self, value):
        config = AIAssist(self.hub)._config()
        return _redact(value, [config["api_key"], self.hub.config["admin_token"], *self.hub.config["nodes"].values(),
                              *(v.get("token", "") for v in self.hub.config.get("centers", {}).values())])

    def authenticate(self, header):
        from .hub import APIError
        try:
            role, identity = self.hub.authenticate(header)
            if role in ("admin", "center"):
                return {"id": role + ":" + str(identity or "local"), "scopes": sorted(SCOPES), "admin": role == "admin"}
        except APIError as error:
            if error.status != 401:
                raise
        if not isinstance(header, str) or not header.startswith("Bearer ") or len(header) > 520:
            raise APIError(401, "Agent bearer credential required")
        digest = hashlib.sha256(header[7:].encode()).hexdigest()
        with self.hub.lock:
            row = self.hub.db.execute("SELECT * FROM agent_credentials WHERE token_hash=?", (digest,)).fetchone()
            if row and row["revoked"] is None and (row["expires"] is None or row["expires"] > now()):
                return {"id": row["id"], "scopes": json.loads(row["scopes"]), "admin": False}
        raise APIError(401, "Agent credential expired, revoked or invalid")

    def credentials(self, payload=None):
        if payload is not None:
            validate(payload, obj({"name": {"type": "string", "minLength": 1, "maxLength": 80},
                "scopes": {"type": "array", "items": {"enum": sorted(SCOPES)}, "minItems": 1, "maxItems": 4},
                "days": {"type": "integer", "minimum": 0, "maximum": 365}}, ("name", "scopes")))
            token, identity = secrets.token_urlsafe(40), uuid.uuid4().hex
            with self.hub.transaction():
                self.hub.db.execute("INSERT INTO agent_credentials VALUES (?,?,?,?,?,?,NULL)", (identity, payload["name"],
                    hashlib.sha256(token.encode()).hexdigest(), json.dumps(sorted(set(payload["scopes"]) | {"read"})),
                    now(), now() + payload["days"] * 86400 if payload.get("days") else None))
            return {"id": identity, "credential": token, "note": "仅此一次显示；可独立吊销，不是管理员凭证"}
        with self.hub.lock:
            rows = self.hub.db.execute("SELECT id,name,scopes,created,expires,revoked FROM agent_credentials ORDER BY created DESC").fetchall()
            return {"credentials": [{**dict(row), "scopes": json.loads(row["scopes"])} for row in rows]}

    def revoke(self, payload):
        validate(payload, obj({"id": TEXT}, ("id",)))
        with self.hub.transaction():
            self.hub.db.execute("UPDATE agent_credentials SET revoked=? WHERE id=?", (now(), payload["id"]))
        return {"revoked": True}

    def topology(self, arguments):
        with self.hub.lock:
            state = self.hub.state()
            offset, limit = arguments.get("offset", 0), arguments.get("limit", 100)
            jobs = state["jobs"]
            # Active work comes first; completed jobs remain available through pagination/detail.
            jobs.sort(key=lambda j: (j["state"] in ("succeeded", "canceled"), -j["updated"]))
            selected = [{key: j.get(key) for key in ("id", "node_id", "state", "detail", "created", "updated", "attempt",
                "generation", "command_id", "command_ack", "action", "execution_state", "handoff", "queue_control", "timing", "metrics")}
                | {"spec": {key: j["spec"].get(key) for key in ("name", "algorithm", "project_bundle_id", "experiment_id", "backend", "resources", "scheduling", "planning", "resume_supported")}}
                for j in jobs[offset:offset+limit]]
            # Topology is a summary. Full execution diagnostics stay queryable per job.
            for job in selected:
                execution = job.get("execution_state") or {}
                job["execution_state"] = {k: execution.get(k) for k in ("ever_started", "stopped_confirmed", "release", "checkpoint")}
                job["execution_state"]["progress"] = {k: execution.get("progress", {}).get(k) for k in ("step", "epoch", "total_steps") if k in execution.get("progress", {})}
                job["metrics"] = dict(list((job.get("metrics") or {}).items())[:12])
            nodes = [{key: n.get(key) for key in ("id", "display_name", "mode", "online", "last_seen", "compute_policy", "resource_policy")}
                | {"snapshot": {key: n["snapshot"].get(key) for key in ("capabilities", "local_time", "ac_power", "local_compute_policy", "availability", "compute_control",
                     "compute_policy_revision", "resource_policy_revision", "resource_policy_error", "policy", "gpus", "delivery", "execution_delivery", "free_ram_mb", "disk_free_mb", "assets", "profiles")}}
                for n in state["nodes"]]
            result = {"time": state["time"], "center": self.hub.center_info(), "nodes": nodes,
                      "job_total": len(jobs), "job_offset": offset, "next_offset": offset+limit if offset+limit < len(jobs) else None,
                      "job_counts": {s: sum(j["state"] == s for j in jobs) for s in sorted({j["state"] for j in jobs})}, "jobs": selected,
                      "pending_handoff_count": self.hub.db.execute("SELECT COUNT(*) FROM execution_handoffs WHERE status IN ('requested','waiting_checkpoint','waiting_capacity')").fetchone()[0],
                      "operation_schemas": OPERATIONS}
            control_states = []
            for job in sorted(jobs, key=lambda j: j["id"]):
                value = {k: job.get(k) for k in ("id", "state", "node_id", "generation", "attempt", "command_id", "command_ack", "queue_control", "delivery")}
                value["handoff"] = {k: v for k, v in (job.get("handoff") or {}).items() if k != "updated"}
                value.update(checkpoint=(job.get("execution_state") or {}).get("checkpoint"), stopped=(job.get("execution_state") or {}).get("stopped_confirmed"))
                control_states.append(value)
            result["queue_signature"] = hashlib.sha256(json.dumps(control_states, sort_keys=True).encode()).hexdigest()
            catalog = [[p["digest"], p.get("deleted_at"), [[d["node_id"], d["revision"], d["status"]] for d in p.get("deployments", [])]]
                       for p in state["projects"] + state["project_deletions"]]
            catalog.append([list(row) for row in self.hub.db.execute("SELECT id,revision,deleted_at FROM matrices ORDER BY id")])
            result["catalog_signature"] = hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest()
            return self.clean(result)

    def _automatic_guard(self, operation, arguments):
        """Automatic management never relaxes a human's active personal-use policy."""
        from .hub import APIError
        if operation == "availability":
            from .compute_policy import validate as compute, evaluate, MODES
            current = self.hub._compute_policy(arguments["node_id"])
            old = self._baseline(operation, arguments, current)
            desired = compute(arguments["policy"])
            if old:
                # Evaluate personal-use modes with AC present; an unplugged laptop must
                # not hide the fact that a proposed policy removes a gaming release.
                before, after = evaluate(old, {"ac_power": True}), evaluate(desired, {"ac_power": True})
                if ((MODES[after["mode"]] < MODES[before["mode"]])
                        or (not before["available"] and (old["until"] is None and desired["until"] is not None
                            or old["until"] is not None and desired["until"] is not None and desired["until"] < old["until"]))
                        or old["require_ac"] and not desired["require_ac"]
                        or any(window not in desired["maintenance"] for window in old["maintenance"])):
                    raise APIError(409, "自动托管不能解除手动释放、插电要求或维护限制")
        if operation == "node_mode":
            current = self.hub.db.execute("SELECT mode FROM nodes WHERE id=?", (arguments["node_id"],)).fetchone()
            baseline = self._baseline(operation, arguments, current[0] if current else None)
            if arguments["mode"] == "run" and baseline == "drain":
                raise APIError(409, "自动托管不能解除人工暂停接单")
        if operation == "queue":
            current = self.hub.job(arguments["job_id"])["queue_control"]
            baseline = self._baseline(operation, arguments, current)
            if baseline.get("hold") and not arguments["hold"]:
                raise APIError(409, "自动托管不能解除已暂缓的队列任务")
        if operation in ("resume", "pause", "handoff"):
            row = self.hub._find_job(arguments["job_id"])
            spec = json.loads(row["spec"])
            state = json.loads(row["execution_state"]) if row["execution_state"] else {}
            managed = self.hub.db.execute("SELECT command_id,generation FROM agent_managed_stops WHERE job_id=?", (row["id"],)).fetchone()
            own_stop = managed and managed["command_id"] == row["command_id"] and managed["generation"] == row["generation"]
            if operation == "handoff":
                canceled = self.hub.db.execute("SELECT status FROM execution_handoffs WHERE job_id=?", (row["id"],)).fetchone()
                if canceled and canceled[0] == "canceled":
                    raise APIError(409, "自动托管不能重启用户取消的交接")
                if row["state"] in ("failed", "canceled", "succeeded") or (row["state"] in ("paused", "interrupted") and not state.get("release") and not own_stop):
                    raise APIError(409, "不能自动接手人工停止或已结束的实验")
                if state.get("ever_started") and spec.get("resume_supported") is not True and spec.get("backend") != "demo":
                    raise APIError(409, "无续训能力的实验不能被自动迁移")
            elif operation == "resume":
                handoff = self.hub.db.execute("SELECT status FROM execution_handoffs WHERE job_id=?", (row["id"],)).fetchone()
                if (not state.get("release") and not own_stop) or handoff and handoff[0] == "canceled":
                    raise APIError(409, "自动托管不能恢复用户手动停止的实验")
                if not state.get("stopped_confirmed") or (state.get("ever_started") and (not state.get("checkpoint")
                        or spec.get("resume_supported") is not True and spec.get("backend") != "demo")):
                    raise APIError(409, "等待停止确认和真实续训检查点")
            else:
                if row["state"] not in ("assigned", "preparing", "ready", "starting", "running") or row["command_id"] > row["command_ack"]:
                    raise APIError(409, "等待当前指令完成；不能接管人工停止的实验")
                if spec.get("resume_supported") is not True and spec.get("backend") != "demo":
                    raise APIError(409, "无续训能力的运行中实验不能被自动抢占")

    def _baseline(self, operation, arguments, current):
        target = arguments.get("node_id", arguments.get("job_id"))
        row = self.hub.db.execute("SELECT baseline,current FROM agent_managed_policies WHERE kind=? AND target=?", (operation, target)).fetchone()
        return json.loads(row["baseline"]) if row and json.loads(row["current"]) == current else current

    def _policy_value(self, operation, arguments):
        if operation == "availability":
            return self.hub._compute_policy(arguments["node_id"])
        if operation == "queue":
            return self.hub.job(arguments["job_id"])["queue_control"]
        row = self.hub.db.execute("SELECT mode FROM nodes WHERE id=?", (arguments["node_id"],)).fetchone()
        return row[0] if row else None

    def operate(self, operation, arguments, request_id, actor, automatic=False, check=None):
        from .hub import APIError
        if operation not in OPERATIONS:
            raise ValueError("Unknown operation")
        validate(arguments, OPERATIONS[operation])
        request_id = _text(request_id, "request_id", 200)
        original = json.dumps(arguments, sort_keys=True, allow_nan=False)
        fingerprint = hashlib.sha256((operation + original).encode()).hexdigest()
        key = (actor["id"], request_id)
        # A reservation survives a crash. An uncertain action is inspected, never blindly replayed.
        with self.hub.lock:
            if check:
                check()
            previous = self.hub.db.execute("SELECT * FROM agent_tool_receipts WHERE actor=? AND request_id=?", key).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise APIError(409, "request_id already identifies a different action")
                return json.loads(previous["result"]) if previous["result"] else {"status": "uncertain", "request_id": request_id,
                    "message": "上次操作结果未确认；先读取当前状态核验，不重复发起同一动作"}
            if automatic:
                self._automatic_guard(operation, arguments)
            tracked = automatic and operation in ("availability", "queue", "node_mode")
            baseline = self._baseline(operation, arguments, self._policy_value(operation, arguments)) if tracked else None
            with self.hub.transaction():
                self.hub.db.execute("INSERT INTO agent_tool_receipts VALUES (?,?,?,?,?,'pending',NULL,?,?)",
                    (*key, fingerprint, operation, json.dumps(self.clean(arguments), ensure_ascii=False), now(), now()))
            try:
                if check:
                    check()
                body = copy.deepcopy(arguments)
                if operation in ("pause", "resume", "cancel"):
                    value = self.hub.action({**body, "action": "stop" if operation == "pause" else operation})
                else:
                    if operation in ("submit", "matrix_start"):
                        body["request_id"] = "agent:" + hashlib.sha256((key[0] + ":" + key[1]).encode()).hexdigest()
                    method = {"queue": "set_queue_control", "handoff": "request_handoff", "availability": "set_compute_policy",
                        "node_mode": "set_mode", "resources": "set_node_policy", "submit": "submit",
                        "matrix_start": "matrix_start", "project_deploy": "project_deploy"}[operation]
                    value = getattr(self.hub, method)(body)
                result = {"status": "requested", "operation": operation, "request_id": request_id, "result": self.clean(value),
                          "verification": "读取任务/节点状态及 command_ack、generation、策略确认；请求成功不代表节点已完成操作"}
                if tracked:
                    self.hub.db.execute("INSERT OR REPLACE INTO agent_managed_policies VALUES (?,?,?,?)", (operation,
                        arguments.get("node_id", arguments.get("job_id")), json.dumps(baseline), json.dumps(self._policy_value(operation, arguments))))
                if automatic and operation == "pause":
                    self.hub.db.execute("INSERT OR REPLACE INTO agent_managed_stops VALUES (?,?,?)", (arguments["job_id"], value["command_id"],
                        self.hub._find_job(arguments["job_id"])["generation"]))
            except (ValueError, TypeError, APIError) as error:
                result = {"status": "rejected", "operation": operation, "error": self.clean(str(error)), "code": getattr(error, "status", 400)}
            self.hub.db.execute("UPDATE agent_tool_receipts SET state=?,result=?,updated=? WHERE actor=? AND request_id=?",
                (result["status"], json.dumps(result, ensure_ascii=False), now(), *key))
            return result

    def call(self, name, arguments, actor, automatic=False, check=None):
        from .hub import APIError
        tool = next((t for t in TOOLS if t[0] == name), None)
        if not tool:
            raise ValueError("Unknown agent tool")
        if tool[3] not in actor["scopes"]:
            raise APIError(403, "Agent credential lacks scope: " + tool[3])
        validate(arguments, tool[2])
        if len(json.dumps(arguments, allow_nan=False).encode()) > 256 * 1024:
            raise ValueError("Agent tool input exceeds 256 KiB")
        with self.hub.lock:
            if check:
                check()
            if name == "exlab_topology":
                return self.topology(arguments)
            if name == "exlab_agent_history":
                return self.hub.autonomy.history(arguments.get("offset", 0), min(20, arguments.get("limit", 5)))
            if name == "exlab_rules":
                return self.hub.autonomy.rules.listing(arguments.get("offset", 0), min(8, arguments.get("limit", 5)))
            if name == "exlab_inspection":
                return self.hub.autonomy.rules.report(arguments.get("offset", 0), min(20, arguments.get("limit", 10)))
            if name == "exlab_rule_preview":
                return self.hub.autonomy.rules.preview(arguments["document"])
            if name == "exlab_rule_save":
                return self.hub.autonomy.rules.save(arguments, actor, automatic)
            if name == "exlab_rule_delete":
                return self.hub.autonomy.rules.delete(arguments, actor, automatic)
            if name == "exlab_job":
                value = self.hub.job(arguments["job_id"])
                result = {k: value.get(k) for k in ("id", "state", "node_id", "detail", "attempt", "generation", "command_id", "command_ack", "action", "time")}
                fields = arguments.get("fields", ["spec", "execution_state", "handoff", "queue_control", "execution_checkpoints", "runtime", "metrics", "timing", "events", "log_tail", "experiment_results", "experiment_progress", "evidence", "artifacts"])
                totals = {}
                for field in fields:
                    item = value.get(field)
                    if isinstance(item, list):
                        totals[field] = len(item)
                        if field == "events":
                            item = list(reversed(item))
                        offset, limit = arguments.get("offset", 0), arguments.get("limit", 20)
                        item = item[offset:offset+limit]
                    if field == "log_tail" and isinstance(item, str):
                        item = item[-arguments.get("log_chars", 8000):] if arguments.get("log_chars", 8000) else ""
                    result[field] = item
                result["array_totals"] = totals
                return self.clean(result)
            if name == "exlab_agent_status":
                return self.hub.autonomy.status()
            if name == "exlab_agent_run":
                return self.hub.autonomy.run_detail(arguments["run_id"])
            if name == "exlab_events":
                where, values = "event_id>?", [arguments.get("after", 0)]
                if arguments.get("job_id"):
                    where += " AND job_id=?"
                    values.append(arguments["job_id"])
                rows = self.hub.db.execute("SELECT * FROM events WHERE " + where + " ORDER BY event_id LIMIT ?", (*values, arguments.get("limit", 100))).fetchall()
                return self.clean({"events": [{**dict(row), "data": json.loads(row["data"])} for row in rows],
                    "cursor": rows[-1]["event_id"] if rows else arguments.get("after", 0)})
            if name == "exlab_catalog":
                kind, offset, limit = arguments["kind"], arguments.get("offset", 0), arguments.get("limit", 100)
                if kind == "results":
                    rows = self.hub.db.execute("SELECT job_id,attempt,experiment_id,summary FROM experiment_results ORDER BY received DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
                    return self.clean({"items": [{**{k: row[k] for k in ("job_id", "attempt", "experiment_id")}, "result": json.loads(row["summary"])} for row in rows],
                        "total": self.hub.db.execute("SELECT COUNT(*) FROM experiment_results").fetchone()[0]})
                state = self.hub.state() if kind == "projects" else self.hub.matrices()
                items = state.get(kind, [])
                return self.clean({"items": items[offset:offset+limit], "total": len(items)})
            if name == "exlab_control":
                return self.operate(arguments["operation"], arguments["arguments"], arguments["request_id"], actor, automatic, check)
            if name == "exlab_delegate":
                return self.hub.autonomy.enqueue(arguments["instructions"], actor["id"], arguments["request_id"])
            if name == "exlab_automation":
                if automatic:
                    raise APIError(403, "AI 不能改写自身托管授权和预算")
                return self.hub.autonomy.settings({k: v for k, v in arguments.items() if k != "request_id"})
        raise ValueError("Unhandled agent tool")
