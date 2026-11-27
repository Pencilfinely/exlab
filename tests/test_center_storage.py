import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from expman import center_storage, common
from expman.hub import APIError, Hub
from tests.support import temporary_directory


class CenterStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = temporary_directory()
        self.root = Path(temporary.__enter__())
        self.addCleanup(temporary.__exit__, None, None, None)
        self.hub = Hub(self.root)
        self.addCleanup(lambda: self.hub.close())
        self.hub.add_node('worker')
        self.snapshot = {'capabilities': ['project-bundle-v1', 'project-delete-v1']}
        self.old = common.now() - center_storage.RETENTION_SECONDS - 60

    def project(self, variant, state='succeeded', *, recent=False, pending=False, deployment='installed'):
        bundle = hashlib.sha256(variant.encode()).hexdigest()
        data = ('bundle ' + variant).encode()
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / 'projects' / (digest + '.zip')
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        self.hub.db.execute('INSERT INTO projects(digest,project_id,name,size,created,bundle_id) VALUES (?,?,?,?,?,?)',
                            (digest, variant, variant, len(data), self.old, bundle))
        self.hub.db.execute('INSERT INTO project_deployments VALUES (?,?,1,?,?,?)',
                            (digest, 'worker', deployment, '', self.old))
        spec = common.validate_task({'backend': 'demo', 'project_bundle_id': bundle})
        self.hub.db.execute('INSERT INTO jobs(id,spec,state,created,updated,command_id) VALUES (?,?,?,?,?,?)',
                            (digest[:32], json.dumps(spec), state, self.old, common.now() if recent else self.old, int(pending)))
        return digest, bundle, path

    def test_automatic_archive_preserves_bundle_and_jobs_across_restart_and_restore(self):
        digest, bundle, path = self.project('complete')
        data = path.read_bytes()
        snapshot = dict(self.snapshot, task_templates=[common.validate_task({'backend': 'demo', 'project_bundle_id': bundle})])
        self.hub.sync('worker', {'node_id': 'worker', 'snapshot': snapshot,
                                'project_reports': [{'digest': digest, 'revision': 1, 'status': 'installed', 'detail': 'Repeated status'}]})
        self.hub.storage_maintenance.cycle()
        state = self.hub.state()
        self.assertEqual(state['projects'], [])
        self.assertEqual(state['nodes'][0]['snapshot']['task_templates'], [])
        self.assertEqual(state['jobs'][0]['state'], 'succeeded')
        self.assertTrue(state['project_deletions'][0]['auto_archived'])
        self.assertIn(f'projects/{digest}.zip', self.hub.center_snapshot()['files'])
        self.hub.close()
        self.hub = Hub(self.root)
        self.assertEqual(path.read_bytes(), data)
        incoming, _, _ = self.project('incoming', recent=True, deployment='queued')
        declarations = self.hub.sync('worker', {'node_id': 'worker', 'snapshot': snapshot})['project_deployments']
        self.assertEqual([(d['digest'], d['action'], d['revision']) for d in declarations],
                         [(digest, 'delete', 2), (incoming, 'install', 1)])
        with self.assertRaises(APIError) as error:
            self.hub.project_restore({'digest': digest})
        self.assertEqual(error.exception.status, 409)
        self.hub.sync('worker', {'node_id': 'worker', 'snapshot': self.snapshot,
                                'project_reports': [{'digest': digest, 'revision': 2, 'status': 'deleted'}]})
        self.assertEqual(self.hub.project_restore({'digest': digest})['status'], 'restored')
        self.hub.storage_maintenance.cycle()
        self.assertEqual(self.hub.state()['projects'][0]['digest'], digest)
        self.assertEqual(path.read_bytes(), data)

    def test_recoverable_recent_pending_deploying_and_matrix_projects_are_retained(self):
        for status in ('failed', 'paused', 'interrupted', 'running', 'queued'):
            self.project(status, state=status)
        self.project('recent', recent=True)
        self.project('pending', pending=True)
        self.project('deploying', deployment='queued')
        deployed, _, _ = self.project('fresh-deploy')
        self.hub.db.execute('UPDATE projects SET last_deployed=? WHERE digest=?', (common.now(), deployed))
        _, matrix_bundle, _ = self.project('matrix')
        definition = json.dumps({'name': 'Saved matrix', 'spec': {'backend': 'demo', 'project_bundle_id': matrix_bundle}})
        self.hub.db.execute('INSERT INTO matrices VALUES (?,?,1,1,?,?,NULL)', ('a' * 32, definition, self.old, self.old))
        _, _, corrupt = self.project('corrupt')
        corrupt.write_bytes(b'broken archive')
        self.hub.storage_maintenance.cycle()
        self.assertEqual(len(self.hub.state()['projects']), 11)
        self.assertEqual(len(common.read_json(self.root / 'storage-status.json')['errors']), 1)

    def test_new_job_during_archive_validation_keeps_the_deployment(self):
        digest, _, path = self.project('in-use')
        def verify(path, **options):
            self.hub.db.execute("UPDATE jobs SET state='running' WHERE id=?", (digest[:32],))
            return digest
        with patch.object(common, 'sha256_file', side_effect=verify):
            self.hub.storage_maintenance.cycle()
        self.assertTrue(path.is_file())
        self.assertEqual(self.hub.state()['projects'][0]['digest'], digest)

    def test_cleanup_retries_after_repeated_failure_heartbeats(self):
        digest, _, _ = self.project('retry')
        self.hub.storage_maintenance.cycle()
        payload = {'node_id': 'worker', 'snapshot': self.snapshot,
                   'project_reports': [{'digest': digest, 'revision': 2, 'status': 'delete_failed', 'detail': 'Busy'}]}
        self.hub.sync('worker', payload)
        self.hub.db.execute('UPDATE project_deployments SET updated=? WHERE digest=?', (self.old, digest))
        self.hub.sync('worker', payload)
        self.hub.storage_maintenance.cycle()
        declarations = self.hub.sync('worker', payload)['project_deployments']
        self.assertEqual([(d['action'], d['revision']) for d in declarations], [('delete', 3)])

    def test_expired_partial_uploads_are_removed_without_interrupting_active_uploads(self):
        partials = self.root / 'project-partials'
        partials.mkdir()
        for name in ('a' * 32, 'b' * 32, 'c' * 32):
            path = partials / (name + '.part')
            path.write_bytes(b'partial data')
            self.hub.db.execute('INSERT INTO project_uploads VALUES (?,30,NULL,NULL)', (name,))
            if name[0] != 'c':
                os.utime(path, (self.old, self.old))
        with self.hub._project_upload_guard('b' * 32):
            self.hub.storage_maintenance.cycle()
        self.assertEqual({path.stem for path in partials.iterdir()}, {'b' * 32, 'c' * 32})
        self.assertEqual({row[0] for row in self.hub.db.execute('SELECT id FROM project_uploads')}, {'b' * 32, 'c' * 32})
