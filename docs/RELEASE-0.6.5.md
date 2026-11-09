# ExLab 0.6.5

When a worker cannot save state because SQLite reports `SQLITE_FULL` or the
filesystem reports `ENOSPC` / `EDQUOT`, the management process now retains the
existing experiment records and retries with a 10–60 second backoff. It reopens
all database lanes together. Docker experiments are not recreated or stopped.
Corruption, unrelated I/O errors, and failed save-and-exit requests still fail
explicitly instead of being treated as recoverable capacity failures.

Before closing a failed SQLite connection, the worker captures its page count
and connection-local page limit, database/WAL sizes, filesystem free space and
inodes, temporary-directory configuration, and process file-size limit. The
redacted evidence is included in the worker log and status. If the log or status
filesystem is also full, the supervisor retains the diagnostic in memory and
tries to persist it before reconnecting. It does not delete files to make room.

Background result and checkpoint lanes report the same storage fault to the
main loop, rather than dying while trying to save an error in the full database.

Validation includes real SQLite page-limit exhaustion, failure of status/log
writes, task/attempt preservation, and a Linux integration test that exhausts a
private 2 MiB tmpfs and verifies recovery after space becomes available. These
tests establish the recovery behavior; they do not establish the physical
condition that originally triggered a historical `SQLITE_FULL` incident.
