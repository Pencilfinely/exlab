"""Reproducible history-scan benchmark; creates and removes only test fixtures."""
import json
from pathlib import Path
import statistics
import time
import tracemalloc

from expman import common, scan_index, result_delivery
from expman.agent import Agent
from tests.support import temporary_directory


def sample(call):
    durations = []
    for _ in range(7):
        start = time.perf_counter();value = call();durations.append((time.perf_counter() - start) * 1000)
    tracemalloc.start();value = call();_, peak = tracemalloc.get_traced_memory();tracemalloc.stop()
    return {'median_ms': round(statistics.median(durations), 3), 'peak_python_bytes': peak, 'records': len(value)}


def main():
    with temporary_directory() as temporary:
        base = Path(temporary)
        common.atomic_json(base / 'node.json', {'node_id': 'benchmark', 'token': 'fixture', 'hub_url': 'http://localhost:1', 'root': str(base / 'node')})
        agent = Agent(base / 'node.json')
        try:
            count = 20000
            spec = common.validate_task({'backend': 'demo', 'params': {'seed': 42, 'batch_size': 128}})
            rows = [(f'{i:032x}', json.dumps({'id': f'{i:032x}', 'state': 'succeeded', 'archive_scanned': True,
                    'seq': 1, 'attempt': 1, 'spec': spec, 'metrics': {'loss': .2}})) for i in range(count)]
            with agent.db:
                agent.db.executemany('INSERT INTO tasks VALUES (?,?)', rows)
                agent.db.execute('UPDATE task_scan_queue SET results_due=?,evidence_due=?', (common.now() + 3600,) * 2)
                agent.db.execute('INSERT INTO tasks VALUES (?,?)', ('f' * 32, json.dumps({'id': 'f' * 32, 'state': 'running', 'seq': 1, 'spec': spec})))
            output = {'history_jobs': count, 'active_jobs': 1,
                'previous_full_discovery': sample(lambda: agent.result_delivery._records(agent.db)),
                'indexed_discovery': sample(lambda: scan_index.due(agent.db, 'results'))}
            print(json.dumps(output, indent=2))
        finally:
            agent.close()


if __name__ == '__main__':
    main()
