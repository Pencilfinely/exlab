from pathlib import Path
import unittest
from unittest.mock import patch

from expman import worker_migration as migration


class DesktopSocketTests(unittest.TestCase):
    def probe(self, endpoint, resolved, info=None, expected='desktop-id'):
        info = info if info is not None else {'ID': 'desktop-id', 'OSType': 'linux', 'OperatingSystem': 'Docker Desktop'}
        with patch.object(Path, 'resolve', return_value=Path(resolved)), \
                patch.object(Path, 'is_socket', return_value=True), \
                patch.object(migration, 'objects', return_value=info) as objects:
            result = migration.desktop_source(endpoint, expected)
            objects.assert_called_once_with(['info', '--format', '{{json .}}'], endpoint)
            return result

    def test_proxy_mount_and_symlink_layouts_keep_working_client_alias(self):
        for endpoint, resolved in (
                ('unix:///var/run/docker.sock', '/run/docker.sock'),
                ('unix:///run/docker.sock', '/run/docker.sock'),
                ('unix:///var/run/docker.sock', '/mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.sock'),
                ('unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.sock', '/mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.sock')):
            with self.subTest(endpoint=endpoint, resolved=resolved):
                self.assertEqual(self.probe(endpoint, resolved)[0], endpoint)

    def test_path_does_not_prove_desktop_or_engine_identity(self):
        for info in ({'ID': 'other', 'OSType': 'linux', 'OperatingSystem': 'Docker Desktop'},
                     {'ID': 'desktop-id', 'OSType': 'linux', 'OperatingSystem': 'Ubuntu'},
                     {'ID': 'desktop-id', 'OSType': 'windows', 'OperatingSystem': 'Docker Desktop'},
                     {'OSType': 'linux', 'OperatingSystem': 'Docker Desktop'}):
            with self.subTest(info=info), self.assertRaises(ValueError):
                self.probe('unix:///mnt/wsl/looks-like-desktop.sock', '/mnt/wsl/looks-like-desktop.sock', info)

    def test_owned_paths_and_aliases_cannot_be_used_as_source(self):
        for resolved in ('/run/exlab-docker/docker.sock', '/run/exlab-containerd/containerd.sock',
                         '/var/lib/exlab-docker/proxy.sock', '/opt/exlab/docker/proxy.sock'):
            with self.subTest(resolved=resolved), self.assertRaises(ValueError):
                self.probe('unix:///var/run/docker.sock', resolved)

    def test_missing_socket_and_regular_file_fail_before_docker_commands(self):
        with patch.object(migration, 'objects') as objects:
            with patch.object(Path, 'resolve', side_effect=FileNotFoundError()), self.assertRaisesRegex(ValueError, '暂不可用'):
                migration.desktop_source('unix:///var/run/docker.sock', 'desktop-id')
            with patch.object(Path, 'resolve', return_value=Path('/run/docker.sock')), \
                    patch.object(Path, 'is_socket', return_value=False), self.assertRaisesRegex(ValueError, '不是 Unix socket'):
                migration.desktop_source('unix:///var/run/docker.sock', 'desktop-id')
            objects.assert_not_called()

    def test_remote_and_malformed_endpoints_are_rejected_before_filesystem_access(self):
        with patch.object(Path, 'resolve') as resolve:
            for endpoint in ('tcp://localhost:2375', 'ssh://remote', 'unix://relative', 'unix:///bad path', None):
                with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                    migration.source_socket(endpoint)
            resolve.assert_not_called()
