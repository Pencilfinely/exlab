import configparser
import io
import json
from pathlib import Path
import tarfile
import unittest
from unittest.mock import patch

from expman import common, worker_migration as migration, worker_service
from expman.agent import Agent
from tests.support import temporary_directory


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.job = Path(self.tmp.__enter__())
        self.addCleanup(self.tmp.__exit__, None, None, None)
        self.root = self.job / 'service'
        self.root.mkdir()
        self.config = self.job / 'node.json'
        self.config.write_text(json.dumps({'node_id': 'original', 'token': 'private', 'hub_url': 'http://hub',
                                          'root': str(self.root), 'profiles': {}}))
        self.value = {'job_id': 'a' * 32, 'node_id': 'original', 'service_root': str(self.root),
                      'config': str(self.config), 'worker_root': str(self.root), 'linux_user': 'owner',
                      'engine_id': 'desktop-original', 'endpoint': 'unix:///old.sock', 'registries': [],
                      'images': {}, 'gpus': [{'name': 'RTX 5070 Ti', 'uuid': 'GPU-original'}]}
        common.atomic_json(self.job / 'manifest.json', self.value)
        common.atomic_json(self.job / 'source-ready.json', {'config_sha256': migration.digest(self.config), 'files': {}})
        common.atomic_json(self.job / 'target-engine.json', {'engine_id': 'native-new'})

    def write_value(self, **changes):
        self.value.update(changes)
        common.atomic_json(self.job / 'manifest.json', self.value)

    def registry(self):
        return {'Name': '/expman-worker-registry', 'Id': 'owned-container', 'Image': 'sha256:registry',
                'Config': {'Labels': {'expman.component': 'worker-registry'}, 'Env': []},
                'Mounts': [{'Type': 'volume', 'Name': 'expman-worker-registry-data', 'Destination': '/var/lib/registry'}],
                'HostConfig': {'NetworkMode': 'bridge', 'PortBindings': {
                    '5000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '5001'}]}}, 'State': {'Running': True}}

    def test_only_exact_owned_registry_volume_and_local_network_are_migrated(self):
        item = self.registry()
        self.assertEqual(migration.registry(item, 'expman-worker-registry')['id'], 'owned-container')
        for mutation in (lambda i: i['Mounts'].append({'Type': 'bind', 'Source': '/private'}),
                         lambda i: i['Config']['Labels'].clear(),
                         lambda i: i['HostConfig']['PortBindings']['5000/tcp'][0].update(HostIp='0.0.0.0'),
                         lambda i: i['Mounts'][0].update(Name='unrelated-volume')):
            item = self.registry()
            mutation(item)
            with self.assertRaises(ValueError):
                migration.registry(item, 'expman-worker-registry')

    def test_existing_host_registry_contract_is_supported(self):
        item = self.registry()
        item['HostConfig'] = {'NetworkMode': 'host'}
        item['Config']['Env'] = ['REGISTRY_HTTP_ADDR=127.0.0.1:5001', 'REGISTRY_HTTP_DEBUG_ADDR=']
        self.assertEqual(migration.registry(item, 'expman-worker-registry')['port'], 5001)
        item['Config']['Env'].pop()
        with self.assertRaises(ValueError):
            migration.registry(item, 'expman-worker-registry')

    def container(self, name='other-app', mounts=None, labels=None, running=True):
        return {'Id': 'id-' + name, 'Name': '/' + name, 'State': {'Running': running},
                'Config': {'Labels': labels}, 'Mounts': mounts or []}

    def test_unrelated_desktop_containers_remain_running_and_do_not_block(self):
        items = [self.registry(), self.container('redis'),
                 self.container('database', [{'Type': 'volume', 'Name': 'postgres-data'}]),
                 self.container('windows-app', [{'Type': 'bind', 'Source': '/run/desktop/mnt/host/c/data'}]),
                 self.container('worker-job', labels={'expman.node': 'original', 'expman.job': 'job-1'})]
        before = json.dumps(items)
        with patch.object(migration, 'docker') as docker:
            owned, preserved = migration.container_plan(items, 'original')
            docker.assert_not_called()
        self.assertEqual([r['name'] for r in owned], ['expman-worker-registry'])
        self.assertEqual([r['name'] for r in preserved], ['redis', 'database', 'windows-app'])
        self.assertEqual(json.dumps(items), before)

    def test_blockers_name_containers_and_actual_shared_resource(self):
        items = [self.container('notebook', [{'Type': 'bind', 'Source': '/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/Ubuntu/hash'}]),
                 self.container('registry-writer', [{'Type': 'volume', 'Name': 'expman-worker-registry-data'}])]
        with self.assertRaises(ValueError) as raised:
            migration.container_plan(items, 'original')
        self.assertIn('notebook', str(raised.exception))
        self.assertIn('registry-writer', str(raised.exception))
        self.assertIn('Linux/WSL', str(raised.exception))
        self.assertIn('镜像仓库卷', str(raised.exception))

    def test_stopped_containers_and_windows_drives_are_allowed_but_unknown_binds_are_not(self):
        for source in ('C:/data', '/host_mnt/d/data'):
            self.assertEqual(len(migration.container_plan([self.container(mounts=[{'Type': 'bind', 'Source': source}])], 'original')[1]), 1)
        for source in ('/home/user/data', '/mnt/c/data', '/var/run/docker.sock'):
            item = self.container(mounts=[{'Type': 'bind', 'Source': source}])
            with self.assertRaises(ValueError):
                migration.container_plan([item], 'original')
            item['State']['Running'] = False
            self.assertEqual(migration.container_plan([item], 'original'), ([], []))

    def test_restart_loops_and_missing_mount_inventory_are_not_ignored(self):
        item = self.container(running=False)
        item['State']['Restarting'] = True
        del item['Mounts']
        with self.assertRaisesRegex(ValueError, 'other-app'):
            migration.container_plan([item], 'original')

    def test_bind_from_another_identified_distribution_can_keep_running(self):
        item = self.container('another-distro', [{'Type': 'bind', 'Source': '/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/Ubuntu-24.04/hash'}])
        self.assertEqual(len(migration.container_plan([item], 'original', 'Ubuntu-22.04')[1]), 1)
        for distribution in (None, 'Ubuntu-24.04'):
            with self.assertRaises(ValueError):
                migration.container_plan([item], 'original', distribution)

    def test_resources_are_rechecked_immediately_before_wsl_export(self):
        with patch.object(migration, 'same_source'), \
                patch.object(migration, 'inspect_containers', side_effect=ValueError('new-container uses WSL')), \
                patch.object(migration, 'idle') as idle:
            with self.assertRaisesRegex(ValueError, 'new-container'):
                migration.pre_export(self.job)
            idle.assert_not_called()

    def test_unpinned_images_are_never_silently_replaced(self):
        ref = 'localhost:5001/runtime@sha256:' + 'b' * 64
        self.assertEqual(migration.image_refs({'profiles': [{'image': ref}]}), {ref})
        with self.assertRaises(ValueError):
            migration.image_refs({'image': 'runtime:latest'})

    def test_cloned_docker_configuration_does_not_require_desktop_after_uninstall(self):
        original = {'currentContext': 'desktop-linux', 'credsStore': 'desktop.exe',
                    'auths': {'private-registry': {'auth': 'keep-inline-auth'}},
                    'credHelpers': {'localhost:5001': 'desktop.exe', 'private': 'custom-helper'}}
        result = migration.docker_client_config(original)
        self.assertNotIn('credsStore', result)
        self.assertEqual(result['currentContext'], 'default')
        self.assertNotIn('localhost:5001', result['credHelpers'])
        self.assertEqual(result['auths'], original['auths'])
        self.assertEqual(original['credsStore'], 'desktop.exe')

    def test_local_endpoint_ignores_docker_context_and_rejects_remote(self):
        with patch.dict(migration.os.environ, {'DOCKER_CONTEXT': 'remote'}):
            self.assertNotIn('DOCKER_CONTEXT', migration.environment('unix:///var/run/docker.sock'))
        for endpoint in ('tcp://host:2375', 'ssh://remote', 'unix:///bad path'):
            with self.assertRaises(ValueError):
                migration.environment(endpoint)

    def test_first_clone_boot_disables_systemd_boot_command_and_external_mounts(self):
        text = migration.safe_wsl_conf('[boot]\nsystemd=true\ncommand=run-old-agent\n[automount]\nmountFsTab=true\n', 'owner', systemd=False)
        parsed = configparser.ConfigParser()
        parsed.read_string(text)
        self.assertFalse(parsed['boot'].getboolean('systemd'))
        self.assertNotIn('command', parsed['boot'])
        self.assertFalse(parsed['automount'].getboolean('mountFsTab'))
        self.assertEqual(parsed['user']['default'], 'owner')

    def test_export_is_sanitized_before_import_and_data_members_survive(self):
        archive = self.job / 'distribution.tar'
        with tarfile.open(archive, 'w') as out:
            for name, data in (('./etc/wsl.conf', b'[boot]\nsystemd=true\ncommand=unsafe\n'),
                               ('home/owner/experiment.bin', b'checkpoint-data')):
                item = tarfile.TarInfo(name)
                item.size = len(data)
                out.addfile(item, io.BytesIO(data))
        migration.sanitize(self.job)
        with tarfile.open(archive) as source:
            self.assertEqual(source.extractfile('home/owner/experiment.bin').read(), b'checkpoint-data')
            config = source.extractfile('etc/wsl.conf').read().decode()
            self.assertIn('systemd = false', config)
            self.assertNotIn('unsafe', config)
            self.assertEqual(json.load(source.extractfile('etc/exlab-migration.json'))['job_id'], 'a' * 32)
        self.assertEqual(common.read_json(self.job / 'distribution-sha256.json')['sha256'], migration.digest(archive))

    def test_registry_archive_corruption_stops_before_loading_docker(self):
        (self.job / 'images.tar').write_bytes(b'broken')
        common.atomic_json(self.job / 'checksums.json', {'images.tar': 'incorrect'})
        with patch.object(migration, 'target_identity', return_value={'ID': 'native-new'}), patch.object(migration, 'stream') as transfer:
            with self.assertRaises(ValueError):
                migration.restore(self.job)
            transfer.assert_not_called()

    def test_cuda_failure_does_not_rebind_engine_or_unlock_node(self):
        state = self.job / 'setup-state.json'
        common.atomic_json(state, {'docker_id': 'desktop-original'})
        fence = self.root / migration.FENCE
        common.atomic_json(fence, {'job_id': 'a' * 32})
        with patch.object(migration, 'target_identity', return_value={'ID': 'native-new'}), \
                patch.object(migration.worker_gpu, '_profiles', return_value={'profile': 'image@sha256:' + 'a' * 64}), \
                patch.object(migration.worker_gpu, '_verify', side_effect=RuntimeError('CUDA failure')):
            with self.assertRaisesRegex(RuntimeError, 'CUDA failure'):
                migration.verify(self.job)
        self.assertEqual(common.read_json(state)['docker_id'], 'desktop-original')
        self.assertTrue(fence.exists())
        self.assertFalse((self.job / 'verified.json').exists())

    def test_database_mismatch_blocks_gpu_and_handoff(self):
        database = self.root / 'node.sqlite3'
        database.write_bytes(b'changed')
        common.atomic_json(self.job / 'source-ready.json', {'config_sha256': migration.digest(self.config),
                                                         'files': {str(database): 'original-sha'}})
        with patch.object(migration, 'target_identity', return_value={'ID': 'native-new'}), \
                patch.object(migration.worker_gpu, '_verify') as gpu:
            with self.assertRaises(ValueError):
                migration.verify(self.job)
            gpu.assert_not_called()

    def test_verified_target_keeps_node_identity_and_records_actual_cuda_receipts(self):
        original = self.config.read_bytes()
        with patch.object(migration, 'target_identity', return_value={'ID': 'native-new'}), \
                patch.object(migration.worker_gpu, '_profiles', return_value={'profile': 'fixed-image'}), \
                patch.object(migration.worker_gpu, '_verify', return_value={'status': 'passed', 'gpu_uuid': 'GPU-original'}) as gpu, \
                patch.object(migration.service, 'install', return_value={'running': False}), \
                patch.object(migration.worker_boot, 'inspect', return_value={'ready': True}):
            self.assertTrue(migration.verify(self.job)['ready'])
            self.assertEqual(gpu.call_args.args[1], 'GPU-original')
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(common.read_json(self.job / 'setup-state.json')['docker_id'], 'native-new')
        self.assertEqual(common.read_json(self.job / 'verified.json')['cuda'][0]['status'], 'passed')

    def test_activation_requires_current_engine_and_gpu_receipt(self):
        fence = self.root / migration.FENCE
        common.atomic_json(fence, {'job_id': 'a' * 32})
        common.atomic_json(self.job / 'verified.json', {'node_id': 'original', 'engine_id': 'other', 'cuda': [{}]})
        with patch.object(migration, 'target_identity', return_value={'ID': 'native-new'}):
            with self.assertRaises(ValueError):
                migration.activate(self.job)
        self.assertTrue(fence.exists())

    def test_fenced_source_cannot_start_or_serve_or_open_agent(self):
        common.atomic_json(self.root / migration.FENCE, {'job_id': 'a' * 32})
        common.atomic_json(self.job / migration.FENCE, {'job_id': 'a' * 32})
        with self.assertRaises(RuntimeError):
            worker_service.start(self.root)
        self.assertEqual(worker_service.serve(self.root), 0)
        with self.assertRaises(RuntimeError):
            Agent(self.config)

    def test_rollback_restores_original_policy_but_never_changes_foreign_fence(self):
        policy = {'mode': 'drain', 'until': None, 'reason': 'original'}
        common.atomic_json(self.job / 'rollback.json', {'override': policy, 'was_running': True})
        common.atomic_json(self.job / 'compute-override.json', {'mode': 'stop'})
        common.atomic_json(self.root / migration.FENCE, {'job_id': 'a' * 32})
        common.atomic_json(self.job / migration.FENCE, {'job_id': 'a' * 32})
        with patch.object(migration, 'MARKER', self.job / 'missing'), patch.object(migration, 'same_source'):
            self.assertTrue(migration.rollback(self.job)['was_running'])
        self.assertEqual(common.read_json(self.job / 'compute-override.json'), policy)
        self.assertFalse((self.root / migration.FENCE).exists())
        common.atomic_json(self.root / migration.FENCE, {'job_id': 'another-operation'})
        with self.assertRaises(ValueError):
            migration.remove_fence(self.root / migration.FENCE, 'a' * 32)
        self.assertTrue((self.root / migration.FENCE).exists())

    def test_rollback_refuses_to_run_in_clone(self):
        marker = self.job / 'marker'
        common.atomic_json(marker, {'job_id': 'a' * 32})
        with patch.object(migration, 'MARKER', marker), patch.object(migration, 'same_source') as source:
            with self.assertRaises(ValueError):
                migration.rollback(self.job)
            source.assert_not_called()

    def test_idle_requires_no_executing_containers_and_preservable_records(self):
        with patch.object(migration.service, 'update_status', return_value={'ready_for_update': True}) as state, \
                patch.object(migration.service, '_settings', return_value={'config': str(self.config)}), \
                patch.object(migration.service, '_update_containers', return_value=['running-container']) as containers:
            self.assertFalse(migration.idle(self.root))
            containers.return_value = []
            self.assertTrue(migration.idle(self.root))
            self.assertTrue(state.call_args.kwargs['preserve_experiments'])

    def test_cancel_while_draining_is_recoverable_without_stopping_experiments(self):
        common.atomic_json(self.job / 'compute-override.json', {'mode': 'available'})
        (self.job / 'cancel').touch()
        with patch.object(migration, 'same_source'), \
                patch.object(migration.service, 'status', return_value={'running': True}), \
                patch.object(migration.service, 'compute_availability'), \
                patch.object(migration.service, 'stop_for_update') as stop:
            with self.assertRaisesRegex(RuntimeError, '取消'):
                migration.prepare(self.job)
            stop.assert_not_called()
        self.assertEqual(common.read_json(self.job / 'rollback.json')['override']['mode'], 'available')


if __name__ == '__main__':
    unittest.main()
