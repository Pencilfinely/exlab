"""Provision/stream/rollback contract on a disposable GitHub Ubuntu runner.

No GPU acceptance is claimed here. Refuses ordinary developer machines.
"""
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import time
import uuid
from unittest.mock import patch

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
    # Exact unlabelled registry created by the original node installer. It is
    # permanent infrastructure, not work that can ever naturally finish.
    legacy_name = 'expman-registry'
    migration.docker(['pull', 'registry:3'], source, timeout=600)
    migration.docker(['run', '-d', '--name', legacy_name, '--restart', 'unless-stopped',
                      '-p', '127.0.0.1:5000:5000', '-e', 'OTEL_TRACES_EXPORTER=none',
                      '-v', 'expman-registry-data:/var/lib/registry', 'registry:3'], source)
    legacy = migration.registry(migration.objects(['inspect', legacy_name], source)[0], legacy_name)
    local_tag = 'localhost:5000/migration-fixture:original'
    migration.docker(['tag', ref, local_tag], source)
    for attempt in range(15):
        try:
            migration.docker(['push', local_tag], source, timeout=120)
            break
        except RuntimeError:
            if attempt == 14:
                raise
            time.sleep(1)
    migration.docker(['pull', local_tag], source)
    local_image = migration.objects(['image', 'inspect', local_tag], source)[0]
    local_ref = next(ref for ref in local_image['RepoDigests'] if ref.startswith('localhost:5000/'))
    assert migration.LOCAL_IMAGE.fullmatch(local_ref)
    value = {'mode': 'in_place', 'job_id': job.name, 'config': str(config), 'service_root': str(job / 'service'),
             'worker_root': str(job / 'node'), 'linux_user': pwd.getpwuid(os.getuid()).pw_name, 'uid': os.getuid(),
             'node_id': 'ci-engine', 'endpoint': source, 'engine_id': source_id, 'containerd_store': store == 'containerd',
             'images': {ref: helper['Id'], local_ref: local_image['Id']}, 'helper': ref,
             'registries': [registry, legacy], 'move_disk': True}
    common.atomic_json(job / 'manifest.json', value)
    # Real running container with a Linux bind: preflight accepts a queue, and
    # the queue waits for its natural exit without terminating it. Node drain
    # itself is exercised separately with active-record/cancellation fixtures.
    busy_name = 'migration-queue-' + job.name
    busy = migration.docker(['run', '-d', '--name', busy_name, '--network', 'none',
        '--mount', 'type=bind,src=' + str(job) + ',dst=/queue-result', helper['Id'], 'python', '-c',
        "import time;from pathlib import Path;time.sleep(6);Path('/queue-result/finished').write_text('completed')"], source)
    waiting = []
    migration.inspect_containers(source, value['node_id'], waiting)
    assert any(busy_name in detail for detail in waiting)
    # Bound the regression: the broken implementation would wait forever for
    # expman-registry, even after the actual workload had exited successfully.
    queue_deadline = time.monotonic() + 45
    def idle(_root):
        assert time.monotonic() < queue_deadline, 'Permanent registry blocked migration queue'
        assert migration.objects(['inspect', legacy['id']], source)[0]['State']['Running']
        return True
    with patch.object(migration, 'idle', side_effect=idle):
        migration.wait_until_idle(job, value)
    state = migration.objects(['inspect', busy], source)[0]['State']
    assert not state['Running'] and state['ExitCode'] == 0
    assert (job / 'finished').read_text() == 'completed'
    assert migration.objects(['inspect', legacy['id']], source)[0]['State']['Running']
    migration.stop_registries(value)
    assert not migration.objects(['inspect', legacy['id']], source)[0]['State']['Running']
    print('PASS queue waited for real work, kept legacy registry alive, then stopped it for transfer')
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
for registry in value['registries']:
 assert t.summary(value['endpoint'],registry['volume'],value['images'][value['helper']],hashed=True)==t.summary(e.ENDPOINT,registry['volume'],value['images'][value['helper']],hashed=True)
 restored=m.registry(m.objects(['inspect',registry['name']],e.ENDPOINT)[0],registry['name'])
 assert restored['running'] and restored['port']==registry['port']
local_ref=next(ref for ref in value['images'] if ref.startswith('localhost:5000/'))
assert m.docker(['run','--rm','--network','none',local_ref,'python','-c','print(6*7)'],e.ENDPOINT).strip()=='42'
print('PASS isolated engine, legacy registry port/digest pull, all volume hashes, replay and CPU container')
"""
    probe = job / 'probe.py'
    probe.write_text(script)
    subprocess.run(['sg', 'docker', '-c', 'PYTHONPATH=' + str(checkout) + ' python3 ' + str(probe) + ' ' + str(job)], check=True)
    run('sudo', 'bash', str(package / 'prepare.sh'), job.name, str(job), 'rollback')
    assert not engine.ENGINE.exists() and not engine.DATA.exists()
    assert migration.objects(['info', '--format', '{{json .}}'], source)['ID'] == source_id
    assert migration.objects(['inspect', registry['id']], source)[0]['Id'] == registry['id']
    migration.rollback(job)
    assert migration.objects(['inspect', legacy['id']], source)[0]['State']['Running']
    print('PASS rollback preserved original daemon/data and restarted the original legacy registry')


if __name__ == '__main__':
    main()
