# ExLab 0.5.12 — bounded Python stack observations

A worker may remain online while a training process consumes CPU without using
its assigned GPU or changing its log. Version 0.5.12 adds a nonblocking Python
stack sample to the existing runtime observation, so the executing Python
function and line can be inspected.

Sampling is automatic only after five minutes of unchanged, successfully read
logs, at least 50% CPU and zero utilization on the assigned GPU. It is limited
to once per ten minutes per job attempt and immutable container ID. These
conditions indicate a useful diagnostic opportunity, not proof of failure.

A disposable auxiliary container uses the exact cached image ID and target's
private PID namespace, with no network, no scientific data/checkpoint mounts,
read-only filesystem, dropped capabilities except SYS_PTRACE, and bounded CPU,
memory, output and time. No host PID namespace or Docker socket is exposed.
Only the sampler's own, label-verified container is cleaned up.

The pinned py-spy 0.4.2 executable is hash verified. Sampling uses --nonblocking
and JSON output without locals or native mode. Only file basenames, function
names, line numbers and activity flags are forwarded; process arguments,
environment, thread names, local values and full paths are excluded. A failed
or incomplete sample is reported without signaling the target.

There is no automatic stop, retry, retraining, checkpoint modification, runtime
image change or GPU policy change. A Python frame can identify the calling code
but may not expose the exact native/CUDA cause. Nonblocking samples may be
incomplete. Publishing or installing this version does not prove recovery.

The existing runtime-observation-v1 Center receiver remains compatible.
This release distributes only the Windows Worker ZIP and Setup plus checksums.
No Ubuntu or Center asset is published. Existing Ubuntu workers are unchanged.

Validation includes identity, namespace, hash, cleanup, privacy, timeout and
rate-limit tests; two live Docker samples of a non-root Python child preserve
the original PID, running state and zero restart count.
