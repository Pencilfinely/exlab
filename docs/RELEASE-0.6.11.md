# ExLab 0.6.11

Linux and WSL workers now detect training interrupted by a host reboot. After verifying a published local checkpoint and confirming the old Docker container has stopped, the Center queues a resume on the original worker. Pending stop and cancel commands take priority. Replayed heartbeats and lost acknowledgments reuse the same resume command.

Update the Center first, then the workers. Jobs already recorded as interrupted or failed by older versions can be resumed manually if they have a usable checkpoint.
