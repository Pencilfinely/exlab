"""The node installer's permanent registry must never wait for its own exit."""
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from expman import common, worker_migration as migration
from tests.support import temporary_directory


def legacy_registry():
    # Match scripts/node_installer.py before the component label was added.
    return {'Name': '/expman-registry', 'Id': 'original-registry', 'Image': 'sha256:original',
            'Config': {'Image': 'registry:3', 'Labels': None, 'Env': ['OTEL_TRACES_EXPORTER=none']},
            'Mounts': [{'Type': 'volume', 'Name': 'expman-registry-data', 'Destination': '/var/lib/registry'}],
            'HostConfig': {'NetworkMode': 'bridge', 'PortBindings': {
                '5000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '5000'}]}}, 'State': {'Running': True}}


class LegacyRegistryMigrationTests(unittest.TestCase):
    def test_original_and_labelled_restored_contracts_preserve_port_and_volume(self):
        original = legacy_registry()
        restored = copy.deepcopy(original)
        restored['Config'].update(Image=original['Image'], Labels={'expman.component': 'node-registry'},
                                 Env=['REGISTRY_HTTP_ADDR=127.0.0.1:5000', 'REGISTRY_HTTP_DEBUG_ADDR='])
        restored['HostConfig'] = {'NetworkMode': 'host'}
        for item in (original, restored):
            with self.subTest(item=item):
                registries, preserved = migration.container_plan([item], 'node', 'Ubuntu', [])
                self.assertEqual(preserved, [])
                self.assertEqual(registries[0]['port'], 5000)
                self.assertEqual(registries[0]['volume'], 'expman-registry-data')

    def test_name_does_not_authorize_foreign_or_modified_registry(self):
        mutations = (
            lambda i: i['Config'].update(Image='other/app:3'),
            lambda i: i['Config'].update(Labels={'expman.component': 'unrelated'}),
            lambda i: i['Config'].update(Env=[]),
            lambda i: i['Mounts'][0].update(Name='other-data'),
            lambda i: i['Mounts'].append({'Type': 'bind', 'Source': '/home/private'}),
            lambda i: i['HostConfig']['PortBindings']['5000/tcp'][0].update(HostIp='0.0.0.0'),
            lambda i: i['HostConfig'].update(NetworkMode='host'),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), patch.object(migration, 'docker') as docker:
                item = legacy_registry()
                mutation(item)
                # Invalid ownership is an actionable error, not endless queuing.
                with self.assertRaises(ValueError) as error:
                    migration.container_plan([item], 'node', 'Ubuntu', [])
                self.assertNotIsInstance(error.exception, migration.MigrationBusy)
                docker.assert_not_called()

    def test_foreign_user_of_legacy_volume_still_blocks_transfer(self):
        writer = {'Name': '/writer', 'Id': 'writer', 'Config': {}, 'State': {'Running': True},
                  'Mounts': [{'Type': 'volume', 'Name': 'expman-registry-data'}]}
        waiting = []
        migration.container_plan([legacy_registry(), writer], 'node', 'Ubuntu', waiting)
        self.assertEqual(len(waiting), 1)
        self.assertIn('writer', waiting[0])
        self.assertIn('仓库卷', waiting[0])

    def test_move_queue_waits_only_for_experiment_keeps_registry_alive_then_hands_off(self):
        with temporary_directory() as temporary:
            job = Path(temporary)
            value = {'endpoint': 'unix:///fixture.sock', 'node_id': 'node', 'service_root': str(job),
                     'move_disk': True, 'registries': [migration.registry(legacy_registry(), 'expman-registry')]}
            observed = []
            def sleep(_):
                observed.append(common.read_json(job / 'waiting.json')['detail'])
            with patch.object(migration, 'same_source'), patch.object(migration, 'objects', return_value=[legacy_registry()]), \
                    patch.object(migration, 'docker', return_value='original-registry') as docker, \
                    patch.object(migration, 'idle', side_effect=[False, True]), \
                    patch.object(migration.time, 'sleep', side_effect=sleep):
                migration.wait_until_idle(job, value)
                self.assertEqual(observed, ['等待结束：当前实验或结果回传'])
                self.assertTrue(all(call.args[0] == ['ps', '-aq'] for call in docker.call_args_list))
                migration.stop_registries(value)
                docker.assert_called_with(['stop', 'original-registry'], value['endpoint'])

    def test_registry_identity_change_prevents_stop(self):
        item = legacy_registry()
        value = {'endpoint': 'unix:///fixture.sock', 'registries': [migration.registry(item, 'expman-registry')]}
        item['Id'] = 'replacement'
        with patch.object(migration, 'objects', return_value=[item]), patch.object(migration, 'docker') as docker:
            with self.assertRaises(ValueError):
                migration.stop_registries(value)
            docker.assert_not_called()

    def test_rollback_restarts_original_registry_without_removing_data(self):
        with temporary_directory() as temporary:
            job = Path(temporary)
            item = legacy_registry()
            value = {'job_id': 'a' * 32, 'endpoint': 'unix:///fixture.sock', 'service_root': str(job / 'service'),
                     'config': str(job / 'node.json'), 'registries': [migration.registry(item, 'expman-registry')]}
            common.atomic_json(job / 'manifest.json', value)
            item['State']['Running'] = False
            with patch.object(migration, 'MARKER', job / 'missing'), patch.object(migration, 'same_source'), \
                    patch.object(migration, 'objects', return_value=[item]), patch.object(migration, 'docker') as docker:
                self.assertTrue(migration.rollback(job)['ready'])
                docker.assert_called_once_with(['start', 'original-registry'], value['endpoint'])

    def test_old_local_pinned_images_are_included_alongside_new_registries(self):
        for host in ('localhost', '127.0.0.1'):
            for port in (5000, 5001, 5002):
                self.assertTrue(migration.LOCAL_IMAGE.fullmatch(f'{host}:{port}/runtime@sha256:' + 'a' * 64))
        self.assertIsNone(migration.LOCAL_IMAGE.fullmatch('localhost:5000/runtime:latest'))


if __name__ == '__main__':
    unittest.main()
