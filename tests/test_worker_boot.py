import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from expman import worker_boot as boot


class WorkerBootTests(unittest.TestCase):
    def inspect(self, *, native=True, engine='engine-original', expected='engine-original', desktop=False,
                gpu=True, uid=1000, nvidia=True, backend='detached'):
        info = {'Name': 'docker-desktop' if desktop else 'ubuntu',
                'Runtimes': {'nvidia': {}} if nvidia else {}, 'OperatingSystem': 'Ubuntu'}
        with patch.object(boot.worker_service, '_settings', return_value={'config': '/worker/node.json', 'backend': backend}), \
                patch.object(Path, 'exists', return_value=False), \
                patch.object(boot, '_candidate', return_value={'node_id': 'original-node', 'path': '/worker/node.json'}), \
                patch.object(boot.os, 'getuid', return_value=uid, create=True), \
                patch.object(boot, 'native_engine', return_value=native), \
                patch.object(boot.runtime, 'docker_status', return_value={'docker_ready': True, 'docker_id': engine,
                    'docker_endpoint': 'unix:///var/run/docker.sock'}), \
                patch.object(boot.runtime, 'docker_command', return_value=subprocess.CompletedProcess([], 0, json.dumps(info), '')), \
                patch.object(boot.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0 if gpu else 1,
                    'GPU 0: NVIDIA (UUID: GPU-original)' if gpu else '', '')):
            return boot.inspect('/service', expected)

    def test_native_original_engine_and_gpu_are_required(self):
        result = self.inspect()
        self.assertTrue(result['ready'])
        self.assertEqual(result['node_id'], 'original-node')
        self.assertEqual(result['engine_id'], 'engine-original')
        for options in ({'native': False}, {'desktop': True}, {'gpu': False}, {'engine': 'replacement'},
                        {'uid': 0}, {'nvidia': False}, {'backend': 'systemd'}):
            with self.subTest(options=options):
                self.assertFalse(self.inspect(**options)['ready'])

    def test_unconfigured_environment_does_not_pair_or_create_identity(self):
        with patch.object(boot.worker_service, '_settings', return_value={}), \
                patch.object(boot.runtime, 'docker_status') as docker:
            self.assertFalse(boot.inspect('/missing')['ready'])
            docker.assert_not_called()

    def test_old_user_autostart_must_be_disabled_before_windows_takes_ownership(self):
        with patch.object(boot.worker_service, '_settings', return_value={'config': '/worker/node.json', 'backend': 'detached'}), \
                patch.object(boot, '_candidate', return_value={'node_id': 'original', 'path': '/worker/node.json'}), \
                patch.object(boot.os, 'getuid', return_value=1000, create=True), \
                patch.object(Path, 'exists', return_value=True), \
                patch.object(boot.runtime, 'docker_status') as docker:
            for output in ('enabled\n', '', 'linked\n'):
                with self.subTest(output=output), patch.object(boot.subprocess, 'run',
                        return_value=subprocess.CompletedProcess([], 0, output, '')):
                    self.assertFalse(boot.inspect('/service')['ready'])
            docker.assert_not_called()

    def test_native_engine_checks_systemd_without_root_process_exe_access(self):
        with patch.object(Path, 'read_text', return_value='dockerd\n') as read, \
                patch.object(boot.os, 'readlink', side_effect=PermissionError) as forbidden:
            for executable, expected in (('/usr/bin/dockerd', True), ('/mnt/wsl/docker-desktop/dockerd', False)):
                with self.subTest(executable=executable), patch.object(boot.subprocess, 'run', return_value=
                        subprocess.CompletedProcess([], 0, 'ActiveState=active\nMainPID=42\n'
                        f'ExecStart={{ path={executable} ; argv[]={executable} -H fd:// ; }}\n', '')):
                    self.assertEqual(boot.native_engine(), expected)
            read.assert_called_once_with()
            forbidden.assert_not_called()

    def test_inactive_or_unverifiable_native_daemon_is_rejected(self):
        for properties in ('ActiveState=inactive\nMainPID=0', 'ActiveState=active\nMainPID=42',
                'ActiveState=active\nMainPID=invalid\nExecStart={ path=/usr/bin/dockerd ; }'):
            with self.subTest(properties=properties), patch.object(boot.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, properties, '')):
                self.assertFalse(boot.native_engine())
        with patch.object(boot.subprocess, 'run', side_effect=subprocess.TimeoutExpired('systemctl', 15)):
            self.assertFalse(boot.native_engine())


if __name__ == '__main__':
    unittest.main()
