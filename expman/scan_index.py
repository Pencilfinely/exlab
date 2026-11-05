"""Durable bounded discovery queues; historical runs do not poll at 4 Hz."""
from . import common
import json


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS task_scan_queue (
            job_id TEXT PRIMARY KEY, results_due REAL NOT NULL DEFAULT 0,
            evidence_due REAL NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS task_results_due ON task_scan_queue(results_due,job_id);
        CREATE INDEX IF NOT EXISTS task_evidence_due ON task_scan_queue(evidence_due,job_id);
        CREATE INDEX IF NOT EXISTS tasks_state ON tasks(json_extract(record,'$.state'));
        CREATE TRIGGER IF NOT EXISTS tasks_scan_insert AFTER INSERT ON tasks BEGIN
            INSERT INTO task_scan_queue(job_id) VALUES (NEW.id)
            ON CONFLICT(job_id) DO UPDATE SET results_due=0,evidence_due=0,revision=revision+1;
        END;
        CREATE TRIGGER IF NOT EXISTS tasks_scan_update AFTER UPDATE ON tasks BEGIN
            INSERT INTO task_scan_queue(job_id) VALUES (NEW.id)
            ON CONFLICT(job_id) DO UPDATE SET results_due=0,evidence_due=0,revision=revision+1;
        END;
        CREATE TRIGGER IF NOT EXISTS results_scan_evidence AFTER INSERT ON node_results BEGIN
            UPDATE task_scan_queue SET evidence_due=0,revision=revision+1 WHERE job_id=NEW.job_id;
        END;
        INSERT OR IGNORE INTO task_scan_queue(job_id) SELECT id FROM tasks;
    ''')
    db.commit()


def due(db, lane, limit=128):
    if lane not in ('results', 'evidence'):
        raise ValueError('Unknown scan lane')
    column = lane + '_due'
    active = "('assigned','preparing','ready','starting','running')"
    timestamp = common.now()
    rows = db.execute('SELECT t.record,q.revision FROM tasks t JOIN task_scan_queue q ON t.id=q.job_id '
                      f"WHERE json_extract(t.record,'$.state') IN {active} AND q.{column}<=? "
                      f'ORDER BY q.{column},q.job_id LIMIT ?', (timestamp, min(64, limit))).fetchall()
    rows += db.execute('SELECT t.record,q.revision FROM task_scan_queue q JOIN tasks t ON t.id=q.job_id '
                       f"WHERE q.{column}<=? AND json_extract(t.record,'$.state') NOT IN {active} "
                       f'ORDER BY q.{column},q.job_id LIMIT ?', (timestamp, limit - len(rows))).fetchall()
    return [dict(json.loads(row[0]), _scan_revision=row[1]) for row in rows]


def completed(db, lane, record, delay):
    if lane not in ('results', 'evidence'):
        raise ValueError('Unknown scan lane')
    if '_scan_revision' not in record:
        return
    with db:
        db.execute(f'UPDATE task_scan_queue SET {lane}_due=? WHERE job_id=? AND revision=? AND EXISTS '
                   '(SELECT 1 FROM tasks WHERE id=? AND json_extract(record,\'$.seq\')=?)',
                   (common.now() + delay, record['id'], record['_scan_revision'], record['id'], record.get('seq', 0)))
