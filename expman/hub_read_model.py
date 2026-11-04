"""Transactionally maintained, small projections for frequent Center reads.

The canonical jobs table retains runners, logs and complete observations. SQLite
triggers update this disposable projection in the same transaction as a job, so
polling never has to read historical embedded programs or depend on a TTL cache.
"""
from contextlib import contextmanager
import json


TERMINAL_SQL = "'canceled','failed','interrupted','paused','succeeded'"
PENDING_SQL = f"(state NOT IN ({TERMINAL_SQL}) OR command_id>command_ack)"

FIELDS = {
    "id": "TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE",
    "spec": "TEXT NOT NULL", "node_id": "TEXT", "state": "TEXT NOT NULL",
    "detail": "TEXT NOT NULL", "created": "REAL NOT NULL", "updated": "REAL NOT NULL",
    "attempt": "INTEGER NOT NULL", "seq": "INTEGER NOT NULL", "metrics": "TEXT NOT NULL",
    "command_id": "INTEGER NOT NULL", "command_ack": "INTEGER NOT NULL", "action": "TEXT",
    "timing": "TEXT", "runtime": "TEXT", "generation": "INTEGER NOT NULL",
    "execution_state": "TEXT", "resume_checkpoint": "TEXT",
}


@contextmanager
def write_batch(db):
    """One durable commit, also usable within an existing Hub transaction."""
    db.execute("SAVEPOINT center_read_model_batch")
    try:
        yield
        db.execute("RELEASE center_read_model_batch")
    except BaseException:
        db.execute("ROLLBACK TO center_read_model_batch")
        db.execute("RELEASE center_read_model_batch")
        raise


def _projection(prefix):
    omitted = {"spec": ("$.command",), "runtime": ("$.diagnostics",),
               "execution_state": ("$.diagnostics", "$.resources")}
    return ",".join(
        "json_remove(" + prefix + key + "," + ",".join("'" + path + "'" for path in omitted[key]) + ")"
        if key in omitted else prefix + key for key in FIELDS)


def initialize(db):
    columns = ",".join(FIELDS)
    definitions = ",".join(key + " " + declaration for key, declaration in FIELDS.items())
    # These triggers deliberately use only SQLite built-ins: backup/replication
    # and maintenance connections must not require a process-local SQL function.
    db.executescript(f"""
        CREATE TABLE IF NOT EXISTS job_summaries ({definitions});
        CREATE TABLE IF NOT EXISTS job_workload_keys (
            job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE, value TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS job_summary_insert AFTER INSERT ON jobs BEGIN
            INSERT OR REPLACE INTO job_summaries ({columns}) VALUES ({_projection('NEW.')});
        END;
        CREATE TRIGGER IF NOT EXISTS job_summary_update AFTER UPDATE OF {columns} ON jobs BEGIN
            INSERT OR REPLACE INTO job_summaries ({columns}) VALUES ({_projection('NEW.')});
        END;
        CREATE TRIGGER IF NOT EXISTS job_workload_invalidate AFTER UPDATE OF spec ON jobs
        WHEN OLD.spec IS NOT NEW.spec BEGIN
            DELETE FROM job_workload_keys WHERE job_id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS job_summary_delete AFTER DELETE ON jobs BEGIN
            DELETE FROM job_summaries WHERE id=OLD.id;
            DELETE FROM job_workload_keys WHERE job_id=OLD.id;
        END;
        CREATE INDEX IF NOT EXISTS job_summaries_created ON job_summaries(created DESC,id);
        CREATE INDEX IF NOT EXISTS job_summaries_state ON job_summaries(state,node_id);
        CREATE INDEX IF NOT EXISTS job_summaries_pending ON job_summaries(node_id,created,id)
            WHERE {PENDING_SQL};
        CREATE INDEX IF NOT EXISTS job_summaries_queue ON job_summaries(created,id)
            WHERE state='queued' AND node_id IS NULL;
        CREATE INDEX IF NOT EXISTS job_summaries_active_node ON job_summaries(node_id,state)
            WHERE node_id IS NOT NULL AND state NOT IN ({TERMINAL_SQL});
        CREATE INDEX IF NOT EXISTS job_summaries_policy ON job_summaries(node_id,id)
            WHERE state NOT IN ('succeeded','canceled','failed');
        CREATE INDEX IF NOT EXISTS execution_owners_retired ON execution_owners(node_id,job_id,generation)
            WHERE last_attempt IS NOT NULL;
    """)
    with write_batch(db):
        db.execute(f"INSERT INTO job_summaries ({columns}) SELECT {_projection('j.')} FROM jobs j "
                   "WHERE NOT EXISTS (SELECT 1 FROM job_summaries s WHERE s.id=j.id)")


def workload_keys(db):
    """Hash a task definition once, invalidating exactly when that definition changes."""
    from .scheduler import workload_key
    missing = db.execute(
        "SELECT s.id FROM job_summaries s LEFT JOIN job_workload_keys k ON k.job_id=s.id "
        "WHERE k.job_id IS NULL AND s.node_id IS NOT NULL AND "
        "((s.state='succeeded' AND s.generation=1 AND s.timing IS NOT NULL) OR "
        f"s.state NOT IN ({TERMINAL_SQL}))").fetchall()
    if missing:
        with write_batch(db):
            for row in missing:
                spec = db.execute("SELECT spec FROM jobs WHERE id=?", (row["id"],)).fetchone()[0]
                db.execute("INSERT INTO job_workload_keys VALUES (?,?)", (row["id"], workload_key(json.loads(spec))))
    return {row["job_id"]: row["value"] for row in db.execute("SELECT * FROM job_workload_keys")}
