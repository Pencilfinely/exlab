"""Owned storage maintenance, independent from scientific execution.

Results and checkpoint archives are never deleted. Workspaces are reclaimed only
after a verified source archive is durable. Cleanup is resumable and each item
is revalidated; a scan is an estimate, never deletion authority by itself.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid

from . import common, worker_database, storage_environments
from .launcher import InstanceLock
from .project_cleanup import owned_path, _validate_tree

GROUPS = {'runs': '实验输出', 'retained_evidence': '权重与证据档案',
          'result_cache': '结果档案', 'upload_cache': '回传副本',
          'worktrees': '代码工作副本', 'repos': '代码版本库',
          'code_archive': '代码归档', 'distributed-projects': '项目与数据集',
          'project-downloads': '项目下载'}
WEIGHTS = {'.pt', '.pth', '.ckpt', '.safetensors', '.bin', '.npz', '.npy', '.h5', '.hdf5', '.onnx'}


def _records(db):
    return {row[0]: json.loads(row[1]) for row in db.execute('SELECT id,record FROM tasks')}


def _finished(record):
    # Failed/paused/interrupted jobs may resume and need their original files.
    return (record.get('state') == 'succeeded' and record.get('archive_scanned')
            and not record.get('execution_retired'))


def _path(root, value):
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return owned_path(root, path.relative_to(root).as_posix())


def _size(path, seen=None):
    """Allocated file bytes, counting hardlinks once and never following links."""
    seen = seen if seen is not None else set()
    total = 0
    if not path.exists() or path.is_symlink():
        return 0
    def children():
        for parent, directories, names in os.walk(path, followlinks=False):
            directories[:] = [name for name in directories if not redirected(Path(parent) / name)]
            yield from (Path(parent) / name for name in names)
    def redirected(item):
        info = item.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
    files = [path] if path.is_file() else children()
    for item in files:
        if redirected(item):
            continue
        info = item.stat()
        identity = (info.st_dev, info.st_ino)
        if info.st_ino and identity in seen:
            continue
        seen.add(identity)
        total += getattr(info, 'st_blocks', (info.st_size + 511) // 512) * 512
    return total


def command(config_path):
    def execute(argv, **options):
        env = dict(os.environ)
        endpoint = common.read_json(Path(config_path).parent / 'setup-state.json', {}).get('docker_endpoint')
        if endpoint and argv[0] == 'docker':
            from .runtime import docker_environment
            env = docker_environment(endpoint)
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', env=env, timeout=options.get('timeout', 30))
        if options.get('check', True) and result.returncode:
            raise RuntimeError((result.stderr or result.stdout)[-700:])
        return result
    return execute


def inspect(root, config, db, execute):
    root = Path(root).resolve()
    seen, groups, items, warnings = set(), [], [], []
    for name, label in GROUPS.items():
        try:
            size = _size(owned_path(root, name), seen)
            groups.append({'name': name, 'label': label, 'bytes': size})
        except (OSError, ValueError) as error:
            warnings.append(label + '：' + str(error)[:200])
    records = _records(db)
    for identity, record in records.items():
        if not _finished(record) or not re.fullmatch('[a-f0-9]{32}', identity):
            continue
        path = owned_path(root, 'worktrees/' + identity)
        if path.is_dir() and record.get('spec', {}).get('backend') == 'docker':
            items.append({'kind': 'workspace', 'job_id': identity, 'bytes': _size(path)})
    for row in db.execute("SELECT job_id,source,path,sha256,size FROM node_evidence WHERE retention='retained' AND availability='available'"):
        record = records.get(row[0], {})
        if not _finished(record) or not row[3] or not row[4]:
            continue
        try:
            source, target = _path(root, row[1]), _path(root, row[2])
            if (source.is_relative_to(root / 'runs' / row[0]) and target.is_relative_to(root / 'retained_evidence')
                    and source.suffix.lower() in WEIGHTS and source.is_file() and target.is_file()
                    and source.stat().st_size == target.stat().st_size == row[4]
                    and not os.path.samefile(source, target)):
                items.append({'kind': 'duplicate_weight', 'job_id': row[0], 'source': str(source),
                              'target': str(target), 'sha256': row[3], 'bytes': _size(source)})
        except (OSError, ValueError):
            continue
    try:
        ids = execute(['docker', 'ps', '-aq', '--filter', 'label=expman.node=' + config['node_id']]).stdout.split()
        containers = json.loads(execute(['docker', 'inspect', *ids]).stdout) if ids else []
        for item in containers:
            labels = item.get('Config', {}).get('Labels') or {}
            identity = labels.get('expman.job')
            if (_finished(records.get(identity, {})) and labels.get('expman.node') == config['node_id']
                    and item.get('State', {}).get('Status') in ('exited', 'dead')
                    and item.get('HostConfig', {}).get('ReadonlyRootfs') is True):
                items.append({'kind': 'container', 'job_id': identity, 'container_id': item['Id'], 'bytes': 0})
        environments = storage_environments.plan(config, list(records.values()), execute)
        items.extend(environments)
        if environments:
            groups.append({'name': 'obsolete_environments', 'label': '旧环境缓存（逻辑大小，含共享层）',
                           'bytes': sum(item['logical_bytes'] for item in environments)})
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        warnings.append('容器检查暂不可用：' + str(error)[:250])
    # A weight can be referenced by multiple evidence entries. Count it once.
    unique = {(i['kind'], i.get('source', i.get('container_id', i.get('image', i['job_id'])))): i for i in items}
    return {'status': 'ready', 'groups': groups, 'items': list(unique.values()),
            'reclaimable_bytes': sum(i['bytes'] for i in unique.values()),
            'free_bytes': shutil.disk_usage(root).free, 'warnings': warnings,
            'detail': '可清理项已检查；结果、权重档案和待续训数据保留。'}


def _archive_source(root, record, execute):
    identity = record['id']
    source = record['spec']['source']
    commit = source['commit']
    if not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('代码版本无效')
    checkout = owned_path(root, 'worktrees/' + identity)
    _validate_tree(checkout)
    if (checkout / '.gitmodules').exists() or any(re.search(r'\b(?:filter\s*=|export-ignore|export-subst)', p.read_text(encoding='utf-8', errors='replace')) for p in checkout.rglob('.gitattributes')):
        raise ValueError('代码含子模块、内容过滤或特殊归档规则，工作副本保留')
    actual = execute(['git', '-C', str(checkout), 'rev-parse', 'HEAD']).stdout.strip()
    dirty = execute(['git', '-C', str(checkout), 'status', '--porcelain', '--untracked-files=all', '--ignored=matching']).stdout.strip()
    if actual != commit or dirty:
        raise ValueError('代码工作副本有修改，已保留')
    archive_root = owned_path(root, 'code_archive')
    archive_root.mkdir(exist_ok=True)
    temporary = archive_root / (uuid.uuid4().hex + '.tmp')
    try:
        execute(['git', '-C', str(checkout), 'archive', '--format=tar.gz', '--output=' + str(temporary), commit], timeout=600)
        with temporary.open('rb+') as stream:
            os.fsync(stream.fileno())
        sha = common.sha256_file(temporary)
        destination = owned_path(root, 'code_archive/' + sha + '.tar.gz')
        if destination.exists():
            if common.sha256_file(destination) != sha:
                raise ValueError('已有代码归档校验失败')
        else:
            os.replace(temporary, destination)
        common.atomic_json(owned_path(root, 'runs/' + identity + '/code-archive.json'),
            {'source': source, 'sha256': sha, 'path': str(destination),
             'environments': record['spec'].get('environments', []), 'params': record['spec'].get('params', {})})
        # Git itself rejects locked/changed worktrees; never force their removal.
        common_dir = Path(execute(['git', '-C', str(checkout), 'rev-parse', '--git-common-dir']).stdout.strip())
        common_dir = (common_dir if common_dir.is_absolute() else checkout / common_dir).resolve()
        if not common_dir.is_relative_to(root / 'repos'):
            raise ValueError('工作副本对应外部代码库，已保留')
        _path(root, str(common_dir))
        execute(['git', '--git-dir', str(common_dir), 'worktree', 'remove', str(checkout)], timeout=120)
    finally:
        temporary.unlink(missing_ok=True)


def reclaim(root, config, db, execute, plan, report=None, cancelled=lambda: False):
    root = Path(root).resolve()
    before = shutil.disk_usage(root).free
    result = {'status': 'completed', 'removed_bytes': 0, 'completed_items': 0, 'skipped': [],
              'free_bytes_before': before}
    for item in plan['items']:
        if cancelled():
            result['status'] = 'cancelled'
            break
        try:
            identity = item['job_id']
            if not re.fullmatch('[a-f0-9]{32}', identity):
                raise ValueError('实验标识无效')
            row = db.execute('SELECT record FROM tasks WHERE id=?', (identity,)).fetchone()
            record = json.loads(row[0]) if row else {}
            if not _finished(record):
                raise ValueError('实验状态已变化，已保留')
            if item['kind'] == 'workspace':
                _archive_source(root, record, execute)
            elif item['kind'] == 'duplicate_weight':
                source, target = _path(root, item['source']), _path(root, item['target'])
                if (not source.is_relative_to(root / 'runs' / identity) or not target.is_relative_to(root / 'retained_evidence')
                        or source.suffix.lower() not in WEIGHTS):
                    raise ValueError('文件归属变化')
                row = db.execute("SELECT 1 FROM node_evidence WHERE job_id=? AND source=? AND path=? AND sha256=? AND retention='retained' AND availability='available'",
                                 (identity, str(source), str(target), item['sha256'])).fetchone()
                before_source, before_target = source.stat(), target.stat()
                if not row or common.sha256_file(source) != item['sha256'] or common.sha256_file(target) != item['sha256']:
                    raise ValueError('权重副本校验失败，已保留两份文件')
                _path(root, str(source));_path(root, str(target))
                if any((before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns)
                       for before, after in ((before_source, source.stat()), (before_target, target.stat()))):
                    raise ValueError('文件在清理期间变化，已保留')
                if os.path.samefile(source, target):
                    continue
                # Persist the lookup before removing only the duplicate live-output copy.
                index = owned_path(root, 'runs/' + identity + '/archived-files.json')
                links = common.read_json(index, {})
                links[source.relative_to(root / 'runs' / identity).as_posix()] = {'path': str(target), 'sha256': item['sha256']}
                common.atomic_json(index, links)
                source.unlink()
            elif item['kind'] == 'container':
                container = json.loads(execute(['docker', 'inspect', item['container_id']]).stdout)[0]
                labels = container.get('Config', {}).get('Labels') or {}
                if (container['Id'] != item['container_id'] or labels.get('expman.node') != config['node_id']
                        or labels.get('expman.job') != identity or container.get('State', {}).get('Status') not in ('exited', 'dead')
                        or container.get('HostConfig', {}).get('ReadonlyRootfs') is not True):
                    raise ValueError('容器归属或状态变化，已保留')
                execute(['docker', 'rm', container['Id']])
            elif item['kind'] == 'environment':
                storage_environments.reclaim(item, config, list(_records(db).values()), execute)
            else:
                raise ValueError('未知清理项目')
            result['completed_items'] += 1
            result['removed_bytes'] += item['bytes']
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            result['skipped'].append({'job_id': item['job_id'], 'detail': str(error)[:300]})
        if report:
            report(dict(result, status='running'))
    result['free_bytes_after'] = shutil.disk_usage(root).free
    result['filesystem_reclaimed_bytes'] = max(0, result['free_bytes_after'] - before)
    result['detail'] = '清理已完成。权重可通过实验档案及 archived-files.json 找到。'
    return result


def run_request(config_path, request, report, cancelled=lambda: False):
    config = common.read_json(config_path)
    configured = Path(config['root']).expanduser()
    root = (configured if configured.is_absolute() else Path(config_path).parent / configured).resolve()
    execute = command(config_path)
    db = worker_database.connect(root / 'node.sqlite3')
    try:
        plan = inspect(root, config, db, execute)
        if cancelled():
            return {'status': 'cancelled', 'detail': '已取消，现有文件保留。'}
        if request['action'] == 'scan':
            return plan
        if request['action'] != 'clean':
            raise ValueError('未知存储操作')
        return reclaim(root, config, db, execute, plan, report, cancelled)
    finally:
        db.close()


class Maintenance:
    """One optional worker; execution admission pauses during cleanup, not scans."""
    def __init__(self, agent):
        self.agent, self.thread = agent, None
        self.stop = threading.Event()
        self.cleaning = False
        self.last_id = None
        self.next_auto = time.monotonic() + 300

    def tick(self):
        if self.thread and self.thread.is_alive():
            return
        root = self.agent.root
        request = common.read_json(root / 'storage-request.json', {})
        policy = common.read_json(root / 'storage-policy.json', {})
        active = self.agent.db.execute("SELECT 1 FROM tasks WHERE json_extract(record,'$.state') IN ('assigned','preparing','ready','starting','running') LIMIT 1").fetchone()
        if not active and time.monotonic() >= self.next_auto and policy.get('automatic', True):
            try:
                with InstanceLock(root / 'storage-request.lock'):
                    request = common.read_json(root / 'storage-request.json', {})
                    previous = common.read_json(root / 'storage-status.json', {})
                    if not request or previous.get('id') == request.get('id') and previous.get('status') in ('ready', 'completed', 'failed', 'cancelled'):
                        request = {'id': uuid.uuid4().hex, 'action': 'clean', 'automatic': True}
                        common.atomic_json(root / 'storage-request.json', request)
                        self.next_auto = time.monotonic() + 6 * 3600
            except RuntimeError:
                return
        identity = request.get('id')
        if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{32}', identity) or identity == self.last_id:
            return
        previous = common.read_json(root / 'storage-status.json', {})
        if previous.get('id') == identity and previous.get('status') in ('ready', 'completed', 'failed', 'cancelled'):
            self.last_id = identity
            return
        self.cleaning = request.get('action') == 'clean'
        if self.cleaning and (self.agent.preparation or self.agent.project_delivery.installing):
            return
        self.last_id = identity
        def publish(value):
            common.atomic_json(root / 'storage-status.json', dict(value, id=identity, time=common.now()))
        def run():
            try:
                if sys.platform == 'linux':
                    # Linux nice/ionice operate on this maintenance thread; the
                    # scheduler, result lanes and training processes keep priority.
                    try:
                        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 15)
                        if shutil.which('ionice'):
                            subprocess.run(['ionice', '-c', '3', '-p', str(threading.get_native_id())],
                                           capture_output=True, timeout=5)
                    except (OSError, subprocess.SubprocessError):
                        pass
                publish({'status': 'running', 'detail': '正在清理…' if self.cleaning else '正在扫描…'})
                publish(run_request(self.agent.config_path, request, publish,
                    lambda: self.stop.is_set() or common.read_json(root / 'storage-cancel.json', {}).get('id') == identity))
            except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
                publish({'status': 'failed', 'detail': str(error)[:800]})
            finally:
                self.cleaning = False
        self.thread = threading.Thread(target=run, name='storage-maintenance', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join()
