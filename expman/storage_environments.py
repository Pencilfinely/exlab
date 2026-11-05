"""Reclaim only obsolete, pinned, remotely recoverable environment caches."""
import json
import re
import urllib.parse


def obsolete(config, records):
    protected = {p.get('image') for p in config.get('profiles', {}).values()}
    candidates = {}
    for spec in config.get('task_templates', []):
        protected.update(e.get('image') for e in spec.get('environments', []))
    for record in records:
        for environment in record.get('spec', {}).get('environments', []):
            ref = environment.get('image', '')
            if record.get('state') == 'succeeded' and record.get('archive_scanned') and not record.get('execution_retired'):
                if re.fullmatch(r'[^\s@]+@sha256:[a-f0-9]{64}', ref):
                    candidates[ref] = record['id']
            else:
                protected.add(ref)
    return {ref: identity for ref, identity in candidates.items() if ref not in protected}


def inspect_image(ref, execute):
    result = execute(['docker', 'image', 'inspect', ref], check=False)
    if result.returncode:
        return None
    item = json.loads(result.stdout)[0]
    # Shared tags or digests may belong to another application. Do not untag them.
    if item.get('RepoTags') or set(item.get('RepoDigests') or []) != {ref}:
        return None
    used = execute(['docker', 'ps', '-aq', '--filter', 'ancestor=' + item['Id']]).stdout.strip()
    return None if used else item


def plan(config, records, execute):
    result = []
    for ref, job_id in obsolete(config, records).items():
        item = inspect_image(ref, execute)
        if item:
            result.append({'kind': 'environment', 'job_id': job_id, 'image': ref,
                           'image_id': item['Id'], 'bytes': 0, 'logical_bytes': item.get('Size', 0)})
    return result


def reclaim(item, config, records, execute):
    ref = item['image']
    if ref not in obsolete(config, records):
        raise ValueError('环境仍被节点或实验引用，已保留')
    current = inspect_image(ref, execute)
    if not current or current['Id'] != item['image_id']:
        raise ValueError('环境缓存身份或引用变化，已保留')
    argv = ['docker', 'manifest', 'inspect', '--verbose']
    host = urllib.parse.urlsplit('https://' + ref.split('/')[0]).hostname
    if host in ('localhost', '127.0.0.1', '::1'):
        argv.append('--insecure')
    manifest = json.loads(execute([*argv, ref], timeout=20).stdout)
    if not isinstance(manifest, dict) or manifest.get('Descriptor', {}).get('digest') != ref.rsplit('@', 1)[1]:
        raise ValueError('原环境无法从仓库按摘要恢复，缓存已保留')
    # Docker itself refuses deleting an image still used by any container.
    execute(['docker', 'image', 'rm', ref], timeout=120)
