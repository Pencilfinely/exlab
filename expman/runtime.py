"""Inspect and release the local compute runtime without touching unrelated work."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

from .common import read_json


def docker_command(argv, endpoint=None, *, timeout=15):
    environment = dict(os.environ)
    if endpoint:
        environment.pop('DOCKER_CONTEXT', None)
        environment.pop('DOCKER_HOST', None)
        argv = ['--host', endpoint, *argv]
    return subprocess.run(['docker', *argv], capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout, env=environment)


def docker_status(worker_root=None, *, prefer_saved=False):
    result = {'docker_ready': False, 'docker_installed': bool(shutil.which('docker')),
              'docker_endpoint': None, 'detail': 'Docker 未安装或未启用此 Ubuntu 的 WSL 集成'}
    if not result['docker_installed']:
        return result
    try:
        saved = read_json(Path(worker_root) / 'setup-state.json', {}) if worker_root else {}
        context = None if prefer_saved and saved.get('docker_endpoint') else os.environ.get('DOCKER_CONTEXT')
        endpoint = (saved.get('docker_endpoint') if prefer_saved and saved.get('docker_endpoint')
                    else None if context else os.environ.get('DOCKER_HOST') or saved.get('docker_endpoint'))
        # Docker Desktop recreates its socket after restarting. A historical
        # socket which vanished must not permanently pin setup to a dead daemon.
        if endpoint and endpoint.startswith('unix:///') and not Path(endpoint[7:]).exists():
            endpoint = None
        if not endpoint:
            reply = docker_command(['context', 'inspect', *([context] if context else []),
                                    '--format', '{{.Endpoints.docker.Host}}'])
            if reply.returncode:
                raise ValueError('无法读取 Docker context，请检查 WSL 集成')
            endpoint = reply.stdout.strip()
        if not isinstance(endpoint, str) or not endpoint.startswith('unix:///') or any(c.isspace() for c in endpoint):
            raise ValueError('算力端需要本机 Linux Docker socket，请检查 Docker context')
        result['docker_endpoint'] = endpoint
        reply = docker_command(['info', '--format', '{{json .}}'], endpoint)
        if reply.returncode:
            raise ValueError('Docker 尚未启动，或此 Ubuntu 未启用 Docker Desktop 集成')
        info = json.loads(reply.stdout)
        if info.get('OSType') != 'linux':
            raise ValueError('请在 Docker Desktop 中切换到 Linux containers')
        if saved.get('docker_id') and info.get('ID') != saved['docker_id']:
            raise ValueError('Docker 服务身份已变化，请检查原 Docker context；本次保留实验状态')
        result.update(docker_ready=True, detail='Docker 已就绪', docker_version=info.get('ServerVersion'),
                      docker_id=info.get('ID'))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result['detail'] = str(error)[:300]
    return result


def other_user_processes(proc_root='/proc'):
    """Only identify user workloads; do not signal any of them."""
    if not hasattr(os, 'getuid'):
        return []
    own = {os.getpid()}
    parent = os.getppid()
    while parent > 1 and parent not in own:
        own.add(parent)
        try:
            fields = (Path(proc_root) / str(parent) / 'stat').read_text().rsplit(')', 1)[1].split()
            parent = int(fields[1])
        except (OSError, ValueError, IndexError):
            break
    processes = []
    for folder in Path(proc_root).glob('[0-9]*'):
        try:
            if int(folder.name) in own or folder.stat().st_uid != os.getuid():
                continue
            argv = (folder / 'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace').strip()
            if not argv or argv.startswith(('/usr/lib/systemd/', '/lib/systemd/', '(sd-pam)')):
                continue
            # A held WSL client exits after the stopped supervisor. Windows
            # disposes its handle before terminating the distribution.
            if 'expman.worker_service _hold' in argv:
                continue
            processes.append(int(folder.name))
        except (OSError, ValueError):
            continue
    return processes


def release_runtime(worker_root=None, node_id=None):
    from .worker_upgrade import discover_configs, agent_is_running
    result = docker_status(worker_root, prefer_saved=True)
    result.update(can_stop_docker=False, can_terminate_wsl=False, registry_stopped=False)
    if any(agent_is_running(item['root']) for item in discover_configs()):
        return dict(result, detail='另一个算力代理仍在运行，运行环境已保留')
    if not result['docker_ready']:
        # Failure to contact a daemon is not evidence that its containers stopped.
        return dict(result, detail='Docker 未连接，无法确认容器状态；运行环境已保留')
    endpoint = result['docker_endpoint']
    try:
        reply = docker_command(['ps', '--format', '{{json .}}'], endpoint)
        if reply.returncode:
            raise ValueError('无法核查 Docker 容器')
        live = [json.loads(line) for line in reply.stdout.splitlines() if line.strip()]
        others = [item for item in live if item.get('Names') != 'expman-worker-registry']
        if others:
            return dict(result, other_containers=len(others),
                        detail=f'{len(others)} 个其他容器仍在运行，Docker 与 WSL 已保留')
        if live:
            reply = docker_command(['inspect', 'expman-worker-registry'], endpoint)
            if reply.returncode:
                raise ValueError('无法确认镜像仓库归属')
            labels = json.loads(reply.stdout)[0].get('Config', {}).get('Labels') or {}
            if labels.get('expman.component') != 'worker-registry':
                raise ValueError('同名镜像仓库不属于 ExLab，运行环境已保留')
            reply = docker_command(['stop', '--time', '10', 'expman-worker-registry'], endpoint, timeout=30)
            if reply.returncode:
                raise ValueError('镜像仓库尚未停止')
            result['registry_stopped'] = True
        # Recheck after stopping the auxiliary registry before releasing a VM.
        reply = docker_command(['ps', '-q'], endpoint)
        if reply.returncode or reply.stdout.strip():
            raise ValueError('容器状态发生变化，运行环境已保留')
        workloads = other_user_processes()
        result.update(can_stop_docker=True, can_terminate_wsl=not workloads,
                      other_processes=len(workloads),
                      detail='计算环境已空闲' if not workloads else 'Ubuntu 中另有进程；可停止 Docker，WSL 已保留')
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as error:
        result['detail'] = str(error)[:300]
    return result
