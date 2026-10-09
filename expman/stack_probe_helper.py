"""Fixed read-only sampler entrypoint. Runs in a disposable target PID namespace."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

LIMIT = 3500


def clean(value, size=90):
    return re.sub(r"[^a-zA-Z0-9_.<>:/ +\-]", "?", str(value))[:size]


def summarize(traces):
    result = []
    if not isinstance(traces, list):
        return result
    for trace in traces[:8]:
        if not isinstance(trace, dict):
            continue
        frames = []
        for frame in (trace.get("frames") or [])[:10]:
            if not isinstance(frame, dict):
                continue
            filename = str(frame.get("filename") or "").replace("\\", "/").rsplit("/", 1)[-1]
            line = frame.get("line")
            frames.append({"file": clean(filename), "function": clean(frame.get("name", "")),
                           "line": line if type(line) is int and 0 <= line < 10000000 else None})
        result.append({"active": trace.get("active") is True,
                       "owns_gil": trace.get("owns_gil") is True, "frames": frames})
    return result


def candidates(proc=Path("/proc")):
    own = os.getpid()
    rows = {}
    for path in list(proc.glob("[0-9]*/stat"))[:4096]:
        try:
            raw = path.read_text()
            pid = int(path.parent.name)
            end = raw.rindex(")")
            fields = raw[end + 2:].split()
            rows[pid] = {"pid": pid, "ppid": int(fields[1]), "state": fields[0],
                         "comm": raw[raw.index("(") + 1:end],
                         "ticks": int(fields[11]) + int(fields[12]), "start": int(fields[19])}
        except (OSError, ValueError, IndexError):
            continue
    def owned(row):
        seen = set()
        while row["pid"] != 1:
            if row["pid"] == own or row["pid"] in seen:
                return False
            seen.add(row["pid"])
            row = rows.get(row["ppid"])
            if row is None:
                return False
        return True
    found = [r for r in rows.values() if r["pid"] != own and re.fullmatch(r"python[0-9.]*", r["comm"]) and owned(r)]
    return sorted(found, key=lambda r: (r["state"] == "R", r["ticks"]), reverse=True)[:1]


def main():
    # No signal, local variables, environment, command line or native-stack mode.
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024, 512 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    output = {"mode": "python-stack-nonblocking-v1", "processes": []}
    for row in candidates():
        item = {"pid_in_container": row["pid"], "start_ticks": row["start"]}
        with tempfile.TemporaryFile() as stream:
            try:
                reply = subprocess.run(["/probe/py-spy", "dump", "--pid", str(row["pid"]),
                                        "--json", "--nonblocking"],
                                       stdin=subprocess.DEVNULL, stdout=stream,
                                       stderr=subprocess.DEVNULL, timeout=5, check=False)
                stream.seek(0)
                raw = stream.read(512 * 1024)
                # PID reuse during sampling invalidates this sample.
                stat = Path("/proc") / str(row["pid"]) / "stat"
                current = stat.read_text()
                if int(current[current.rindex(")") + 2:].split()[19]) != row["start"]:
                    raise ValueError("Process changed")
                if reply.returncode:
                    item["error"] = "sampling_failed"
                else:
                    item["threads"] = summarize(json.loads(raw))
            except subprocess.TimeoutExpired:
                item["error"] = "timeout"
            except (OSError, ValueError, IndexError):
                item["error"] = "sampling_failed"
        output["processes"].append(item)
    if not output["processes"]:
        output["error"] = "no_owned_python_process"
    data = json.dumps(output, ensure_ascii=True, separators=(",", ":"))
    # Keep a valid JSON object; never truncate in the middle of a trace.
    while len(data.encode()) > LIMIT:
        changed = False
        for item in output["processes"]:
            threads = item.get("threads", [])
            if threads:
                if len(threads) > 1:
                    threads.pop()
                elif threads[0]["frames"]:
                    threads[0]["frames"].pop()
                else:
                    item.pop("threads")
                changed = True
                break
        output["truncated"] = True
        if not changed:
            output = {"mode": output["mode"], "error": "sample_too_large"}
        data = json.dumps(output, ensure_ascii=True, separators=(",", ":"))
    print(data, flush=True)


if __name__ == "__main__":
    main()
