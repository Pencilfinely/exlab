"""Per-attempt lease communication between the worker and the container watchdog."""
import os
from importlib import resources
import re
import stat

from . import common

CAPABILITY = "docker-execution-lease-v1"


def directory(agent, record):
    return agent.root / "execution-leases" / record["id"] / f"{record.get('generation', 1)}-{record['attempt']}"


def prepare(agent, record, image):
    folder = directory(agent, record)
    watch, grants = folder / "watch", folder / "grants"
    watch.mkdir(parents=True, exist_ok=True)
    grants.mkdir(parents=True, exist_ok=True)
    (grants / "guard.py").write_bytes(resources.files("expman").joinpath("lease_guard.py").read_bytes())
    entry = image.get("Config", {}).get("Entrypoint") or []
    command = ["-I", "-S", "/exlab-grants/guard.py", str(record["spec"]["planning"]["lease_seconds"]),
               *entry, *record["spec"]["command"]]
    return [(watch, "/exlab-watch", False), (grants, "/exlab-grants", True)], command


def challenges(agent):
    pending = {}
    for record in agent.records(("starting", "running")):
        if not record["spec"].get("planning", {}).get("lease_seconds") or record.get("execution_retired"):
            continue
        file = directory(agent, record) / "watch" / "challenge"
        try:
            fd = os.open(file, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                value = stream.read(33)
            if re.fullmatch(b"[0-9a-f]{32}", value):
                pending[record["id"]] = (record, value.decode("ascii"))
        except OSError:
            continue
    return pending


def deliver(agent, job, pending):
    item = pending.get(job["id"])
    if not item or job.get("lease_granted") is not True:
        return
    record, nonce = item
    if (record.get("generation", 1), record["attempt"]) == (job["generation"], job["attempt"]):
        common.atomic_json(directory(agent, record) / "grants" / "grant.json", nonce)
