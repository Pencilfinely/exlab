"""Ubuntu releases are verified, installed at idle boundaries and retried durably."""
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import unittest
from unittest.mock import patch
import zipfile

from expman import common, worker_update as update, worker_service as service
from tests.support import temporary_directory


def published(version='0.5.9', prefix='ExLab', role=update.ROLE, **changes):
    tag = 'v' + version
    name = f'{prefix}-{version}-{role}.zip'
    result = dict(tag_name=tag, draft=False, prerelease='-' in version, assets=[
        dict(name=name, state='uploaded', size=100, browser_download_url=update._release_url(tag, name)),
        dict(name='SHA256SUMS.txt', state='uploaded', browser_download_url=update._release_url(tag, 'SHA256SUMS.txt'))])
    result.update(changes)
    return result


class Response(io.BytesIO):
    def __init__(self, data, size=None):
        super().__init__(data)
        self.headers = {} if size is None else {'Content-Length': str(size)}


class UbuntuReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.folder = Path(self.temporary.__enter__())

    def tearDown(self):
        self.temporary.__exit__(None, None, None)

    def package(self, *, role=update.ROLE, version='0.5.9', extra=None, tamper_manifest=False):
        files = {'release-role.json': json.dumps({'version': version, 'role': role}).encode(),
            'expman/__init__.py': b'__version__ = "0.5.9"\n',
            'expman/worker_service.py': b'# verified fixture, never executed\n',
            'expman/worker_update.py': b'# updater fixture, never executed\n'}
        files.update(extra or {})
        manifest = {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in files.items()}
        if tamper_manifest:
            manifest['expman/worker_service.py']['sha256'] = '0' * 64
        files['manifest.json'] = json.dumps(manifest).encode()
        target = self.folder / 'fixture.zip'
        with zipfile.ZipFile(target, 'w') as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        release = update.select_release([published()], '0.5.8')
        release.update(size=target.stat().st_size, sha256=common.sha256_file(target))
        return target, release

    def test_selection_orders_versions_channels_and_exact_ubuntu_role(self):
        releases = [published('0.5.9'), published('0.6.0-rc.2'), published('0.5.10'),
                    published('1.0.0', draft=True), published('2.0.0', role='windows-worker-x64')]
        self.assertEqual(update.select_release(releases, '0.5.8')['version'], '0.5.10')
        self.assertEqual(update.select_release(releases, '0.5.8rc1')['version'], '0.6.0-rc.2')
        self.assertIsNone(update.select_release([published('0.5.8')], '0.5.8'))
        self.assertEqual(update.select_release([published(prefix='ExperimentManager')], '0.5.8')['name'],
                         'ExperimentManager-0.5.9-ubuntu-worker-x64.zip')
        self.assertGreater(update.version_key('0.6.0-rc.10'), update.version_key('0.6.0-rc.2'))
        self.assertTrue(update.same_version('0.6.0rc1', '0.6.0-rc.1'))
        for bad in ('0.5.9-rc.01', '../0.5.9', '00.5.9'):
            with self.assertRaises(ValueError):
                update.version_key(bad)

    def test_ambiguous_assets_wrong_origin_sizes_and_checksums_are_rejected(self):
        for mutation in ('duplicate', 'origin', 'size'):
            item = published()
            if mutation == 'duplicate':
                item['assets'].append(item['assets'][0])
            elif mutation == 'origin':
                item['assets'][0]['browser_download_url'] = 'https://evil.example/package.zip'
            else:
                item['assets'][0]['size'] = True
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                update.select_release([item], '0.5.8')
        name = 'ExLab-0.5.9-ubuntu-worker-x64.zip'
        line = 'a' * 64 + '  ' + name
        self.assertEqual(update.parse_checksum(line, name), 'a' * 64)
        for text in ('a' * 64 + '  another.zip', line + '\n' + line):
            with self.assertRaises(ValueError):
                update.parse_checksum(text, name)
        for url in ('http://github.com/file', 'https://github.com.evil.test/file', 'https://user@github.com/file',
                    'https://api.github.com/repos/other/project', 'file:///etc/passwd'):
            with self.assertRaises(ValueError):
                update._allow_download_url(url)
        update._allow_download_url('https://release-assets.githubusercontent.com/asset?signature=fixture')

    def test_download_and_extraction_verify_cache_and_each_manifest_file(self):
        archive, release = self.package()
        with patch.object(update, '_open', return_value=Response(archive.read_bytes(), release['size'])) as network:
            downloaded = update.download(release, self.folder / 'downloads')
            self.assertEqual(update.download(release, self.folder / 'downloads'), downloaded)
            self.assertEqual(network.call_count, 1)
        extracted = update.extract_package(downloaded, release, self.folder / 'packages')
        self.assertEqual(update.extract_package(downloaded, release, self.folder / 'packages'), extracted)
        self.assertEqual(common.read_json(extracted / 'release-role.json')['role'], update.ROLE)
        (extracted / 'sitecustomize.py').write_text('raise Exception("untrusted extra code")')
        with self.assertRaises(ValueError):
            update.extract_package(downloaded, release, self.folder / 'packages')

    def test_bad_download_never_finalizes_and_staging_is_removed(self):
        archive, release = self.package()
        for body, size in ((b'bad', None), (archive.read_bytes(), release['size'] + 1)):
            with self.subTest(size=size), patch.object(update, '_open', return_value=Response(body, size)), self.assertRaises(ValueError):
                update.download(release, self.folder / 'downloads')
            self.assertEqual(list((self.folder / 'downloads').iterdir()), [])

    def test_paths_symlinks_wrong_role_version_and_tampered_contents_are_rejected(self):
        for options in ({'extra': {'../escaped': b'x'}}, {'extra': {'/absolute': b'x'}},
                        {'role': 'windows-worker-x64'}, {'version': '0.5.10'}, {'tamper_manifest': True}):
            with self.subTest(options=options):
                archive, release = self.package(**options)
                with self.assertRaises(ValueError):
                    update.extract_package(archive, release, self.folder / 'packages')
        archive, release = self.package()
        with zipfile.ZipFile(archive, 'a') as package:
            link = zipfile.ZipInfo('link')
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            package.writestr(link, '/outside')
        release.update(size=archive.stat().st_size, sha256=common.sha256_file(archive))
        with self.assertRaises(ValueError):
            update.extract_package(archive, release, self.folder / 'packages')
        self.assertFalse((self.folder.parent / 'escaped').exists())


class UbuntuUpdateLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.folder = Path(self.temporary.__enter__())
        self.root = self.folder / 'client with spaces'
        self.home = self.folder / 'home'
        self.config_path = self.folder / 'node/node.ready.json'
        self.config = {'node_id': 'a6000', 'token': 'private-updater-test-token', 'hub_url': 'http://127.0.0.1:8765',
                       'root': 'runtime', 'gpu_policy': {'GPU-a6000': {'max_jobs': 2}}, 'allow_demo': True}
        common.atomic_json(self.config_path, self.config)
        service.select_configuration(self.root, config=str(self.config_path))
        self.before = {**service._settings(self.root), 'package_dir': str(self.folder / 'old'),
                       'python': '/usr/bin/python3', 'backend': 'detached', 'version': '0.5.8'}
        service._save_settings(self.root, self.before)
        self.release = update.select_release([published()], '0.5.8')
        self.release['sha256'] = 'a' * 64
        self.package = self.folder / 'new package'
        self.package.mkdir()
        self.ready = {'ready_for_update': True, 'running': False, 'status': 'stopped'}
        self.data = self.config_path.parent / 'runtime'
        self.data.mkdir()
        (self.data / 'node.sqlite3').write_bytes(b'original database fixture')
        (self.data / 'saved.bin').write_bytes(b'original checkpoint fixture')
        common.atomic_json(self.config_path.parent / 'setup-state.json', {'docker_endpoint': 'unix:///a6000/docker.sock'})
        self.original = {path: path.read_bytes() for path in self.config_path.parent.rglob('*') if path.is_file()}

    def tearDown(self):
        self.temporary.__exit__(None, None, None)

    def assert_data_preserved(self):
        self.assertEqual({path: path.read_bytes() for path in self.config_path.parent.rglob('*') if path.is_file()}, self.original)

    def save_queue(self, **values):
        update._save(self.root, release=self.release, target_version='0.5.9', **values)

    def test_running_work_waits_without_stopping_or_installing(self):
        self.save_queue(automatic=False)
        with patch.object(service, 'update_status', return_value={'ready_for_update': False, 'detail': '实验仍在运行'}), \
                patch.object(service, 'stop_for_update') as stop, patch.object(update, '_invoke') as invoke:
            result = update._apply(self.root, self.package, self.release)
        self.assertEqual(result['phase'], 'waiting')
        stop.assert_not_called()
        invoke.assert_not_called()
        self.assert_data_preserved()

    def test_installs_real_immutable_service_files_and_keeps_stopped_worker_stopped(self):
        entries = [(Path('worker_service.py'), b'# new immutable code\n'), (Path('worker_update.py'), b'# new updater\n')]
        self.save_queue(automatic=False)
        def invoke(package, root, action, *options):
            self.assertEqual(action, 'install')
            self.assertIn('--no-start', options)
            self.assertEqual(options[-1], str(self.config_path))
            with patch.object(service, '__version__', '0.5.9'), patch.object(service, '_require_linux'), \
                    patch.object(service, '_systemd_available', return_value=False), patch.object(Path, 'home', return_value=self.home), \
                    patch.object(service, '_installation_payload', return_value=(entries, {'role': update.ROLE, 'version': '0.5.9'}, 'new-code')):
                return service.install(root, config=str(self.config_path), backend='detached', start_now=False)
        with patch.object(service, 'update_status', return_value=self.ready), \
                patch.object(service, 'stop_for_update', return_value=self.ready), \
                patch.object(service, 'install_status', return_value={'ready_for_install': True}), \
                patch.object(update, '_invoke', side_effect=invoke), patch.object(update, '_restart_and_verify') as restart:
            result = update._apply(self.root, self.package, self.release)
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual(service._settings(self.root)['version'], '0.5.9')
        self.assertEqual((self.root / 'software/new-code/expman/worker_service.py').read_bytes(), entries[0][1])
        self.assertIn('software', (self.root / 'Update-Worker.sh').read_text())
        restart.assert_not_called()
        self.assert_data_preserved()

    def test_failed_handoff_restores_old_service_and_running_choice(self):
        self.save_queue(automatic=False)
        with patch.object(service, 'update_status', side_effect=[{**self.ready, 'running': True}, self.ready]), \
                patch.object(service, 'stop_for_update', return_value=self.ready), \
                patch.object(service, 'install_status', return_value={'ready_for_install': True}), \
                patch.object(update, '_invoke', side_effect=OSError('installation failed')), \
                patch.object(service, 'start', return_value={'status': 'starting', 'running': False}) as restart:
            with self.assertRaisesRegex(RuntimeError, '已恢复'):
                update._apply(self.root, self.package, self.release)
        self.assertEqual(service._settings(self.root), self.before)
        restart.assert_called_once_with(self.root)
        self.assert_data_preserved()

    def test_restart_after_interrupted_handoff_does_not_install_again(self):
        self.save_queue(automatic=False, before_settings=self.before, resume_service=True, phase='restarting')
        service._save_settings(self.root, {**self.before, 'version': '0.5.9', 'package_dir': str(self.package)})
        with patch.object(service, 'update_status') as readiness, patch.object(update, '_invoke') as invoke, \
                patch.object(update, '_restart_and_verify') as restart:
            result = update._apply(self.root, self.package, self.release)
        self.assertEqual(result['phase'], 'completed')
        readiness.assert_not_called()
        invoke.assert_not_called()
        restart.assert_called_once_with(self.package, self.root, self.release)
        self.assert_data_preserved()

    def test_separate_manual_upgrade_supersedes_waiting_queue_without_downgrade_or_stop(self):
        for version in ('0.5.9', '0.6.0'):
            with self.subTest(version=version):
                self.save_queue(automatic=False, phase='waiting', before_settings=None)
                service._save_settings(self.root, {**self.before, 'version': version})
                with patch.object(service, 'update_status') as ready, patch.object(service, 'stop_for_update') as stop, \
                        patch.object(update, '_invoke') as invoke:
                    result = update._apply(self.root, self.package, self.release)
                self.assertEqual(result['phase'], 'completed')
                self.assertEqual(result['current_version'], version)
                self.assertIsNone(update._state(self.root)['release'])
                ready.assert_not_called()
                stop.assert_not_called()
                invoke.assert_not_called()
                self.assertEqual(service._settings(self.root)['version'], version)
        self.assert_data_preserved()

    def test_failed_systemd_update_restores_original_unit_as_well_as_software(self):
        self.before['backend'] = 'systemd'
        service._save_settings(self.root, self.before)
        unit = self.home / '.config/systemd/user' / service._unit_name(self.root)
        unit.parent.mkdir(parents=True)
        unit.write_text('[Service]\nWorkingDirectory=/original/package\n')
        original_unit = unit.read_bytes()
        self.save_queue(automatic=False)
        def partial_install(*args):
            unit.write_text('[Service]\nWorkingDirectory=/new/package\n')
            service._save_settings(self.root, {**self.before, 'version': '0.5.9', 'package_dir': str(self.package)})
            raise OSError('new unit installation failed')
        with patch.object(Path, 'home', return_value=self.home), \
                patch.object(service, 'update_status', side_effect=[{**self.ready, 'running': True}, self.ready]), \
                patch.object(service, 'stop_for_update', return_value=self.ready), \
                patch.object(service, 'install_status', return_value={'ready_for_install': True}), \
                patch.object(update, '_invoke', side_effect=partial_install), \
                patch.object(update.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as reload, \
                patch.object(service, 'start', return_value={'status': 'starting', 'running': False}) as restart:
            with self.assertRaisesRegex(RuntimeError, '已恢复'):
                update._apply(self.root, self.package, self.release)
        self.assertEqual(unit.read_bytes(), original_unit)
        self.assertEqual(service._settings(self.root), self.before)
        reload.assert_called_once()
        restart.assert_called_once()
        self.assert_data_preserved()

    def test_update_retry_persists_release_and_never_rechecks_github_while_waiting(self):
        with patch.object(update, 'check', return_value=self.release) as check, \
                patch.object(update, 'download', return_value=self.folder / 'verified.zip'), \
                patch.object(update, 'extract_package', return_value=self.package), \
                patch.object(service, 'update_status', return_value={'ready_for_update': False, 'detail': '等待回传'}):
            self.assertEqual(update.update_once(self.root)['phase'], 'waiting')
            self.assertEqual(update.update_once(self.root)['phase'], 'waiting')
        check.assert_called_once_with('0.5.8')
        self.assertEqual(update._state(self.root)['release'], self.release)
        self.assert_data_preserved()

    def test_auto_defaults_off_obeys_hourly_check_and_backs_off_network_failures(self):
        with patch.object(update, 'check', return_value=None) as check:
            update.update_once(self.root, automatic=True)
            check.assert_not_called()
            common.atomic_json(self.root / 'updates/settings.json', {'enabled': True})
            self.assertEqual(update.update_once(self.root, automatic=True)['phase'], 'up_to_date')
            update.update_once(self.root, automatic=True)
            check.assert_called_once_with('0.5.8')
        update._save(self.root, next_check_at=0)
        with patch.object(update, 'check', side_effect=OSError('network unavailable')) as check:
            self.assertEqual(update.update_once(self.root, automatic=True)['phase'], 'failed')
            update.update_once(self.root, automatic=True)
            check.assert_called_once()
        self.assertGreater(update._state(self.root)['retry_at'], update._state(self.root)['updated_at'])
        self.assert_data_preserved()

    def test_disabling_auto_after_download_does_not_stop_agent_and_manual_can_resume(self):
        self.save_queue(automatic=True)
        with patch.object(service, 'update_status', return_value=self.ready), patch.object(service, 'stop_for_update') as stop:
            self.assertEqual(update._apply(self.root, self.package, self.release)['phase'], 'paused')
            stop.assert_not_called()

    def test_auto_timer_and_stable_launcher_are_scoped_to_original_service_directory(self):
        with patch.object(service, '_require_linux'), patch.object(service, '_systemd_available', return_value=True), \
                patch.object(Path, 'home', return_value=self.home), \
                patch.object(update.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            self.assertTrue(update.configure_auto(self.root, True)['auto_update'])
            unit_dir = self.home / '.config/systemd/user'
            unit = next(unit_dir.glob('*-update.service'))
            timer = next(unit_dir.glob('*.timer'))
            self.assertIn('--automatic', unit.read_text())
            self.assertIn('KillMode=process', unit.read_text())
            self.assertIn(service._quoted_unit(self.root / 'Update-Worker.sh'), unit.read_text())
            self.assertIn('OnUnitInactiveSec=60s', timer.read_text())
            self.assertIn(str(self.root), (self.root / 'Update-Worker.sh').read_text())
            self.assertFalse(update.configure_auto(self.root, False)['auto_update'])
        self.assertTrue(any(call.args[0][2:4] == ['disable', '--now'] for call in run.call_args_list))
        self.assertFalse(any('stop' in call.args[0] for call in run.call_args_list))
        self.assert_data_preserved()

    def test_auto_enable_failure_does_not_claim_it_is_enabled_and_wrong_node_is_refused(self):
        with patch.object(service, '_require_linux'), patch.object(service, '_systemd_available', return_value=True), \
                patch.object(Path, 'home', return_value=self.home), \
                patch.object(update.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'user bus unavailable')):
            with self.assertRaises(RuntimeError):
                update.configure_auto(self.root, True)
        self.assertFalse(update.public_status(self.root)['auto_update'])
        with self.assertRaises(ValueError):
            update._register(self.root, self.folder / 'another-node.json')
        self.assertNotIn(self.config['token'], json.dumps(update.public_status(self.root)))

    def test_unregistered_foreground_node_cannot_be_reported_as_already_updated(self):
        service._save_settings(self.root, {})
        with patch.object(update, 'check') as check, patch.object(update, 'download') as download:
            result = update.update_once(self.root, config=str(self.config_path))
        self.assertEqual(result['phase'], 'failed')
        self.assertIn('Install-Worker.sh', result['detail'])
        check.assert_not_called()
        download.assert_not_called()
        self.assertFalse((self.root / 'Update-Worker.sh').exists())
        self.assert_data_preserved()

    def test_windows_release_preserves_legacy_update_command(self):
        with patch.object(service, '_release_role', return_value={'role': 'windows-worker-x64'}), \
                patch('expman.worker_upgrade.main', return_value=0) as legacy:
            self.assertEqual(update.main(['--config', str(self.config_path)]), 0)
        legacy.assert_called_once_with(['--config', str(self.config_path)])


if __name__ == '__main__':
    unittest.main()
