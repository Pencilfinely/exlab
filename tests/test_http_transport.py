from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from expman import common
from expman.agent import Agent
from tests.support import temporary_directory


def reply(body, *, status=200, declared_size=None):
    size = len(body) if declared_size is None else declared_size
    return (f"HTTP/1.1 {status} Test\r\nContent-Length: {size}\r\n"
            "Connection: close\r\n\r\n").encode() + body


@contextmanager
def response_server(responses):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            received.append(json.loads(body) if body else None)
            response = responses[len(received) - 1]
            if callable(response):
                response = response(received[-1])
            self.wfile.write(response)
            self.wfile.flush()
            self.close_connection = True

        do_GET = do_POST

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Exercise real HTTP parsing without machine-specific proxy settings.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with patch.object(common.urllib.request, 'urlopen', opener.open):
            yield f'http://127.0.0.1:{server.server_port}', received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class HTTPTransportTests(unittest.TestCase):
    def test_truncated_reply_is_retryable_and_post_is_not_replayed(self):
        # Even syntactically complete JSON must not be accepted if framing says
        # more bytes were expected. The server may already have committed a POST.
        body = b'{"ack":{"job":1}}'
        with response_server([reply(body, declared_size=len(body) + 100)]) as (url, received):
            with self.assertRaises(OSError) as caught:
                common.api_request(url, 'private-test-token', {'operation': 'once'})
            self.assertIsInstance(caught.exception.__cause__, http.client.IncompleteRead)
            self.assertNotIn('private-test-token', str(caught.exception))
            self.assertEqual(received, [{'operation': 'once'}])

    def test_truncated_chunked_reply_is_retryable(self):
        response = (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n'
                    b'Connection: close\r\n\r\n10\r\n{"ack":')
        with response_server([response]) as (url, _):
            with self.assertRaises(OSError) as caught:
                common.api_request(url, 'test-token')
            self.assertIsInstance(caught.exception.__cause__, http.client.IncompleteRead)

    def test_malformed_status_line_is_retryable(self):
        with response_server([b'not-http\r\n\r\n']) as (url, _):
            with self.assertRaises(OSError) as caught:
                common.api_request(url, 'test-token')
            self.assertIsInstance(caught.exception.__cause__, http.client.BadStatusLine)

    def test_http_status_and_json_errors_are_preserved(self):
        for status in (401, 409):
            with self.subTest(status=status), response_server([reply(b'{}', status=status)]) as (url, _):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    common.api_request(url, 'test-token')
                self.assertEqual(caught.exception.code, status)
                caught.exception.close()
        with response_server([reply(b'{bad-json')]) as (url, _):
            with self.assertRaises(ValueError):
                common.api_request(url, 'test-token')

    def test_worker_survives_lost_reply_and_replays_ack_without_restarting_job(self):
        job_id = 'a' * 32
        responses = [reply(b'{"ack":', declared_size=1024),
                     lambda request: reply(json.dumps({
                         'jobs': [], 'ack': {item['id']: item['seq'] for item in request['reports']}
                     }).encode())]
        with temporary_directory() as folder, response_server(responses) as (url, received):
            config = Path(folder) / 'node.json'
            common.atomic_json(config, {'node_id': 'test', 'hub_url': url,
                'token': 'test-token', 'root': str(Path(folder) / 'runtime'), 'allow_demo': True})
            agent = Agent(config)
            record = {'id': job_id, 'state': 'running', 'attempt': 1, 'generation': 1,
                      'seq': 0, 'command_ack': 0, 'prepared': True, 'metrics': {},
                      'spec': common.validate_task({'backend': 'demo'})}
            agent._save(record)
            expected = agent.records()
            try:
                with patch.object(agent, '_reconcile'), patch.object(agent, '_metrics'), \
                        patch.object(agent.result_delivery, 'start'), \
                        patch.object(agent.execution_transfer, 'start'), \
                        patch.object(agent, '_start', side_effect=AssertionError('Must not restart training')):
                    first = agent.tick()
                    self.assertFalse(first['online'])
                    self.assertIn('IncompleteRead', first['error'])
                    self.assertEqual(list(agent.db.execute('SELECT * FROM report_acks')), [])
                    second = agent.tick()
                self.assertTrue(second['online'])
                self.assertIsNone(second['error'])
                self.assertFalse(agent.closed)
                self.assertEqual(agent.records(), expected)
                self.assertEqual(received[0]['reports'], received[1]['reports'])
                self.assertEqual(dict(agent.db.execute('SELECT job_id,seq FROM report_acks')),
                                 {job_id: expected[0]['seq']})
            finally:
                agent.close()

    def test_worker_tries_another_center_after_truncated_reply(self):
        with temporary_directory() as folder, \
                response_server([reply(b'{', declared_size=100)]) as (first_url, _), \
                response_server([reply(b'{"jobs":[],"ack":{}}')]) as (second_url, _):
            config = Path(folder) / 'node.json'
            common.atomic_json(config, {'node_id': 'test', 'hub_url': first_url,
                'hub_urls': [second_url], 'token': 'test-token',
                'root': str(Path(folder) / 'runtime'), 'allow_demo': True})
            agent = Agent(config)
            try:
                agent._sync({})
                self.assertTrue(agent.online)
                self.assertEqual(agent.config['hub_url'], second_url)
            finally:
                agent.close()


if __name__ == '__main__':
    unittest.main()
