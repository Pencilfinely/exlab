"""Reclaim obsolete deployments and abandoned uploads on the primary Center."""
import logging
import os
import re
import sqlite3
import threading

from . import common
from .project_cleanup import owned_path


RETENTION_SECONDS = 14 * 86400
PARTIAL_SECONDS = 7 * 86400
CHECK_SECONDS = 300
RETRY_SECONDS = 6 * 3600
LOGGER = logging.getLogger(__name__)


def eligible_bundles(db, cutoff, *, bundle_id=None, limit=8):
    # A fresh publish or deploy is use, even before its first job is submitted.
    return [row[0] for row in db.execute("""
        WITH use_by_bundle AS (
            SELECT json_extract(spec,'$.project_bundle_id') AS bundle,
                   MAX(updated) AS last_use,
                   SUM(state='succeeded') AS succeeded,
                   SUM(state NOT IN ('succeeded','canceled') OR command_id>command_ack) AS protected
            FROM job_summaries
            WHERE (? IS NULL OR json_extract(spec,'$.project_bundle_id')=?)
            GROUP BY bundle
        )
        SELECT p.bundle_id FROM projects p JOIN use_by_bundle j ON j.bundle=p.bundle_id
        LEFT JOIN project_deployments d ON d.digest=p.digest
        WHERE p.deleted_at IS NULL AND p.bundle_id!=''
          AND j.succeeded>0 AND j.protected=0 AND j.last_use<=?
          AND NOT EXISTS (SELECT 1 FROM matrices m WHERE m.deleted_at IS NULL
                          AND json_extract(m.definition,'$.spec.project_bundle_id')=p.bundle_id)
        GROUP BY p.bundle_id
        HAVING MAX(p.created)<=? AND MAX(p.last_deployed)<=?
          AND SUM(d.status='installed')>0
          AND SUM(d.status NOT IN ('installed','failed'))=0
        ORDER BY j.last_use,p.bundle_id LIMIT ?
    """, (bundle_id, bundle_id, cutoff, cutoff, cutoff, limit))]


def archive_signature(root, digest):
    path = owned_path(root, f'projects/{digest}.zip')
    if not path.is_file():
        raise ValueError('项目归档文件缺失')
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class CenterMaintenance:
    def __init__(self, hub):
        self.hub = hub
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='center-storage', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join()

    def run(self):
        while not self.stop.wait(CHECK_SECONDS):
            try:
                self.cycle()
            except (OSError, ValueError, sqlite3.Error):
                LOGGER.exception('Center storage maintenance failed')

    def cycle(self):
        hub = self.hub
        policy = common.read_json(hub.root / 'storage-policy.json', {})
        with hub.lock:
            if hub.center_state['mode'] != 'primary' or hub.updating or not policy.get('automatic', True):
                return
            timestamp = common.now()
            cutoff = timestamp - RETENTION_SECONDS
            candidates = eligible_bundles(hub.db, cutoff)
        report = {'time': timestamp, 'archived_projects': [], 'expired_uploads': 0,
                  'expired_upload_bytes': 0, 'retried_deployments': 0, 'errors': []}
        self.reclaim_partials(timestamp - PARTIAL_SECONDS, report)
        if self.stop.is_set():
            return
        with hub.transaction():
            report['retried_deployments'] = hub.db.execute("""
                UPDATE project_deployments SET revision=revision+1,status='delete_pending',
                       detail='Automatic storage cleanup retry',updated=?
                WHERE status='delete_failed' AND updated<=?
                  AND digest IN (SELECT digest FROM projects WHERE auto_archived=1)
            """, (timestamp, timestamp - RETRY_SECONDS)).rowcount
            retry_archives = [row[0] for row in hub.db.execute(
                "SELECT digest FROM projects WHERE auto_archived=1 AND cleanup_error!='' LIMIT 8")]
        if retry_archives:
            hub._cleanup_project_archives(retry_archives)
        for bundle in candidates:
            if self.stop.is_set():
                return
            try:
                with hub.lock:
                    projects = list(hub.db.execute('SELECT digest,size FROM projects WHERE bundle_id=? AND deleted_at IS NULL', (bundle,)))
                archives = {}
                for digest, size in projects:
                    signature = archive_signature(hub.root, digest)
                    path = owned_path(hub.root, f'projects/{digest}.zip')
                    if signature[2] != size or common.sha256_file(path, cancelled=self.stop.is_set) != digest:
                        raise ValueError('项目归档校验失败，保留部署')
                    archives[digest] = signature
                if archives and not self.stop.is_set():
                    result = hub.project_delete({'digest': next(iter(archives))}, retire=(cutoff, archives))
                    report['archived_projects'].extend(archives)
                    if result['controller_cleanup_error']:
                        report['errors'].append({'bundle_id': bundle, 'detail': result['controller_cleanup_error']})
            except (OSError, ValueError) as error:
                report['errors'].append({'bundle_id': bundle, 'detail': str(error)[:500]})
        if not self.stop.is_set():
            common.atomic_json(hub.root / 'storage-status.json', report)

    def reclaim_partials(self, cutoff, report):
        hub = self.hub
        partials = owned_path(hub.root, 'project-partials')
        for entry in partials.glob('*.part'):
            if self.stop.is_set():
                break
            upload = entry.stem
            if not re.fullmatch(r'[0-9a-f]{32}', upload):
                continue
            try:
                with hub.lock:
                    if upload in hub.project_upload_locks:
                        continue
                    path = owned_path(hub.root, f'project-partials/{upload}.part')
                    if not path.is_file() or path.stat().st_mtime > cutoff:
                        continue
                    with hub.transaction():
                        size = path.stat().st_size
                        os.unlink(path)
                        hub.db.execute('DELETE FROM project_uploads WHERE id=? AND completed_digest IS NULL', (upload,))
                    report['expired_uploads'] += 1
                    report['expired_upload_bytes'] += size
            except (OSError, ValueError) as error:
                report['errors'].append({'upload_id': upload, 'detail': str(error)[:500]})
