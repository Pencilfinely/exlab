import base64
import hashlib
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from expman import common, compute_policy, execution_control, result_protocol, scheduler
from expman.agent import Agent
from expman.hub import APIError, Hub, make_server
from expman.__main__ import node_config
from tests.support import temporary_directory


def snapshot():
    return {"allow_demo": True, "policy": {"max_running": 1, "max_prefetch": 4},
            "local_time": "23:00", "capabilities": [compute_policy.CAPABILITY, execution_control.CAPABILITY,
                result_protocol.CAPABILITY, result_protocol.EVIDENCE_CAPABILITY]}


class DynamicComputeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.hub = Hub(self.root / "hub")
        for name in ("a", "b"):
            self.hub.add_node(name)
            self.sync(name)

    def tearDown(self):
        self.hub.close()
        self.temporary.__exit__(None, None, None)

    def sync(self, node, reports=None, snap=None):
        return self.hub.sync(node, {"node_id": node, "snapshot": snap or snapshot(), "reports": reports or []})

    def submit(self, **extra):
        spec = {"backend": "demo", "params": {"steps": 40, "delay": .03}, "resources": {"gpu_memory_mb": 0}, **extra}
        return self.hub.submit({"spec": spec, "request_id": str(time.time_ns())})["ids"][0]

    def report(self, job, seq=1, state="running", generation=1, attempt=1, started=True, stopped=False, manifest=None):
        return {"id": job, "seq": seq, "state": state, "attempt": attempt, "generation": generation,
                "command_ack": self.hub.job(job)["command_id"],
                "execution_state": {"schema_version": 1, "attempt": attempt, "generation": generation,
                    "ever_started": started, "stopped_confirmed": stopped, "checkpoint": manifest,
                    "progress": {"step": 5}, "diagnostics": {"note": "large mutable observation" * 100}}}

    def manifest(self, data=b'{"step":5}'):
        return {"path": "checkpoint.bin", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "step": 5, "time": 1234}

    def upload(self, job, data=b'{"step":5}', offset=0, generation=1, attempt=1, manifest=None):
        return self.hub.upload_execution_checkpoint("a", {"job_id": job, "attempt": attempt,
            "generation": generation, "manifest": manifest or self.manifest(data), "offset": offset,
            "data": base64.b64encode(data).decode()})

    def test_compute_policy_cas_timed_release_and_maintenance(self):
        policy = compute_policy.validate({"mode": "release", "until": 2000})
        saved = self.hub.set_compute_policy({"node_id": "a", "policy": policy})["policy"]
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(self.hub.set_compute_policy({"node_id": "a", "policy": policy})["policy"], saved)
        with self.assertRaises(APIError):
            self.hub.set_compute_policy({"node_id": "a", "policy": {"revision": 0, "kind": "portable"}})
        self.assertTrue(compute_policy.evaluate(policy, timestamp=1999)["stopping"])
        self.assertTrue(compute_policy.evaluate(policy, timestamp=2000)["available"])
        policy = compute_policy.validate({"maintenance": [{"start": 2100, "end": 2200}]})
        self.assertEqual(compute_policy.evaluate(policy, timestamp=2000)["available_seconds"], 100)
        self.assertTrue(compute_policy.evaluate(policy, timestamp=2100)["stopping"])
        self.assertTrue(compute_policy.evaluate(policy, timestamp=2200)["available"])

    def test_stop_and_hold_requires_updated_worker_and_keeps_resource_release_distinct(self):
        with self.assertRaises(APIError):
            self.hub.set_compute_policy({'node_id': 'a', 'policy': {'mode': 'stop'}})
        snap = snapshot()
        snap['capabilities'].append(compute_policy.CONTROL_CAPABILITY)
        self.sync('a', snap=snap)
        self.hub.set_compute_policy({'node_id': 'a', 'policy': {'mode': 'stop'}})
        value = compute_policy.evaluate(self.hub._compute_policy('a'))
        self.assertTrue(value['stopping'])
        self.assertFalse(value['release_resources'])

    def test_ac_unknown_and_manual_override_are_local_admission_gates(self):
        policy = compute_policy.validate({"require_ac": True})
        for value in (False, None):
            self.assertTrue(compute_policy.evaluate(policy, {"ac_power": value})["stopping"])
        self.assertTrue(compute_policy.evaluate(policy, {"ac_power": True})["available"])
        path = self.root / 'node.json'
        common.atomic_json(path, node_config('a', 'http://127.0.0.1:9', 'test', self.root / 'node', True))
        agent = Agent(path)
        try:
            common.atomic_json(path.parent / 'compute-override.json', {"mode": "release", "reason": "游戏"})
            agent._sync = lambda _: None
            agent.tick()
            self.assertFalse(agent.snapshot()['availability']['available'])
            agent.close()
            agent = Agent(path)
            self.assertTrue(agent.snapshot()['availability']['stopping'])
        finally:
            agent.close()

    def test_risk_and_expected_work_change_assignment_without_changing_params(self):
        spec = common.validate_task({"backend": "demo", "params": {"seed": 42}, "resources": {"gpu_memory_mb": 0}})
        nodes = self.hub._scheduling_nodes()
        nodes[0]['snapshot']['availability'] = {'risk': .9}
        self.assertEqual(scheduler.choose_node(spec, nodes, {}), 'b')
        nodes[0]['snapshot']['availability'] = {'available': True}
        nodes[0]['queued_work_seconds'] = 5000
        self.assertEqual(scheduler.choose_node(spec, nodes, {}), 'b')
        self.assertEqual(spec['params'], {'seed': 42})

    def test_nonresumable_work_includes_queue_time_before_maintenance(self):
        spec = common.validate_task({'backend': 'demo', 'resources': {'gpu_memory_mb': 0},
                                     'planning': {'expected_seconds': 100}})
        # Use the admission rules of a non-resumable algorithm without a GPU fixture.
        spec['backend'] = 'docker'
        nodes = self.hub._scheduling_nodes()
        nodes[0]['snapshot']['availability'] = {'available': True, 'available_seconds': 500}
        nodes[0]['queued_work_seconds'] = 450
        with patch.object(scheduler, 'eligible', return_value=True):
            self.assertEqual(scheduler.choose_node(spec, nodes, {}), 'b')

    def test_one_bad_checkpoint_does_not_starve_other_experiments(self):
        path = self.root / 'node.json'
        common.atomic_json(path, node_config('a', 'http://127.0.0.1:9', 'test', self.root / 'node', True))
        agent = Agent(path)
        try:
            agent._set_meta('execution_protocol', 1)
            for identity in ('a' * 32, 'b' * 32):
                agent.db.execute('INSERT INTO tasks(id,record) VALUES (?,?)', (identity, json.dumps({'id': identity})))
            agent.db.commit()
            lane = agent.execution_transfer
            def freeze(db, record):
                if record['id'] == 'a' * 32:
                    raise OSError('producer published a broken checkpoint')
            with patch.object(lane, '_restore'), patch.object(lane, '_freeze', side_effect=freeze), \
                    patch.object(lane, '_upload') as upload, \
                    patch.object(lane.stop, 'wait', side_effect=lambda _: lane.stop.set()):
                lane._run()
            self.assertEqual(upload.call_args.args[1]['id'], 'b' * 32)
            from expman.execution_transfer import telemetry
            self.assertIn('broken checkpoint', telemetry(agent.db)['last_error'])
        finally:
            agent.close()

    def test_local_and_remote_restrictions_compose_without_forging_worker_ack(self):
        local = compute_policy.validate({'mode': 'drain', 'reliability': .4})
        remote = compute_policy.validate({'revision': 4, 'mode': 'release'})
        snap = snapshot()
        snap.update(local_compute_policy=local, compute_policy_revision=3)
        compute_policy.apply(snap, remote)
        self.assertTrue(snap['availability']['stopping'])
        self.assertAlmostEqual(snap['availability']['risk'], .6)
        self.assertEqual(snap['compute_policy_revision'], 3)
        # A center allowing work cannot remove a local release or AC restriction.
        snap['local_compute_policy'] = compute_policy.validate({'mode': 'release'})
        compute_policy.apply(snap, compute_policy.validate({}))
        self.assertTrue(snap['availability']['stopping'])
        self.sync('a', snap=snap)
        self.hub.set_compute_policy({'node_id': 'a', 'policy': {}})
        center_view = next(n for n in self.hub._scheduling_nodes() if n['id'] == 'a')
        self.assertFalse(center_view['snapshot']['availability']['available'])

    def test_snapshot_rejects_malformed_availability_and_maintenance_preserves_manual_hold(self):
        for extra in ({'availability': {'reason': {}}}, {'availability': {'risk': 'high'}},
                      {'availability': {'available': 1}}, {'ac_power': 'yes'},
                      {'local_compute_policy': {'require_ac': 1}}, {'compute_policy_revision': True}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.sync('a', snap={**snapshot(), **extra})
        policy = compute_policy.validate({'mode': 'release', 'until': 2400,
            'maintenance': [{'start': 2100, 'end': 2200}, {'start': 2110, 'end': 2300}]})
        self.assertEqual(compute_policy.evaluate(policy, timestamp=2150)['until'], 2400)
        policy['until'] = None
        self.assertIsNone(compute_policy.evaluate(policy, timestamp=2150)['until'])

    def test_execution_reports_are_mutable_and_independent_of_final_results(self):
        job = self.submit()
        self.sync('a', [self.report(job)])
        self.assertEqual(self.hub.job(job)['execution_state']['progress']['step'], 5)
        self.assertEqual(self.hub.db.execute('SELECT COUNT(*) FROM experiment_results').fetchone()[0], 0)
        self.assertNotIn('diagnostics', self.hub.state()['jobs'][0]['execution_state'])
        stale = self.report(job, state='paused', stopped=True)
        self.sync('a', [stale])
        self.assertEqual(self.hub.job(job)['state'], 'running')
        bad = self.report(job, seq=2, stopped=True)
        with self.assertRaises(ValueError):
            self.sync('a', [bad])
        self.assertEqual(self.hub.job(job)['state'], 'running')

    def test_confirmed_stop_and_backup_are_both_required_for_handoff(self):
        job = self.submit()
        manifest = self.manifest()
        self.sync('a', [self.report(job, manifest=manifest)])
        self.hub.request_handoff({'job_id': job, 'generation': 1})
        self.assertEqual(self.hub.job(job)['node_id'], 'a')
        self.sync('a', [self.report(job, seq=2, state='paused', stopped=True, manifest=manifest)])
        self.assertEqual(self.hub.job(job)['handoff']['status'], 'waiting_checkpoint')
        self.assertTrue(self.upload(job)['complete'])
        current = self.hub.job(job)
        self.assertEqual((current['node_id'], current['generation'], current['attempt']), ('b', 2, 2))
        self.assertEqual(len(current['execution_checkpoints']), 1)
        # Lost final upload ACK remains idempotent after the ownership change.
        self.assertTrue(self.upload(job)['complete'])
        old = self.report(job, seq=999, generation=1, attempt=1)
        response = self.sync('a', [old])
        self.assertIn({'id': job, 'generation': 1}, response['retired'])
        self.assertEqual(self.hub.job(job)['state'], 'assigned')
        self.sync('b', [self.report(job, generation=2, attempt=2)])
        self.assertEqual(self.hub.job(job)['state'], 'running')
        with self.assertRaises(APIError):
            self.sync('a', [self.report(job, seq=1000, generation=2, attempt=2)])
        self.hub._execution_result_owner('a', job, 1)
        with self.assertRaises(APIError):
            self.hub._execution_result_owner('a', job, 2)
        self.hub.upload_execution_checkpoint('b', {'job_id': job, 'attempt': 2,
            'generation': 2, 'manifest': manifest, 'offset': 0,
            'data': base64.b64encode(b'{"step":5}').decode()})
        with self.assertRaises(APIError):
            self.upload(job, attempt=2, generation=1)

    def test_unstarted_work_can_be_returned_without_fake_resume(self):
        job = self.submit()
        self.hub.request_handoff({'job_id': job, 'generation': 1, 'target_node_id': 'b'})
        self.sync('a', [self.report(job, state='paused', started=False, stopped=True)])
        current = self.hub.job(job)
        self.assertEqual(current['node_id'], 'b')
        self.assertIsNone(current['resume_checkpoint'])

    def test_disconnect_never_reassigns_unconfirmed_running_process(self):
        job = self.submit()
        self.sync('a', [self.report(job)])
        self.hub.request_handoff({'job_id': job, 'generation': 1})
        self.hub.db.execute('UPDATE nodes SET last_seen=0 WHERE id=?', ('a',))
        self.sync('b')
        self.assertEqual(self.hub.job(job)['node_id'], 'a')
        self.assertEqual(self.hub.job(job)['generation'], 1)

    def test_chunk_resume_checksum_and_bad_paths(self):
        job = self.submit()
        manifest = self.manifest(b'hello world')
        self.sync('a', [self.report(job, manifest=manifest)])
        self.assertEqual(self.upload(job, b'hello ', manifest=manifest)['offset'], 6)
        self.assertEqual(self.upload(job, b'', manifest=manifest)['offset'], 6)
        self.assertTrue(self.upload(job, b'world', offset=6, manifest=manifest)['complete'])
        with self.assertRaises(ValueError):
            self.upload(job, b'', manifest={**manifest, 'path': '../bad'})
        with self.assertRaises(APIError):
            self.upload(job, b'', manifest={**manifest, 'step': 6})
        bad = self.manifest(b'right')
        with self.assertRaises(APIError):
            self.upload(job, b'wrong', manifest=bad)
        self.assertEqual(self.upload(job, b'', manifest=bad)['offset'], 0)

    def test_release_state_and_pending_handoff_survive_controller_restart(self):
        job = self.submit()
        self.sync('a', [self.report(job)])
        self.hub.set_compute_policy({'node_id': 'a', 'policy': {'mode': 'release'}})
        self.sync('a')
        self.hub.close()
        self.hub = Hub(self.root / 'hub')
        self.assertEqual(self.hub._compute_policy('a')['mode'], 'release')
        self.assertEqual(self.hub.job(job)['handoff']['status'], 'requested')

    def test_manual_stop_cancels_pending_automatic_handoff(self):
        job = self.submit()
        self.sync('a', [self.report(job)])
        self.hub.set_compute_policy({'node_id': 'a', 'policy': {'mode': 'release'}})
        self.sync('a')
        self.assertEqual(self.hub.job(job)['handoff']['status'], 'requested')
        self.hub.action({'job_id': job, 'action': 'stop'})
        report = self.report(job, seq=2, state='paused', stopped=True)
        report['execution_state']['release'] = '游戏'
        self.sync('a', [report])
        self.sync('b')
        self.assertEqual(self.hub.job(job)['handoff']['status'], 'canceled')
        self.assertEqual(self.hub.job(job)['generation'], 1)

    def test_checkpoint_manifest_and_bytes_are_part_of_full_center_replication(self):
        job = self.submit()
        data = b'{"step":5}'
        manifest = self.manifest(data)
        self.sync('a', [self.report(job, manifest=manifest)])
        self.upload(job, data, manifest=manifest)
        replica = self.hub.center_snapshot()
        key = 'execution-checkpoints/' + job + '/' + manifest['sha256']
        self.assertEqual(replica['files'][key], {'sha256': manifest['sha256'], 'size': len(data)})
        self.assertEqual(base64.b64decode(self.hub.center_file(key, 0)['data']), data)
        (self.hub.root / key).unlink()
        with self.assertRaises(APIError):
            self.hub.center_snapshot()

    def test_queue_hold_priority_and_retries_preserve_immutable_scientific_spec(self):
        self.hub.set_mode({'node_id': 'a', 'mode': 'drain'})
        self.hub.set_mode({'node_id': 'b', 'mode': 'drain'})
        job = self.submit()
        original = self.hub.job(job)['spec']
        payload = {'job_id': job, 'priority': 90, 'hold': True, 'revision': 0}
        result = self.hub.set_queue_control(payload)
        self.assertEqual(self.hub.set_queue_control(payload)['revision'], result['revision'])
        self.hub.set_mode({'node_id': 'a', 'mode': 'run'})
        self.assertIsNone(self.hub.job(job)['node_id'])
        self.assertEqual(self.hub.job(job)['spec'], original)
        self.hub.set_queue_control({**payload, 'revision': 1, 'hold': False})
        self.assertEqual(self.hub.job(job)['node_id'], 'a')
        with self.assertRaises(APIError):
            self.hub.set_queue_control({**payload, 'revision': 2, 'priority': 80})

    def test_ai_advice_is_source_free_validated_and_never_applied_implicitly(self):
        from expman.ai_assist import AIAssist
        self.hub.set_mode({'node_id': 'a', 'mode': 'drain'})
        self.hub.set_mode({'node_id': 'b', 'mode': 'drain'})
        job = self.submit()
        reply = {'summary': '建议降低便携设备的承诺', 'warnings': ['断电时间不确定'],
                 'node_policies': [{'node_id': 'a', 'policy': {'kind': 'portable', 'require_ac': True}}],
                 'queue_priorities': [{'job_id': job, 'priority': 80}]}
        with patch.object(AIAssist, '_complete', return_value=reply) as complete:
            result = self.hub.scheduling_assist({'instructions': 'ROG 只在插电时运行'})
        self.assertIsNone(self.hub._compute_policy('a'))
        self.assertEqual(self.hub.job(job)['queue_control']['priority'], 0)
        context = complete.call_args.args[1]
        text = json.dumps(context)
        self.assertNotIn('"source"', text)
        self.assertNotIn(self.hub.config['admin_token'], text)
        self.assertEqual(result['node_policies'][0]['policy']['revision'], 0)
        for invalid in ({'node_policies': [{'node_id': 'unknown', 'policy': {}}]},
                        {'node_policies': [{'node_id': 'a', 'policy': {'revision': 99}}]},
                        {'queue_priorities': [{'job_id': job, 'priority': True}]},
                        {'queue_priorities': [{'job_id': job, 'priority': 101}]}):
            with patch.object(AIAssist, '_complete', return_value=invalid), self.assertRaises(ValueError):
                self.hub.scheduling_assist({'instructions': '安排实验'})

    def test_historical_lightweight_result_stays_owned_by_its_original_execution(self):
        job = self.submit()
        manifest = self.manifest()
        self.sync('a', [self.report(job, manifest=manifest)])
        original = self.hub.job(job)
        self.hub.request_handoff({'job_id': job, 'generation': 1})
        self.sync('a', [self.report(job, seq=2, state='paused', stopped=True, manifest=manifest)])
        self.upload(job)
        record = {'id': job, 'attempt': 1, 'spec': original['spec'], 'state': 'paused'}
        envelope = result_protocol.envelope(record, {}, 'default', 'a', common.now())
        raw = result_protocol.encoded(envelope)
        payload = {'sha256': hashlib.sha256(raw).hexdigest(), 'data': base64.b64encode(raw).decode()}
        self.hub.receive_result('a', payload)
        self.assertEqual(self.hub.job(job)['generation'], 2)
        self.assertEqual(self.hub.job(job)['state'], 'assigned')
        self.assertLess(len(raw), result_protocol.SOFT_LIMIT)


class DynamicComputeHTTPTests(unittest.TestCase):
    def test_pause_admission_then_stop_holds_work_until_explicit_resume(self):
        with temporary_directory() as directory:
            root = Path(directory)
            hub = Hub(root / 'hub')
            server = make_server(hub, port=0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            agents = []
            try:
                for name in ('a', 'b'):
                    config = node_config(name, 'http://127.0.0.1:' + str(server.server_address[1]),
                                         hub.add_node(name), root / name, True)
                    path = root / (name + '.json')
                    common.atomic_json(path, config)
                    agents.append(Agent(path))
                    agents[-1].tick()
                job = hub.submit({'spec': {'backend': 'demo', 'params': {'steps': 200, 'delay': .04},
                    'resources': {'gpu_memory_mb': 0}}, 'request_id': 'three-controls'})['ids'][0]
                def pump(predicate):
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        for agent in agents:
                            agent.tick()
                        if predicate():
                            return
                        time.sleep(.03)
                    self.fail(str(hub.job(job)))
                def change(mode):
                    old = hub._compute_policy('a') or compute_policy.validate({})
                    hub.set_compute_policy({'node_id': 'a', 'policy': {**old, 'mode': mode}})
                pump(lambda: hub.job(job)['metrics'].get('step', 0) >= 3)
                change('drain')
                step = hub.job(job)['metrics']['step']
                pump(lambda: hub.job(job)['metrics'].get('step', 0) >= step + 3)
                self.assertEqual(hub.job(job)['state'], 'running')
                self.assertEqual(agents[0].last_compute_control['phase'], 'admission_paused')
                change('stop')
                pump(lambda: hub.job(job)['state'] == 'paused')
                self.assertEqual(agents[0].last_compute_control['phase'], 'stopped')
                self.assertFalse(agents[0].last_compute_control['resources_released'])
                self.assertEqual(hub.job(job)['node_id'], 'a')
                self.assertEqual(hub.job(job)['generation'], 1)
                self.assertIsNone(hub.job(job)['handoff'])
                self.assertFalse(agents[1].records())
                change('available')
                for agent in agents:
                    agent.tick()
                self.assertEqual(hub.job(job)['state'], 'paused')
                hub.action({'job_id': job, 'action': 'resume'})
                pump(lambda: hub.job(job)['attempt'] == 2 and hub.job(job)['state'] == 'running')
                self.assertEqual(hub.job(job)['node_id'], 'a')
                self.assertEqual(hub.job(job)['generation'], 1)
            finally:
                for agent in agents:
                    for record in agent.records():
                        if record['state'] in ('starting', 'running'):
                            common.atomic_json(agent._output(record) / 'STOP', {})
                    for process in agent.processes.values():
                        process.wait(timeout=5)
                    agent.close()
                server.shutdown()
                server.server_close()
                hub.close()

    def test_real_workers_resume_on_another_node_with_lightweight_results(self):
        with temporary_directory() as directory:
            root = Path(directory)
            hub = Hub(root / 'hub')
            server = make_server(hub, port=0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = 'http://127.0.0.1:' + str(server.server_address[1])
            agents = []
            try:
                for name in ('a', 'b'):
                    config = node_config(name, url, hub.add_node(name), root / name, True)
                    config['sync_timeout'] = 1
                    path = root / (name + '.json')
                    common.atomic_json(path, config)
                    agents.append(Agent(path))
                    agents[-1].tick()
                job = hub.submit({'spec': {'backend': 'demo', 'params': {'steps': 60, 'delay': .03},
                    'resources': {'gpu_memory_mb': 0}}, 'request_id': 'handoff-real'})['ids'][0]
                def pump(predicate):
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        for agent in agents:
                            agent.tick()
                        if predicate():
                            return
                        time.sleep(.025)
                    self.fail(str(hub.job(job)) + ' / ' + repr([a.last_error for a in agents]))
                pump(lambda: hub.job(job)['metrics'].get('step', 0) >= 3)
                hub.set_compute_policy({'node_id': 'a', 'policy': {'mode': 'release', 'reason': '游戏', 'until': common.now()+3600}})
                pump(lambda: hub.job(job)['state'] == 'succeeded' and hub.job(job)['generation'] == 2)
                final = hub.job(job)
                self.assertEqual(final['node_id'], 'b')
                self.assertEqual(final['metrics']['step'], 60)
                record = next(item for item in agents[1].records() if item['id'] == job)
                self.assertTrue(record['resume_from_checkpoint'])
                self.assertTrue(record['checkpoint_restored'])
                pump(lambda: hub.db.execute('SELECT COUNT(*) FROM experiment_results WHERE job_id=? AND attempt=2', (job,)).fetchone()[0] == 1)
                self.assertTrue(final['execution_checkpoints'])
                self.assertNotIn('execution_state', result_protocol.envelope(record, {}, 'default', 'b', common.now()))
                self.assertEqual(hub.db.execute('SELECT COUNT(*) FROM execution_owners WHERE job_id=?', (job,)).fetchone()[0], 2)
            finally:
                for agent in agents:
                    for record in agent.records():
                        if record['state'] in ('starting', 'running'):
                            common.atomic_json(agent._output(record)/'STOP', {})
                    for process in agent.processes.values():
                        process.wait(timeout=5)
                    agent.close()
                server.shutdown()
                server.server_close()
                hub.close()


if __name__ == '__main__':
    unittest.main()
