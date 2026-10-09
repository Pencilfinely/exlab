import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from expman import python_stack_observation as stack, stack_probe_helper as helper
from tests.support import temporary_directory


class StackTests(unittest.TestCase):
    def setUp(self):
        self.identity = "b"*64
        self.aux = "c"*64
        self.record = {"id": "a"*32, "attempt": 1}
        self.container = {"Id": self.identity, "Image": "sha256:"+"e"*64,
                          "HostConfig": {"PidMode": ""},
                          "State": {"Status": "running"}}
        self.calls = []
        self.labels = {}
        def execute(argv, **kwargs):
            self.calls.append((argv, kwargs))
            out = ""
            if argv[:2] == ["docker", "create"]:
                self.labels = dict(argv[i+1].split("=", 1) for i,x in enumerate(argv) if x == "--label")
                out = self.aux
            elif argv[:2] == ["docker", "start"]:
                out = json.dumps({"mode": "python-stack-nonblocking-v1", "processes": []})
            elif argv[:2] == ["docker", "inspect"]:
                out = json.dumps([{"Id": self.aux, "Config": {"Labels": self.labels}}])
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        self.agent = SimpleNamespace(config={"node_id": "node"}, _inspect=lambda _:self.container,
                                     _exec=execute)
        self.files = patch.object(stack, "_files", return_value=(Path("/safe/sampler"),Path("/safe/helper.py")))
        self.files.start()
        self.addCleanup(self.files.stop)

    def value(self):
        return {"container":{"id":self.identity,"status":"running"},
                "log":{"changed_at":100},
                "diagnostics":{"checked_at":500,"processes":"processes",
                 "stats":json.dumps({"CPUPerc":"101%"}), "gpu":"GPU-x, 0, 100, 8000, P8"}}

    def test_gate_requires_stale_readable_log_busy_cpu_idle_gpu(self):
        v=self.value()
        self.assertTrue(stack.eligible(v,500))
        self.assertFalse(stack.eligible(v,200))
        for change in [lambda x:x["log"].update(error="timeout"),
                       lambda x:x["container"].update(status="paused"),
                       lambda x:x["diagnostics"].update(gpu="GPU-x, 90, 100, 8000, P0"),
                       lambda x:x["diagnostics"].update(stats='{"CPUPerc":"0%"}'),
                       lambda x:x["diagnostics"].update(errors={"gpu":"timeout"})]:
            v=self.value();change(v);self.assertFalse(stack.eligible(v,500))

    def test_sampler_never_mounts_science_or_shares_host_and_cleanup_is_auxiliary_only(self):
        out=stack.probe(self.agent,self.record,self.identity)
        self.assertEqual(out["mode"],"python-stack-nonblocking-v1")
        create=self.calls[0][0]
        self.assertIn("--pid=container:"+self.identity,create)
        self.assertNotIn("--pid=host",create)
        self.assertIn("--network=none",create)
        self.assertIn("--read-only",create)
        self.assertIn("--cap-drop=ALL",create)
        self.assertIn("--cap-add=SYS_PTRACE",create)
        self.assertEqual(sum(x=="--mount" for x in create),2)
        self.assertEqual(self.calls[-1][0],["docker","rm","--force",self.aux])
        self.assertFalse(any(x in a for a,_ in self.calls for x in ("stop","restart","pause","exec","kill")))

    def test_bad_identity_shared_namespace_or_image_prevents_create(self):
        for field,value in [("Id","d"*64),("Image","mutable:latest"),("HostConfig",{"PidMode":"host"}),
                            ("HostConfig",{"PidMode":"container:other"}),("State",{"Status":"paused"}),
                            ("State",{"Status":"running","Paused":True})]:
            old=self.container[field];self.container[field]=value;self.calls=[]
            result=stack.probe(self.agent,self.record,self.identity)
            self.assertEqual(result["error"],"sampling_unavailable")
            self.assertFalse(any(a[:2]==["docker","create"] for a,_ in self.calls))
            self.container[field]=old

    def test_cleanup_refuses_unrelated_labels_or_target_identity(self):
        original=self.agent._exec
        for actual,labels in [(self.aux,{}),(self.identity,{"match":True})]:
            self.calls=[]
            def execute(argv,**kwargs):
                if argv[:2]==["docker","inspect"]:
                    self.calls.append((argv,kwargs))
                    return SimpleNamespace(returncode=0,stdout=json.dumps([{"Id":actual,"Config":{"Labels":self.labels if labels else {}}}]))
                return original(argv,**kwargs)
            self.agent._exec=execute
            stack.probe(self.agent,self.record,self.identity)
            self.assertFalse(any(a[:2]==["docker","rm"] for a,_ in self.calls))

    def test_timeout_does_not_stop_target_and_error_hides_command(self):
        original=self.agent._exec
        def execute(argv,**kwargs):
            if argv[:2]==["docker","start"]:
                raise subprocess.TimeoutExpired("secret",12)
            return original(argv,**kwargs)
        self.agent._exec=execute
        result=stack.probe(self.agent,self.record,self.identity)
        self.assertEqual(result,{"mode":"python-stack-nonblocking-v1","error":"timeout"})
        self.assertEqual(self.calls[-1][0],["docker","rm","--force",self.aux])

    def test_rate_limit_attempt_and_container_fencing_and_old_receiver(self):
        from expman.runtime_observation import validate
        with patch.object(stack,"probe",return_value={"mode":"python-stack-nonblocking-v1","processes":[]}) as probe:
            for timestamp in (500,560,620):
                v=self.value();stack.observe(self.agent,self.record,v,timestamp)
                self.assertIn(stack.MARKER,v["diagnostics"]["processes"])
            self.assertEqual(probe.call_count,1)
            self.record["attempt"]=2
            v=self.value();stack.observe(self.agent,self.record,v,700)
            self.assertEqual(probe.call_count,2)
            v.update(version=1,attempt=2,observed_at=700)
            validate(v,2)
            v["container"]["id"]="d"*64;stack.observe(self.agent,self.record,v,710)
            self.assertEqual(probe.call_count,3)

    def test_trace_redacts_locals_arguments_paths_and_unknown_fields(self):
        result=helper.summarize([{"active":True,"owns_gil":True,"thread_name":"secret","process_info":"secret",
                  "frames":[{"name":"foo","filename":"/private/user/project/mod.py","line":42,"locals":["secret"]}]}])
        self.assertEqual(result,[{"active":True,"owns_gil":True,"frames":[{"file":"mod.py","function":"foo","line":42}]}])
        self.assertNotIn("secret",json.dumps(result))
        self.assertEqual(helper.summarize({"secret":True}),[])

    def test_probe_file_hash_guard_and_installed_vendor_included(self):
        from expman import worker_service
        entries,marker,digest=worker_service._installation_payload()
        paths={p.as_posix() for p,_ in entries}
        self.assertIn("vendor/py-spy-0.4.2-linux-x64.bin",paths)
        self.assertIn("vendor/py-spy-LICENSE.txt",paths)
        for name,data in entries:
            if name.as_posix()=="vendor/py-spy-0.4.2-linux-x64.bin":
                import hashlib
                self.assertEqual(hashlib.sha256(data).hexdigest(),stack.BINARY_SHA256)
        self.files.stop()
        with temporary_directory() as temp:
            self.agent.root=Path(temp)
            binary,helper_file=stack._files(self.agent)
            binary.chmod(0o700);binary.write_bytes(b"bad")
            with self.assertRaises(ValueError):
                stack._files(self.agent)
            helper_file.chmod(0o600)


if __name__=="__main__":
    unittest.main()
