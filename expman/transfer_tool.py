"""Review/migrate a scoped upload queue and request retained evidence via auth API."""
import argparse
import json
import time
import urllib.error
import uuid

from .common import api_request, read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Running controller URL")
    parser.add_argument("--hub-config", required=True, help="Existing hub.json; token is never printed")
    subs = parser.add_subparsers(dest="command", required=True)
    migrate = subs.add_parser("migrate")
    migrate.add_argument("--project-id", required=True)
    migrate.add_argument("--jobs", nargs="+", required=True)
    migrate.add_argument("--result-files", help="JSON file of explicit job_id/experiment_id/path mappings")
    migrate.add_argument("--request-id")
    migrate.add_argument("--apply-plan", help="Apply this completed dry-run ID (otherwise preview only)")
    evidence = subs.add_parser("evidence")
    evidence.add_argument("--job", required=True)
    evidence.add_argument("--attempt", type=int, required=True)
    evidence.add_argument("--experiment-id", required=True)
    evidence.add_argument("--ids", nargs="+", required=True)
    evidence.add_argument("--request-id")
    status = subs.add_parser("status")
    status.add_argument("--id", required=True)
    args = parser.parse_args(argv)
    token = read_json(args.hub_config)["admin_token"]
    origin = args.url.rstrip("/")
    if args.command == "status":
        from urllib.parse import quote
        response = api_request(origin + "/api/transfers/request?id=" + quote(args.id), token)
    elif args.command == "migrate":
        request_id = args.request_id or uuid.uuid4().hex
        if args.apply_plan:
            # Scope is checked against the reviewed plan, never against new positional arguments.
            from urllib.parse import quote
            plan = api_request(origin + "/api/transfers/request?id=" + quote(args.apply_plan), token)
            if plan["payload"].get("project_id") != args.project_id or set(plan["payload"].get("job_ids", [])) != set(args.jobs):
                raise ValueError("Requested project/jobs do not match the reviewed plan")
            payload = {"request_id": request_id, "action": "apply", "plan_id": args.apply_plan}
        else:
            payload = {"request_id": request_id, "action": "dry-run", "project_id": args.project_id,
                       "job_ids": args.jobs, "result_files": read_json(args.result_files) if args.result_files else []}
        response = api_request(origin + "/api/transfers/migrate", token, payload)
    else:
        response = api_request(origin + "/api/evidence/request", token, {"request_id": args.request_id or uuid.uuid4().hex,
            "job_id": args.job, "attempt": args.attempt, "experiment_id": args.experiment_id, "evidence_ids": args.ids})
    if args.command != "status":
        from urllib.parse import quote
        deadline = time.monotonic() + 10
        while response["status"] in ("pending", "running") and time.monotonic() < deadline:
            time.sleep(0.5)
            response = api_request(origin + "/api/transfers/request?id=" + quote(response["request_id"]), token)
    print(json.dumps(response, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        print("Transfer API rejected the request:", error.code, error.read(2000).decode("utf-8", errors="replace"))
        raise SystemExit(1)
