"""Provision/stream/rollback contract on a disposable GitHub Ubuntu runner.

No GPU acceptance is claimed here. Refuses ordinary developer machines.
"""
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import uuid

from expman import common, worker_engine as engine, worker_migration as migration, worker_service as service
from expman import worker_transfer as transfer


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_OS') != 'Linux' or os.getuid() == 0:
        raise SystemExit('Disposable GitHub Ubuntu runner required; never run this on a Worker.')
    checkout = Path(__file__).resolve().parents[1]
    job = checkout / '.test-runs/engine-integration' / uuid.uuid4().hex
    package = job / 'package'
    package.mkdir(parents=True)
    (package / 'expman').symlink_to(checkout / 'expman', target_is_directory=True)
    shutil.copyfile(checkout / 'deploy/release/Prepare-Isolated-Engine.sh', package / 'prepare.sh')
    source = 'unix:///var/run/docker.sock'
    # Both image-store identity conventions must survive streaming unchanged.
    # The guard above restricts this daemon reconfiguration to disposable CI.
    store = os.environ.get('EXLAB_TEST_IMAGE_STORE', 'classic')
    assert store in ('classic', 'containerd')
    daemon = common.read_json('/etc/docker/daemon.json', {})
    daemon.setdefault('features', {})['containerd-snapshotter'] = store == 'containerd'
    common.atomic_json(job / 'source-daemon.json', daemon)
    run('sudo', 'cp', str(job / 'source-daemon.json'), '/etc/docker/daemon.json')
    run('sudo', 'systemctl', 'restart', 'docker.service')
    assert migration.containerd_store(migration.objects(['info', '--format', '{{json .}}'], source)) == (store == 'containerd')
    source_id = migration.objects(['info', '--format', '{{json .}}'], source)['ID']
    # Keep the standard /var/run proxy path used by real Desktop integration.
    # A synthetic /mnt/wsl prefix previously concealed the 0.6.0 regression.
    assert migration.source_socket(source) == source
    socket_alias = job / 'source.sock'
    socket_alias.symlink_to('/var/run/docker.sock')
    assert migration.source_socket('unix://' + str(socket_alias)) == 'unix://' + str(socket_alias)
    config = job / 'node.json'
    common.atomic_json(config, {'node_id': 'ci-engine', 'token': 'fixture-only', 'hub_url': 'http://127.0.0.1:1',
                               'root': str(job / 'node'), 'profiles': {}})
    service._save_settings(job / 'service', {'config': str(config), 'backend': 'detached'})
    migration.docker(['pull', 'python:3.13-slim'], source, timeout=600)
    helper = migration.objects(['image', 'inspect', 'python:3.13-slim'], source)[0]
    ref = helper['RepoDigests'][0]
    migration.docker(['pull', 'registry:2'], source, timeout=600)
    name, volume = 'expman-worker-registry', 'expman-worker-registry-data'
    if name in migration.docker(['ps', '-a', '--format', '{{.Names}}'], source).splitlines():
        raise RuntimeError('Fixture registry already exists')
    migration.docker(['volume', 'create', volume], source)
    migration.docker(transfer.volume_args(volume, helper['Id'], "from pathlib import Path;Path('/backup/fixture').write_bytes(b'registry-preserved'*100000)", readonly=False), source)
    migration.docker(['create', '--name', name, '--network', 'host', '--label', 'expman.component=worker-registry',
                      '-v', volume + ':/var/lib/registry', '-e', 'REGISTRY_HTTP_ADDR=127.0.0.1:5001',
                      '-e', 'REGISTRY_HTTP_DEBUG_ADDR=', 'registry:2'], source)
    registry = migration.registry(migration.objects(['inspect', name], source)[0], name)
    value = {'mode': 'in_place', 'job_id': job.name, 'config': str(config), 'service_root': str(job / 'service'),
             'worker_root': str(job / 'node'), 'linux_user': pwd.getpwuid(os.getuid()).pw_name, 'uid': os.getuid(),
             'node_id': 'ci-engine', 'endpoint': source, 'engine_id': source_id, 'containerd_store': store == 'containerd',
             'images': {ref: helper['Id']}, 'helper': ref, 'registries': [registry]}
    common.atomic_json(job / 'manifest.json', value)
    run('sudo', 'bash', str(package / 'prepare.sh'), job.name, str(job))
    run('sudo', 'systemctl', 'start', engine.SERVICE)
    # Refresh group membership without changing the CI user's login session.
    script = """import sys;from pathlib import Path
from expman import worker_transfer as t,worker_migration as m,worker_engine as e
job=Path(sys.argv[1])
try:t.transfer(job);t.transfer(job)
except Exception:
 print(m.docker(['image','ls','--no-trunc'],e.ENDPOINT));print(m.load_job(job)[1]['images']);raise
_,value=m.load_job(job)
assert m.objects(['info','--format','{{json .}}'],e.ENDPOINT)['ID'] != value['engine_id']
assert m.docker(['run','--rm','--network','none',value['helper'],'python','-c','print(6*7)'],e.ENDPOINT).strip()=='42'
assert t.summary(value['endpoint'],value['registries'][0]['volume'],value['images'][value['helper']],hashed=True)==t.summary(e.ENDPOINT,value['registries'][0]['volume'],value['images'][value['helper']],hashed=True)
print('PASS isolated engine provisioning, streamed images/volume hashes, replay and CPU container')
"""
    probe = job / 'probe.py'
    probe.write_text(script)
    subprocess.run(['sg', 'docker', '-c', 'PYTHONPATH=' + str(checkout) + ' python3 ' + str(probe) + ' ' + str(job)], check=True)
    run('sudo', 'bash', str(package / 'prepare.sh'), job.name, str(job), 'rollback')
    assert not engine.ENGINE.exists() and not engine.DATA.exists()
    assert migration.objects(['info', '--format', '{{json .}}'], source)['ID'] == source_id
    assert migration.objects(['inspect', registry['id']], source)[0]['Id'] == registry['id']
    print('PASS rollback preserved the original daemon, source volume and container')


if __name__ == '__main__':
    main()
