"""Node-scoped Windows control survives shutdown of the WSL training runtime."""
import json
import re

from .common import now
from .compute_policy import apply as apply_policy, control_intent

CAPABILITY = 'windows-runtime-control-v1'


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS node_host_control (node_id TEXT PRIMARY KEY REFERENCES nodes(id), '
               'host_id TEXT NOT NULL, value TEXT NOT NULL, updated REAL NOT NULL)')


def desired(hub, node):
    snapshot = json.loads(node['snapshot']) if isinstance(node['snapshot'], str) else dict(node['snapshot'])
    snapshot['node_mode'] = node['mode']
    apply_policy(snapshot, hub._compute_policy(node['id']) or {})
    availability = snapshot['availability']
    return {'mode': availability['mode'], 'source': availability.get('source'),
            'intent': control_intent(availability), 'policy': {k: availability.get(k) for k in
                ('mode', 'revision', 'source', 'until', 'reason')},
            'require_ac': bool((hub._compute_policy(node['id']) or {}).get('require_ac') or
                               (snapshot.get('local_compute_policy') or {}).get('require_ac'))}


def sync(hub, node_id, payload):
    from .hub import APIError
    if hub.center_state['mode'] != 'primary':
        raise APIError(503, 'Windows 控制连接需重新连接当前调度主控')
    if not isinstance(payload, dict) or set(payload) - {'node_id', 'host_id', 'report'}:
        raise ValueError('Invalid Windows host control request')
    if payload.get('node_id') != node_id:
        raise APIError(403, 'Host control belongs to another node')
    host_id = payload.get('host_id')
    if not isinstance(host_id, str) or not re.fullmatch('[a-f0-9]{32}', host_id):
        raise ValueError('Invalid Windows host identity')
    report = payload.get('report')
    if report is None:
        report = {}
    if (not isinstance(report, dict) or set(report) - {'intent', 'phase', 'detail', 'experiments_stopped',
            'docker_stopped', 'wsl_stopped', 'resources_released'}):
        raise ValueError('Invalid Windows runtime report')
    if report:
        if (not isinstance(report.get('intent'), str) or not re.fullmatch('[a-f0-9]{64}', report['intent'])
                or report.get('phase') not in ('retained', 'releasing', 'released', 'blocked', 'restarting')
                or not isinstance(report.get('detail'), str) or len(report['detail']) > 1000):
            raise ValueError('Invalid Windows runtime report')
        for key in ('experiments_stopped', 'docker_stopped', 'wsl_stopped', 'resources_released'):
            if type(report.get(key)) is not bool:
                raise ValueError('Windows runtime confirmation must be boolean')
        if (report['phase'] == 'released') != report['resources_released'] or report['resources_released'] and not all(
                report[k] for k in ('experiments_stopped', 'docker_stopped', 'wsl_stopped')):
            raise ValueError('Docker and WSL must both be verified stopped')
    with hub.transaction():
        node = hub.db.execute('SELECT * FROM nodes WHERE id=?', (node_id,)).fetchone()
        if node is None:
            raise APIError(404, 'Unknown node')
        request = desired(hub, node)
        hub.db.execute('INSERT OR REPLACE INTO node_host_control VALUES (?,?,?,?)',
                       (node_id, host_id, json.dumps(report), now()))
        return {'protocol': 1, 'node_id': node_id, 'desired': request,
                'report_matches': report.get('intent') == request['intent'], 'time': now()}


def overlay(hub, node, timestamp):
    row = hub.db.execute('SELECT * FROM node_host_control WHERE node_id=?', (node['id'],)).fetchone()
    snapshot = node['snapshot']
    if row is None and snapshot.get('runtime_host') != 'windows':
        return
    snapshot['runtime_host'] = 'windows'
    if not isinstance(snapshot.get('availability'), dict):
        snapshot['node_mode'] = node['mode']
        apply_policy(snapshot, hub._compute_policy(node['id']) or {})
    control = snapshot.get('compute_control') or {}
    availability = snapshot.get('availability') or {}
    report = json.loads(row['value']) if row else {}
    fresh = row is not None and timestamp - row['updated'] <= 45
    matching = report.get('intent') == control_intent(availability)
    snapshot['host_control'] = {**report, 'online': fresh, 'observed_at': row['updated'] if row else None,
                                'matches': matching}
    if fresh:
        snapshot['capabilities'] = list(dict.fromkeys([*snapshot.get('capabilities', []), CAPABILITY]))
        node['worker_online'] = node['online']
        node['online'] = True  # The Windows agent is online; scheduler still uses worker last_seen.
        if not node['worker_online']:
            snapshot['telemetry_stale'] = True
            snapshot['free_ram_mb'] = None
            for gpu in snapshot.get('gpus', []):
                gpu['free_mb'] = None
            snapshot['availability']['available'] = False
    if availability.get('mode') != 'release':
        return
    released = fresh and matching and report.get('resources_released') is True
    updated = dict(control)
    updated.update(mode='release', intent=control_intent(availability),
        policy={k: availability.get(k) for k in ('mode', 'revision', 'source', 'until', 'reason')},
        observed_at=row['updated'] if row else timestamp, runtime_retained=not released,
        training_resources_released=bool(control.get('resources_released') or control.get('training_resources_released')),
        resources_released=bool(released))
    if fresh and matching and report:
        updated.update(phase=report['phase'] if report['phase'] in ('releasing', 'released', 'blocked') else 'releasing',
                       detail=report['detail'], experiments_stopped=report['experiments_stopped'])
    else:
        updated.update(phase='releasing', detail='等待 Windows 客户端释放并核实 Docker / WSL；尚未确认运行环境已释放')
        updated.setdefault('experiments_stopped', False)
    snapshot['compute_control'] = updated
