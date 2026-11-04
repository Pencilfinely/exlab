#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
export PYTHONPATH="$PWD"
args=()
if [[ -n "${1:-}" ]]; then args+=(--service-root "$1"); fi
if [[ -n "${2:-}" ]]; then args+=(--expected-engine "$2"); fi
exec python3 -m expman.worker_boot "${args[@]}"
