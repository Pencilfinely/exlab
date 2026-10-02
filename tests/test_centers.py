import base64
import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from expman import common
from expman.centers import validate_center_credential
from expman.hub import APIError, Hub, make_server
from expman.pairing import parse_connection_text, validate_pairing
from tests.support import temporary_directory
from tests.test_hub import SPEC, SNAPSHOT


class CenterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.primary, self.replica = Hub(self.root / 'primary'), Hub(self.root / 'replica')
        self.servers = []
        for hub in (self.primary, self.replica):
            server = make_server(hub, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.servers.append((server, thread))
        self.url, self.replica_url = [f'http://127.0.0.1:{item[0].server_port}' for item in self.servers]

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            thread.join(3)
            server.server_close()
        self.replica.close()
        self.primary.close()
        self.temporary.__exit__(None, None, None)

    def join(self):
        self.credential = self.primary.center_enroll({'name': 'ROG 5070', 'hub_url': self.url})
        return self.replica.center_connect({'credential': self.credential, 'hub_url': self.replica_url})

    def request(self, path, payload=None, token=None, replica=True):
        url = self.replica_url if replica else self.url
        hub = self.replica if replica else self.primary
        request = urllib.request.Request(url + path, data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization': 'Bearer ' + (token or hub.config['admin_token']), 'Content-Type': 'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
            return json.load(response)

    def test_snapshot_preserves_jobs_nodes_and_local_administrator_identity(self):
        node_token = self.primary.add_node('5070ti')
        job = self.primary.submit({'request_id': 'before-sync', 'spec': SPEC})['ids'][0]
        local_token = self.replica.config['admin_token']
        result = self.join()
        self.assertEqual(result['mode'], 'replica')
        self.assertEqual(self.replica.config['admin_token'], local_token)
        self.assertNotEqual(local_token, self.primary.config['admin_token'])
        self.assertEqual(self.replica.config['nodes']['5070ti'], node_token)
        self.assertEqual(self.replica.state()['jobs'][0]['id'], job)
        self.assertNotIn('admin_token', self.primary.center_snapshot()['configuration'])
        self.assertIn(self.replica_url, self.primary.center_addresses())

    def test_replica_forwards_writes_without_creating_its_own_schedule(self):
        self.join()
        result = self.request('/api/jobs', {'request_id': 'from-rog', 'spec': SPEC})
        self.assertEqual(self.primary.state()['jobs'][0]['id'], result['ids'][0])
        self.assertFalse(self.replica.state()['jobs'])
        with self.assertRaises(APIError):
            self.replica.submit({'request_id': 'illegal-local', 'spec': SPEC})
        self.replica.center_sync()
        self.assertEqual(self.replica.state()['jobs'][0]['id'], result['ids'][0])

    def test_handover_fences_source_before_recipient_accepts_work(self):
        self.primary.add_node('node-a')
        job = self.primary.submit({'request_id': 'original', 'spec': SPEC})['ids'][0]
        self.primary.sync('node-a', {'node_id': 'node-a', 'snapshot': SNAPSHOT, 'reports': []})
        self.join()
        self.replica.center_promote({})
        self.assertEqual(self.primary.center_state['mode'], 'handoff')
        self.assertEqual(self.replica.center_state['mode'], 'primary')
        with self.assertRaises(APIError):
            self.primary.sync('node-a', {'node_id': 'node-a', 'snapshot': SNAPSHOT, 'reports': []})
        report = self.replica.sync('node-a', {'node_id': 'node-a', 'snapshot': SNAPSHOT, 'reports': []})
        self.assertEqual(report['jobs'][0]['id'], job)
        self.assertEqual(len(self.replica.state()['jobs']), 1)
        self.replica.center_promote({})  # Retry cannot create another primary.

    def test_unreachable_primary_cannot_be_promoted_from_stale_state(self):
        self.join()
        with patch('expman.centers.remote_json', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                self.replica.center_promote({})
        self.assertEqual(self.replica.center_state['mode'], 'replica')
        self.assertEqual(self.primary.center_state['mode'], 'primary')

    def test_worker_switches_to_new_primary_with_same_job_and_attempt(self):
        from expman.agent import Agent
        token = self.primary.add_node('worker')
        job = self.primary.submit({'request_id': 'existing-job', 'spec': SPEC})['ids'][0]
        self.join()
        config = self.root / 'worker.json'
        common.atomic_json(config, {'node_id': 'worker', 'token': token, 'hub_url': self.url,
                                    'root': str(self.root / 'worker'), 'allow_demo': True})
        agent = Agent(config)
        try:
            agent._sync(SNAPSHOT)
            self.assertTrue(agent.online)
            self.assertIn(self.replica_url, agent._meta('hub_urls'))
            self.assertEqual(agent.records()[0]['id'], job)
            self.replica.center_promote({})
            agent._sync(SNAPSHOT)
            self.assertTrue(agent.online)
            self.assertEqual(agent.config['hub_url'], self.replica_url)
            self.assertEqual([(record['id'], record['attempt']) for record in agent.records()], [(job, 1)])
        finally:
            agent.close()

    def test_old_primary_syncs_back_and_can_take_control_again(self):
        self.join()
        self.replica.center_promote({})
        self.primary.center_sync()
        self.assertEqual(self.primary.center_state['mode'], 'replica')
        self.assertEqual(self.primary.center_state['upstream']['hub_url'], self.replica_url)
        created = self.replica.submit({'request_id': 'on-laptop', 'spec': SPEC})['ids'][0]
        self.primary.center_sync()
        self.assertEqual(self.primary.state()['jobs'][0]['id'], created)
        self.primary.center_promote({})
        self.assertEqual(self.primary.center_state['mode'], 'primary')
        self.assertEqual(self.replica.center_state['mode'], 'handoff')
        self.replica.center_sync()
        self.assertEqual(self.replica.center_state['mode'], 'replica')

    def test_offline_replica_serves_cached_reads_and_refuses_writes(self):
        self.primary.submit({'request_id': 'cached', 'spec': SPEC})
        self.join()
        with patch('expman.hub.open_remote', side_effect=OSError('offline')):
            cached = self.request('/api/state')
            self.assertEqual(len(cached['jobs']), 1)
            self.assertEqual(cached['center']['mode'], 'replica')
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request('/api/jobs', {'request_id': 'unsafe-offline-write', 'spec': SPEC})
        self.assertEqual(caught.exception.code, 503)
        self.assertEqual(len(self.replica.state()['jobs']), 1)

    def test_revoked_credential_cannot_read_or_proxy_new_data(self):
        self.join()
        self.primary.center_revoke({'center_id': self.credential['center_id']})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/state')
        self.assertEqual(caught.exception.code, 401)

    def test_existing_workspace_is_not_overwritten_on_join(self):
        self.replica.add_node('local-node')
        credential = self.primary.center_enroll({'name': 'ROG', 'hub_url': self.url})
        with self.assertRaises(APIError):
            self.replica.center_connect({'credential': credential, 'hub_url': self.replica_url})
        self.assertIn('local-node', self.replica.config['nodes'])
        self.assertEqual(self.replica.center_state['mode'], 'primary')

    def test_failed_file_hash_does_not_commit_snapshot(self):
        self.join()
        snapshot = self.primary.center_snapshot()
        snapshot['sha256'] = '0' * 64
        with patch('expman.centers.remote_json', return_value=snapshot):
            with self.assertRaises(ValueError):
                self.replica.center_sync()
        self.assertFalse(self.replica.state()['jobs'])

    def test_result_files_are_mirrored_and_verified_before_handover(self):
        self.primary.add_node('node-a')
        job = self.primary.submit({'request_id': 'result', 'spec': SPEC})['ids'][0]
        self.primary.sync('node-a', {'node_id': 'node-a', 'snapshot': SNAPSHOT, 'reports': []})
        data = b'checkpoint data'
        digest = hashlib.sha256(data).hexdigest()
        self.primary.upload('node-a', {'job_id': job, 'name': 'checkpoint.bin', 'sha256': digest,
            'size': len(data), 'offset': 0, 'data': base64.b64encode(data).decode()})
        self.join()
        self.assertEqual(self.replica.artifact(job, digest).read_bytes(), data)

    def test_center_credential_cannot_access_local_machine_routes(self):
        self.join()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/local/centers/status', token=self.credential['token'], replica=False)
        self.assertEqual(caught.exception.code, 403)

    def test_name_changes_preserve_pairing_identity(self):
        token = self.primary.add_node('gpu-a')
        self.primary.rename_node({'node_id': 'gpu-a', 'name': '5070 Ti · 桌面'})
        self.assertEqual(self.primary.state()['nodes'][0]['display_name'], '5070 Ti · 桌面')
        self.assertEqual(self.primary.config['nodes']['gpu-a'], token)
        self.primary.center_rename({'name': '我的主控'})
        self.assertEqual(self.primary.center_info()['name'], '我的主控')

    def test_connection_codes_round_trip_and_reject_credentials_in_urls(self):
        credential = self.primary.center_enroll({'name': '笔记本', 'hub_url': self.url})
        code = 'exlab://connect/' + base64.urlsafe_b64encode(json.dumps(credential).encode()).decode().rstrip('=')
        self.assertEqual(validate_center_credential(parse_connection_text(code)), credential)
        with self.assertRaises(ValueError):
            validate_center_credential(dict(credential, hub_url='http://secret:password@example.test'))
        pairing = {'schema': 1, 'node_id': 'test', 'token': 'example-worker-credential-1234',
                   'hub_url': self.url, 'hub_urls': [self.replica_url]}
        self.assertEqual(validate_pairing(pairing)['hub_urls'], [self.url, self.replica_url])
