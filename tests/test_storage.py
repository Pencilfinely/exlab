import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

from expman import common, storage, scan_index
from expman.agent import Agent
from tests.support import temporary_directory


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.base = Path(self.tmp.__enter__())
        self.addCleanup(self.tmp.__exit__, None, None, None)
        self.config = self.base / 'node.json'
        common.atomic_json(self.config, {'node_id': 'owner', 'token': 'test', 'hub_url': 'http://localhost:1', 'root': str(self.base / 'node')})
        self.agent = Agent(self.config)
        self.addCleanup(self.agent.close)
        self.root = self.agent.root
        self.execute = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        self.identity = 'a' * 32
        self.record = {'id': self.identity, 'state': 'succeeded', 'archive_scanned': True, 'attempt': 1,
                       'spec': {'backend': 'demo'}, 'seq': 0}
        self.agent._save(self.record)
        self.output = self.root / 'runs' / self.identity
        self.output.mkdir()

    def weight(self):
        source = self.output / 'best.pt'
        source.write_bytes(b'important-model-weight' * 1000)
        target = self.root / 'retained_evidence' / self.identity / 'best'
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)
        sha = common.sha256_file(source)
        with self.agent.db:
            self.agent.db.execute("INSERT INTO node_evidence(job_id,attempt,experiment_id,evidence_id,name,kind,size,sha256,source,path,retention,availability) VALUES(?,1,'exp','weight','best.pt','weight',?,?,?,?,'retained','available')",
                                  (self.identity, source.stat().st_size, sha, str(source), str(target)))
        return source, target, sha

    def plan(self):
        return storage.inspect(self.root, self.agent.config, self.agent.db, self.execute)

    def clean(self, plan):
        return storage.reclaim(self.root, self.agent.config, self.agent.db, self.execute, plan)

    def test_reclaims_only_verified_duplicate_and_preserves_lookup_and_archive(self):
        source, target, sha = self.weight()
        plan = self.plan()
        self.assertEqual([i['kind'] for i in plan['items']], ['duplicate_weight'])
        result = self.clean(plan)
        self.assertEqual(result['completed_items'], 1)
        self.assertFalse(source.exists())
        self.assertEqual(common.sha256_file(target), sha)
        self.assertEqual(common.read_json(self.output / 'archived-files.json')['best.pt']['path'], str(target))
        self.assertEqual(self.agent.records()[0]['state'], 'succeeded')
        self.assertEqual(self.plan()['items'], [])

    def test_archive_corruption_preserves_original(self):
        source, target, _ = self.weight()
        plan = self.plan()
        target.write_bytes(b'corrupt')
        result = self.clean(plan)
        self.assertEqual(result['completed_items'], 0)
        self.assertEqual(len(result['skipped']), 1)
        self.assertTrue(source.exists())

    def test_state_change_and_resume_states_protect_files(self):
        source, target, _ = self.weight()
        plan = self.plan()
        for state in ('running', 'paused', 'failed', 'interrupted', 'assigned', 'ready'):
            self.agent._save(self.record, state=state)
            self.assertEqual(self.plan()['items'], [])
            self.assertEqual(self.clean(plan)['completed_items'], 0)
            self.assertTrue(source.exists())

    def test_unretained_weight_and_external_destination_are_never_deleted(self):
        source, target, _ = self.weight()
        with self.agent.db:
            self.agent.db.execute("UPDATE node_evidence SET retention='pending'")
        self.assertEqual(self.plan()['items'], [])
        with self.agent.db:
            self.agent.db.execute("UPDATE node_evidence SET retention='retained',path=?", (str(self.base / 'outside'),))
        self.assertEqual(self.plan()['items'], [])
        self.assertTrue(source.exists())

    def test_changed_source_and_cancellation_preserve_weight(self):
        source, target, _ = self.weight()
        plan = self.plan()
        result = storage.reclaim(self.root, self.agent.config, self.agent.db, self.execute, plan, cancelled=lambda: True)
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(source.exists())
        source.write_bytes(b'new-training-output')
        self.assertEqual(self.clean(plan)['completed_items'], 0)
        self.assertEqual(source.read_bytes(), b'new-training-output')

    def test_running_foreign_and_writable_containers_are_preserved(self):
        container = {'Id': 'exact-container', 'Config': {'Labels': {'expman.node': 'owner', 'expman.job': self.identity}},
                     'State': {'Status': 'exited'}, 'HostConfig': {'ReadonlyRootfs': True}}
        def execute(argv, **options):
            return subprocess.CompletedProcess(argv, 0, 'exact-container' if argv[1] == 'ps' else json.dumps([container]), '')
        self.execute.side_effect = execute
        plan = self.plan()
        self.assertEqual(len(plan['items']), 1)
        for change in ('running', 'foreign', 'writable'):
            candidate = json.loads(json.dumps(container))
            if change == 'running':
                candidate['State']['Status'] = 'running'
            elif change == 'foreign':
                candidate['Config']['Labels']['expman.node'] = 'other'
            else:
                candidate['HostConfig']['ReadonlyRootfs'] = False
            with patch.object(self, 'execute', return_value=subprocess.CompletedProcess([], 0, json.dumps([candidate]), '')) as runner:
                self.assertEqual(self.clean(plan)['completed_items'], 0)
                self.assertFalse(any(c.args[0][1] == 'rm' for c in runner.call_args_list))

    def test_hardlinked_archives_do_not_claim_duplicate_space(self):
        source, target, _ = self.weight()
        target.unlink()
        try:
            __import__('os').link(source, target)
        except OSError:
            self.skipTest('Hard links unavailable')
        self.assertEqual(self.plan()['items'], [])

    @unittest.skipUnless(shutil.which('git'), 'Git required')
    def test_source_is_archived_and_verified_before_worktree_removal(self):
        repo = self.root / 'repos/original'
        repo.mkdir()
        def writable():
            for path in repo.rglob('*'):
                if path.is_file() and not path.is_symlink():
                    path.chmod(0o600)
        self.addCleanup(writable)
        def git(*args):
            result = subprocess.run(['git', '-c', 'safe.directory=*', *map(str, args)], capture_output=True, text=True, check=True)
            return result.stdout.strip()
        git('init', repo)
        (repo / 'model.py').write_text('model = 42\n')
        git('-C', repo, 'add', 'model.py')
        git('-C', repo, '-c', 'user.name=Test', '-c', 'user.email=test@local', '-c', 'commit.gpgsign=false', 'commit', '-m', 'fixture')
        commit = git('-C', repo, 'rev-parse', 'HEAD')
        checkout = self.root / 'worktrees' / self.identity
        git('-C', repo, 'worktree', 'add', '--detach', checkout, commit)
        self.agent._save(self.record, spec={'backend': 'docker', 'source': {'repo': str(repo), 'commit': commit}, 'params': {'seed': 42}, 'environments': []})
        def execute(argv, **options):
            if argv[0] == 'docker':
                return subprocess.CompletedProcess(argv, 0, '', '')
            return subprocess.run(['git', '-c', 'safe.directory=*', *argv[1:]], capture_output=True, text=True, check=True)
        self.execute.side_effect = execute
        plan = self.plan()
        (checkout / 'untracked.txt').write_text('keep me')
        self.assertEqual(self.clean(plan)['completed_items'], 0)
        self.assertTrue(checkout.exists())
        (checkout / 'untracked.txt').unlink()
        result = self.clean(plan)
        self.assertEqual(result['completed_items'], 1, result)
        receipt = common.read_json(self.output / 'code-archive.json')
        self.assertEqual(receipt['source']['commit'], commit)
        self.assertEqual(receipt['params'], {'seed': 42})
        self.assertEqual(common.sha256_file(receipt['path']), receipt['sha256'])
        self.assertFalse(checkout.exists())
        self.assertTrue((repo / 'model.py').exists())

    def test_index_bounds_history_scans_and_wakes_changed_record(self):
        for i in range(400):
            record = dict(self.record, id=f'{i:032x}', seq=1)
            with self.agent.db:
                self.agent.db.execute('INSERT INTO tasks VALUES (?,?)', (record['id'], json.dumps(record)))
        due = scan_index.due(self.agent.db, 'results')
        self.assertEqual(len(due), 128)
        for record in due:
            scan_index.completed(self.agent.db, 'results', record, 60)
        self.assertTrue(set(r['id'] for r in due).isdisjoint(r['id'] for r in scan_index.due(self.agent.db, 'results')))
        changed = due[0]
        self.agent._save(changed, state='running')
        self.assertIn(changed['id'], [r['id'] for r in scan_index.due(self.agent.db, 'results', limit=1000)])

    def test_scan_completion_cannot_hide_concurrent_new_generation(self):
        record = scan_index.due(self.agent.db, 'results')[0]
        self.agent._save(self.record, attempt=2, state='running')
        scan_index.completed(self.agent.db, 'results', record, 60)
        self.assertEqual(scan_index.due(self.agent.db, 'results')[0]['attempt'], 2)

    def test_running_results_take_priority_over_unscanned_history(self):
        with self.agent.db:
            self.agent.db.executemany('INSERT INTO tasks VALUES (?,?)',
                [(f'{i:032x}', json.dumps(dict(self.record, id=f'{i:032x}', seq=1))) for i in range(1000)])
        running = dict(self.record, id='f' * 32, state='running')
        self.agent._save(running)
        due = scan_index.due(self.agent.db, 'results')
        self.assertEqual(due[0]['id'], running['id'])
        self.assertLessEqual(len(due), 128)

    def test_cleanup_admission_defers_new_generation_without_losing_it(self):
        self.agent.maintenance.cleaning = True
        spec = common.validate_task({'backend': 'demo'})
        response = {'jobs': [{'id': self.identity, 'spec': spec, 'generation': 2, 'attempt': 2, 'command_id': 0}]}
        with patch.object(self.agent, '_sync_request', return_value=response):
            self.agent._sync({})
        self.assertEqual(self.agent.records()[0]['attempt'], 1)
        self.agent.maintenance.cleaning = False
        with patch.object(self.agent, '_sync_request', return_value=response):
            self.agent._sync({})
        self.assertEqual(self.agent.records()[0]['attempt'], 2)
