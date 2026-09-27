import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memreport.py"
spec = importlib.util.spec_from_file_location("memreport", SCRIPT)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


def proc(pid, kind="shim", rss=12, pss=8, container=None, inside=False):
    return m.ProcInfo(pid, kind, kind, "/host", rss, pss, None, None,
                      container, inside)


def service():
    s = m.Service("go-alpine", "go", "alpine", "a" * 64, "sha256:test",
                  status="Up 1 minute", http_ok=True)
    s.cg = m.CgroupMem(path="/sys/fs/cgroup/docker-test.scope", current=100,
                       inactive_file=20, pids=5)
    return s


def data():
    s = service()
    shim = proc(2, container=s.name)
    s.procs = [shim]
    daemon = proc(1, "dockerd", rss=60, pss=50)
    env = {
        "timestamp": "2026-09-27", "kernel": "Linux", "cpus": 8,
        "docker": {"server_version": "test", "cgroup_version": "2"},
        "host_mem": {"used": 1000, "available": 2000, "total": 3000},
        "epoch": 100, "boot_id": "boot", "host_used_formula": "formula",
        "have_pss": True,
    }
    baseline = {"env": dict(env), "host_mem": dict(env["host_mem"]),
                "epoch": 90, "running_memtest_containers": 0,
                "docker_procs": [m.asdict(proc(1, "dockerd", rss=40, pss=30))]}
    return {"env": env, "services": [s], "all_docker_procs": [daemon, shim],
            "shared_procs": [daemon], "baseline": baseline, "samples": 3,
            "requests_per_service": 50}


class AccountingTests(unittest.TestCase):
    def test_working_set_and_included_host_processes(self):
        d = data()
        s = d["services"][0]
        inside = proc(3, inside=True, container=s.name)
        unrelated = proc(4)
        s.procs.append(inside)
        d["all_docker_procs"] += [inside, unrelated]
        t = m.accounting(d)
        self.assertEqual(s.cg.working_set, 80)
        self.assertEqual(s.host_tax, 8)
        self.assertEqual(s.total, 88)
        self.assertEqual(t["estimated_total"], 138)
        self.assertEqual(t["unattributed_host_processes"], 8)
        self.assertIn("unattributed", " ".join(m.baseline_issues(d)))

    def test_complete_partial_and_absent_pss(self):
        self.assertEqual(m.memory_metric([proc(1, pss=0)]), "PSS")
        self.assertEqual(m.memory_metric([proc(1), proc(2, pss=None)]), "mixed PSS/RSS")
        self.assertEqual(m.memory_metric([proc(1, pss=None)]), "RSS")
        self.assertEqual(m.best(proc(1, pss=0)), 0)
        self.assertEqual(m.memory_metric([]), "unavailable")

    def test_incremental_subtracts_baseline_daemon(self):
        d = data()
        self.assertEqual(m.baseline_issues(d), [])
        with patch.object(m, "mib", side_effect=lambda value: str(value)):
            report = m.render(d)
        self.assertIn("**108 MiB** = total 138 − baseline shared daemons 30 (PSS)", report)

    def test_rss_baseline_cannot_be_subtracted_from_pss(self):
        d = data()
        d["baseline"]["docker_procs"][0]["pss"] = None
        report = m.render(d)
        self.assertIn("Incremental comparison unavailable", report)
        self.assertNotIn("Estimated increase over", report)

    def test_unknown_boot_age_and_running_baseline_rejected(self):
        d = data()
        del d["baseline"]["epoch"]
        del d["baseline"]["env"]["boot_id"]
        d["baseline"]["running_memtest_containers"] = 1
        issues = " ".join(m.baseline_issues(d))
        self.assertIn("boot identity", issues)
        self.assertIn("age is unknown", issues)
        self.assertIn("stopped", issues)

    def test_daemon_restart_rejects_baseline(self):
        d = data()
        d["baseline"]["docker_procs"][0]["pid"] = 99
        self.assertIn("identities differ", " ".join(m.baseline_issues(d)))

    def test_partial_pss_table_is_not_partial_sum(self):
        d = data()
        d["all_docker_procs"].append(proc(3, pss=None, container="go-alpine"))
        report = m.render(d)
        self.assertIn("mixed PSS/RSS", report)
        self.assertIn("n/a", report)

    def test_json_totals_match_accounting(self):
        d = data()
        out = json.loads(m.to_json(d))
        self.assertEqual(out["accounting"], m.accounting(d))
        self.assertEqual(out["services"][0]["total"], 88)
        self.assertEqual(out["baseline_issues"], [])

    def test_cgroup_descendant_excluded_without_substring_matches(self):
        s = service()
        p = proc(1)
        p.cgroup = "/docker-test.scope/child"
        with patch.object(m, "docker", return_value=s.container_id), \
                patch.object(m, "proc_cmdline", return_value="shim -id " + s.container_id):
            m.attribute_procs([s], [p])
            self.assertTrue(p.inside_container_cgroup)
            self.assertEqual(s.host_tax, 0)
            p.cgroup = "/docker-test.scope-other"
            m.attribute_procs([s], [p])
        self.assertFalse(p.inside_container_cgroup)
        self.assertEqual(len(s.procs), 1)
        self.assertEqual(s.host_tax, 8)


