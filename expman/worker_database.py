"""Short, serialized writes shared by a worker's independent SQLite lanes."""
import sqlite3
import threading


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


class Connection(sqlite3.Connection):
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
        finally:
            self.write_lock.release()


def connect(path, *, write_lock=None, **kwargs):
    db = sqlite3.connect(path, factory=Connection, **kwargs)
    db.write_lock = write_lock if write_lock is not None else threading.RLock()
    return db
