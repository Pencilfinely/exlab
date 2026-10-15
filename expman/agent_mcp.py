"""Authenticated agent HTTP tools and stateless MCP, with a dependency-free stdio bridge."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .agent_tools import definitions, obj, validate, TEXT

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


def http_request(hub, header, method, path, payload, query):
    from .hub import APIError
    with hub.lock:
        actor = hub.autonomy.gateway.authenticate(header)
        gateway = hub.autonomy.gateway
        if path in ("/api/agent/credentials", "/api/agent/credentials/revoke"):
            if not actor["admin"]:
                raise APIError(403, "仅主控管理员可签发或撤销 agent 凭证")
            if method == "GET" and path.endswith("credentials"):
                return gateway.credentials()
            if method == "POST":
                return gateway.revoke(payload) if path.endswith("revoke") else gateway.credentials(payload)
        if method == "GET":
            if path == "/api/agent/tools":
                return {"tools": definitions(actor["scopes"])}
            if path == "/api/agent/status":
                return gateway.call("exlab_agent_status", {}, actor)
            if path == "/api/agent/history":
                return gateway.call("exlab_agent_history", {key: int(query[key][0]) for key in ("offset", "limit") if key in query}, actor)
            if path == "/api/agent/inspection":
                return gateway.call("exlab_inspection", {key: int(query[key][0]) for key in ("offset", "limit") if key in query}, actor)
            if path == "/api/agent/rules":
                return gateway.call("exlab_rules", {key: int(query[key][0]) for key in ("offset", "limit") if key in query}, actor)
            if path == "/api/agent/run" and len(query.get("id", [])) == 1:
                return gateway.call("exlab_agent_run", {"run_id": query["id"][0]}, actor)
        if method == "POST":
            if path in ("/api/agent/rules/preview", "/api/agent/rules/save", "/api/agent/rules/delete"):
                name = {"preview": "exlab_rule_preview", "save": "exlab_rule_save", "delete": "exlab_rule_delete"}[path.rsplit("/", 1)[-1]]
                return gateway.call(name, payload, actor)
            if path == "/api/agent/call":
                validate(payload, obj({"name": TEXT, "arguments": {"type": "object"}}, ("name", "arguments")))
                return gateway.call(payload["name"], payload["arguments"], actor)
            if path == "/api/agent/settings":
                return gateway.call("exlab_automation", {**payload, "request_id": payload.get("request_id", uuid.uuid4().hex)}, actor)
            if path == "/api/agent/delegate":
                return gateway.call("exlab_delegate", payload, actor)
            if path == "/api/agent/interrupt":
                validate(payload, obj({"run_id": TEXT}, ("run_id",)))
                run = hub.autonomy.run_detail(payload["run_id"])
                if "automation" not in actor["scopes"] and ("delegate" not in actor["scopes"] or run["actor"] != actor["id"]):
                    raise APIError(403, "只能中止自己委托的任务")
                return hub.autonomy.cancel(payload["run_id"])
        raise APIError(404, "Unknown agent endpoint")


def validate_http(headers):
    """MCP requires Origin validation even for locally bound servers (DNS rebinding)."""
    from .hub import APIError
    origin = headers.get("Origin")
    if origin:
        parsed = urlsplit(origin)
        host = headers.get("Host", "")
        if (parsed.scheme not in ("http", "https") or parsed.netloc.lower() != host.lower()
                or parsed.path or parsed.query or parsed.fragment or parsed.username):
            raise APIError(403, "MCP Origin is not allowed")
    version = headers.get("MCP-Protocol-Version")
    if version and version not in PROTOCOLS[:2]:
        raise APIError(400, "Unsupported MCP protocol version")


def dispatch(hub, request, actor):
    from .hub import APIError
    identity = request.get("id")
    def error(code, message):
        return {"jsonrpc": "2.0", "id": identity, "error": {"code": code, "message": message}}
    if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str) or ("id" in request and (type(identity) not in (int, str))):
        return error(-32600, "Invalid JSON-RPC request")
    method, args = request["method"], request.get("params", {})
    if "id" not in request:
        return None  # stateless notifications have no response
    if not isinstance(args, dict):
        return error(-32602, "params must be an object")
    try:
        if method == "initialize":
            result = {"protocolVersion": args.get("protocolVersion") if args.get("protocolVersion") in PROTOCOLS else PROTOCOLS[0],
                      "capabilities": {"tools": {}, "resources": {}}, "serverInfo": {"name": "exlab", "version": __version__},
                      "instructions": "Use scoped tools to inspect topology, operate directly or delegate to ExLab AI. Verify worker feedback after control requests."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": definitions(actor["scopes"])}
        elif method == "tools/call":
            validate(args, obj({"name": TEXT, "arguments": {"type": "object"}, "_meta": {"type": "object"}}, ("name",)))
            value = hub.autonomy.gateway.call(args["name"], args.get("arguments", {}), actor)
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, allow_nan=False)}],
                      "structuredContent": value, "isError": value.get("status") == "rejected"}
        elif method == "resources/list":
            result = {"resources": [{"uri": "exlab://topology", "name": "Experiment pool topology", "mimeType": "application/json"},
                                    {"uri": "exlab://agent", "name": "AI manager status", "mimeType": "application/json"}]}
        elif method == "resources/read":
            name = {"exlab://topology": "exlab_topology", "exlab://agent": "exlab_agent_status"}.get(args.get("uri"))
            if not name:
                return error(-32602, "Unknown resource URI")
            value = hub.autonomy.gateway.call(name, {}, actor)
            result = {"contents": [{"uri": args["uri"], "mimeType": "application/json", "text": json.dumps(value, ensure_ascii=False)}]}
        else:
            return error(-32601, "Method not found")
    except (ValueError, TypeError, APIError) as exc:
        if method == "tools/call":
            result = {"content": [{"type": "text", "text": hub.autonomy.gateway.clean(str(exc))}], "isError": True}
        else:
            return error(-32602, hub.autonomy.gateway.clean(str(exc)))
    return {"jsonrpc": "2.0", "id": identity, "result": result}


def main(argv=None):
    parser = argparse.ArgumentParser(description="ExLab MCP stdio bridge (stdout is JSON-RPC only)")
    parser.add_argument("--url", required=True, help="Center base URL, e.g. http://127.0.0.1:8765")
    parser.add_argument("--credential-file", type=Path, help="File containing an independent agent credential; otherwise EXLAB_AGENT_TOKEN")
    options = parser.parse_args(argv)
    endpoint = options.url.rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        parser.error("Invalid Center URL")
    credential = options.credential_file.read_text(encoding="utf-8").strip() if options.credential_file else os.environ.get("EXLAB_AGENT_TOKEN", "").strip()
    if not credential or any(ord(c) < 32 for c in credential):
        parser.error("Set EXLAB_AGENT_TOKEN or --credential-file")
    from .ai_assist import _NoRedirect
    opener = urllib.request.build_opener(_NoRedirect)
    source = getattr(sys.stdin, "buffer", sys.stdin)
    for line in iter(lambda: source.readline(2 * 1024 * 1024 + 1), b"" if hasattr(sys.stdin, "buffer") else ""):
        identity = None
        notification = False
        try:
            if len(line) > 2 * 1024 * 1024:
                raise ValueError("Request too large")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("JSON-RPC object required")
            identity = request.get("id")
            notification = "id" not in request
            raw = json.dumps(request, allow_nan=False).encode("utf-8")
            with opener.open(urllib.request.Request(endpoint + "/mcp", raw, headers={"Authorization": "Bearer " + credential,
                    "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOLS[0]}), timeout=30) as response:
                data = response.read(4 * 1024 * 1024 + 1)
                if not notification and data:
                    value = json.loads(data)
                elif notification:
                    continue
                else:
                    raise ValueError("Empty MCP response")
        except (ValueError, OSError, urllib.error.HTTPError):
            if notification:
                continue
            value = {"jsonrpc": "2.0", "id": identity, "error": {"code": -32000, "message": "ExLab MCP unavailable; check Center address and agent credential"}}
        sys.stdout.write(json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
