"""Export the real Worker status protocol for the native desktop regression test.

No agent or Docker process starts: only lifecycle files and process identity are
stubbed. The status values come from worker_service.status, not the C# consumer.
"""
import json
from pathlib import Path
import sys
from unittest.mock import patch

import subprocess
from expman import common, runtime, worker_service as service


def export(path):
    path = Path(path)
    root = path.parent / 'status-protocol-fixture'
    common.atomic_json(root / 'service.json', {'node_id': 'original-node', 'backend': 'detached'})
    states = []
    with patch.object(service, '_owned_process', return_value=True):
        for phase in ('preparing', 'offline', 'online'):
            service._write_status(root, status=phase, pid=4217, process_identity='fixture',
                                  online=phase == 'online', detail='Connected to controller' if phase == 'online' else phase)
            states.append(service.status(root))
    path.write_text(json.dumps(states, ensure_ascii=True), encoding='utf-8')
    docker_states = []
    with patch.object(runtime.shutil, 'which', return_value='/usr/bin/docker'), \
            patch.object(runtime, 'read_json', return_value={'docker_id': 'original-engine'}), \
            patch.dict(runtime.os.environ, {}, clear=True):
        replies = [
            [subprocess.CompletedProcess([], 1, 'WSL integration not ready', '')],
            [subprocess.CompletedProcess([], 0, 'unix:///var/run/docker.sock', ''),
             subprocess.CompletedProcess([], 1, '', 'daemon not ready')],
            [subprocess.CompletedProcess([], 0, 'unix:///var/run/docker.sock', ''),
             subprocess.CompletedProcess([], 0, json.dumps({'ID': 'original-engine', 'OSType': 'linux'}), '')]]
        for sequence in replies:
            with patch.object(runtime, 'docker_command', side_effect=sequence):
                docker_states.append(runtime.docker_status('/worker', prefer_saved=True))
    path.with_name('worker-docker-fixture.json').write_text(json.dumps(docker_states, ensure_ascii=True), encoding='utf-8')


if __name__ == '__main__':
    export(sys.argv[1])
