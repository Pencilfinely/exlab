import io
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from expman import worker_migration as migration
from tests.support import temporary_directory


class MigrationSpaceTests(unittest.TestCase):
    def test_shared_cuda_layers_are_counted_once_with_one_load_peak(self):
        gib = 1024 ** 3
        # Twenty variants share a large base; Size alone would budget 320 GiB.
        images = [{'Id': str(i), 'Size': 8 * gib} for i in range(20)]
        response = io.BytesIO(json.dumps({'LayersSize': 12 * gib}).encode())
        response.status = 200
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(migration.http.client, 'HTTPConnection', return_value=connection), \
                patch.object(migration.socket, 'AF_UNIX', 1, create=True), patch.object(migration.socket, 'socket'), \
                patch.object(migration, 'docker', return_value=f'0\n{7*gib}\n{gib}'):
            space = migration.image_space('unix:///source.sock', images)
        self.assertEqual(space['image_bytes'], 12 * gib)
        self.assertEqual(space['transfer_peak_bytes'], 8 * gib)
        self.assertEqual(space['image_size_sum_bytes'], 160 * gib)
        connection.close.assert_called_once()

    def test_missing_storage_statistics_do_not_report_zero_required_space(self):
        connection = MagicMock()
        response = io.BytesIO(b'{}')
        response.status = 200
        connection.getresponse.return_value = response
        with patch.object(migration.http.client, 'HTTPConnection', return_value=connection), \
                patch.object(migration.socket, 'AF_UNIX', 1, create=True), patch.object(migration.socket, 'socket'), self.assertRaises(ValueError):
            migration.image_space('unix:///source.sock', [])

    def test_recorded_old_environments_survive_without_copying_unreferenced_cache(self):
        ref = lambda name: f'localhost:5000/{name}@sha256:' + 'a' * 64
        with temporary_directory() as folder:
            root = Path(folder)
            with closing(sqlite3.connect(root / 'node.sqlite3')) as db, db:
                db.execute('CREATE TABLE tasks (record TEXT)')
                for state in ('paused', 'failed', 'succeeded'):
                    db.execute('INSERT INTO tasks VALUES (?)', (json.dumps({'state': state,
                        'spec': {'environments': [{'image': ref(state)}]}}),))
            refs = migration.required_images({'profiles': {'gpu': {'image': ref('current')}}}, root,
                                             {ref(x) for x in ('paused', 'failed', 'succeeded', 'unreferenced-cache')})
            self.assertEqual(refs, {ref(x) for x in ('current', 'paused', 'failed', 'succeeded')})
            self.assertNotIn(ref('unreferenced-cache'), refs)
