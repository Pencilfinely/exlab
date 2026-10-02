"""One shared workspace, synchronized Center replicas and explicit handover.

Replicas forward operations to the primary. Handover durably fences the old
primary before the recipient downloads its last snapshot or schedules work.
"""
from __future__ import annotations

import base64
import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid

from . import __version__
from .common import atomic_json, read_json, safe_child, sha256_file
from .pairing import validate_origin

MAX_SNAPSHOT = 64 * 1024 * 1024
FILE_KEY = re.compile(r'(?:projects/[0-9a-f]{64}\.zip|archives/[0-9a-f]{32}/[0-9a-f]{64})')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a Center credential to a redirected origin.


def open_remote(url, token, *, data=None, method=None):
    request = urllib.request.Request(url, data=data, method=method,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=30)


def remote_json(link, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    with open_remote(link['hub_url'] + path, link['token'], data=data) as response:
        raw = response.read(MAX_SNAPSHOT * 2 + 1)
        if len(raw) > MAX_SNAPSHOT * 2:
            raise ValueError('主控同步信息过大')
        return json.loads(raw)


def validate_center_credential(value):
    if not isinstance(value, dict) or value.get('schema') != 1 or value.get('kind') != 'exlab-center':
        raise ValueError('请粘贴 ExLab Center 连接凭证')
    for key in ('workspace_id', 'source_center_id', 'center_id'):
        if not isinstance(value.get(key), str) or not re.fullmatch(r'[0-9a-f]{32}', value[key]):
            raise ValueError('主控身份无效')
    token = value.get('token')
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,512}', token):
        raise ValueError('主控凭证无效')
    return {**{key: value[key] for key in ('schema', 'kind', 'workspace_id', 'source_center_id', 'center_id', 'token')},
            'hub_url': validate_origin(value.get('hub_url')), 'name': str(value.get('name', 'Center'))[:80]}


def initialize(hub):
    hub.center_stop = threading.Event()
    hub.center_thread = None
    hub.center_sync_lock = threading.Lock()
    hub.center_export_lock = threading.Lock()
    hub.center_state_path = hub.root / 'center.json'
    hub.center_state = read_json(hub.center_state_path, {})
    if not hub.center_state:
        hub.center_state = dict(device_id=uuid.uuid4().hex, name=socket.gethostname(),
                                workspace_id=uuid.uuid4().hex, mode='primary')
        atomic_json(hub.center_state_path, hub.center_state)
    hub.config.setdefault('workspace_id', hub.center_state['workspace_id'])
    hub.config.setdefault('centers', {})
    atomic_json(hub.root / 'hub.json', hub.config)
    hub.center_error = ''
    hub.center_last_sync = hub.center_state.get('last_sync')
    hub.center_file_manifest = {}


