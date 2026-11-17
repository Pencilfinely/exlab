import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import unittest
from unittest.mock import patch

from expman import common, worker_engine as engine
from tests.support import temporary_directory


class PreparationFailureTests(unittest.TestCase):
    def test_failure_is_persisted_and_redacted_for_windows(self):
        with temporary_directory() as root:
            job = Path(root)
            common.atomic_json(job / 'manifest.json', {'service_root': str(job / 'service')})
            error = job / 'stderr'
            error.write_text('curl: (35) Connection reset by peer\nhttps://name:password@proxy/ node-secret', encoding='utf-8')
            output = io.StringIO()
            with patch('expman.worker_service._redact', side_effect=lambda root, text: text.replace('node-secret', '[redacted]')), contextlib.redirect_stdout(output):
                engine.preparation_failure(job, '下载 Docker 签名密钥', 35, error)
            reply = json.loads(output.getvalue())
            self.assertEqual(reply, common.read_json(job / 'engine-error.json'))
            self.assertFalse(reply['ready'])
            self.assertEqual(reply['exit_code'], 35)
            self.assertIn('Connection reset by peer', reply['detail'])
            self.assertNotIn('password', reply['detail'])
            self.assertNotIn('node-secret', reply['detail'])


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash') and shutil.which('curl'), 'Linux bash/curl required')
class DownloadTests(unittest.TestCase):
    def test_real_curl_recovers_after_connection_resets(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            attempts = 0

            def do_GET(self):
                type(self).attempts += 1
                if self.attempts <= 2:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'verified-download-body')

            def log_message(self, *args):
                pass

        with temporary_directory() as root, http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                script = Path(__file__).resolve().parents[1] / 'deploy/release/Prepare-Isolated-Engine.sh'
                destination = Path(root) / 'download'
                result = subprocess.run(['bash', '-c', 'source "$1"; job_path="$2"; fetch "$3" "$4"',
                                         'test', str(script), str(root), f'http://127.0.0.1:{server.server_port}/key', str(destination)],
                                        text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(Handler.attempts, 3)
                self.assertEqual(destination.read_bytes(), b'verified-download-body')
                self.assertFalse((Path(root) / 'engine-error.json').exists())
            finally:
                server.shutdown()
                thread.join()

    def test_shell_failure_keeps_exit_code_and_reports_command_detail(self):
        with temporary_directory() as root:
            checkout = Path(__file__).resolve().parents[1]
            script = checkout / 'deploy/release/Prepare-Isolated-Engine.sh'
            environment = dict(os.environ, PYTHONPATH=str(checkout), PYTHONUTF8='1')
            result = subprocess.run(['bash', '-c', 'source "$1"; job_path="$2"; run_step "下载 Docker 密钥" bash -c \'echo "curl: (35) Connection reset by peer" >&2; exit 35\'',
                                     'test', str(script), str(root)], env=environment, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 35, result.stdout + result.stderr)
            reply = json.loads(result.stdout)
            self.assertIn('下载 Docker 密钥', reply['detail'])
            self.assertIn('Connection reset by peer', reply['detail'])
            self.assertEqual(reply['exit_code'], 35)
