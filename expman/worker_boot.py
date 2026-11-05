"""Preflight for Windows unattended workers using a distribution-owned engine.

This module never installs software, changes engine identity, or starts jobs.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from . import runtime, worker_service
from .worker_upgrade import _candidate


def engine_service():
    return 'exlab-docker.service' if Path('/etc/exlab-docker.json').is_file() else 'docker.service'


def native_engine(proc=Path('/proc')):
    # A normal user cannot read /proc/<root dockerd>/exe. Query systemd's
    # public service properties instead, then verify the live main process.
    try:
        reply = subprocess.run(['systemctl', 'show', engine_service(), '--no-pager',
            '--property=ActiveState,MainPID,ExecStart'], capture_output=True,
            text=True, timeout=15)
        if reply.returncode:
            return False
        properties = dict(line.split('=', 1) for line in reply.stdout.splitlines() if '=' in line)
        executable = re.search(r'\bpath=([^\s;]+)', properties.get('ExecStart', ''))
        pid = int(properties.get('MainPID', '0'))
        return bool(properties.get('ActiveState') == 'active' and pid > 1 and executable
            and executable[1] in ('/usr/bin/dockerd', '/usr/sbin/dockerd', '/bin/dockerd',
                '/sbin/dockerd', '/usr/local/bin/dockerd', '/usr/local/sbin/dockerd', '/opt/exlab/docker/usr/bin/dockerd')
            and (proc / str(pid) / 'comm').read_text().strip() == 'dockerd')
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def inspect(service_root=None, expected_engine=None):
    root = worker_service._root(service_root)
    settings = worker_service._settings(root)
    candidate = _candidate(settings.get('config', '')) if settings.get('config') else None
    result = {'ready': False, 'service_root': str(root)}
    if not candidate:
        return dict(result, detail='请先在此 Ubuntu 安装并配置 Worker；不会创建或替换原节点身份。')
    result.update(node_id=candidate['node_id'], config=candidate['path'])
    if os.getuid() == 0:
        return dict(result, detail='请选择拥有此节点配置的普通 Linux 用户，不能使用 root。')
    if settings.get('backend') != 'detached':
        return dict(result, detail='请先停止原 Worker，并用 --backend detached --no-start 安装，由 Windows 后台统一管理启动。')
    unit = worker_service._unit_name(root)
    if (Path.home() / '.config/systemd/user' / unit).exists():
        enabled = subprocess.run(['systemctl', '--user', 'is-enabled', unit], capture_output=True,
                                 text=True, timeout=15)
        if enabled.stdout.strip() not in ('disabled', 'masked', 'not-found'):
            return dict(result, detail='原 Worker 的 systemd 用户自启尚未禁用或无法确认，请先禁用旧启动入口。')
    if not native_engine():
        return dict(result, detail='此环境未运行独立 Docker Engine，可能仍依赖 Docker Desktop。请先完成环境准备及数据迁移。')
    checked = runtime.docker_status(Path(candidate['path']).parent, prefer_saved=True)
    if not checked.get('docker_ready'):
        return dict(result, detail=checked.get('detail', 'Docker 未就绪'))
    if expected_engine and checked.get('docker_id') != expected_engine:
        return dict(result, detail='Docker 引擎身份发生变化；无人值守启动已阻止，请检查迁移状态。')
    info = runtime.docker_command(['info', '--format', '{{json .}}'], checked['docker_endpoint'])
    if info.returncode:
        return dict(result, detail='无法核验 Docker 引擎类型。')
    details = json.loads(info.stdout)
    if 'docker desktop' in str(details.get('OperatingSystem', '')).lower() or details.get('Name') == 'docker-desktop':
        return dict(result, detail='当前连接的是 Docker Desktop，不能用于此无人值守模式。')
    if 'nvidia' not in (details.get('Runtimes') or {}):
        return dict(result, detail='独立 Docker 尚未配置 NVIDIA Container Toolkit。')
    gpu = subprocess.run(['/usr/lib/wsl/lib/nvidia-smi', '-L'], capture_output=True, text=True, timeout=15)
    if gpu.returncode or 'GPU-' not in gpu.stdout:
        return dict(result, detail='WSL 尚未发现可用 NVIDIA GPU；稍后重试。')
    return dict(result, ready=True, engine_id=checked['docker_id'], engine_service=engine_service(), detail='独立 Docker、GPU 与原节点身份检查通过。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service-root')
    parser.add_argument('--expected-engine')
    args = parser.parse_args()
    try:
        value = inspect(args.service_root, args.expected_engine)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        value = {'ready': False, 'detail': str(error)[:600]}
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    main()
