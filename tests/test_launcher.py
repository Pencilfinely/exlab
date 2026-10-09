"""The shipped command-line launcher participates in native stop and updates."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from expman import __version__, common, desktop, launcher
from expman.hub import Hub
from tests.support import temporary_directory


class LauncherLifecycleTests(unittest.TestCase):
    @contextmanager
    def command_line_center(self, root):
        process = subprocess.Popen([sys.executable, '-u', '-m', 'expman.launcher', 'controller',
                                    '--root', str(root), '--port', '0', '--host', '127.0.0.1', '--no-browser'],
                                   cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8'))
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                status = desktop.controller_status(root)
                if status.get('responsive'):
                    break
                if process.poll() is not None:
                    self.fail('CLI exited before startup: ' + process.communicate()[1].decode('utf-8', 'replace'))
                time.sleep(0.05)
            else:
                self.fail('CLI did not become ready')
            self.assertTrue(status['managed'])
            self.assertEqual(status['version'], __version__)
            self.assertEqual(status['update_stop_protocol'], 2)
            self.assertEqual(common.read_json(root / 'desktop-process.json')['pid'], process.pid)
            yield process, status
        finally:
            if process.poll() is None:
                owner = common.read_json(root / 'desktop-process.json', {})
                if owner.get('nonce'):
                    common.atomic_json(root / 'desktop-stop.json', {'nonce': owner['nonce']})
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    # This is an isolated test process with disposable data.
                    process.kill()
                    process.communicate(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_cli_update_stop_keeps_remote_experiments_and_identity(self):
        with temporary_directory() as folder:
            root = Path(folder) / 'center'
            hub = Hub(root)
            try:
                hub.add_node('offline-worker')
                with hub.transaction():
                    hub.db.execute("INSERT INTO jobs(id,spec,state,node_id,created,updated) "
                                   "VALUES (?,'{}','running','offline-worker',1,2)", ('a' * 32,))
                before = [dict(row) for row in hub.db.execute('SELECT * FROM jobs')]
            finally:
                hub.close()
            identity = (root / 'hub.json').read_bytes()
            with self.command_line_center(root) as (process, status):
                result = desktop.controller_stop_for_update(root)
                self.assertTrue(result['ready_for_update'], result)
                self.assertEqual(result['status'], 'stopping')
                output, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error.decode('utf-8', 'replace'))
                self.assertIn(b'ExLab Center', output)
                self.assertNotIn(common.read_json(root / 'hub.json')['admin_token'].encode(), output)
            self.assertTrue(desktop.controller_install_status(root)['ready_for_install'])
            self.assertFalse((root / 'desktop-process.json').exists())
            self.assertEqual((root / 'hub.json').read_bytes(), identity)
            hub = Hub(root)
            try:
                self.assertEqual([dict(row) for row in hub.db.execute('SELECT * FROM jobs')], before)
            finally:
                hub.close()

    def test_cli_deactivation_and_explicit_port_override_saved_port(self):
        with temporary_directory() as folder:
            root = Path(folder) / 'center'
            Hub(root).close()
            common.atomic_json(root / 'launcher.json', {'port': 1, 'host': '127.0.0.1'})
            with self.command_line_center(root) as (process, status):
                self.assertGreater(status['port'], 1)
                self.assertEqual(common.read_json(root / 'launcher.json')['port'], status['port'])
                self.assertEqual(desktop.controller_stop(root)['status'], 'stopping')
                process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)
            self.assertFalse(desktop.controller_status(root)['running'])

    def test_cli_browser_keeps_token_in_fragment_and_reuses_same_backend(self):
        with temporary_directory() as folder, patch.object(launcher.webbrowser, 'open') as browser:
            root = Path(folder) / 'center'
            errors = []
            def run():
                try:
                    launcher.controller(root, 0, '127.0.0.1', True)
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not errors:
                    status = desktop.controller_status(root)
                    if status.get('responsive'):
                        break
                    time.sleep(0.01)
                self.assertEqual(errors, [])
                self.assertTrue(status['responsive'])
                token = common.read_json(root / 'hub.json')['admin_token']
                browser.assert_called_once_with(launcher.browser_url(root, status['port']))
                self.assertIn('/#token=' + token, browser.call_args.args[0])
                owner = common.read_json(root / 'desktop-process.json')
                launcher.controller(root, 0, '127.0.0.1', False)
                self.assertEqual(common.read_json(root / 'desktop-process.json'), owner)
            finally:
                owner = common.read_json(root / 'desktop-process.json', {})
                common.atomic_json(root / 'desktop-stop.json', {'nonce': owner.get('nonce')})
                thread.join(5)
                self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
