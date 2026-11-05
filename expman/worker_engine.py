"""Privileged configuration transaction for the isolated ExLab Docker service.

Never replaces Desktop's executable/socket or copies/deletes experiment data.
"""
import argparse
import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

from . import common
from .worker_setup import private_write

MARKER = Path('/etc/exlab-migration.json')
SERVICE = 'exlab-docker.service'
ENDPOINT = 'unix:///run/exlab-docker/docker.sock'
ENGINE = Path('/opt/exlab/docker')
DATA = Path('/var/lib/exlab-docker')
CONFIG = Path('/etc/exlab-docker.json')
UNIT = Path('/etc/systemd/system') / SERVICE
WSL = Path('/etc/wsl.conf')


def capture(path):
    path = Path(path)
    if path.is_symlink():
        return {'kind': 'link', 'target': os.readlink(path)}
    if not path.exists():
        return {'kind': 'absent'}
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
        raise ValueError('配置不是受支持的小文件：' + str(path))
    return {'kind': 'file', 'data': base64.b64encode(path.read_bytes()).decode(),
            'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid}


def restore_file(path, entry):
    path = Path(path)
    if path.is_symlink():
        path.unlink()
    if entry['kind'] == 'absent':
        path.unlink(missing_ok=True)
    elif entry['kind'] == 'link':
        path.unlink(missing_ok=True)
        path.symlink_to(entry['target'])
    elif entry['kind'] == 'file':
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(entry['data'], validate=True))
        os.chmod(path, entry['mode'])
        if hasattr(os, 'chown'):
            os.chown(path, entry['uid'], entry['gid'])
    else:
        raise ValueError('未知配置备份格式')


def systemctl(*args):
    return subprocess.run(['systemctl', *args], capture_output=True, text=True, timeout=60)


def direct(path):
    """Privileged writes must not follow a user-controlled path alias."""
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('配置或引擎路径被重定向，已保留：' + str(path))
    return path


def context(job, identity):
    import pwd
    job = Path(job).resolve(strict=True)
    value = common.read_json(job / 'manifest.json')
    if (os.getuid() != 0 or not re.fullmatch('[a-f0-9]{32}', identity)
            or job.name != identity or value.get('job_id') != identity or value.get('mode') != 'in_place'):
        raise ValueError('迁移操作归属不符')
    user = pwd.getpwnam(value['linux_user'])
    if user.pw_uid == 0 or user.pw_uid != value['uid']:
        raise ValueError('原 Linux 用户已变化')
    config = Path(value['config'])
    if config.is_symlink() or config.stat().st_uid != user.pw_uid:
        raise ValueError('节点配置归属不符')
    unit = Path(user.pw_dir) / '.config/systemd/user' / ('experiment-manager-worker-' + hashlib.sha256(value['service_root'].encode()).hexdigest()[:12] + '.service')
    paths = [WSL, unit, config.parent / 'setup-state.json',
             Path('/etc/apt/sources.list.d/exlab-docker.list'), Path('/etc/apt/sources.list.d/exlab-nvidia.list'),
             Path('/etc/apt/keyrings/exlab-docker.asc'), Path('/usr/share/keyrings/exlab-nvidia.gpg')]
    for path in (*paths, MARKER, ENGINE, DATA, CONFIG, UNIT, Path(user.pw_dir) / '.config/exlab/docker'):
        direct(path)
    return job, value, user, paths


def begin(job, identity):
    job, value, user, paths = context(job, identity)
    backup_path = job / 'engine-before.json'
    if backup_path.exists():
        if common.read_json(backup_path).get('job_id') != identity or common.read_json(MARKER, {}).get('job_id') != identity:
            raise ValueError('已有配置备份不属于当前环境')
        return
    client = Path(user.pw_dir) / '.config/exlab/docker'
    if any(path.exists() or path.is_symlink() for path in (MARKER, ENGINE, DATA, CONFIG, UNIT, client)):
        raise ValueError('此 Ubuntu 已有 ExLab 引擎或未完成的环境切换，请先恢复')
    if not value['endpoint'].startswith('unix:///mnt/wsl/'):
        raise ValueError('无法保留独立于新引擎的 Desktop socket')
    private_write(backup_path, {'job_id': identity, 'files': {str(p): capture(p) for p in paths}})
    private_write(MARKER, {'job_id': identity, 'source_engine': value['engine_id'], 'service_root': value['service_root'],
                           'linux_user': value['linux_user'], 'mode': 'in_place'})
    MARKER.chmod(0o644)


