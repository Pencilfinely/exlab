import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from expman import common, worker_engine as engine, worker_transfer as transfer, worker_migration as migration
from tests.support import temporary_directory


class EngineTransactionTests(unittest.TestCase):
    def setUp(self):
        temp = temporary_directory()
        self.root = Path(temp.__enter__()).resolve()
        self.addCleanup(temp.__exit__, None, None, None)
        self.job = self.root / ('a' * 32)
        self.job.mkdir()
        self.user = SimpleNamespace(pw_dir=str(self.root / 'home'), pw_uid=1000, pw_gid=1000, pw_name='owner')
        self.value = {'job_id': self.job.name, 'endpoint': 'unix:///mnt/wsl/desktop/docker.sock',
                      'engine_id': 'desktop-id', 'linux_user': 'owner', 'service_root': str(self.root / 'service'),
                      'config': str(self.root / 'node/node.json')}
        Path(self.value['config']).parent.mkdir()
        self.paths = [self.root / 'wsl.conf', self.root / 'user.service', self.root / 'node/setup-state.json']
        self.paths[0].write_text('[boot]\nsystemd=false\n')
        for name, relative in {'MARKER': 'marker.json', 'ENGINE': 'engine', 'DATA': 'data', 'CONFIG': 'daemon.json',
                               'UNIT': 'engine.service', 'WSL': 'wsl.conf',
                               'CONTAINERD_UNIT': 'containerd.service', 'CONTAINERD_CONFIG': 'containerd.toml'}.items():
            manager = patch.object(engine, name, self.root / relative)
            manager.start();self.addCleanup(manager.stop)
        manager = patch.object(engine, 'context', return_value=(self.job, self.value, self.user, self.paths))
        manager.start();self.addCleanup(manager.stop)
        manager = patch.object(engine, 'systemctl', return_value=subprocess.CompletedProcess([], 0, 'inactive\n', ''))
        self.systemctl = manager.start();self.addCleanup(manager.stop)
        manager = patch.object(engine.os, 'chown', create=True)
        manager.start();self.addCleanup(manager.stop)

    def test_existing_client_configuration_is_never_claimed_or_deleted(self):
        client = Path(self.user.pw_dir) / '.config/exlab/docker'
        client.mkdir(parents=True)
        (client / 'valuable').write_text('preserve')
        with self.assertRaises(ValueError):
            engine.begin(self.job, self.job.name)
        self.assertFalse((self.job / 'engine-before.json').exists())
        self.assertEqual((client / 'valuable').read_text(), 'preserve')

    def test_rollback_restores_small_configuration_and_only_its_new_directories(self):
        original = self.paths[0].read_bytes()
        outside = self.root / 'experiments'
        outside.mkdir();(outside / 'weight.pt').write_text('keep')
        engine.begin(self.job, self.job.name)
        self.paths[0].write_text('changed')
        for path in (engine.ENGINE, engine.DATA, Path(self.user.pw_dir) / '.config/exlab/docker'):
            path.mkdir(parents=True);(path / 'created').write_text('temporary')
        engine.rollback(self.job, self.job.name)
        self.assertEqual(self.paths[0].read_bytes(), original)
        self.assertEqual((outside / 'weight.pt').read_text(), 'keep')
        self.assertFalse(engine.DATA.exists())
        self.assertEqual(common.read_json(self.paths[2])['docker_endpoint'], self.value['endpoint'])
        self.assertTrue(all(call.args[-1] in (engine.SERVICE, engine.CONTAINERD_SERVICE) for call in self.systemctl.call_args_list if call.args[0] != 'daemon-reload'))

    def test_committed_or_foreign_marker_blocks_rollback_before_any_service_change(self):
        engine.begin(self.job, self.job.name)
        common.atomic_json(self.job / 'committed.json', {'job_id': self.job.name})
        with self.assertRaises(ValueError):
            engine.rollback(self.job, self.job.name)
        self.systemctl.assert_not_called()
        (self.job / 'committed.json').unlink()
        common.atomic_json(engine.MARKER, {'job_id': 'b' * 32})
        with self.assertRaises(ValueError):
            engine.rollback(self.job, self.job.name)
        self.systemctl.assert_not_called()

    def test_crash_between_journal_and_marker_is_recoverable_without_mutation(self):
        engine.begin(self.job, self.job.name)
        engine.MARKER.unlink()
        engine.rollback(self.job, self.job.name)
        self.systemctl.assert_not_called()
        self.assertTrue(common.read_json(self.job / 'engine-rollback.json')['ready'])

    def test_configure_keeps_global_socket_and_client_untouched(self):
        engine.begin(self.job, self.job.name)
        binary = engine.ENGINE / 'usr/bin'
        binary.mkdir(parents=True)
        for name in ('docker', 'dockerd', 'containerd', 'runc'):
            (binary / name).write_text('extracted')
        desktop = Path(self.user.pw_dir) / '.docker/config.json'
        common.atomic_json(desktop, {'credsStore': 'desktop.exe', 'auths': {'registry': {'auth': 'test'}}})
        before = desktop.read_bytes()
        with patch.object(engine.subprocess, 'run'):
            engine.configure(self.job, self.job.name)
        daemon = common.read_json(engine.CONFIG)
        self.assertEqual(daemon['hosts'], [engine.ENDPOINT])
        self.assertEqual(daemon['data-root'], str(engine.DATA))
        self.assertNotIn('runc', daemon['runtimes'])  # reserved by Docker
        self.assertEqual(daemon['default-runtime'], 'exlab-runc')
        self.assertEqual(daemon['containerd'], '/run/exlab-containerd/containerd.sock')
        self.assertIn(str(engine.DATA / 'containerd').replace('\\', '\\\\'), engine.CONTAINERD_CONFIG.read_text())
        client = common.read_json(Path(self.user.pw_dir) / '.config/exlab/docker/config.json')
        self.assertNotIn('credsStore', client)
        self.assertEqual(desktop.read_bytes(), before)
        self.assertIn('exlab-docker', engine.UNIT.read_text())

    def test_redirected_privileged_path_is_rejected(self):
        link = self.root / 'alias'
        try:
            link.symlink_to(self.job, target_is_directory=True)
        except OSError:
            self.skipTest('Symbolic links unavailable')
        with self.assertRaises(ValueError):
            engine.direct(link / 'new-config')


