import copy
import json
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request

from expman import common, compute_policy, host_control, worker_service
from expman.hub import APIError, Hub, make_server
from tests.support import temporary_directory
from tests.test_hub import SPEC, SNAPSHOT


class HostControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.root = Path(self.tmp.__enter__())
        self.hub = Hub(self.root)
        self.token = self.hub.add_node('windows')
        self.snapshot = {**copy.deepcopy(SNAPSHOT), 'runtime_host': 'windows',
                         'capabilities': [*SNAPSHOT.get('capabilities', []), compute_policy.CAPABILITY, compute_policy.CONTROL_CAPABILITY]}
        self.hub.sync('windows', {'node_id': 'windows', 'snapshot': self.snapshot, 'reports': []})
        self.policy = self.hub.set_compute_policy({'node_id': 'windows', 'policy': {'revision': 0, 'mode': 'release'}})['policy']
        self.request = {'node_id': 'windows', 'host_id': 'a' * 32}

    def tearDown(self):
        self.hub.close()
        self.tmp.__exit__(None, None, None)

    def sync(self, report=None):
        return host_control.sync(self.hub, 'windows', {**self.request, 'report': report})

    def report(self):
        intent = self.sync()['desired']['intent']
        return dict(intent=intent, phase='released', detail='Docker / WSL verified stopped',
                    experiments_stopped=True, docker_stopped=True, wsl_stopped=True, resources_released=True)

    def node(self):
        return self.hub.state(summary=True)['nodes'][0]

    def test_training_receipt_is_not_full_windows_release(self):
        snapshot = copy.deepcopy(self.snapshot)
        compute_policy.apply(snapshot, self.policy)
        availability = snapshot['availability']
        snapshot['compute_control'] = dict(mode='release', intent=compute_policy.control_intent(availability),
            policy={k: availability.get(k) for k in ('mode', 'revision', 'source', 'until', 'reason')},
            observed_at=common.now(), phase='released', experiments_stopped=True,
            resources_released=True, runtime_retained=True, detail='Training containers cleared')
        self.hub.sync('windows', {'node_id': 'windows', 'snapshot': snapshot, 'reports': []})
        control = self.node()['snapshot']['compute_control']
        self.assertFalse(control['resources_released'])
        self.assertTrue(control['training_resources_released'])
        self.assertEqual(control['phase'], 'releasing')
        report = self.report()
        self.sync(report)
        control = self.node()['snapshot']['compute_control']
        self.assertTrue(control['resources_released'])
        self.assertFalse(control['runtime_retained'])

    def test_each_runtime_confirmation_is_required(self):
        report = self.report()
        for key in ('experiments_stopped', 'docker_stopped', 'wsl_stopped', 'resources_released'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.sync({**report, key: False})

    def test_stale_intent_or_lost_host_heartbeat_cannot_confirm_release(self):
        report = self.report()
        self.sync(report)
        self.hub.db.execute('UPDATE node_host_control SET updated=0')
        self.assertFalse(self.node()['snapshot']['compute_control']['resources_released'])
        self.hub.set_compute_policy({'node_id': 'windows', 'policy': {**self.policy, 'reason': 'new release request'}})
        self.assertFalse(self.sync(report)['report_matches'])
        self.assertFalse(self.node()['snapshot']['compute_control']['resources_released'])

    def test_windows_heartbeat_survives_dormancy_without_making_worker_schedulable(self):
        self.hub.db.execute('UPDATE nodes SET last_seen=0')
        self.sync(self.report())
        node = self.node()
        self.assertTrue(node['online'])
        self.assertFalse(node['worker_online'])
        self.assertEqual(node['last_seen'], 0)
        self.hub.set_compute_policy({'node_id': 'windows', 'policy': {**self.policy, 'mode': 'available'}})
        self.assertEqual(self.sync()['desired']['mode'], 'available')
        identity = self.hub.submit({'spec': SPEC, 'request_id': 'worker-still-offline'})['ids'][0]
        self.assertIsNone(self.hub.job(identity)['node_id'])

    def test_host_cannot_report_for_another_node(self):
        with self.assertRaises(APIError) as error:
            host_control.sync(self.hub, 'windows', {**self.request, 'node_id': 'other'})
        self.assertEqual(error.exception.status, 403)

    def test_host_registration_before_first_worker_snapshot_is_safe(self):
        self.hub.add_node('new-windows')
        host_control.sync(self.hub, 'new-windows', {'node_id': 'new-windows', 'host_id': 'b' * 32})
        node = next(n for n in self.hub.state(summary=True)['nodes'] if n['id'] == 'new-windows')
        self.assertTrue(node['online'])
        self.assertFalse(node['worker_online'])
        self.assertFalse(node['snapshot']['availability']['available'])

    def test_http_uses_node_credential_and_leaves_administrator_scope_separate(self):
        server = make_server(self.hub, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(token):
            return opener.open(urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/host/sync',
                data=json.dumps(self.request).encode(), headers={'Authorization': 'Bearer ' + token,
                'Content-Type': 'application/json'}), timeout=3)
        try:
            with request(self.token) as response:
                self.assertEqual(json.load(response)['desired']['mode'], 'release')
            for token in ('bad', self.hub.config['admin_token']):
                with self.assertRaises(urllib.error.HTTPError):
                    request(token)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_local_host_link_contains_only_this_node_credential(self):
        root = self.root / 'worker-service'
        config = self.root / 'worker.json'
        common.atomic_json(config, dict(node_id='windows', token=self.token, hub_url='http://127.0.0.1:8765',
                                       unrelated_private_value='not for host'))
        common.atomic_json(root / 'service.json', dict(config=str(config), node_id='windows'))
        self.assertEqual(worker_service.host_info(root), dict(node_id='windows', token=self.token, hub_url='http://127.0.0.1:8765'))


if __name__ == '__main__':
    unittest.main()