def configure(job, identity):
    from .worker_migration import safe_wsl_conf, docker_client_config
    job, value, user, paths = context(job, identity)
    if common.read_json(MARKER).get('job_id') != identity:
        raise ValueError('引擎归属不符')
    for binary in ('docker', 'dockerd', 'containerd', 'runc'):
        if not (ENGINE / 'usr/bin' / binary).is_file():
            raise ValueError('独立引擎缺少组件：' + binary)
    daemon = {'hosts': [ENDPOINT], 'data-root': str(DATA), 'exec-root': '/run/exlab-docker/exec',
              'pidfile': '/run/exlab-docker/dockerd.pid', 'group': 'docker',
              'bridge': 'none', 'iptables': False, 'ip6tables': False, 'ip-masq': False,
              'builder': {'gc': {'enabled': True, 'defaultKeepStorage': '2GB'}},
              'log-driver': 'local', 'log-opts': {'max-size': '10m', 'max-file': '3'},
              'default-runtime': 'exlab-runc',
              'runtimes': {'exlab-runc': {'path': str(ENGINE / 'usr/bin/runc')}}}
    init = next((p for p in (ENGINE / 'usr/libexec/docker/docker-init', ENGINE / 'usr/bin/docker-init') if p.is_file()), None)
    if init:
        daemon['init-path'] = str(init)
    private_write(CONFIG, daemon)
    CONFIG.chmod(0o644)
    subprocess.run(['nvidia-ctk', 'runtime', 'configure', '--runtime=docker', '--config=' + str(CONFIG)], check=True, timeout=60)
    subprocess.run([str(ENGINE / 'usr/bin/dockerd'), '--validate', '--config-file=' + str(CONFIG)],
                   check=True, capture_output=True, text=True, timeout=30)
    unit_text = ('[Unit]\nDescription=ExLab compute engine\nAfter=network-online.target\nWants=network-online.target\n'
                '[Service]\nType=notify\nEnvironment="PATH=/opt/exlab/docker/usr/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n'
                'ExecStart=/opt/exlab/docker/usr/bin/dockerd --config-file=/etc/exlab-docker.json\n'
                'Restart=on-failure\nRestartSec=5\nRuntimeDirectory=exlab-docker\nRuntimeDirectoryMode=0755\n'
                'Delegate=yes\nKillMode=process\nTimeoutStartSec=120\nLimitNOFILE=1048576\n'
                '[Install]\nWantedBy=multi-user.target\n')
    UNIT.write_text(unit_text)
    UNIT.chmod(0o644)
    client = Path(user.pw_dir) / '.config/exlab/docker'
    if client.exists():
        raise ValueError('独立 Docker 客户端配置目录已存在')
    client.mkdir(parents=True, mode=0o700)
    previous = common.read_json(Path(user.pw_dir) / '.docker/config.json', {})
    config = docker_client_config(previous)
    config['cliPluginsExtraDirs'] = [str(ENGINE / 'usr/libexec/docker/cli-plugins')]
    private_write(client / 'config.json', config)
    for path in (client.parent, client, client / 'config.json'):
        os.chown(path, user.pw_uid, user.pw_gid)
    wsl = WSL
    wsl.write_text(safe_wsl_conf(wsl.read_text() if wsl.exists() else '', user.pw_name, systemd=True))
    systemctl('daemon-reload')
    result = systemctl('enable', SERVICE)
    if result.returncode:
        raise ValueError('无法登记独立引擎服务')


def rollback(job, identity):
    job, value, user, paths = context(job, identity)
    backup = common.read_json(job / 'engine-before.json')
    if backup.get('job_id') != identity:
        raise ValueError('配置恢复归属不符')
    if common.read_json(job / 'committed.json', {}).get('job_id') == identity:
        raise ValueError('环境已交接，不能恢复旧配置')
    client = Path(user.pw_dir) / '.config/exlab/docker'
    if not MARKER.exists() and not any(p.exists() for p in (ENGINE, DATA, CONFIG, UNIT, client)):
        # Crash between writing the journal and its marker: no changes started.
        private_write(job / 'engine-rollback.json', {'job_id': identity, 'ready': True})
        return
    if common.read_json(MARKER, {}).get('job_id') != identity:
        raise ValueError('配置恢复归属不符')
    if UNIT.exists() and 'ExecStart=/opt/exlab/docker/usr/bin/dockerd --config-file=/etc/exlab-docker.json' not in UNIT.read_text():
        raise ValueError('独立引擎服务配置变化，已保留')
    systemctl('stop', SERVICE)
    if systemctl('is-active', SERVICE).stdout.strip() == 'active':
        raise ValueError('独立引擎尚未停止')
    systemctl('disable', SERVICE)
    if set(backup['files']) != {str(p) for p in paths}:
        raise ValueError('备份含非配置路径')
    for path, entry in backup['files'].items():
        restore_file(path, entry)
    setup = Path(value['config']).parent / 'setup-state.json'
    state = common.read_json(setup, {})
    state.update(docker_endpoint=value['endpoint'], docker_id=value['engine_id'])
    private_write(setup, state)
    os.chown(setup, user.pw_uid, user.pw_gid)
    # These exact directories were absent before begin(), and belong to the
    # nonce-checked marker. Never delete the Ubuntu distribution or Desktop data.
    for path in (ENGINE, DATA, client):
        if path.is_symlink() or path.resolve() != path:
            raise ValueError('独立引擎路径被重定向，已保留')
        if path.exists():
            shutil.rmtree(path)
    CONFIG.unlink(missing_ok=True)
    UNIT.unlink(missing_ok=True)
    systemctl('daemon-reload')
    MARKER.unlink()
    private_write(job / 'engine-rollback.json', {'job_id': identity, 'ready': True})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('begin', 'configure', 'rollback'))
    parser.add_argument('--job', required=True)
    parser.add_argument('--id', required=True)
    args = parser.parse_args()
    globals()[args.action](args.job, args.id)


if __name__ == '__main__':
    main()
