#!/usr/bin/env bash
# In-place migration: install into ExLab-owned paths, never replace Desktop CLI.
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
apt-get update
apt-get install -y ca-certificates curl gnupg iptables systemd systemd-sysv dbus-user-session
install -m 0755 -d /etc/apt/keyrings
if ! grep -Rqs 'download.docker.com/linux/ubuntu' /etc/apt/sources.list /etc/apt/sources.list.d; then
curl --fail --silent --show-error --location --retry 3 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/exlab-docker.asc
chmod a+r /etc/apt/keyrings/exlab-docker.asc
printf 'deb [arch=amd64 signed-by=/etc/apt/keyrings/exlab-docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' "$VERSION_CODENAME" >/etc/apt/sources.list.d/exlab-docker.list
fi
if ! grep -Rqs 'nvidia.github.io/libnvidia-container/stable' /etc/apt/sources.list /etc/apt/sources.list.d; then
curl --fail --silent --show-error --location --retry 3 https://nvidia.github.io/libnvidia-container/gpgkey -o "$job_path/nvidia-key.asc"
gpg --batch --yes --dearmor -o /usr/share/keyrings/exlab-nvidia.gpg "$job_path/nvidia-key.asc"
curl --fail --silent --show-error --location --retry 3 https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list -o /etc/apt/sources.list.d/exlab-nvidia.list
sed -i 's#deb https://#deb [signed-by=/usr/share/keyrings/exlab-nvidia.gpg] https://#g' /etc/apt/sources.list.d/exlab-nvidia.list
fi
apt-get update
apt-get install -y nvidia-container-toolkit
# apt authenticates the package hashes against signed repository metadata.
# Extraction avoids replacing distro/ Desktop services and executables.
install -m 0755 -d /opt/exlab/docker/packages
cd /opt/exlab/docker/packages
apt-get download docker-ce docker-ce-cli containerd.io docker-buildx-plugin
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
