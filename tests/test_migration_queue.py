import unittest
from pathlib import Path
from unittest.mock import patch

from expman import common, worker_migration as migration
from tests.support import temporary_directory


class MigrationQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.job = Path(self.temp.__enter__())
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.value = {'endpoint': 'unix:///fixture.sock', 'node_id': 'worker',
                      'service_root': str(self.job), 'move_disk': True}

    def test_preflight_collects_busy_mounts_instead_of_rejecting_submission(self):
        container = {'Name': '/training', 'Id': 'foreign', 'State': {'Running': True},
                     'Config': {}, 'Mounts': [{'Type': 'bind', 'Source': '/home/model'}]}
        waiting = []
        with patch.object(migration, 'docker') as docker:
            migration.container_plan([container], 'worker', 'Ubuntu', waiting)
            self.assertIn('training', waiting[0])
            with self.assertRaises(migration.MigrationBusy):
                migration.container_plan([container], 'worker', 'Ubuntu')
            docker.assert_not_called()

    def test_active_experiment_then_other_container_finish_before_handoff(self):
        observed = []
        def containers(endpoint, node, waiting):
            return [], [{'name': 'other-training'}] if len(observed) == 1 else []
        def sleep(_):
            observed.append(common.read_json(self.job / 'waiting.json')['detail'])
        with patch.object(migration, 'same_source'), patch.object(migration, 'idle', side_effect=[False, True, True]), \
                patch.object(migration, 'inspect_containers', side_effect=containers), \
                patch.object(migration.time, 'sleep', side_effect=sleep), \
                patch.object(migration.time, 'monotonic', side_effect=AssertionError('No 24-hour timeout')), \
                patch.object(migration.service, 'stop_for_update') as stop, patch.object(migration, 'docker') as docker:
            migration.wait_until_idle(self.job, self.value)
            self.assertIn('当前实验', observed[0])
            self.assertIn('other-training', observed[1])
            self.assertEqual(common.read_json(self.job / 'waiting.json')['detail'], '')
            stop.assert_not_called()
            docker.assert_not_called()

    def test_cancel_during_wait_does_not_stop_any_container(self):
        def sleep(_):
            (self.job / 'cancel').touch()
        with patch.object(migration, 'same_source'), patch.object(migration, 'idle', return_value=False), \
                patch.object(migration, 'inspect_containers', return_value=([], [])), \
                patch.object(migration.time, 'sleep', side_effect=sleep), \
                patch.object(migration.service, 'stop_for_update') as stop:
            with self.assertRaisesRegex(RuntimeError, '取消'):
                migration.wait_until_idle(self.job, self.value)
            stop.assert_not_called()

    def test_in_place_migration_does_not_wait_for_unrelated_containers(self):
        self.value['move_disk'] = False
        with patch.object(migration, 'same_source'), patch.object(migration, 'idle', return_value=True), \
                patch.object(migration, 'inspect_containers', return_value=([], [{'name': 'redis'}])), \
                patch.object(migration.time, 'sleep', side_effect=AssertionError('Should not wait')):
            migration.wait_until_idle(self.job, self.value)

    def test_container_inspection_failure_is_not_silently_treated_as_idle(self):
        with patch.object(migration, 'same_source'), \
                patch.object(migration, 'inspect_containers', side_effect=RuntimeError('docker offline')):
            with self.assertRaisesRegex(RuntimeError, 'docker offline'):
                migration.wait_until_idle(self.job, self.value)


if __name__ == '__main__':
    unittest.main()
