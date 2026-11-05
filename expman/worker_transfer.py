"""Stream verified images/registry volumes between two local engines.

No complete image archive or WSL clone is created. Source objects are retained
until the destination identity and CUDA verification have committed.
"""
import json
from pathlib import Path
import subprocess
import time

from . import common, worker_migration as m, worker_database
from .worker_setup import private_write

VOLUME_SUMMARY = '''import hashlib,json,os,stat,sys
total=0;digest=hashlib.sha256()
for root,dirs,files in os.walk('/backup'):
    dirs.sort();files.sort()
    for name in dirs+files:
        if os.path.islink(os.path.join(root,name)): raise RuntimeError('Linked registry content')
    for name in files:
        path=os.path.join(root,name);info=os.stat(path)
        if not stat.S_ISREG(info.st_mode): raise RuntimeError('Non-file registry content')
        total+=info.st_size
        if sys.argv[-1]=='hash':
            digest.update(os.path.relpath(path,'/backup').encode()+b'\\0'+str(info.st_size).encode()+b'\\0')
            with open(path,'rb') as f:
                for block in iter(lambda:f.read(1048576),b''): digest.update(block)
print(json.dumps({'bytes':total,'sha256':digest.hexdigest()}))
'''


def volume_args(volume, image, script, *, readonly=True, interactive=False):
    args = ['run', '--rm', '--user', '0:0', '--network', 'none', '--read-only', '--cap-drop=ALL', '--entrypoint', 'python']
    if interactive:
        args.append('-i')
    return args + ['--mount', 'type=volume,src=' + volume + ',dst=/backup' + (',readonly' if readonly else ''), image, '-c', script]


def summary(endpoint, volume, image, *, hashed=False):
    return m.objects(volume_args(volume, image, VOLUME_SUMMARY) + ['hash' if hashed else 'size'], endpoint)


