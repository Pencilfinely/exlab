"""Container PID 1 for leased Docker jobs. Runs using only the image's Python stdlib."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time


def boot_clock():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def supervise(watch, grants, seconds, argv, clock=boot_clock):
    # A grant acknowledges a fresh challenge. Its lifetime starts when this
    # process issued the challenge, even if the response arrives much later.
    child = None
    deadline = clock() + seconds
    nonce = None
    try:
        while clock() < deadline:
            if child is not None and child.poll() is not None:
                return child.returncode if child.returncode >= 0 else 128 - child.returncode
            if nonce is None:
                issued = clock()
                nonce = secrets.token_hex(16)
                tmp = watch / "challenge.tmp"
                tmp.write_text(nonce, encoding="ascii")
                os.replace(tmp, watch / "challenge")
            try:
                granted = json.loads((grants / "grant.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                granted = None
            if granted == nonce and clock() < min(deadline, issued + seconds):
                deadline = issued + seconds
                if child is None:
                    child = subprocess.Popen(argv)
                nonce = None
            time.sleep(0.2)
        (watch / "expired").touch()
        return 75
    finally:
        if child is not None and child.poll() is None:
            child.kill()
        # Exiting PID 1 also terminates descendants in this container's PID namespace.


if __name__ == "__main__":
    sys.exit(supervise(Path("/exlab-watch"), Path("/exlab-grants"), int(sys.argv[1]), sys.argv[2:]))
