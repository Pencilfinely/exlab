"""Reentrant FIFO admission for the Center's shared SQLite connection."""
from collections import deque
import threading
import time


class HubLock:
    """A background loop cannot repeatedly overtake already waiting heartbeats."""

    def __init__(self):
        self._condition = threading.Condition(threading.Lock())
        self._owner = None
        self._depth = 0
        self._waiters = deque()

    def acquire(self, blocking=True, timeout=-1):
        if not blocking and timeout != -1:
            raise ValueError("can't specify a timeout for a non-blocking call")
        if timeout < 0 and timeout != -1:
            raise ValueError("timeout value must be positive")
        identity = threading.get_ident()
        deadline = time.monotonic() + timeout if timeout >= 0 else None
        with self._condition:
            if self._owner == identity:
                self._depth += 1
                return True
            if self._owner is None and not self._waiters:
                self._owner, self._depth = identity, 1
                return True
            if not blocking:
                return False
            ticket = object()
            self._waiters.append(ticket)
            try:
                while self._owner is not None or self._waiters[0] is not ticket:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    self._condition.wait(remaining)
                self._waiters.popleft()
                self._owner, self._depth = identity, 1
                return True
            finally:
                # Timeout/interruption must not strand later requests behind a
                # ticket whose thread is no longer waiting.
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
                    self._condition.notify_all()

    def release(self):
        with self._condition:
            if self._owner != threading.get_ident():
                raise RuntimeError("cannot release un-acquired lock")
            self._depth -= 1
            if not self._depth:
                self._owner = None
                self._condition.notify_all()

    def owned(self):
        return self._owner == threading.get_ident()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()
