import json
import subprocess
import unittest
from unittest.mock import Mock

from expman import storage_environments as env


class EnvironmentCacheTests(unittest.TestCase):
    def setUp(self):
        self.ref = 'registry.example/old@sha256:' + 'a' * 64
        self.record = {'id': 'b' * 32, 'state': 'succeeded', 'archive_scanned': True,
                       'spec': {'environments': [{'image': self.ref, 'profile': 'old'}]}}
        self.image = {'Id': 'sha256:' + 'c' * 64, 'RepoTags': [], 'RepoDigests': [self.ref], 'Size': 1234}
        self.calls = []
        def execute(argv, **options):
            self.calls.append(argv)
            output = json.dumps([self.image]) if argv[1:3] == ['image', 'inspect'] else ''
            if argv[1:3] == ['manifest', 'inspect']:
                output = json.dumps({'Descriptor': {'digest': self.ref.split('@')[1]}})
            return subprocess.CompletedProcess(argv, 0, output, '')
        self.execute = Mock(side_effect=execute)

    def test_current_profiles_templates_and_resumable_tasks_keep_the_image(self):
        self.assertEqual(env.obsolete({'profiles': {'old': {'image': self.ref}}}, [self.record]), {})
        self.assertEqual(env.obsolete({'task_templates': [self.record['spec']]}, [self.record]), {})
        for state in ('running', 'paused', 'failed', 'interrupted', 'ready'):
            self.assertEqual(env.obsolete({}, [self.record, dict(self.record, id='x', state=state)]), {})

    def test_shared_aliases_and_any_container_reference_block_removal(self):
        self.image['RepoTags'] = ['other-app:latest']
        self.assertEqual(env.plan({}, [self.record], self.execute), [])
        self.image['RepoTags'] = []
        self.image['RepoDigests'].append('other-app@sha256:' + 'd' * 64)
        self.assertEqual(env.plan({}, [self.record], self.execute), [])
        self.image['RepoDigests'] = [self.ref]
        self.execute.side_effect = [subprocess.CompletedProcess([], 0, json.dumps([self.image])), subprocess.CompletedProcess([], 0, 'foreign-container')]
        self.assertEqual(env.plan({}, [self.record], self.execute), [])

    def test_cleanup_rechecks_remote_digest_then_removes_only_exact_reference(self):
        item = env.plan({}, [self.record], self.execute)[0]
        self.assertEqual(item['bytes'], 0)  # shared layer sizes are not actual reclaimable bytes
        env.reclaim(item, {}, [self.record], self.execute)
        self.assertEqual(self.calls[-1], ['docker', 'image', 'rm', self.ref])
        self.assertFalse(any('prune' in args or '--force' in args for args in self.calls))

    def test_new_reference_or_unrecoverable_remote_keeps_cache(self):
        item = env.plan({}, [self.record], self.execute)[0]
        self.calls.clear()
        with self.assertRaises(ValueError):
            env.reclaim(item, {'profiles': {'active': {'image': self.ref}}}, [self.record], self.execute)
        self.assertEqual(self.calls, [])
        self.execute.side_effect = [subprocess.CompletedProcess([], 0, json.dumps([self.image])),
                                   subprocess.CompletedProcess([], 0, ''), subprocess.CompletedProcess([], 0, '{}')]
        with self.assertRaises(ValueError):
            env.reclaim(item, {}, [self.record], self.execute)
        self.assertFalse(any(c.args[0][1:3] == ['image', 'rm'] for c in self.execute.call_args_list))
