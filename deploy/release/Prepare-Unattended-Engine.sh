#!/usr/bin/env bash
# Runs only in the newly imported, marked clone, before systemd is enabled.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
[[ "$(id -u)" == 0 && $# == 2 ]] || exit 2
job_id="$1"
exec 9>"$2/operation.lock"
flock -n 9 || exit 2
python3 - "$job_id" <<'PY'
import json, pathlib, re, sys
p = pathlib.Path('/etc/exlab-migration.json')
m = json.loads(p.read_text())
assert re.fullmatch('[a-f0-9]{32}', sys.argv[1]) and m['job_id'] == sys.argv[1], 'Wrong migration owner'
assert pathlib.Path('/proc/1/comm').read_text().strip() != 'systemd', 'Clone was booted before preparation'
assert m['linux_user'] != 'root' and re.fullmatch('[a-z_][a-z0-9_-]*', m['linux_user'])
device = pathlib.Path('/').stat().st_dev
for name in ('/etc', '/etc/apt', '/etc/apt/sources.list.d', '/etc/apt/keyrings', '/etc/docker',
             '/etc/docker/daemon.json', '/etc/containerd', '/etc/containerd/config.toml',
             '/usr/bin', '/usr/local/bin', '/usr/sbin', '/usr/sbin/policy-rc.d',
             '/etc/wsl.conf', '/usr/share/keyrings'):
    target = pathlib.Path(name)
    assert not target.exists() or target.stat().st_dev == device, 'Shared system path: ' + name
p.chmod(0o644)
PY
linux_user="$(python3 -c 'import json; print(json.load(open("/etc/exlab-migration.json"))["linux_user"])')"
id "$linux_user" >/dev/null
. /etc/os-release
[[ "$ID" == ubuntu && "$(dpkg --print-architecture)" == amd64 ]] || exit 2
case "$VERSION_ID" in 22.04|24.04|26.04|26.10) ;; *) exit 2 ;; esac

# Prevent package scripts starting daemons until this clone is configured.
if [[ -e /usr/sbin/policy-rc.d || -L /usr/sbin/policy-rc.d ]]; then
    cp -a /usr/sbin/policy-rc.d /etc/exlab-policy-rc.d.before-migration
fi
printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d
apt-get update
apt-get install -y ca-certificates curl gnupg systemd systemd-sysv dbus-user-session
conflicts=()
for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
    if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then conflicts+=("$package"); fi
done
if (( ${#conflicts[@]} )); then apt-get remove -y "${conflicts[@]}"; fi

# Desktop injects symlinks into distributions. Remove only its known CLI links
# in this clone; the source distribution and Desktop settings remain untouched.
python3 <<'PY'
from pathlib import Path
for parent in ('/usr/bin', '/usr/local/bin'):
    for name in ('docker', 'docker-compose'):
        p = Path(parent) / name
        if p.is_symlink() and str(p.resolve()).startswith('/mnt/wsl/docker-desktop/'):
            p.unlink()
for name in ('/run/docker.sock', '/var/run/docker.sock'):
    p = Path(name)
    if p.is_symlink() and str(p.resolve()).startswith('/mnt/wsl/docker-desktop/'):
        p.unlink()
PY
install -m 0755 -d /etc/apt/keyrings
python3 <<'PY'
from pathlib import Path
for name in ('docker.list', 'docker.sources', 'nvidia-container-toolkit.list', 'libnvidia-container.list'):
    p = Path('/etc/apt/sources.list.d') / name
    if p.is_file() and any(host in p.read_text() for host in ('download.docker.com', 'nvidia.github.io/libnvidia-container')):
        p.rename(p.with_name(p.name + '.before-exlab'))
PY
curl --fail --silent --show-error --location --retry 3 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
printf 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' "$VERSION_CODENAME" >/etc/apt/sources.list.d/exlab-docker.list
curl --fail --silent --show-error --location --retry 3 https://nvidia.github.io/libnvidia-container/gpgkey -o /tmp/exlab-nvidia-key.asc
gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg /tmp/exlab-nvidia-key.asc
curl --fail --silent --show-error --location --retry 3 https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list -o /etc/apt/sources.list.d/exlab-nvidia.list
sed -i 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' /etc/apt/sources.list.d/exlab-nvidia.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin nvidia-container-toolkit
usermod -aG docker "$linux_user"
mkdir -p /etc/docker
if [[ -e /etc/docker/daemon.json ]]; then cp -a /etc/docker/daemon.json /etc/docker/daemon.json.before-exlab; fi
printf '{"data-root":"/var/lib/exlab-native-docker","log-driver":"local"}\n' >/etc/docker/daemon.json
mkdir -p /etc/containerd
if [[ -e /etc/containerd/config.toml ]]; then cp -a /etc/containerd/config.toml /etc/containerd/config.toml.before-exlab; fi
printf 'version = 2\nroot = "/var/lib/exlab-native-containerd"\nstate = "/run/containerd"\n' >/etc/containerd/config.toml
nvidia-ctk runtime configure --runtime=docker
systemctl enable docker.service containerd.service

PYTHONPATH="$(dirname -- "$0")" python3 <<'PY'
import configparser, hashlib, json, pathlib, pwd
import os
from expman.worker_migration import docker_client_config
m = json.loads(pathlib.Path('/etc/exlab-migration.json').read_text())
user = pwd.getpwnam(m['linux_user'])
home = pathlib.Path(user.pw_dir)
assert home.stat().st_dev == pathlib.Path('/').stat().st_dev, 'User home is shared with source'
docker = home / '.docker'
client = docker / 'config.json'
old = json.loads(client.read_text()) if client.is_file() else {}
# Detach shared Desktop links before writing so the source's Windows Docker
# configuration can never be changed through a symlink in the clone.
if docker.is_symlink(): docker.unlink()
docker.mkdir(mode=0o700, exist_ok=True)
os.chown(docker, user.pw_uid, user.pw_gid)
if client.is_symlink(): client.unlink()
client.write_text(json.dumps(docker_client_config(old)))
client.chmod(0o600)
os.chown(client, user.pw_uid, user.pw_gid)
unit = 'experiment-manager-worker-' + hashlib.sha256(m['service_root'].encode()).hexdigest()[:12] + '.service'
folder = pathlib.Path(user.pw_dir) / '.config/systemd/user'
p = folder / unit
if p.exists() or p.is_symlink():
    p.rename(folder / (unit + '.before-migration'))
folder.mkdir(parents=True, exist_ok=True)
p.symlink_to('/dev/null')
for link in folder.glob('*.wants/' + unit):
    if link.is_symlink(): link.unlink()
config = configparser.ConfigParser(interpolation=None, strict=False)
config.read('/etc/wsl.conf')
config['boot']['systemd'] = 'true'
with open('/etc/wsl.conf', 'w') as out: config.write(out)
pathlib.Path('/etc/exlab-engine-prepared').write_text(m['job_id'])
PY
if [[ -e /etc/exlab-policy-rc.d.before-migration || -L /etc/exlab-policy-rc.d.before-migration ]]; then
    cp -a /etc/exlab-policy-rc.d.before-migration /usr/sbin/policy-rc.d
else
    rm -f /usr/sbin/policy-rc.d
fi
printf '\nExLab independent Docker prepared.\n'
