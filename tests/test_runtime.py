import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from expman import common, runtime
from expman.worker_service import deactivate
from tests.support import temporary_directory


def reply(data, code=0):
    return subprocess.CompletedProcess([], code, data if isinstance(data, str) else json.dumps(data), '')


class RuntimeTests(unittest.TestCase):
    def test_not_running_docker_is_reported_without_starting_a_daemon(self):
        with patch.object(runtime.shutil, 'which', return_value='/usr/bin/docker'), \
                patch.object(runtime, 'docker_command', side_effect=[reply('unix:///var/run/docker.sock'), reply('', 1)]) as command:
            result = runtime.docker_status()
        self.assertFalse(result['docker_ready'])
        self.assertEqual(len(command.call_args_list), 2)
        self.assertNotIn('start', str(command.call_args_list))

    def test_stale_saved_socket_falls_back_to_current_context(self):
        with temporary_directory() as path:
            common.atomic_json(Path(path) / 'setup-state.json', {'docker_endpoint': 'unix:///does-not-exist/docker.sock'})
            with patch.object(runtime.shutil, 'which', return_value='/usr/bin/docker'), \
                    patch.dict(runtime.os.environ, {}, clear=True), \
                    patch.object(runtime, 'docker_command', side_effect=[reply('unix:///var/run/docker.sock'), reply({'OSType': 'linux', 'ServerVersion': 'test'})]):
                result = runtime.docker_status(path)
            self.assertTrue(result['docker_ready'])
            self.assertEqual(result['docker_endpoint'], 'unix:///var/run/docker.sock')

    def test_remote_context_is_not_accepted_as_local_compute(self):
        with patch.object(runtime.shutil, 'which', return_value='/usr/bin/docker'), \
                patch.dict(runtime.os.environ, {'DOCKER_HOST': 'tcp://remote:2375'}, clear=True), \
                patch.object(runtime, 'docker_command') as command:
            result = runtime.docker_status()
        self.assertFalse(result['docker_ready'])
        command.assert_not_called()

    def release(self, commands, processes=None):
        with patch.object(runtime, 'docker_status', return_value={'docker_ready': True, 'docker_endpoint': 'unix:///run/docker.sock'}), \
                patch('expman.worker_upgrade.discover_configs', return_value=[]), \
                patch.object(runtime, 'docker_command', side_effect=commands) as command, \
                patch.object(runtime, 'other_user_processes', return_value=processes or []):
            return runtime.release_runtime(), command

    def test_other_containers_prevent_docker_and_wsl_shutdown(self):
        result, command = self.release([reply({'Names': 'other-project'})])
        self.assertFalse(result['can_stop_docker'])
        self.assertFalse(result['can_terminate_wsl'])
        self.assertEqual(command.call_count, 1)

    def test_only_owned_registry_is_stopped_and_volumes_remain(self):
        result, command = self.release([reply({'Names': 'expman-worker-registry'}),
            reply([{'Config': {'Labels': {'expman.component': 'worker-registry'}}}]), reply('stopped'), reply('')])
        self.assertTrue(result['registry_stopped'])
        self.assertTrue(result['can_stop_docker'])
        self.assertTrue(result['can_terminate_wsl'])
        self.assertEqual(command.call_args_list[2].args[0], ['stop', '--time', '10', 'expman-worker-registry'])
        self.assertFalse(any(call.args[0][0] in ('rm', 'rmi', 'system', 'volume') for call in command.call_args_list))

    def test_registry_name_alone_is_not_shutdown_authority(self):
        result, command = self.release([reply({'Names': 'expman-worker-registry'}), reply([{'Config': {'Labels': {}}}])])
        self.assertFalse(result['can_stop_docker'])
        self.assertEqual(command.call_count, 2)

    def test_user_workloads_preserve_ubuntu(self):
        result, _ = self.release([reply(''), reply('')], processes=[123])
        self.assertTrue(result['can_stop_docker'])
        self.assertFalse(result['can_terminate_wsl'])
        self.assertEqual(result['other_processes'], 1)
        self.assertEqual(result['other_process_details'][0]['pid'], 123)

    def test_disconnected_docker_still_reports_processes_for_explicit_memory_release(self):
        with patch.object(runtime, 'docker_status', return_value={'docker_ready': False}), \
                patch('expman.worker_upgrade.discover_configs', return_value=[]), \
                patch.object(runtime, 'other_user_processes', return_value=[321]), \
                patch.object(runtime, 'docker_command') as command:
            result = runtime.release_runtime()
        self.assertTrue(result['agents_stopped'])
        self.assertFalse(result['can_stop_docker'])
        self.assertFalse(result['can_terminate_wsl'])
        self.assertEqual(result['other_process_details'][0]['pid'], 321)
        command.assert_not_called()

    def test_dormant_flag_is_not_written_after_failed_shutdown(self):
        with temporary_directory() as path:
            with patch('expman.worker_service.shutdown', return_value={'status': 'exit_failed'}):
                result = deactivate(path)
            self.assertFalse(result['deactivated'])
            self.assertFalse((Path(path) / 'service.json').exists())

    def test_another_agent_preserves_runtime_even_when_no_containers_run(self):
        with patch.object(runtime, 'docker_status', return_value={'docker_ready': True}), \
                patch('expman.worker_upgrade.discover_configs', return_value=[{'root': '/other-node'}]), \
                patch('expman.worker_upgrade.agent_is_running', return_value=True), \
                patch.object(runtime, 'docker_command') as command:
            result = runtime.release_runtime()
        self.assertFalse(result['can_stop_docker'])
        self.assertFalse(result['agents_stopped'])
        command.assert_not_called()
