import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from expman import common, worker_database, worker_service as service
from expman.agent import Agent
from tests.support import temporary_directory


class WorkerDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = (tempfile.TemporaryDirectory(prefix='expman-worker-db-test-')
                          if sys.platform == 'linux' else temporary_directory())
        self.root = Path(self.temporary.__enter__())
        self.config = self.root / 'node.json'
        common.atomic_json(self.config, dict(node_id='test', token='private-test-secret',
            hub_url='http://127.0.0.1:1', root=str(self.root / 'worker'), allow_demo=True))
        self.agent = Agent(self.config)

    def tearDown(self):
        self.agent.close()
        self.temporary.__exit__(None, None, None)

    def test_execution_metadata_waits_for_delivery_transaction_without_sqlite_contention(self):
        delivery = self.agent.result_delivery
        self.agent.db.execute('PRAGMA busy_timeout=0')
        delivery.result_db.execute('PRAGMA busy_timeout=0')
        entered, release = threading.Event(), threading.Event()
        errors = []

        def writer():
            try:
                with delivery.result_db:
                    delivery.result_db.execute("INSERT INTO node_delivery_meta VALUES ('held','true')")
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError('Test writer timed out')
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=writer)
        thread.start()
        timer = threading.Timer(0.12, release.set)
        try:
            self.assertTrue(entered.wait(2))
            timer.start()
            self.agent._set_meta('compute_control', {'phase': 'available'})
        finally:
            release.set()
            thread.join(3)
            timer.join()
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.agent._meta('compute_control'), {'phase': 'available'})
        self.assertEqual(self.agent.db.execute("SELECT value FROM node_delivery_meta WHERE key='held'").fetchone()[0], 'true')

    def test_failed_transaction_rolls_back_and_releases_writer(self):
        db = self.agent.result_delivery.control_db
        with self.assertRaisesRegex(ValueError, 'abort'):
            with db:
                db.execute("INSERT INTO node_delivery_meta VALUES ('partial','true')")
                raise ValueError('abort')
        self.assertFalse(db.in_transaction)
        self.agent._set_meta('after_rollback', True)
        self.assertIsNone(db.execute("SELECT value FROM node_delivery_meta WHERE key='partial'").fetchone())

    def test_real_startup_busy_retries_after_releasing_partial_agent_owner(self):
        self.agent._save(dict(id='existing', state='running', attempt=1, generation=1,
            container_name='expman-existing-1', spec={'backend': 'docker'}))
        expected = self.agent.records()
        # Simulate an older database which needs a table during initialization.
        with self.agent.db:
            self.agent.db.execute('DROP TABLE execution_checkpoint_outbox')
        self.agent.close()
        blocker = sqlite3.connect(self.root / 'worker/node.sqlite3')
        blocker.execute("INSERT INTO metadata VALUES ('external-writer','true')")
        connect = worker_database.connect
        logged, observed, created = [], [], []

        def no_wait(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.execute('PRAGMA busy_timeout=0')
            return db

        def log(root, error):
            logged.append(error)
            blocker.rollback()

        def construct(config):
            created.append(config)
            agent = Agent(config)
            def tick(**kwargs):
                observed.append(agent.records())
                return dict(online=True, jobs=agent.records())
            agent.tick = tick
            return agent

        try:
            with patch.object(worker_database, 'connect', side_effect=no_wait), \
                    patch('expman.agent.Agent', side_effect=construct), \
                    patch.object(service, '_log_exception', side_effect=log), \
                    patch.object(service, '_update_stop_at_boundary', side_effect=lambda *args: bool(observed)), \
                    patch.object(service.time, 'sleep'), \
                    patch.object(Agent, '_exec', side_effect=AssertionError('Must not change Docker')), \
                    patch.object(Agent, '_start', side_effect=AssertionError('Must not create an attempt')):
                service._run_agent(self.root / 'client', str(self.config))
        finally:
            blocker.close()
        self.assertEqual(len(created), 2)
        self.assertEqual(len(logged), 1)
        self.assertTrue(worker_database.transient(logged[0]))
        self.assertEqual(observed, [expected])
        state = common.read_json(self.root / 'client/status.json')
        self.assertTrue(state['online'])
        self.assertEqual(state['database_recoveries'], 1)

    def test_busy_during_compute_control_save_reopens_original_running_records(self):
        self.agent._save(dict(id='existing', state='running', attempt=1, generation=2,
            container_name='expman-existing-1', spec={'backend': 'docker'}))
        expected = self.agent.records()
        self.agent.close()
        blocker = sqlite3.connect(self.root / 'worker/node.sqlite3')
        created, observed = [], []
        def construct(config):
            agent = Agent(config)
            agent.db.execute('PRAGMA busy_timeout=0')
            created.append(agent)
            def tick(**kwargs):
                if len(created) == 1:
                    blocker.execute("INSERT INTO metadata VALUES ('external-writer','true')")
                    agent._set_meta('compute_control', {'phase': 'available'})
                observed.append(agent.records())
                return dict(online=True, jobs=agent.records())
            agent.tick = tick
            return agent
        try:
            with patch('expman.agent.Agent', side_effect=construct), \
                    patch.object(service, '_log_exception', side_effect=lambda *args: blocker.rollback()), \
                    patch.object(service, '_update_stop_at_boundary', side_effect=lambda *args: bool(observed)), \
                    patch.object(service.time, 'sleep'), \
                    patch.object(Agent, '_exec', side_effect=AssertionError('Must not change Docker')), \
                    patch.object(Agent, '_start', side_effect=AssertionError('Must not create an attempt')):
                service._run_agent(self.root / 'client', str(self.config))
        finally:
            blocker.close()
        self.assertEqual(len(created), 2)
        self.assertTrue(all(agent.closed for agent in created))
        self.assertEqual(observed, [expected])

    def test_busy_while_recording_delivery_error_keeps_lane_alive(self):
        delivery = self.agent.result_delivery
        delivery.result_db.execute('PRAGMA busy_timeout=0')
        blocker = sqlite3.connect(self.root / 'worker/node.sqlite3')
        blocker.execute("INSERT INTO metadata VALUES ('external-writer','true')")
        cycles = []
        def scan(*, indexed=False):
            cycles.append(1)
            if len(cycles) == 1:
                raise sqlite3.OperationalError('database is locked')
            delivery.stop.set()
        try:
            with patch.object(delivery, 'results_once', side_effect=scan), \
                    patch.object(delivery.stop, 'wait', side_effect=lambda seconds: blocker.rollback()), \
                    redirect_stdout(io.StringIO()) as output:
                delivery._result_loop()
        finally:
            blocker.close()
        self.assertEqual(len(cycles), 2)
        self.assertIn('status deferred', output.getvalue())

    def test_busy_while_recording_checkpoint_error_keeps_lane_alive(self):
        self.agent._set_meta('execution_protocol', 1)
        transfer = self.agent.execution_transfer
        blocker = sqlite3.connect(self.root / 'worker/node.sqlite3')
        blocker.execute("INSERT INTO metadata VALUES ('external-writer','true')")
        connect = worker_database.connect
        waits = []
        def no_wait(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.execute('PRAGMA busy_timeout=0')
            return db
        def wait(seconds):
            waits.append(seconds)
            blocker.rollback()
            if len(waits) == 2:
                transfer.stop.set()
        try:
            with patch.object(worker_database, 'connect', side_effect=no_wait), \
                    patch.object(transfer.stop, 'wait', side_effect=wait), \
                    redirect_stdout(io.StringIO()) as output:
                transfer._run()
        finally:
            blocker.close()
        self.assertEqual(len(waits), 2)
        self.assertIn('status deferred', output.getvalue())
        row = self.agent.db.execute("SELECT value FROM execution_lane_meta WHERE key='last_error'").fetchone()
        self.assertIsNone(json.loads(row[0]))

    def test_corruption_and_shutdown_errors_are_not_recovered(self):
        for code, shutdown in ((11, False), (5, True), (13, True)):
            with self.subTest(code=code, shutdown=shutdown):
                error = sqlite3.OperationalError('failure')
                error.sqlite_errorcode = code
                with patch('expman.agent.Agent', return_value=self.agent), \
                        patch.object(service, '_agent_loop', side_effect=error), \
                        patch.object(service, '_shutdown_requested', return_value=shutdown), \
                        patch.object(service, '_log_exception') as log:
                    with self.assertRaises(sqlite3.OperationalError):
                        service._run_agent(self.root, str(self.config))
                log.assert_not_called()

    def test_real_sqlite_full_reopens_without_losing_or_relaunching_running_task(self):
        self.agent._save(dict(id='existing', state='running', attempt=1, generation=2,
            container_name='expman-existing-1', spec={'backend': 'docker'}))
        expected = self.agent.records()
        self.agent.close()
        created, observed = [], []

        def construct(config):
            agent = Agent(config)
            created.append(agent)
            def tick(**kwargs):
                if len(created) == 1:
                    pages = agent.db.execute('PRAGMA page_count').fetchone()[0]
                    agent.db.execute('PRAGMA max_page_count=' + str(pages))
                    agent._save(agent.records()[0], payload='x' * (1024 * 1024))
                    self.fail('The real SQLite write should exhaust the page limit')
                observed.append(agent.records())
                return dict(online=True, jobs=agent.records())
            agent.tick = tick
            return agent

        with patch('expman.agent.Agent', side_effect=construct), \
                patch.object(service, '_update_stop_at_boundary', side_effect=lambda *args: bool(observed)), \
                patch.object(service.time, 'sleep'), redirect_stdout(io.StringIO()) as output, \
                patch.object(Agent, '_exec', side_effect=AssertionError('Must not change Docker')), \
                patch.object(Agent, '_start', side_effect=AssertionError('Must not create an attempt')):
            service._run_agent(self.root / 'client', str(self.config))
        self.assertEqual(len(created), 2)
        self.assertEqual(observed, [expected])
        self.assertTrue(all(agent.closed for agent in created))
        state = common.read_json(self.root / 'client/status.json')
        self.assertTrue(state['online'])
        self.assertEqual(state['database_recoveries'], 1)
        self.assertIn('database or disk is full', output.getvalue())
        diagnostic = state['last_database_diagnostics']
        self.assertEqual(diagnostic['pragmas']['page_count'], diagnostic['pragmas']['max_page_count'])
        self.assertGreater(diagnostic['filesystems'][0]['available_bytes'], 1024 * 1024)
        self.assertNotIn('private-test-secret', output.getvalue() + json.dumps(state))

    def test_background_full_is_latched_before_main_loop_can_schedule(self):
        db = self.agent.result_delivery.control_db
        pages = db.execute('PRAGMA page_count').fetchone()[0]
        db.execute('PRAGMA max_page_count=' + str(pages))
        with self.assertRaises(sqlite3.OperationalError) as raised:
            with db:
                db.execute("INSERT INTO node_delivery_meta VALUES ('capacity-test', ?)", ('x' * 1048576,))
        self.assertTrue(worker_database.storage_full(raised.exception))
        self.assertFalse(db.in_transaction)
        with patch.object(self.agent, '_start', side_effect=AssertionError('No scheduling while storage failed')):
            with self.assertRaises(sqlite3.OperationalError) as main_error:
                self.agent.tick()
        self.assertIs(main_error.exception, raised.exception)
        self.assertEqual(raised.exception.storage_diagnostics['phase'], 'execute')

    def test_storage_failure_status_and_log_writes_cannot_kill_recovery(self):
        import errno
        from unittest.mock import Mock
        self.agent.close()
        full = sqlite3.OperationalError('database or disk is full')
        full.sqlite_errorcode, full.sqlite_errorname = 13, 'SQLITE_FULL'
        agent = Mock()
        agent.config = common.read_json(self.config)
        attempts, snapshots = [], []
        write_status = service._write_status
        def loop(root, current):
            attempts.append(1)
            if len(attempts) == 1:
                raise full
            snapshots.append(common.read_json(root / 'status.json'))
        def status(root, **kwargs):
            if len(attempts) == 1 and not getattr(status, 'failed', False):
                status.failed = True
                raise OSError(errno.ENOSPC, 'status filesystem full')
            return write_status(root, **kwargs)
        with patch('expman.agent.Agent', return_value=agent), \
                patch.object(service, '_agent_loop', side_effect=loop), \
                patch.object(service, '_write_status', side_effect=status), \
                patch.object(service, '_log_exception', side_effect=OSError(errno.ENOSPC, 'log filesystem full')), \
                patch.object(service.time, 'sleep'):
            service._run_agent(self.root / 'client', str(self.config))
        self.assertEqual(len(attempts), 2)
        self.assertTrue(snapshots[0]['storage_recovery_pending'])
        self.assertIn('last_database_diagnostics', snapshots[0])
        self.assertEqual(agent.close.call_count, 2)

    def test_storage_full_classification_does_not_retry_corruption_or_other_io(self):
        import errno
        self.assertTrue(worker_database.storage_full(OSError(errno.ENOSPC, 'full')))
        self.assertTrue(worker_database.storage_full(OSError(errno.EDQUOT, 'quota')))
        self.assertFalse(worker_database.storage_full(OSError(errno.EIO, 'I/O error')))
        self.assertTrue(worker_database.storage_full(sqlite3.OperationalError('database or disk is full')))
        error = sqlite3.OperationalError('database or disk is full')
        error.sqlite_errorcode = 11
        self.assertFalse(worker_database.storage_full(error))

    def test_extended_and_legacy_busy_codes_exclude_other_failures(self):
        for code in (5, 6, 261, 262, 517):
            error = sqlite3.OperationalError('extended lock error')
            error.sqlite_errorcode = code
            self.assertTrue(worker_database.transient(error))
        self.assertTrue(worker_database.transient(sqlite3.OperationalError('database table is locked: tasks')))
        self.assertFalse(worker_database.transient(sqlite3.OperationalError('disk I/O error')))
        error = sqlite3.OperationalError('database is locked')
        error.sqlite_errorcode = 11  # SQLITE_CORRUPT
        self.assertFalse(worker_database.transient(error))
        self.assertFalse(worker_database.transient(sqlite3.ProgrammingError('closed database')))
