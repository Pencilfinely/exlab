"""All entry points use the saved Center workspace and fail closed on identity loss."""
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from expman import common, controller_paths, desktop, launcher
from expman.hub import Hub, make_server
from tests.support import temporary_directory


class ControllerPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.folder = Path(self.temporary.__enter__())
        self.settings = self.folder / 'desktop' / 'Controller'
        self.root = self.folder / 'Original Center'
        self.location = patch.object(controller_paths, 'controller_settings_root', return_value=self.settings)
        self.location.start()

    def tearDown(self):
        self.location.stop()
        self.temporary.__exit__(None, None, None)

    def save(self, value, name='settings.json'):
        common.atomic_json(self.settings / name, value)

    def test_no_argument_cli_and_desktop_share_saved_custom_root(self):
        self.save({'data_root': str(self.root), 'auto_update': True})
        self.assertEqual(launcher.default_controller_root(), self.root)
        with patch.object(desktop, 'controller_status', return_value={'running': False}) as status, \
                patch('sys.argv', ['desktop', 'controller-status']), patch('builtins.print'):
            desktop.main()
        status.assert_called_once_with(str(self.root))
        with patch.object(launcher, 'controller') as start, patch('sys.argv', ['launcher', 'controller', '--no-browser']):
            launcher.main()
        self.assertEqual(start.call_args.args[0], self.root)
        self.assertFalse(self.root.exists())

    def test_missing_or_corrupt_settings_recover_without_selecting_empty_default(self):
        self.save({'data_root': str(self.root), 'auto_update': True}, 'settings.json.bak')
        self.assertEqual(launcher.default_controller_root(), self.root)
        (self.settings / 'settings.json').write_text('{broken', encoding='utf-8')
        self.assertEqual(launcher.default_controller_root(), self.root)
        self.assertTrue(controller_paths.controller_settings()['auto_update'])
        (self.settings / 'settings.json.bak').unlink()
        self.save({'data_root': str(self.root), 'hub_initialized': True}, 'controller-root.json')
        self.assertEqual(launcher.default_controller_root(), self.root)

    def test_invalid_settings_never_silently_choose_default_but_explicit_root_can_recover(self):
        for invalid in (None, [], {'data_root': ''}, {'data_root': 'relative-center'}):
            with self.subTest(invalid=invalid):
                self.save(invalid)
                with self.assertRaises(ValueError):
                    launcher.default_controller_root()
                with patch.object(launcher, 'controller') as start, \
                        patch('sys.argv', ['launcher', 'controller', '--root', str(self.root), '--no-browser']):
                    launcher.main()
                self.assertEqual(start.call_args.args[0], str(self.root))
        self.assertFalse(self.root.exists())

    def test_pinned_workspace_missing_drive_or_files_never_creates_new_identity(self):
        self.save({'data_root': str(self.root), 'hub_initialized': True}, 'controller-root.json')
        with patch.object(desktop.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(ValueError, '原 Center 数据目录不可用'):
                desktop.controller_start(self.root)
            spawn.assert_not_called()
        self.assertFalse(self.root.exists())
        self.root.mkdir()
        (self.root / 'hub.sqlite3').write_bytes(b'existing database')
        with self.assertRaisesRegex(ValueError, 'restore the original configuration'):
            Hub(self.root)
        self.assertFalse((self.root / 'hub.json').exists())
        self.assertEqual((self.root / 'hub.sqlite3').read_bytes(), b'existing database')

    def test_pinned_workspace_identity_checked_without_modifying_files(self):
        hub = Hub(self.root)
        identity = hub.config['workspace_id']
        hub.close()
        original = (self.root / 'hub.json').read_bytes()
        self.save({'data_root': str(self.root), 'hub_initialized': True, 'workspace_id': identity}, 'controller-root.json')
        controller_paths.validate_controller_workspace(self.root)
        self.save({'data_root': str(self.root), 'hub_initialized': True, 'workspace_id': 'other'}, 'controller-root.json')
        with self.assertRaisesRegex(ValueError, '身份'):
            controller_paths.validate_controller_workspace(self.root)
        self.assertEqual((self.root / 'hub.json').read_bytes(), original)

    def test_latest_binding_does_not_inherit_identity_from_an_older_backup(self):
        self.save({'data_root': str(self.root), 'hub_initialized': True, 'workspace_id': 'original'}, 'settings.json.bak')
        second = self.folder / 'Second Center'
        self.save({'data_root': str(second)}, 'controller-root.json')
        restored = controller_paths.controller_settings()
        self.assertEqual(restored['data_root'], str(second))
        self.assertNotIn('hub_initialized', restored)
        self.assertNotIn('workspace_id', restored)

    def test_saved_identity_guards_missing_workspace_even_if_binding_file_is_lost(self):
        self.save({'data_root': str(self.root), 'hub_initialized': True, 'workspace_id': 'original'})
        with self.assertRaisesRegex(ValueError, '原 Center 数据目录不可用'):
            desktop.controller_start(self.root)
        self.assertFalse(self.root.exists())

    def test_settings_opens_existing_native_client_without_starting_backend(self):
        with patch.object(desktop.sys, 'platform', 'win32'), patch.object(Path, 'is_file', return_value=True), \
                patch.object(desktop.subprocess, 'Popen') as spawn:
            self.assertTrue(desktop.controller_preferences(self.root)['available'])
            spawn.assert_not_called()
            desktop.controller_preferences(self.root, open_window=True)
        self.assertEqual(spawn.call_args.args[0][1:], ['--settings'])
        self.assertNotIn('--data-root', spawn.call_args.args[0])
        self.assertFalse(self.root.exists())

    def test_settings_endpoint_requires_local_admin_and_does_not_accept_node_token(self):
        hub = Hub(self.root)
        node_token = hub.add_node('worker')
        server = make_server(hub, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = 'http://127.0.0.1:' + str(server.server_address[1]) + '/api/local/desktop/settings'
        try:
            import urllib.error
            for token in ('invalid', node_token):
                with self.assertRaises(urllib.error.HTTPError):
                    common.api_request(url, token, {})
            with patch.object(desktop, 'controller_preferences', return_value={'available': True}) as preferences:
                self.assertTrue(common.api_request(url, hub.config['admin_token'])['available'])
                self.assertTrue(common.api_request(url, hub.config['admin_token'], {})['available'])
                self.assertEqual(preferences.call_count, 2)
                self.assertTrue(preferences.call_args.kwargs['open_window'])
            with patch('expman.project_import.is_loopback', return_value=False), \
                    patch.object(desktop, 'controller_preferences') as preferences:
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    common.api_request(url, hub.config['admin_token'], {})
                self.assertEqual(denied.exception.code, 403)
                preferences.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            hub.close()
