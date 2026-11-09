"""Short, serialized writes shared by a worker's independent SQLite lanes."""
import errno
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time


def transient(error):
    """Recognize lock contention, including extended SQLite result codes."""
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, 'sqlite_errorcode', None)
    if isinstance(code, int):
        return code & 0xff in (5, 6)  # SQLITE_BUSY / SQLITE_LOCKED; also supports Python 3.10.
    # Python 3.10 does not expose SQLite result codes on exceptions.
    return str(error).lower().startswith((
        'database is locked', 'database table is locked', 'database schema is locked'))


def storage_full(error):
    """Capacity failures can clear; corruption and other I/O errors must not retry."""
    if isinstance(error, OSError):
        return error.errno in (errno.ENOSPC, errno.EDQUOT)
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, 'sqlite_errorcode', None)
    if isinstance(code, int):
        return code & 0xff == 13
    return str(error).lower() == 'database or disk is full'


def storage_snapshot(path, db=None, *, phase=None):
    """Read bounded metadata while the failed connection and WAL still exist.

    No SQL text, record values, environment dump, repair, or write probe. A new
    connection would lose connection-local max_page_count, so use the original.
    """
    path = Path(path).resolve()
    result = dict(observed_at=time.time(), phase=phase, database=str(path),
                  pragmas={}, files=[], filesystems=[], temp_environment={})
    if db is not None:
        for name in ('page_size', 'page_count', 'max_page_count', 'freelist_count',
                     'journal_mode', 'temp_store', 'journal_size_limit'):
            try:
                result['pragmas'][name] = sqlite3.Connection.execute(db, 'PRAGMA ' + name).fetchone()[0]
            except sqlite3.Error as error:
                result['pragmas'][name] = dict(error=type(error).__name__,
                    sqlite_errorcode=getattr(error, 'sqlite_errorcode', None))
    for suffix in ('', '-wal', '-shm', '-journal'):
        file = Path(str(path) + suffix)
        try:
            info = file.stat()
            result['files'].append(dict(path=str(file), bytes=info.st_size,
                allocated_bytes=getattr(info, 'st_blocks', 0) * 512, device=info.st_dev))
        except OSError as error:
            result['files'].append(dict(path=str(file), errno=error.errno))
    candidates = [path.parent]
    for key in ('SQLITE_TMPDIR', 'TMPDIR', 'TMP', 'TEMP'):
        if os.environ.get(key):
            result['temp_environment'][key] = os.environ[key]
            candidates.append(Path(os.environ[key]))
    if os.name == 'posix':
        candidates.extend(map(Path, ('/var/tmp', '/usr/tmp', '/tmp', '.')))
        import resource
        result['file_size_limit'] = list(resource.getrlimit(resource.RLIMIT_FSIZE))
    for directory in dict.fromkeys(str(p.resolve()) for p in candidates):
        entry = dict(path=directory)
        try:
            usage = shutil.disk_usage(directory)
            entry.update(total_bytes=usage.total, available_bytes=usage.free)
            if hasattr(os, 'statvfs'):
                stat = os.statvfs(directory)
                entry.update(free_inodes=stat.f_favail, free_bytes=stat.f_bfree * stat.f_frsize)
        except OSError as error:
            entry['errno'] = error.errno
        result['filesystems'].append(entry)
    return result


def raise_storage_fault(db):
    # Lanes share this latch. The supervisor must reopen *all* connections even
    # when a background lane, rather than the heartbeat lane, first runs full.
    if db.storage_faults:
        raise db.storage_faults[0]


class Connection(sqlite3.Connection):
    def _storage_failure(self, error, phase):
        if storage_full(error):
            try:
                error.storage_diagnostics = storage_snapshot(self.database_path, self, phase=phase)
            except Exception:
                # Diagnostics must never hide the original failure.
                pass
            if not self.storage_faults:
                self.storage_faults.append(error)

    def execute(self, *args, **kwargs):
        try:
            return super().execute(*args, **kwargs)
        except sqlite3.Error as error:
            self._storage_failure(error, 'execute')
            raise

    def executescript(self, *args, **kwargs):
        try:
            return super().executescript(*args, **kwargs)
        except sqlite3.Error as error:
            self._storage_failure(error, 'executescript')
            raise

    def executemany(self, *args, **kwargs):
        try:
            return super().executemany(*args, **kwargs)
        except sqlite3.Error as error:
            self._storage_failure(error, 'executemany')
            raise

    def commit(self):
        try:
            return super().commit()
        except sqlite3.Error as error:
            self._storage_failure(error, 'commit')
            raise

    def __enter__(self):
        self.write_lock.acquire()
        try:
            return super().__enter__()
        except BaseException:
            self.write_lock.release()
            raise

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        except sqlite3.Error as error:
            self._storage_failure(error, 'transaction_exit')
            raise
        finally:
            self.write_lock.release()


def connect(path, *, write_lock=None, storage_faults=None, **kwargs):
    db = sqlite3.connect(path, factory=Connection, **kwargs)
    db.write_lock = write_lock if write_lock is not None else threading.RLock()
    db.database_path = path
    db.storage_faults = storage_faults if storage_faults is not None else []
    return db
