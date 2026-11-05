"""Transactional Desktop -> dedicated WSL migration helpers.

Only the Windows wizard imports/terminates distributions. No Windows password
enters this process. Source data and the Desktop daemon are never deleted.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import time

from . import common, runtime, worker_boot, worker_gpu, worker_service as service
from .launcher import InstanceLock
from .worker_setup import private_write

MARKER = Path('/etc/exlab-migration.json')
FENCE = 'migration-owner.json'
REGISTRIES = {'expman-worker-registry': ('worker-registry', 'expman-worker-registry-data', 5001),
              'expman-harness-registry': ('harness-registry', 'expman-harness-registry-data', 5002)}
LOCAL_IMAGE = re.compile(r'(?:localhost|127\.0\.0\.1):500[12]/[^\s@]+@sha256:[a-f0-9]{64}\Z')


def run(args, *, env=None, timeout=60):
    value = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
    if value.returncode:
        # CLI output may contain registry credentials or node secrets. Keep it
        # out of the Windows progress log; retain actionable operation names.
        raise RuntimeError('命令未完成：' + ' '.join(args[:2]) + f'（退出码 {value.returncode}）')
    return value.stdout.strip()


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def environment(endpoint):
    if not isinstance(endpoint, str) or not endpoint.startswith('unix:///') or any(c.isspace() for c in endpoint):
        raise ValueError('迁移只支持本机 Docker socket。')
    return runtime.docker_environment(endpoint)


def docker(args, endpoint, **options):
    return run(['docker', *args], env=environment(endpoint), **options)


def objects(args, endpoint):
    return json.loads(docker(args, endpoint))


def containerd_store(info):
    return ['driver-type', 'io.containerd.snapshotter.v1'] in (info.get('DriverStatus') or [])


def registry(item, name):
    """Accept only our exact volume/network contract, never arbitrary containers."""
    component, volume, port = REGISTRIES[name]
    mounts = item.get('Mounts', [])
    host = item.get('HostConfig', {})
    env = item.get('Config', {}).get('Env', [])
    bindings = host.get('PortBindings', {})
    network = host.get('NetworkMode')
    local = (network == 'host' and 'REGISTRY_HTTP_ADDR=127.0.0.1:' + str(port) in env
             and 'REGISTRY_HTTP_DEBUG_ADDR=' in env)
    bridge = network in ('bridge', 'default') and bindings == {
        '5000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(port)}]}
    if (item.get('Name') != '/' + name or (item.get('Config', {}).get('Labels') or {}).get('expman.component') != component
            or len(mounts) != 1 or mounts[0].get('Type') != 'volume' or mounts[0].get('Name') != volume
            or mounts[0].get('Destination') != '/var/lib/registry' or not (local or bridge)):
        raise ValueError('镜像仓库包含自定义挂载或网络，不能自动迁移：' + name)
    return dict(name=name, volume=volume, port=port, component=component, id=item['Id'],
                image=item['Image'], running=bool(item['State']['Running']))


def image_refs(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'image' and isinstance(item, str) and item:
                if not re.fullmatch(r'[^\s@]+@sha256:[a-f0-9]{64}', item):
                    raise ValueError('配置包含未固定摘要的运行镜像，无法验证迁移前后的一致性。')
                found.add(item)
            else:
                found.update(image_refs(item))
    elif isinstance(value, list):
        for item in value:
            found.update(image_refs(item))
    return found


def docker_client_config(value):
    """Keep inline auths; Desktop-only credential helpers cannot survive uninstall."""
    value = dict(value)
    value['currentContext'] = 'default'
    if value.get('credsStore') in ('desktop', 'desktop.exe'):
        value.pop('credsStore')
    if isinstance(value.get('credHelpers'), dict):
        value['credHelpers'] = {key: helper for key, helper in value['credHelpers'].items()
                                if helper not in ('desktop', 'desktop.exe')}
    return value


def container_plan(containers, node_id, distribution=None):
    """Desktop keeps running: only resources affected by our migration block it.

    An unrelated named volume or Windows bind stays on Desktop. Linux/WSL
    binds cannot be assumed independent of the distribution we will terminate.
    Unknown inspection data fails closed, with the actual container identified.
    """
    registries, preserved, blockers = [], [], []
    volumes = {value[1] for value in REGISTRIES.values()}
    for item in containers:
        name = item['Name'].removeprefix('/')
        if name in REGISTRIES:
            registries.append(registry(item, name))
            continue
        state = item.get('State', {})
        if not (state.get('Running') or state.get('Restarting') or state.get('Paused')):
            continue
        labels = item.get('Config', {}).get('Labels') or {}
        if labels.get('expman.node') == node_id and labels.get('expman.job'):
            continue  # Cooperative drain + idle() checks these before export.
        reasons = []
        mounts = item.get('Mounts')
        if not isinstance(mounts, list):
            reasons.append('无法核查挂载目录')
        for mount in mounts or []:
            if mount.get('Type') == 'volume':
                if mount.get('Name') in volumes:
                    reasons.append('正在使用 Worker 镜像仓库卷')
            elif mount.get('Type') == 'bind':
                source = mount.get('Source', '').replace('\\', '/')
                windows = re.match(r'^(?:/run/desktop/mnt/host/[a-z]/|/host_mnt/[a-z]/|[A-Za-z]:/)', source)
                wsl = re.match(r'^/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/([^/]+)/', source)
                other_distro = bool(distribution and wsl and wsl[1].casefold() != distribution.casefold())
                if not windows and not other_distro:
                    reasons.append('正在使用 Linux/WSL 挂载：' + source[:180])
            elif mount.get('Type') != 'tmpfs':
                reasons.append('挂载类型无法确认')
        if reasons:
            blockers.append(name + '：' + '；'.join(dict.fromkeys(reasons)))
        else:
            preserved.append({'id': item['Id'], 'name': name})
    if blockers:
        raise ValueError('请先暂停以下容器，再重试迁移：\n' + '\n'.join(blockers))
    return registries, preserved


def inspect_containers(endpoint, node_id):
    ids = docker(['ps', '-aq'], endpoint).split()
    return container_plan(objects(['inspect', *ids], endpoint) if ids else [], node_id, os.environ.get('WSL_DISTRO_NAME'))


def inspect(service_root=None):
    import pwd
    if os.getuid() == 0:
        raise ValueError('请使用原节点的普通 Linux 用户。')
    linux_user = pwd.getpwuid(os.getuid()).pw_name
    if not re.fullmatch('[a-z_][a-z0-9_-]*', linux_user):
        raise ValueError('Linux 用户名不符合无人值守配置要求。')
    os_release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if os_release.get('ID', '').strip('"') != 'ubuntu' or os_release.get('VERSION_ID', '').strip('"') not in ('22.04', '24.04', '26.04', '26.10'):
        raise ValueError('一键迁移支持 Ubuntu 22.04、24.04、26.04 和 26.10。')
    if run(['uname', '-m']) != 'x86_64':
        raise ValueError('此迁移包只支持 x64 WSL 2。')
    wsl = configparser.ConfigParser(interpolation=None, strict=False)
    wsl.read('/etc/wsl.conf')
    if wsl.get('boot', 'command', fallback='').strip():
        raise ValueError('原 Ubuntu 使用自定义开机命令，无法确认是否会重复启动旧节点。请使用高级环境配置。')
    fstab = Path('/etc/fstab')
    if fstab.exists() and any(line.strip() and not line.lstrip().startswith('#') for line in fstab.read_text().splitlines()):
        raise ValueError('原 Ubuntu 配置了额外挂载。请先确认数据归属，再使用高级环境配置。')
    root = service._root(service_root)
    path, candidate, config = worker_gpu._configuration(root)
    # Lifecycle, identity and mutable experiment state must be copied, not shared
    # with the source. Windows asset paths may stay shared on fixed local disks.
    root_device = Path('/').stat().st_dev
    for item in (root, path, Path(candidate['root']), Path.home()):
        if item.stat().st_dev != root_device:
            raise ValueError('节点配置和实验目录必须位于 Ubuntu 内部，不能与原节点共享外部目录。')
    for item in (Path.home() / '.config', Path.home() / '.config/systemd', Path.home() / '.config/systemd/user'):
        if item.exists() and item.stat().st_dev != root_device:
            raise ValueError('Linux 用户服务配置使用外部共享目录，不能自动接管。')
    drives = set()
    paths = [*config.get('assets', {}).values(), *(p for p in config.get('allowed_repos', []) if p.startswith('/'))]
    for asset in paths:
        p = Path(asset).expanduser().resolve(strict=True)
        if p.stat().st_dev != root_device:
            match = re.match(r'^/mnt/([a-z])/', str(p))
            if not match:
                raise ValueError('数据集使用了无法在未登录时确认的外部挂载。')
            drives.add(match[1].upper())
    for other in service.discover_configs():
        if other['node_id'] != candidate['node_id'] or other['root'] != candidate['root']:
            raise ValueError('此 Ubuntu 还有其他节点配置，请使用专用 Worker 环境。')
    checked = runtime.docker_status(path.parent, prefer_saved=True)
    if not checked.get('docker_ready'):
        raise ValueError(checked.get('detail', '请先启动原 Docker Desktop。'))
    endpoint = 'unix://' + str(Path(checked['docker_endpoint'][7:]).resolve(strict=True))
    if not endpoint.startswith('unix:///mnt/wsl/'):
        raise ValueError('无法识别 Docker Desktop 独立连接，请检查 WSL 集成。')
    info = objects(['info', '--format', '{{json .}}'], endpoint)
    if 'docker desktop' not in str(info.get('OperatingSystem', '')).lower() and info.get('Name') != 'docker-desktop':
        raise ValueError('当前已是独立 Docker；请使用“检测独立环境”直接登记开机任务。')
    registries, preserved = inspect_containers(endpoint, candidate['node_id'])
    refs = image_refs(config)
    image_ids = docker(['image', 'ls', '-q', '--no-trunc'], endpoint).split()
    all_images = objects(['image', 'inspect', *sorted(set(image_ids))], endpoint) if image_ids else []
    for item in all_images:
        refs.update(ref for ref in (item.get('RepoDigests') or []) if LOCAL_IMAGE.fullmatch(ref))
    images = {ref: objects(['image', 'inspect', ref], endpoint)[0]['Id'] for ref in sorted(refs)}
    gpus = [g for g in worker_gpu._inventory(config) if g['local_enabled']]
    if not gpus:
        raise ValueError('原节点没有已启用且已验证的 GPU，无法进行迁移验收。请先完成原节点的显卡设置。')
    helper = next(iter(worker_gpu._profiles(config, gpus[0]['name']).values()))
    used = int(run(['df', '--output=used', '--block-size=1', '/']).splitlines()[-1].strip())
    cache = sum(int(i.get('Size', 0)) for i in all_images if i['Id'] in set(images.values()) | {r['image'] for r in registries})
    from .worker_transfer import summary
    registry_bytes = sum(summary(endpoint, r['volume'], images[helper])['bytes'] for r in registries)
    # Original filesystem stays in place. Account for the destination image
    # store (including compressed/unpacked layers), registry and package setup.
    required = int(1.15 * (2 * cache + registry_bytes)) + 4 * 1024 ** 3
    return dict(ready=True, service_root=str(root), config=str(path), node_id=candidate['node_id'],
                worker_root=candidate['root'], linux_user=linux_user,
                uid=os.getuid(), gid=os.getgid(), engine_id=info['ID'], endpoint=endpoint,
                containerd_store=containerd_store(info),
                registries=registries, images=images, helper=helper, gpus=gpus,
                required_bytes=required, asset_drives=sorted(drives), preserved_containers=preserved,
                mode='in_place', space={'filesystem_copy_bytes': 0, 'existing_filesystem_bytes': used,
                    'image_bytes': cache, 'registry_bytes': registry_bytes, 'required_bytes': required})


def load_job(job):
    job = Path(job)
    value = common.read_json(job / 'manifest.json')
    if not isinstance(value, dict) or not re.fullmatch('[a-f0-9]{32}', value.get('job_id', '')):
        raise ValueError('迁移记录无效。')
    return job, value


def same_source(value):
    path, candidate, _ = worker_gpu._configuration(service._root(value['service_root']))
    if str(path) != value['config'] or candidate['node_id'] != value['node_id']:
        raise ValueError('原节点身份已变化，已阻止迁移。')
    info = objects(['info', '--format', '{{json .}}'], value['endpoint'])
    if info.get('ID') != value['engine_id']:
        raise ValueError('原 Docker 引擎已变化，已阻止迁移。')
    if 'containerd_store' in value and containerd_store(info) != value['containerd_store']:
        raise ValueError('原 Docker 镜像存储已变化，请重新检测。')


def cancelled(job):
    if (job / 'cancel').exists():
        raise RuntimeError('已取消，正在恢复原节点。')


def idle(root):
    """Queued/paused records can move; executing containers/processes cannot."""
    state = service.update_status(root, preserve_experiments=True)
    if not state.get('ready_for_update'):
        return False
    candidate = service._candidate(service._settings(root)['config'])
    return not service._update_containers(candidate, execution_only=True)


def prepare(job):
    job, value = load_job(job)
    same_source(value)
    root, config = Path(value['service_root']), Path(value['config'])
    with InstanceLock(root / 'migration.lock'):
        override = config.parent / 'compute-override.json'
        unit = Path.home() / '.config/systemd/user' / service._unit_name(root)
        unit_enabled = False
        if unit.exists():
            enabled = subprocess.run(['systemctl', '--user', 'is-enabled', unit.name], capture_output=True, text=True, timeout=15).stdout.strip()
            if enabled not in ('enabled', 'disabled', 'masked', 'not-found'):
                raise ValueError('无法核验原 Worker 用户服务的启用状态。')
            unit_enabled = enabled == 'enabled'
        rollback = {'override': common.read_json(override) if override.exists() else None,
                    'was_running': service.status(root)['running'], 'service_settings': service._settings(root),
                    'unit_enabled': unit_enabled}
        private_write(job / 'rollback.json', rollback)
        service.compute_availability(root, mode='drain')
        deadline = time.monotonic() + 24 * 3600
        while True:
            cancelled(job)
            state = service.status(root)
            if idle(root):
                break
            if not state.get('running') or time.monotonic() >= deadline:
                raise RuntimeError('原节点仍有未完成的实验或回传，暂时不能迁移。原策略将恢复。')
            time.sleep(3)
        stopped = service.stop_for_update(root, preserve_experiments=True)
        if not stopped.get('ready_for_update'):
            raise RuntimeError('原节点尚未完成安全交接。')
        deadline = time.monotonic() + 60
        while not service.install_status(root).get('ready_for_install'):
            cancelled(job)
            if time.monotonic() >= deadline:
                raise RuntimeError('原节点后台未退出。')
            time.sleep(1)
        fresh = inspect(root)
        if fresh['node_id'] != value['node_id'] or fresh['config'] != value['config'] or fresh['engine_id'] != value['engine_id']:
            raise ValueError('等待期间节点身份已改变。')
        fresh['job_id'] = value['job_id']
        value = fresh
        private_write(job / 'manifest.json', value)
        with InstanceLock(root / 'control.lock'), InstanceLock(root / 'supervisor.lock'), InstanceLock(Path(value['worker_root']) / 'agent.lock'):
            if not idle(root):
                raise RuntimeError('停止后实验状态发生变化，已取消迁移。')
            private_write(root / FENCE, {'job_id': value['job_id'], 'detail': '节点正在迁移，原入口已锁定。请在无人值守向导继续或恢复。'})
            private_write(config.parent / FENCE, {'job_id': value['job_id']})
            service._write_status(root, status='stopped', stop_requested=True, online=False)
        # Install this version's fence checks at the source's ordinary launcher.
        result = service.install(root, config=str(config), backend='detached', start_now=False)
        if result.get('running'):
            raise RuntimeError('无法锁定原节点入口。')
        if unit_enabled:
            run(['systemctl', '--user', 'disable', '--now', unit.name])
        same_source(value)
        for item in value['registries']:
            current = objects(['inspect', item['id']], value['endpoint'])[0]
            if registry(current, item['name'])['id'] != item['id']:
                raise ValueError('原镜像仓库身份已变化。')
            if current['State']['Running']:
                docker(['stop', item['id']], value['endpoint'])
        # A clean, stopped SQLite snapshot is copied by WSL, including WAL files.
        files = [config, Path(value['worker_root']) / 'node.sqlite3', Path(value['worker_root']) / 'node.sqlite3-wal']
        private_write(job / 'source-ready.json', {'node_id': value['node_id'], 'config_sha256': digest(config),
                                                'files': {str(p): digest(p) for p in files if p.is_file()}})
    return {'ready': True}


VOLUME_BACKUP = '''import os, sys, tarfile
for root, dirs, files in os.walk('/backup'):
    if any(os.path.islink(os.path.join(root, n)) for n in dirs + files):
        raise RuntimeError('Registry volume contains symlinks')
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as t:
    t.add('/backup', arcname='data', recursive=True)
'''
VOLUME_RESTORE = '''import os, pathlib, sys, tarfile
if os.listdir('/backup'): raise RuntimeError('Target volume is not empty')
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as t:
    for m in t:
        p = pathlib.PurePosixPath(m.name)
        if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0] != 'data' or not (m.isfile() or m.isdir()):
            raise RuntimeError('Unsafe registry archive')
        target = pathlib.Path('/backup').joinpath(*p.parts[1:])
        if m.isdir(): target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as out, t.extractfile(m) as source:
                import shutil
                shutil.copyfileobj(source, out, 1024 * 1024)
'''


def stream(args, endpoint, path, *, writing):
    with Path(path).open('wb' if writing else 'rb') as handle:
        options = {'stdout': handle, 'stdin': subprocess.DEVNULL} if writing else {'stdin': handle, 'stdout': subprocess.DEVNULL}
        result = subprocess.run(['docker', *args], env=environment(endpoint), stderr=subprocess.PIPE,
                                timeout=12 * 3600, **options)
    if result.returncode:
        raise RuntimeError('Docker 数据传输失败，原数据保留。请检查空间和 Docker 状态。')


def export_data(job):
    job, value = load_job(job)
    same_source(value)
    if not (job / 'source-ready.json').exists():
        raise ValueError('尚未完成原节点交接。')
    cancelled(job)
    ids = sorted(set(value['images'].values()) | {r['image'] for r in value['registries']})
    stream(['image', 'save', *ids], value['endpoint'], job / 'images.tar', writing=True)
    checksums = {'images.tar': digest(job / 'images.tar')}
    for item in value['registries']:
        cancelled(job)
        if objects(['inspect', item['id']], value['endpoint'])[0]['State']['Running']:
            raise ValueError('原镜像仓库在备份中被重新启动，已取消迁移。')
        filename = item['volume'] + '.tar'
        stream(['run', '--rm', '--user', '0:0', '--network', 'none', '--read-only', '--cap-drop=ALL', '--entrypoint', 'python',
                '--mount', 'type=volume,src=' + item['volume'] + ',dst=/backup,readonly',
                value['images'][value['helper']], '-c', VOLUME_BACKUP], value['endpoint'], job / filename, writing=True)
        checksums[filename] = digest(job / filename)
    private_write(job / 'checksums.json', checksums)
    return {'ready': True}


def pre_export(job):
    job, value = load_job(job)
    same_source(value)
    inspect_containers(value['endpoint'], value['node_id'])
    if not idle(Path(value['service_root'])) or not service.install_status(value['service_root']).get('ready_for_install'):
        raise ValueError('原节点仍有任务运行，尚未关闭 Ubuntu。')
    return {'ready': True}


def safe_wsl_conf(original, user, *, systemd):
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.read_string(original or '')
    for section in ('boot', 'user', 'automount'):
        if not config.has_section(section):
            config.add_section(section)
    config['boot'].pop('command', None)
    config['boot']['systemd'] = 'true' if systemd else 'false'
    config['user']['default'] = user
    config['automount']['mountFsTab'] = 'false'
    config['automount']['enabled'] = 'true'
    config['automount']['root'] = '/mnt/'
    out = io.StringIO()
    config.write(out)
    return out.getvalue()


def sanitize(job):
    job, value = load_job(job)
    archive = job / 'distribution.tar'
    # Append overrides before the first target boot; Linux services must never
    # run from the unsanitized clone. WSL imports tar members in archive order.
    original = ''
    with tarfile.open(archive, 'r:') as source:
        for member in source:
            if member.name.lstrip('./') == 'etc/wsl.conf':
                if not member.isfile() or member.size > 65536:
                    raise ValueError('原 WSL 配置不是普通文件。')
                original = source.extractfile(member).read().decode('utf-8')
    with tarfile.open(archive, 'a:') as target:
        for name, data, mode in (
                ('etc/wsl.conf', safe_wsl_conf(original, value['linux_user'], systemd=False).encode(), 0o644),
                ('etc/exlab-migration.json', json.dumps({'job_id': value['job_id'], 'source_engine': value['engine_id'],
                    'service_root': value['service_root'], 'linux_user': value['linux_user']}).encode(), 0o600)):
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.uid, entry.gid = len(data), mode, 0, 0
            target.addfile(entry, io.BytesIO(data))
    private_write(job / 'distribution-sha256.json', {'sha256': digest(archive)})
    return {'ready': True}


def native_endpoint(value):
    return 'unix:///run/exlab-docker/docker.sock' if value.get('mode') == 'in_place' else 'unix:///var/run/docker.sock'


def target_identity(value):
    # Normal users can read the public job marker after provisioning.
    marker = common.read_json(MARKER)
    if marker.get('job_id') != value['job_id']:
        raise ValueError('目标 WSL 不属于本次迁移，已拒绝修改。')
    if not worker_boot.native_engine():
        raise ValueError('目标没有运行独立 Docker Engine。')
    info = objects(['info', '--format', '{{json .}}'], native_endpoint(value))
    if (info.get('ID') == value['engine_id'] or 'docker desktop' in str(info.get('OperatingSystem', '')).lower()
            or info.get('Name') == 'docker-desktop' or 'nvidia' not in info.get('Runtimes', {})):
        raise ValueError('目标 Docker 身份或 NVIDIA runtime 不符合要求。')
    return info


def restore(job):
    job, value = load_job(job)
    info = target_identity(value)
    endpoint = 'unix:///var/run/docker.sock'
    for filename, expected in common.read_json(job / 'checksums.json').items():
        if Path(filename).name != filename or digest(job / filename) != expected:
            raise ValueError('备份校验失败，未导入数据。')
    cancelled(job)
    stream(['image', 'load'], endpoint, job / 'images.tar', writing=False)
    for item in value['registries']:
        cancelled(job)
        # The dedicated daemon starts empty. Never replace an existing volume.
        existing = docker(['volume', 'ls', '--format', '{{.Name}}'], endpoint).splitlines()
        if item['volume'] in existing:
            raise ValueError('目标存在同名卷，请使用恢复按钮清理未完成的迁移后重试。')
        docker(['volume', 'create', '--label', 'expman.migration=' + value['job_id'], item['volume']], endpoint)
        stream(['run', '--rm', '-i', '--user', '0:0', '--network', 'none', '--cap-drop=ALL', '--read-only', '--entrypoint', 'python',
                '--mount', 'type=volume,src=' + item['volume'] + ',dst=/backup',
                value['images'][value['helper']], '-c', VOLUME_RESTORE], endpoint, job / (item['volume'] + '.tar'), writing=False)
        docker(['run', '-d', '--restart', 'unless-stopped', '--name', item['name'],
                '--label', 'expman.component=' + item['component'],
                '-p', '127.0.0.1:' + str(item['port']) + ':5000',
                '-v', item['volume'] + ':/var/lib/registry', '-e', 'REGISTRY_HTTP_DEBUG_ADDR=',
                '-e', 'OTEL_TRACES_EXPORTER=none', item['image']], endpoint)
    # save/load retains image IDs but may omit RepoDigests. Pull exact digests
    # from the restored local registries (or original remote registry) to restore
    # the names used by persisted tasks. Never substitute a tag or a new digest.
    for ref, expected in value['images'].items():
        cancelled(job)
        for attempt in range(12):
            try:
                docker(['pull', ref], endpoint, timeout=1800)
                break
            except RuntimeError:
                if attempt == 11:
                    raise
                time.sleep(5)
        if objects(['image', 'inspect', ref], endpoint)[0]['Id'] != expected:
            raise ValueError('迁移后的镜像内容与原节点不同。')
    private_write(job / 'target-engine.json', {'engine_id': info['ID']})
    return {'ready': True}


def verify(job):
    job, value = load_job(job)
    info = target_identity(value)
    if info['ID'] != common.read_json(job / 'target-engine.json')['engine_id']:
        raise ValueError('恢复后目标 Docker 已改变。')
    config = Path(value['config'])
    if digest(config) != common.read_json(job / 'source-ready.json')['config_sha256']:
        raise ValueError('迁移后的节点配置与备份不一致。')
    for filename, expected in common.read_json(job / 'source-ready.json')['files'].items():
        if digest(filename) != expected:
            raise ValueError('原节点数据库或配置未完整复制，已阻止交接。')
    receipts = []
    for gpu in value['gpus']:
        for image in sorted(set(worker_gpu._profiles(common.read_json(config), gpu['name']).values())):
            cancelled(job)
            receipts.append(worker_gpu._verify(image, gpu['uuid'], environment(native_endpoint(value)), time.monotonic() + 90))
    if not receipts:
        raise ValueError('未完成实际 CUDA 验证。')
    state_path = config.parent / 'setup-state.json'
    state = common.read_json(state_path, {})
    state.update(docker_id=info['ID'], docker_endpoint=native_endpoint(value))
    private_write(state_path, state)
    root = Path(value['service_root'])
    result = service.install(root, config=str(config), backend='detached', start_now=False)
    if result.get('running'):
        raise ValueError('目标在交接前已启动。')
    checked = worker_boot.inspect(root, info['ID'])
    if not checked.get('ready'):
        raise ValueError(checked['detail'])
    private_write(job / 'verified.json', {'node_id': value['node_id'], 'engine_id': info['ID'], 'cuda': receipts})
    return dict(ready=True, engine_id=info['ID'])


def remove_fence(path, job_id):
    if path.exists():
        if common.read_json(path).get('job_id') != job_id:
            raise ValueError('节点由另一迁移操作锁定。')
        path.unlink()


def restore_override(job, value):
    override = Path(value['config']).parent / 'compute-override.json'
    previous = common.read_json(job / 'rollback.json')['override']
    if previous is None:
        override.unlink(missing_ok=True)
    else:
        private_write(override, previous)


def activate(job):
    job, value = load_job(job)
    info = target_identity(value)
    verified = common.read_json(job / 'verified.json')
    if verified.get('node_id') != value['node_id'] or verified.get('engine_id') != info['ID'] or not verified.get('cuda'):
        raise ValueError('尚未完成迁移验收。')
    receipt = common.read_json(job / 'committed.json', {})
    if receipt and (receipt.get('job_id') != value['job_id'] or receipt.get('engine_id') != info['ID']):
        raise ValueError('交接记录归属不符')
    if not receipt:
        restore_override(job, value)
        private_write(job / 'committed.json', {'job_id': value['job_id'], 'engine_id': info['ID']})
    for path in (Path(value['service_root']) / FENCE, Path(value['config']).parent / FENCE):
        remove_fence(path, value['job_id'])
    return {'ready': True}


def rollback(job):
    job, value = load_job(job)
    if (job / 'committed.json').exists():
        raise ValueError('环境已经交接，不能恢复旧配置。')
    if MARKER.exists() and common.read_json(MARKER, {}).get('job_id') == value['job_id']:
        raise ValueError('恢复原节点操作不能在迁移副本中执行。')
    same_source(value)
    for item in value['registries']:
        current = registry(objects(['inspect', item['id']], value['endpoint'])[0], item['name'])
        if current['id'] != item['id']:
            raise ValueError('原仓库身份已变化，保留现状。')
        if item['running'] and not current['running']:
            docker(['start', item['id']], value['endpoint'])
    if (job / 'rollback.json').exists():
        restore_override(job, value)
        previous = common.read_json(job / 'rollback.json')
        if previous.get('service_settings'):
            service._save_settings(Path(value['service_root']), previous['service_settings'])
        if previous.get('unit_enabled'):
            run(['systemctl', '--user', 'enable', service._unit_name(Path(value['service_root']))])
    for path in (Path(value['service_root']) / FENCE, Path(value['config']).parent / FENCE):
        remove_fence(path, value['job_id'])
    # Do not resume computation behind the Windows client's back. Its existing
    # Center policy controls startup after the migration window closes.
    return {'ready': True, 'was_running': common.read_json(job / 'rollback.json', {}).get('was_running', False)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'prepare', 'export', 'pre-export', 'sanitize', 'restore', 'verify', 'activate', 'rollback', 'quiescent', 'transfer', 'cleanup-source'))
    parser.add_argument('--job')
    parser.add_argument('--service-root')
    args = parser.parse_args(argv)
    try:
        if args.action == 'inspect':
            result = inspect(args.service_root)
        else:
            from .worker_transfer import transfer, cleanup_source
            job, _ = load_job(args.job)
            with InstanceLock(job / 'operation.lock', timeout=60 if args.action in ('rollback', 'quiescent') else 0):
                result = {'export': export_data, 'pre-export': pre_export, 'prepare': prepare, 'sanitize': sanitize, 'restore': restore,
                          'verify': verify, 'activate': activate, 'rollback': rollback,
                          'transfer': transfer, 'cleanup-source': cleanup_source,
                          'quiescent': lambda _: {'ready': True}}[args.action](args.job)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError) as error:
        detail = service._redact(service._root(args.service_root), str(error))[:4000]
        print(json.dumps({'ready': False, 'detail': detail}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
