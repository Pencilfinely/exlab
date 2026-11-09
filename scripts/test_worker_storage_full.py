"""Linux integration test: real ENOSPC, confined to a private 2 MiB tmpfs.

Run ``python3 scripts/test_worker_storage_full.py`` (unprivileged user namespaces)
or ``sudo python3 scripts/test_worker_storage_full.py`` on disposable CI runners.
The test creates its own mount namespace before mounting or filling anything.
"""
import errno
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from unittest.mock import patch

if sys.platform != 'linux':
    raise SystemExit('This integration test requires Linux mount namespaces')
namespace = os.readlink('/proc/self/ns/mnt')
if len(sys.argv) == 1:
    command = ['unshare', '--mount', '--propagation', 'private']
    if os.geteuid() != 0:
        command += ['--user', '--map-root-user']
    raise SystemExit(subprocess.call(command + [sys.executable, str(Path(__file__).resolve()), namespace]))
if len(sys.argv) != 2 or sys.argv[1] == namespace:
    raise SystemExit('Refusing to mount outside a newly isolated mount namespace')

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from expman import common, worker_database, worker_service as service
from expman.agent import Agent

with tempfile.TemporaryDirectory(prefix='exlab-enospc-isolated-') as temporary:
    root = Path(temporary)
    volume = root / 'volume'
    volume.mkdir()
    subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=2m,mode=0700', 'tmpfs', str(volume)], check=True)
    try:
        fs = os.statvfs(volume)
        assert fs.f_blocks * fs.f_frsize <= 2 * 1024 * 1024
        config = root / 'node.json'
        common.atomic_json(config, dict(node_id='test', token='test-secret',
            hub_url='http://127.0.0.1:1', root=str(volume / 'runtime'), allow_demo=True))
        initial = Agent(config)
        initial._save(dict(id='existing', state='running', attempt=1, generation=1,
            container_name='expman-existing-1', spec={'backend': 'docker'}))
        expected = initial.records()
        initial.close()
        filler = volume / 'capacity-test-filler'
        created, observed, errors = [], [], []
        log = service._log_exception

        def construct(path):
            agent = Agent(path)
            created.append(agent)
            def tick(**kwargs):
                if len(created) == 1:
                    try:
                        with filler.open('wb', buffering=0) as f:
                            while True:
                                f.write(b'x' * 4096)
                    except OSError as error:
                        assert error.errno == errno.ENOSPC
                    agent._save(agent.records()[0], payload='x' * 1048576)
                    raise AssertionError('Expected real ENOSPC from SQLite')
                observed.append(agent.records())
                return dict(online=True, jobs=agent.records())
            agent.tick = tick
            return agent

        def record_error(client, error):
            errors.append(error)
            log(client, error)
            # Simulate capacity becoming available after failure evidence is captured.
            filler.unlink()

        with patch('expman.agent.Agent', side_effect=construct), \
                patch.object(service, '_log_exception', side_effect=record_error), \
                patch.object(service, '_update_stop_at_boundary', side_effect=lambda *args: bool(observed)), \
                patch.object(service.time, 'sleep'), redirect_stdout(io.StringIO()) as output, \
                patch.object(Agent, '_exec', side_effect=AssertionError('No Docker operations')), \
                patch.object(Agent, '_start', side_effect=AssertionError('No experiment restart')):
            service._run_agent(root / 'client', str(config))
        assert len(created) == 2 and len(errors) == 1
        assert worker_database.storage_full(errors[0])
        assert getattr(errors[0], 'sqlite_errorcode', 13) == 13
        assert observed == [expected]
        state = common.read_json(root / 'client/status.json')
        assert state['online'] is True
        diagnostic = state['last_database_diagnostics']
        assert diagnostic['filesystems'][0]['available_bytes'] == 0
        assert diagnostic['pragmas']['page_count'] < diagnostic['pragmas']['max_page_count']
        db = sqlite3.connect(volume / 'runtime/node.sqlite3')
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        db.close()
        print(json.dumps(dict(result='PASS', sqlite=sqlite3.sqlite_version,
            injected_os_error='ENOSPC on private 2 MiB tmpfs',
            sqlite_errorcode=getattr(errors[0], 'sqlite_errorcode', None),
            diagnostic_free_bytes=0, reopened=len(created), records_unchanged=True,
            original_attempt=observed[0][0]['attempt'], online=state['online']), indent=2))
    finally:
        subprocess.run(['umount', str(volume)], check=True)