class CenterHubMixin:
    def center_save(self):
        atomic_json(self.center_state_path, self.center_state)
        if os.name != 'nt':
            self.center_state_path.chmod(0o600)

    def center_info(self):
        with self.lock:
            state = self.center_state
            return dict(workspace_id=state['workspace_id'], device_id=state['device_id'], name=state['name'],
                        mode=state['mode'], last_sync=self.center_last_sync, error=self.center_error,
                        upstream_url=state.get('upstream', {}).get('hub_url'), version=__version__,
                        devices=[{'id': key, 'name': value['name'], 'revoked': value.get('revoked', False)}
                                 for key, value in self.config.get('centers', {}).items()])

    def center_authenticate(self, token):
        for identity, device in self.config.get('centers', {}).items():
            if not device.get('revoked') and secrets.compare_digest(token, device['token']):
                return identity
        return None

    def center_enroll(self, payload):
        from .hub import APIError
        name = payload.get('name')
        url = validate_origin(payload.get('hub_url'))
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise APIError(400, '请输入主控名称（最多 80 字）')
        name = name.strip()
        with self.lock:
            if self.center_state['mode'] != 'primary':
                raise APIError(409, '请在当前调度主控创建连接凭证')
            identity, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
            self.center_state['hub_url'] = url
            self.center_save()
            self.config['centers'][identity] = {'name': name, 'token': token}
            atomic_json(self.root / 'hub.json', self.config)
            return dict(schema=1, kind='exlab-center', workspace_id=self.center_state['workspace_id'],
                        source_center_id=self.center_state['device_id'], center_id=identity,
                        name=name, hub_url=url, token=token)

    def center_revoke(self, payload):
        from .hub import APIError
        with self.lock:
            identity = payload.get('center_id')
            if identity not in self.config['centers']:
                raise APIError(404, '未知主控')
            self.config['centers'][identity]['revoked'] = True
            atomic_json(self.root / 'hub.json', self.config)
        return self.center_info()

    def center_register(self, identity, payload):
        from .hub import APIError
        url = validate_origin(payload.get('hub_url'))
        with self.lock:
            if identity not in self.config['centers']:
                raise APIError(403, '请使用这台 Center 的连接凭证')
            self.config['centers'][identity]['hub_url'] = url
            atomic_json(self.root / 'hub.json', self.config)
        return self.center_info()

    def center_rename(self, payload):
        from .hub import APIError
        name = payload.get('name')
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise APIError(400, '名称应为 1–80 字')
        with self.lock:
            self.center_state['name'] = name.strip()
            self.center_save()
        return self.center_info()

    def center_connect(self, payload):
        from .hub import APIError
        credential = validate_center_credential(payload.get('credential'))
        with self.center_sync_lock:
            with self.lock:
                existing = self.center_state.get('upstream')
                if existing and existing != credential:
                    raise APIError(409, '此 Center 已连接另一个身份，请使用独立数据目录')
                returning = self.center_state['mode'] == 'handoff' and self.center_state['workspace_id'] == credential['workspace_id']
                if not existing and not returning and (self.config['nodes'] or self.db.execute('SELECT 1 FROM jobs LIMIT 1').fetchone()
                        or self.db.execute('SELECT 1 FROM projects LIMIT 1').fetchone()):
                    raise APIError(409, '请在空的数据目录接入工作空间，已有实验不会被覆盖')
                if credential['source_center_id'] == self.center_state['device_id']:
                    raise APIError(400, '不能连接当前 Center 自己')
            identity = remote_json(credential, '/api/centers/identity')
            if (identity['workspace_id'] != credential['workspace_id']
                    or identity['device_id'] != credential['source_center_id'] or identity['mode'] != 'primary'):
                raise APIError(409, '凭证来源已变化，请从当前主控重新创建')
            own_url = validate_origin(payload.get('hub_url'))
            remote_json(credential, '/api/centers/register', {'hub_url': own_url})
            with self.lock:
                self.center_state.update(workspace_id=credential['workspace_id'], mode='replica',
                                         device_id=credential['center_id'], hub_url=own_url,
                                         name=credential['name'], upstream=credential)
                self.center_save()
            self._center_sync()
        self.center_start_sync()
        return self.center_info()

    def center_start_sync(self):
        if self.center_thread is not None or self.center_state['mode'] not in ('replica', 'handoff'):
            return
        def synchronize():
            while not self.center_stop.wait(30):
                if self.center_state['mode'] not in ('replica', 'handoff'):
                    continue
                try:
                    self.center_sync()
                except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error):
                    # Report only a generic error; URLs/tokens from HTTP exceptions
                    # and peer response bodies never become public status text.
                    self.center_error = '主控同步暂未完成，请检查连接；本地副本已保留'
        self.center_thread = threading.Thread(target=synchronize, daemon=True, name='center-sync')
        self.center_thread.start()

    def center_sync(self, payload=None):
        with self.center_sync_lock:
            self._center_sync()
        return self.center_info()

    def center_snapshot(self):
        from .hub import APIError
        with self.center_export_lock:
            scratch = self.root / '.center-sync' / (uuid.uuid4().hex + '.sqlite3')
            scratch.parent.mkdir(exist_ok=True)
            try:
                with self.lock:
                    if self.center_state['mode'] not in ('primary', 'handoff'):
                        raise APIError(409, '此 Center 不是调度主控')
                    with closing(sqlite3.connect(scratch)) as target:
                        self.db.backup(target)
                    configuration = {key: copy.deepcopy(self.config[key]) for key in ('nodes', 'centers', 'workspace_id')}
                    manifest = {}
                    for row in self.db.execute('SELECT digest,size FROM projects WHERE deleted_at IS NULL'):
                        key = f'projects/{row["digest"]}.zip'
                        if safe_child(self.root, key).is_file():
                            manifest[key] = dict(sha256=row['digest'], size=row['size'])
                        else:
                            raise APIError(409, '项目文件缺失，不能生成完整主控副本')
                    for row in self.db.execute('SELECT DISTINCT job_id,sha256,size FROM artifacts'):
                        key = f'archives/{row["job_id"]}/{row["sha256"]}'
                        if not safe_child(self.root, key).is_file():
                            raise APIError(409, '归档文件缺失，不能生成完整主控副本')
                        manifest[key] = dict(sha256=row['sha256'], size=row['size'])
                    self.center_file_manifest = manifest
                if scratch.stat().st_size > MAX_SNAPSHOT:
                    raise APIError(413, '实验数据库超过当前同步上限 64 MiB')
                raw = scratch.read_bytes()
                return dict(schema=1, version=__version__, workspace_id=self.center_state['workspace_id'],
                            source_center_id=self.center_state['device_id'], mode=self.center_state['mode'],
                            database=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest(),
                            configuration=configuration, files=manifest)
            finally:
                scratch.unlink(missing_ok=True)

    def center_file(self, key, offset):
        from .hub import APIError
        if not isinstance(key, str) or not FILE_KEY.fullmatch(key) or key not in self.center_file_manifest:
            raise APIError(404, '未知同步文件')
        if not isinstance(offset, int) or offset < 0:
            raise APIError(400, '文件偏移无效')
        path = safe_child(self.root, key)
        with path.open('rb') as stream:
            stream.seek(offset)
            return {'data': base64.b64encode(stream.read(512 * 1024)).decode()}

    def _center_sync(self):
        from .hub import APIError
        from urllib.parse import urlencode
        if self.center_stop.is_set():
            raise RuntimeError('主控正在停止')
        if self.center_state['mode'] == 'handoff' and self.center_state.get('upstream'):
            link = self.center_state['upstream']
            identity = remote_json(link, '/api/centers/identity')
            if (identity.get('mode') != 'primary' or identity.get('workspace_id') != link['workspace_id']
                    or identity.get('device_id') != link['source_center_id']):
                raise APIError(409, '等待接管主控完成交接')
            with self.lock:
                self.center_state['mode'] = 'replica'
                self.center_save()
        if self.center_state['mode'] != 'replica':
            raise APIError(409, '当前 Center 没有待同步的上游')
        link = copy.deepcopy(self.center_state['upstream'])
        snapshot = remote_json(link, '/api/centers/snapshot')
        if (snapshot.get('version') != __version__ or snapshot.get('schema') != 1
                or snapshot.get('workspace_id') != link['workspace_id']
                or snapshot.get('source_center_id') != link['source_center_id']):
            raise APIError(409, '两台 Center 的版本或工作空间不一致，请先升级到相同版本')
        raw = base64.b64decode(snapshot['database'], validate=True)
        if len(raw) > MAX_SNAPSHOT or hashlib.sha256(raw).hexdigest() != snapshot.get('sha256'):
            raise ValueError('主控数据库校验失败')
        files = snapshot['files']
        if not isinstance(files, dict) or len(files) > 100000:
            raise ValueError('无效的同步文件清单')
        for key, item in files.items():
            if (not FILE_KEY.fullmatch(key) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])
                    or type(item['size']) is not int or not 0 <= item['size'] <= 32 * 1024**3):
                raise ValueError('无效的同步文件')
            destination = safe_child(self.root, key)
            if destination.is_file() and destination.stat().st_size == item['size'] and sha256_file(destination) == item['sha256']:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            partial = destination.with_name(destination.name + '.sync-part')
            if partial.is_symlink():
                raise ValueError('同步临时文件不能是符号链接')
            if partial.exists() and partial.stat().st_size > item['size']:
                partial.unlink()
            with partial.open('ab') as stream:
                while stream.tell() < item['size']:
                    if self.center_stop.is_set():
                        raise RuntimeError('主控正在停止，同步将在下次启动继续')
                    block = remote_json(link, '/api/centers/file?' + urlencode({'key': key, 'offset': stream.tell()}))
                    data = base64.b64decode(block['data'], validate=True)
                    if not data or len(data) > 512 * 1024 or stream.tell() + len(data) > item['size']:
                        raise ValueError('同步文件内容无效')
                    stream.write(data)
            if sha256_file(partial) != item['sha256']:
                partial.unlink()
                raise ValueError('同步文件校验失败，请重试')
            os.replace(partial, destination)
        scratch = self.root / '.center-sync' / (uuid.uuid4().hex + '.sqlite3')
        scratch.parent.mkdir(exist_ok=True)
        scratch.write_bytes(raw)
        try:
            with closing(sqlite3.connect(scratch)) as source:
                if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('主控数据库完整性检查失败')
                if source.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('主控数据库关联检查失败')
                # Commit only after every immutable project/result file arrived.
                with self.lock:
                    configuration = snapshot['configuration']
                    if configuration['workspace_id'] != link['workspace_id']:
                        raise ValueError('主控配置身份不符')
                    source.backup(self.db)
                    self.config.update(configuration)
                    atomic_json(self.root / 'hub.json', self.config)
                    self.center_last_sync = time.time()
                    self.center_state.update(last_sync=self.center_last_sync, snapshot_sha256=snapshot['sha256'])
                    self.center_save()
                    self.center_error = ''
        finally:
            scratch.unlink(missing_ok=True)

    def center_handoff(self, recipient, payload):
        """Fence the source durably; retries by the same recipient are safe."""
        from .hub import APIError
        with self.lock:
            mode = self.center_state['mode']
            if mode == 'handoff' and self.center_state.get('handoff_to') == recipient:
                return self.center_info()
            if mode != 'primary':
                raise APIError(409, '原主控已交接给另一台 Center')
            if self.active_update_requests > 1 or self.project_upload_locks or (self.local_imports and self.local_imports.threads):
                raise APIError(409, '请等待正在进行的导入或文件请求完成后重试交接')
            peer = self.config['centers'].get(recipient, {})
            if not peer.get('hub_url') or not self.center_state.get('hub_url'):
                raise APIError(409, '请先在接管设备完成连接和备用地址登记')
            identity = self.center_state['device_id']
            existing = self.config['centers'].get(identity, {})
            token = existing.get('token') if existing and not existing.get('revoked') else secrets.token_urlsafe(32)
            self.config['centers'][identity] = dict(name=self.center_state['name'], token=token,
                                                    hub_url=self.center_state['hub_url'])
            atomic_json(self.root / 'hub.json', self.config)
            reverse = dict(schema=1, kind='exlab-center', workspace_id=self.center_state['workspace_id'],
                           source_center_id=recipient, center_id=identity, name=self.center_state['name'],
                           hub_url=peer['hub_url'], token=token)
            self.center_state.update(mode='handoff', handoff_to=recipient, upstream=reverse)
            self.center_save()
        self.center_start_sync()
        return self.center_info()

    def center_promote(self, payload):
        from .hub import APIError
        with self.center_sync_lock:
            if self.center_state['mode'] == 'primary':
                return self.center_info()
            if self.center_state['mode'] != 'replica':
                raise APIError(409, '此 Center 已交接，请在接管设备操作')
            link = self.center_state['upstream']
            # No unreachable-source takeover: cached state cannot prove that the
            # other coordinator has stopped accepting writes or assigning jobs.
            remote_json(link, '/api/centers/handoff', {})
            self._center_sync()
            with self.lock:
                self.center_state.update(mode='primary')
                self.center_state.pop('upstream', None)
                self.center_save()
        return self.center_info()

    def center_assert_writable(self):
        from .hub import APIError
        if self.center_state['mode'] != 'primary':
            raise APIError(503, '此 Center 已停用调度，请连接当前调度主控')
