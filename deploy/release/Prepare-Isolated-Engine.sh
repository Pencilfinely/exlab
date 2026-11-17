#!/usr/bin/env bash
# In-place migration: install into ExLab-owned paths, never replace Desktop CLI.
run_step() {
    local step="$1" error code
    shift
    error="$(mktemp)" || return
    if "$@" 2>"$error"; then
        rm -f -- "$error"
    else
        code=$?
        python3 - "$job_path" "$step" "$code" "$error" <<'PY'
import sys
from expman.worker_engine import preparation_failure
preparation_failure(*sys.argv[1:])
PY
        rm -f -- "$error"
        return "$code"
    fi
}

fetch() {
    # These are idempotent GETs. curl's ordinary --retry excludes TLS resets.
    run_step "下载 $1" curl --fail --silent --show-error --location \
        --retry 4 --retry-all-errors --retry-delay 2 --retry-max-time 180 \
        --connect-timeout 15 --max-time 90 "$1" -o "$2"
}

# Allow the download/error contract to be tested without provisioning a host.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then return; fi
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export PYTHONPATH="$(dirname -- "$0")"
[[ "$(id -u)" == 0 && ( $# == 2 || $# == 3 ) ]] || exit 2
job_id="$1"
job_path="$2"
exec 9>"$job_path/operation.lock"
flock -n 9 || exit 2
if [[ "${3:-}" == rollback ]]; then
    python3 -m expman.worker_engine rollback --job "$job_path" --id "$job_id"
    exit
fi
python3 -m expman.worker_engine begin --job "$job_path" --id "$job_id"
. /etc/os-release
[[ "$ID" == ubuntu && "$(dpkg --print-architecture)" == amd64 ]] || exit 2
run_step '更新 Ubuntu 软件源' apt-get -o Acquire::Retries=3 update
run_step '安装引擎基础依赖' apt-get -o Acquire::Retries=3 install -y ca-certificates curl gnupg iptables systemd systemd-sysv dbus-user-session
install -m 0755 -d /etc/apt/keyrings
if ! grep -Rqs 'download.docker.com/linux/ubuntu' /etc/apt/sources.list /etc/apt/sources.list.d; then
fetch https://download.docker.com/linux/ubuntu/gpg /etc/apt/keyrings/exlab-docker.asc
chmod a+r /etc/apt/keyrings/exlab-docker.asc
printf 'deb [arch=amd64 signed-by=/etc/apt/keyrings/exlab-docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' "$VERSION_CODENAME" >/etc/apt/sources.list.d/exlab-docker.list
fi
if ! grep -Rqs 'nvidia.github.io/libnvidia-container/stable' /etc/apt/sources.list /etc/apt/sources.list.d; then
fetch https://nvidia.github.io/libnvidia-container/gpgkey "$job_path/nvidia-key.asc"
run_step '导入 NVIDIA 软件源签名密钥' gpg --batch --yes --dearmor -o /usr/share/keyrings/exlab-nvidia.gpg "$job_path/nvidia-key.asc"
fetch https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list /etc/apt/sources.list.d/exlab-nvidia.list
sed -i 's#deb https://#deb [signed-by=/usr/share/keyrings/exlab-nvidia.gpg] https://#g' /etc/apt/sources.list.d/exlab-nvidia.list
fi
run_step '更新 Docker / NVIDIA 软件源' apt-get -o Acquire::Retries=3 update
run_step '安装 NVIDIA 容器工具' apt-get -o Acquire::Retries=3 install -y nvidia-container-toolkit
# apt authenticates the package hashes against signed repository metadata.
# Extraction avoids replacing distro/ Desktop services and executables.
install -m 0755 -d /opt/exlab/docker/packages
cd /opt/exlab/docker/packages
run_step '下载独立 Docker 引擎' apt-get -o Acquire::Retries=3 download docker-ce docker-ce-cli containerd.io docker-buildx-plugin
for package in ./*.deb; do
    dpkg-deb --extract "$package" /opt/exlab/docker
    rm -f -- "$package"
done
cd /
rmdir /opt/exlab/docker/packages
getent group docker >/dev/null || groupadd --system docker
linux_user="$(python3 -c 'import json; print(json.load(open("/etc/exlab-migration.json"))["linux_user"])')"
usermod -aG docker "$linux_user"
python3 -m expman.worker_engine configure --job "$job_path" --id "$job_id"
printf '\nExLab isolated engine prepared.\n'
