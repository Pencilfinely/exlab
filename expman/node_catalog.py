"""Transfer the installed project inventory separately from hardware heartbeats."""
import base64
import binascii
import hashlib
import json
import re

from . import common


FIELDS = ('task_templates', 'allowed_repos', 'assets', 'profiles')
CHUNK = 256 * 1024
LEGACY_SNAPSHOT_BYTES = 256 * 1024
LEGACY_TEMPLATE_COUNT = 100


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def needs_catalog(snapshot):
    return (len(snapshot.get('task_templates', [])) > LEGACY_TEMPLATE_COUNT
            or any(len(snapshot.get(key, [])) > 1000 for key in ('assets', 'allowed_repos'))
            or len(encode(snapshot)) > LEGACY_SNAPSHOT_BYTES)


def validate(value):
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError('Invalid installed project catalog')
    for field in ('assets', 'allowed_repos'):
        if not isinstance(value[field], list) or any(not isinstance(v, str) for v in value[field]):
            raise ValueError('Invalid catalog ' + field)
    if not isinstance(value['profiles'], dict) or any(not isinstance(v, str) for v in value['profiles'].values()):
        raise ValueError('Invalid catalog profiles')
    if not isinstance(value['task_templates'], list):
        raise ValueError('Invalid catalog templates')
    for template in value['task_templates']:
        if not isinstance(template, dict) or 'token' in template or 'admin_token' in template:
            raise ValueError('Invalid task template')
        common.validate_task(template)


def split(snapshot):
    inventory = {key: snapshot.get(key, {} if key == 'profiles' else []) for key in FIELDS}
    data = encode(inventory)
    digest = hashlib.sha256(data).hexdigest()
    heartbeat = {k: v for k, v in snapshot.items() if k not in FIELDS}
    heartbeat['catalog_digest'] = digest
    return heartbeat, data


def upload(origin, token, digest, data, timeout):
    offset = 0
    while offset < len(data):
        block = data[offset:offset + CHUNK]
        reply = common.api_request(origin + '/api/node-catalog', token, {
            'digest': digest, 'offset': offset, 'size': len(data),
            'data': base64.b64encode(block).decode('ascii')}, timeout=timeout)
        following = reply.get('offset')
        if type(following) is not int or not offset < following <= len(data):
            raise ValueError('Invalid catalog upload acknowledgement')
        offset = following


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS node_catalogs (
            node_id TEXT NOT NULL REFERENCES nodes(id), digest TEXT NOT NULL, data BLOB NOT NULL,
            PRIMARY KEY(node_id,digest));
        CREATE TABLE IF NOT EXISTS node_catalog_parts (
            node_id TEXT NOT NULL REFERENCES nodes(id), digest TEXT NOT NULL, offset INTEGER NOT NULL,
            size INTEGER NOT NULL, data BLOB NOT NULL, PRIMARY KEY(node_id,offset));
    ''')


def valid_digest(digest):
    return isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest) is not None


def receive(hub, node_id, payload):
    digest, offset, size = (payload.get(key) for key in ('digest', 'offset', 'size'))
    if (not valid_digest(digest) or type(offset) is not int or type(size) is not int
            or not 0 <= offset < size <= 2**53 - 1):
        raise ValueError('Invalid catalog upload')
    try:
        block = base64.b64decode(payload['data'], validate=True)
    except (TypeError, KeyError, ValueError, binascii.Error) as error:
        raise ValueError('Invalid catalog data') from error
    if len(block) != min(CHUNK, size - offset) or offset % CHUNK:
        raise ValueError('Invalid catalog chunk length or offset')
    with hub.transaction():
        db = hub.db
        saved = db.execute('SELECT length(data) FROM node_catalogs WHERE node_id=? AND digest=?',
                           (node_id, digest)).fetchone()
        if saved:
            if saved[0] != size:
                raise ValueError('Catalog size mismatch')
            return {'offset': size}
        last = db.execute('SELECT digest,size,offset+length(data) FROM node_catalog_parts '
                          'WHERE node_id=? ORDER BY offset DESC LIMIT 1', (node_id,)).fetchone()
        if last and (last[0], last[1]) != (digest, size):
            if offset != 0:
                raise ValueError('Catalog changed, restart upload')
            db.execute('DELETE FROM node_catalog_parts WHERE node_id=?', (node_id,))
            last = None
        next_offset = last[2] if last else 0
        if next_offset > offset:
            return {'offset': next_offset}
        if next_offset != offset:
            raise ValueError('Catalog chunk missing')
        db.execute('INSERT INTO node_catalog_parts VALUES (?,?,?,?,?)', (node_id, digest, offset, size, block))
        next_offset += len(block)
        if next_offset == size:
            data = b''.join(row[0] for row in db.execute(
                'SELECT data FROM node_catalog_parts WHERE node_id=? ORDER BY offset', (node_id,)))
            if hashlib.sha256(data).hexdigest() != digest:
                # Commit the discard so an identical retry can start cleanly.
                db.execute('DELETE FROM node_catalog_parts WHERE node_id=?', (node_id,))
                return {'offset': 0}
            validate(json.loads(data))
            db.execute("DELETE FROM node_catalogs WHERE node_id=? AND digest IS NOT "
                       "(SELECT json_extract(snapshot,'$.catalog_digest') FROM nodes WHERE id=?)", (node_id, node_id))
            db.execute('INSERT INTO node_catalogs VALUES (?,?,?)', (node_id, digest, data))
            db.execute('DELETE FROM node_catalog_parts WHERE node_id=?', (node_id,))
        return {'offset': next_offset}


def hydrate(db, node_id, snapshot):
    digest = snapshot.get('catalog_digest')
    if digest:
        row = db.execute('SELECT data FROM node_catalogs WHERE node_id=? AND digest=?', (node_id, digest)).fetchone()
        if row is None:
            raise ValueError('Installed project catalog is missing')
        snapshot.update(json.loads(row[0]))
    return snapshot