class StreamTransferTests(unittest.TestCase):
    def setUp(self):
        temp = temporary_directory()
        self.job = Path(temp.__enter__()).resolve()
        self.addCleanup(temp.__exit__, None, None, None)
        self.value = {'mode': 'in_place', 'job_id': 'a' * 32, 'engine_id': 'old',
                      'endpoint': 'unix:///mnt/wsl/old.sock', 'node_id': 'owner',
                      'images': {'registry/project@sha256:abc': 'sha256:abc'}, 'registries': []}
        self.value['helper'] = next(iter(self.value['images']))
        for name, value in [('load_job', (self.job, self.value)), ('same_source', None), ('target_identity', {'ID': 'new'})]:
            manager = patch.object(migration, name, return_value=value)
            manager.start();self.addCleanup(manager.stop)

    def test_stream_saves_verified_receipt_and_does_not_delete_source(self):
        with patch.object(transfer, 'pipe') as pipe, patch.object(migration, 'docker') as docker, \
                patch.object(migration, 'objects', return_value=[{'Id': 'sha256:abc'}]):
            transfer.transfer(self.job)
            pipe.assert_called_once()
            self.assertEqual(common.read_json(self.job / 'stream-receipts.json')['images'], ['sha256:abc'])
            self.assertFalse(any('rm' in c.args[0] for c in docker.call_args_list))
            pipe.reset_mock()
            transfer.transfer(self.job)
            pipe.assert_not_called()

    def test_failed_destination_verification_never_records_success_or_removes_source(self):
        with patch.object(transfer, 'pipe'), patch.object(migration, 'docker') as docker, \
                patch.object(migration, 'objects', return_value=[{'Id': 'wrong'}]):
            with self.assertRaises(ValueError):
                transfer.transfer(self.job)
            self.assertFalse((self.job / 'target-engine.json').exists())
            docker.assert_not_called()

    def test_source_cleanup_requires_matching_commit_cuda_and_stream_receipts(self):
        common.atomic_json(self.job / 'verified.json', {'engine_id': 'new', 'cuda': []})
        common.atomic_json(self.job / 'committed.json', {'job_id': self.value['job_id']})
        with patch.object(migration, 'docker') as docker:
            with self.assertRaises(ValueError):
                transfer.cleanup_source(self.job)
            common.atomic_json(self.job / 'verified.json', {'engine_id': 'new', 'cuda': ['passed']})
            common.atomic_json(self.job / 'stream-receipts.json', {'job_id': 'foreign', 'engine_id': 'new'})
            with self.assertRaises(ValueError):
                transfer.cleanup_source(self.job)
            docker.assert_not_called()

    def test_custom_endpoint_always_selects_isolated_executable_and_client(self):
        from expman.runtime import docker_environment
        with patch.dict(os.environ, {'DOCKER_CONTEXT': 'desktop-linux'}):
            env = docker_environment(engine.ENDPOINT)
        self.assertNotIn('DOCKER_CONTEXT', env)
        self.assertTrue(env['PATH'].startswith('/opt/exlab/docker/usr/bin'))
        self.assertTrue(env['DOCKER_CONFIG'].replace('\\', '/').endswith('.config/exlab/docker'))
        self.assertEqual(env['DOCKER_HOST'], engine.ENDPOINT)