def pipe(source_args, source_endpoint, target_args, target_endpoint):
    source = subprocess.Popen(['docker', *source_args], env=m.environment(source_endpoint),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    target = None
    try:
        target = subprocess.Popen(['docker', *target_args], env=m.environment(target_endpoint),
                                  stdin=source.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        source.stdout.close()
        target_code = target.wait(timeout=12 * 3600)
        source_code = source.wait(timeout=60)
        if source_code or target_code:
            raise RuntimeError('镜像或仓库传输未完成；原引擎数据保留，可恢复后重试。')
    finally:
        source.stdout.close()
        for child in (target, source):
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def transfer(job):
    job, value = m.load_job(job)
    if value.get('mode') != 'in_place':
        raise ValueError('此迁移记录不支持原位切换')
    m.same_source(value)
    info = m.target_identity(value)
    target, source = m.native_endpoint(value), value['endpoint']
    if source == target or info['ID'] == value['engine_id']:
        raise ValueError('源与目标引擎不能相同')
    receipt_path = job / 'stream-receipts.json'
    receipt = common.read_json(receipt_path, {'job_id': value['job_id'], 'engine_id': info['ID'], 'images': [], 'volumes': {}})
    if receipt['job_id'] != value['job_id'] or receipt['engine_id'] != info['ID']:
        raise ValueError('传输记录对应其他目标引擎')
    ids = sorted(set(value['images'].values()) | {r['image'] for r in value['registries']})
    for identity in ids:
        m.cancelled(job)
        if identity not in receipt['images']:
            pipe(['image', 'save', identity], source, ['image', 'load'], target)
        if m.objects(['image', 'inspect', identity], target)[0]['Id'] != identity:
            raise ValueError('目标镜像校验失败')
        if identity not in receipt['images']:
            receipt['images'].append(identity)
            private_write(receipt_path, receipt)
    helper = value['images'][value['helper']]
    for item in value['registries']:
        m.cancelled(job)
        current = m.registry(m.objects(['inspect', item['id']], source)[0], item['name'])
        if current['running']:
            raise ValueError('原仓库重新启动，暂不转移')
        volume = item['volume']
        if volume not in receipt['volumes']:
            existing = m.docker(['volume', 'ls', '--format', '{{.Name}}'], target).splitlines()
            if volume in existing:
                raise ValueError('目标已有仓库卷，请先恢复未完成的切换')
            m.docker(['volume', 'create', '--label', 'expman.migration=' + value['job_id'], volume], target)
            before = summary(source, volume, helper, hashed=True)
            pipe(volume_args(volume, helper, m.VOLUME_BACKUP), source,
                 volume_args(volume, helper, m.VOLUME_RESTORE, readonly=False, interactive=True), target)
            after = summary(target, volume, helper, hashed=True)
            if before != after or before != summary(source, volume, helper, hashed=True):
                raise ValueError('仓库内容校验失败，原仓库保留')
            receipt['volumes'][volume] = before
            private_write(receipt_path, receipt)
        existing_names = m.docker(['ps', '-a', '--format', '{{.Names}}'], target).splitlines()
        if item['name'] in existing_names:
            restored = m.registry(m.objects(['inspect', item['name']], target)[0], item['name'])
            if any(restored[key] != item[key] for key in ('volume', 'image', 'port', 'component')):
                raise ValueError('目标仓库与传输记录不符')
            if not restored['running']:
                m.docker(['start', restored['id']], target)
            continue
        m.docker(['run', '-d', '--restart', 'unless-stopped', '--name', item['name'],
                  '--label', 'expman.component=' + item['component'],
                  '--network', 'host', '-e', 'REGISTRY_HTTP_ADDR=127.0.0.1:' + str(item['port']), '-v', volume + ':/var/lib/registry',
                  '-e', 'REGISTRY_HTTP_DEBUG_ADDR=', '-e', 'OTEL_TRACES_EXPORTER=none', item['image']], target)
    # Resolve the original pinned names; never silently rebuild/replace an image.
    for ref, expected in value['images'].items():
        m.cancelled(job)
        for attempt in range(12):
            try:
                m.docker(['pull', ref], target, timeout=1800)
                break
            except RuntimeError:
                if attempt == 11:
                    raise
                time.sleep(2)
        if m.objects(['image', 'inspect', ref], target)[0]['Id'] != expected:
            raise ValueError('运行环境摘要不匹配')
    private_write(job / 'target-engine.json', {'engine_id': info['ID']})
    return {'ready': True}


def cleanup_source(job):
    job, value = m.load_job(job)
    target_info = m.target_identity(value)
    verified = common.read_json(job / 'verified.json')
    committed = common.read_json(job / 'committed.json')
    if (committed.get('job_id') != value['job_id'] or verified.get('engine_id') != target_info['ID']
            or not verified.get('cuda') or value.get('mode') != 'in_place'):
        raise ValueError('新环境尚未完成交接，不清理原引擎')
    m.same_source(value)
    receipt = common.read_json(job / 'stream-receipts.json')
    if receipt.get('job_id') != value['job_id'] or receipt.get('engine_id') != target_info['ID']:
        raise ValueError('传输记录不属于已验证的新引擎')
    source = value['endpoint']
    skipped, removed = [], []
    # Only exact owned closed containers; no global prune, force, or Desktop stop.
    ids = m.docker(['ps', '-aq'], source).split()
    containers = m.objects(['inspect', *ids], source) if ids else []
    with worker_database.connect(Path(value['worker_root']) / 'node.sqlite3') as db:
        records = {row[0]: json.loads(row[1]) for row in db.execute('SELECT id,record FROM tasks')}
    db.close()
    for container in containers:
        labels = container.get('Config', {}).get('Labels') or {}
        if labels.get('expman.node') != value['node_id']:
            continue
        record = records.get(labels.get('expman.job'), {})
        if (container.get('State', {}).get('Status') in ('exited', 'dead')
                and record.get('state') == 'succeeded' and record.get('archive_scanned')
                and container.get('HostConfig', {}).get('ReadonlyRootfs') is True
                and container.get('Image') in receipt['images']):
            try:
                m.docker(['rm', container['Id']], source)
                removed.append(container['Id'])
            except RuntimeError:
                skipped.append(container.get('Name', container['Id']))
    for item in value['registries']:
        try:
            current = next((c for c in containers if c['Id'] == item['id']), None)
            if current:
                current = m.registry(m.objects(['inspect', item['id']], source)[0], item['name'])
                if any(current[key] != item[key] for key in ('volume', 'image', 'port', 'component')):
                    raise ValueError('原仓库身份发生变化')
            if current and current['running'] or any(any(mount.get('Name') == item['volume'] for mount in c.get('Mounts', []))
                                         for c in containers if c['Id'] != item['id']):
                raise ValueError('仓库仍被使用')
            if item['volume'] not in m.docker(['volume', 'ls', '--format', '{{.Name}}'], source).splitlines():
                continue
            if summary(source, item['volume'], value['images'][value['helper']], hashed=True) != receipt['volumes'].get(item['volume']):
                raise ValueError('原仓库在交接后变化')
            if current:
                m.docker(['rm', item['id']], source)
            m.docker(['volume', 'rm', item['volume']], source)
            removed.append(item['volume'])
        except (ValueError, RuntimeError):
            skipped.append(item['name'])
    for ref in value['images']:
        try:
            m.docker(['image', 'rm', ref], source)
            removed.append(ref)
        except RuntimeError:
            skipped.append(ref)
    result = {'ready': True, 'removed': removed, 'skipped': skipped}
    private_write(job / 'source-cleanup.json', result)
    return result
