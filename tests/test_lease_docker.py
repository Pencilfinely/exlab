"""CPU Docker integration. CI supplies the image digest and substitutes only GPU admission."""
from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from expman import common, sdk
from expman.__main__ import node_config
from expman.agent import Agent
from expman.hub import Hub, make_server
from tests.support import temporary_directory


@unittest.skipUnless(os.environ.get("EXLAB_LEASE_TEST_IMAGE"), "requires Linux Docker and a pinned Python image")
class DockerLeaseTests(unittest.TestCase):
    def test_source_guard_stops_without_worker_and_destination_restores(self):
        image = os.environ["EXLAB_LEASE_TEST_IMAGE"]
        with ExitStack() as stack:
            root = Path(stack.enter_context(temporary_directory()))
            source = root / "source"
            source.mkdir()
            shutil.copy(sdk.__file__, source / "sdk.py")
            (source / "train.py").write_text(
                "from sdk import Run\nimport time\nr = Run()\n"
                "if r.resuming:\n"
                "    step = int(r.checkpoint().read_text()) + 1\n"
                "    r.metric(step, loss=0)\n    r.finish({'step': step})\n"
                "else:\n"
                "    (r.output / 'weights').write_text('7')\n    r.publish_checkpoint('weights', 7)\n"
                "    r.metric(7, loss=1)\n    time.sleep(180)\n", encoding="utf-8")
            for args in (["init", "-q"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=test@example.invalid",
                                                      "commit", "-qm", "fixture"]):
                subprocess.run(["git", "-C", str(source), *args], check=True)
            commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
            hub = Hub(root / "center")
            stack.callback(hub.close)
            server = make_server(hub, port=0)
            stack.callback(server.server_close)
            stack.callback(server.shutdown)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            agents = []
            for owner in ("a", "b"):
                config = node_config(owner, f"http://127.0.0.1:{server.server_port}", hub.add_node(owner), root / owner, False)
                config["allowed_repos"] = [source.as_uri()]
                path = root / (owner + ".json")
                common.atomic_json(path, config)
                agent = Agent(path)
                agents.append(agent)
                stack.callback(agent.close)
                snapshot = {**agent.snapshot(), "profiles": {"test": image}, "docker_available": True,
                            "gpus": [{"uuid": "GPU-test", "profiles": ["test"], "total_mb": 12000, "free_mb": 12000}],
                            "free_ram_mb": 8192, "disk_free_mb": 8192}
                stack.enter_context(patch.object(agent, "snapshot", return_value=snapshot))
                def cpu_docker(argv, execute=agent._exec, **kwargs):
                    if argv[:2] == ["docker", "create"]:
                        argv = list(argv)
                        index = argv.index("--gpus")
                        del argv[index:index + 2]
                    return execute(argv, **kwargs)
                stack.enter_context(patch.object(agent, "_exec", side_effect=cpu_docker))
                agent.tick()
            spec = {"backend": "docker", "source": {"repo": source.as_uri(), "commit": commit},
                    "command": ["python", "train.py"], "environments": [{"profile": "test", "image": image}],
                    "resources": {"cpu": 1, "ram_mb": 256, "gpu_memory_mb": 100},
                    "scheduling": {"preferred_node_ids": ["a"]}, "resume_supported": True, "planning": {"lease_seconds": 30}}
            job = hub.submit({"spec": spec, "request_id": "docker-lease"})["ids"][0]
            containers = [f"expman-{job}-{n}" for n in (1, 2)]
            stack.callback(subprocess.run, ["docker", "rm", "-f", *containers], capture_output=True)

            def pump(workers, predicate):
                until = time.monotonic() + 100
                while time.monotonic() < until:
                    for worker in workers:
                        worker.tick()
                    if predicate():
                        return
                    time.sleep(.2)
                self.fail(str([(r["state"], r.get("detail")) for worker in workers for r in worker.records()]))

            pump(agents, lambda: bool(hub.job(job)["execution_checkpoints"]))
            agents[0].close()  # Docker keeps running; all source heartbeat and renewal traffic ceases.
            pump(agents[1:], lambda: hub.job(job)["state"] == "succeeded")
            final = hub.job(job)
            self.assertEqual((final["node_id"], final["generation"], final["metrics"]["step"]), ("b", 2, 8))
            status = subprocess.check_output(["docker", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", containers[0]], text=True)
            self.assertEqual(status.strip(), "false 75")