class CollectionTests(unittest.TestCase):
    def test_incomplete_matrix_is_fatal(self):
        with patch.object(m, "environment", return_value=data()["env"]), \
                patch.object(m, "discover_services", return_value=[service()]), \
                patch.object(m, "docker", return_value=json.dumps({"services": {"go-alpine": {}, "go-ubuntu": {}}})), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.collect(0, 0, 1)

    def test_report_requires_inventory(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(m, "INVENTORY", Path(tmp) / "missing.json"), \
                patch.object(m, "environment", return_value=data()["env"]), \
                patch.object(m, "discover_services", return_value=[service()]), \
                patch.object(m, "docker", return_value=json.dumps({"services": {"go-alpine": {}}})), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.collect(0, 0, 1)

    def test_inventory_probes_require_a_new_container_lifetime(self):
        s = service()
        with tempfile.TemporaryDirectory() as tmp:
            inventory = Path(tmp) / "inventory.json"
            inventory.write_text(json.dumps({"epoch": 100, "images": {s.image: {"runtime_version": "test"}}}))
            replies = [json.dumps({"services": {s.name: {}}}), json.dumps([{
                "State": {"Running": True, "Pid": 10, "StartedAt": "1970-01-01T00:00:50.123456789Z"},
                "Image": s.image,
            }])]
            with patch.object(m, "INVENTORY", inventory), \
                    patch.object(m, "environment", return_value=data()["env"]), \
                    patch.object(m, "discover_services", return_value=[s]), \
                    patch.object(m, "docker", side_effect=replies), \
                    contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
                m.collect(0, 0, 1)
            self.assertIn("restart after inventory", errors.getvalue())

    def test_unreadable_cgroup_is_not_zero(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                m.collect_cgroup(Path(d))

    def test_endpoint_body_must_match_exactly(self):
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, size):
                return body
        for body, expected in [(b"Hello World", True), (b"Hello World\n", False),
                               (b"unrelated server", False)]:
            with self.subTest(body=body), patch.object(m.urllib.request, "urlopen", return_value=Response()):
                self.assertEqual(m.hit("8080")[0], expected)

    def test_failed_warmup_is_fatal(self):
        s = service()
        s.port = "8080"
        with patch.object(m, "hit", return_value=(False, "bad body")), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.warm([s], 2)

    def test_baseline_with_running_stack_is_fatal(self):
        with patch.object(m, "discover_services", return_value=[service()]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.cmd_baseline(None)

    def test_host_process_churn_is_fatal(self):
        with patch.object(m, "all_pids", side_effect=[[1], [1, 2]]), \
                patch.object(m, "classify", return_value=("dockerd", "dockerd")), \
                patch.object(m, "proc_mem", return_value={"rss": 10, "pss": 5, "private": 2, "swap": 0}), \
                patch.object(m, "proc_cgroup", return_value="/host"), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.collect_docker_procs(2, interval=0)

    def test_invalid_cli_arguments_fail_before_collection(self):
        for args in [("--samples", "0"), ("--requests", "-1"), ("--settle", "nan"),
                     ("--settle", "-1"), ("--settle", "inf")]:
            with self.subTest(args=args):
                cp = subprocess.run([sys.executable, str(SCRIPT), "report", *args],
                                    capture_output=True, text=True)
                self.assertEqual(cp.returncode, 2)


class HistoricalEvidenceTests(unittest.TestCase):
    def test_original_totals_reproduce_but_baseline_is_incompatible(self):
        source = SCRIPT.parents[1] / "docs/evidence/original-205526.json"
        d = json.loads(source.read_text())
        services = []
        for raw in d["services"]:
            s = m.Service(**{k: v for k, v in raw.items() if k in m.Service.__dataclass_fields__})
            s.cg = m.CgroupMem(**raw["cgroup"])
            s.procs = [m.ProcInfo(**p) for p in raw["host_procs"]]
            self.assertEqual(s.total, raw["total"])
            services.append(s)
        d["services"] = services
        d["all_docker_procs"] = [m.ProcInfo(**p) for p in d["all_docker_procs"]]
        d["shared_procs"] = [m.ProcInfo(**p) for p in d["shared_procs"]]
        t = m.accounting(d)
        self.assertEqual(len(services), 10)
        self.assertEqual(round(t["estimated_total"] / m.MIB, 1), 292.7)
        self.assertEqual(round(t["attributed_host_tax"] / m.MIB, 1), 68.2)
        self.assertIn("metrics differ", " ".join(m.baseline_issues(d)))


if __name__ == "__main__":
    unittest.main()
