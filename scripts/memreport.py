#!/usr/bin/env python3
"""Estimate the memory footprint of the docker-mem-test stack.

Three layers are accounted for separately, because "how much RAM does this
cost me" is not answered by `docker stats` alone:

  1. container cgroups  -- what each service's processes actually charged
  2. per-container host tax -- the containerd shim + docker-proxy that the
     daemon spawns for every container / published port
  3. shared daemon      -- dockerd + containerd themselves

The combined estimate is not exact physical-memory accounting. An incremental
estimate can be compared with a host delta only with a compatible baseline.

Usage:
  memreport.py baseline                 # run with the stack DOWN
  memreport.py report [--requests N]    # run with the stack UP
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
BASELINE = REPORTS / "baseline.json"
INVENTORY = REPORTS / "inventory.json"
CGROUP_ROOT = Path("/sys/fs/cgroup")
PAGE = os.sysconf("SC_PAGE_SIZE")

# Host-side processes that exist only because Docker is running.
DAEMON_PATTERNS = {
    "dockerd": ("dockerd",),
    "containerd": ("containerd",),
    "shim": ("containerd-shim-runc-v2", "containerd-shim"),
    "proxy": ("docker-proxy",),
}
# dockerd/containerd are shared by the whole host; shims/proxies are per-container.
SHARED_KINDS = ("dockerd", "containerd")
PER_CONTAINER_KINDS = ("shim", "proxy")

MIB = 1024 * 1024


# --------------------------------------------------------------------------- #
# shelling out
# --------------------------------------------------------------------------- #

DOCKER: list[str] = []


def resolve_docker() -> list[str]:
    """Return the argv prefix that can actually talk to the daemon."""
    global DOCKER
    if DOCKER:
        return DOCKER
    exe = shutil.which("docker")
    if not exe:
        die("docker not found on PATH")
    for candidate in ([exe], ["sudo", "-n", exe]):
        try:
            subprocess.run(
                candidate + ["version", "--format", "{{.Server.Version}}"],
                capture_output=True, timeout=30, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            continue
        DOCKER = candidate
        return DOCKER
    die(
        "cannot reach the docker daemon as this user.\n"
        "  Fix with:  sudo addgroup --system docker && "
        "sudo adduser $USER docker && newgrp docker\n"
        "  Or re-run this script under sudo."
    )
    return []  # unreachable


def docker(*args: str, check: bool = True) -> str:
    cp = subprocess.run(
        resolve_docker() + list(args), capture_output=True, text=True, timeout=120
    )
    if check and cp.returncode != 0:
        die(f"docker {' '.join(args)} failed:\n{cp.stderr.strip()}")
    return cp.stdout


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# /proc and cgroup readers
# --------------------------------------------------------------------------- #

def read_kv(path: Path, *, unit_kb: bool = False) -> dict[str, int]:
    """Parse `key value` files (memory.stat, /proc/meminfo, smaps_rollup)."""
    out: dict[str, int] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) < 2:
            continue
        try:
            val = int(parts[1])
        except ValueError:
            continue
        out[parts[0]] = val * 1024 if unit_kb else val
    return out


def read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def meminfo() -> dict[str, int]:
    return read_kv(Path("/proc/meminfo"), unit_kb=True)


def host_mem() -> dict[str, int]:
    mi = meminfo()
    total, avail, free = mi.get("MemTotal", 0), mi.get("MemAvailable", 0), mi.get("MemFree", 0)
    return {
        "total": total,
        "available": avail,
        "free": free,
        # Explicit legacy cache-excluding formula; not modern free(1)'s used.
        "used": total - free - mi.get("Buffers", 0) - mi.get("Cached", 0) - mi.get("SReclaimable", 0),
        "committed": mi.get("Committed_AS", 0),
    }


def proc_mem(pid: int) -> dict[str, int | None]:
    """RSS from statm and PSS/USS from smaps_rollup when permissions allow.

    PSS matters here: the 10 shims and 10 proxies are the same binary, so their
    RSS figures triple-count shared text. PSS divides shared pages by the number
    of sharers, which is what you want for a "total footprint" number.
    """
    m: dict[str, int | None] = {"rss": None, "pss": None, "private": None, "swap": None}
    statm = read_ints(Path(f"/proc/{pid}/statm"))
    if len(statm) >= 2:
        m["rss"] = statm[1] * PAGE
    roll = read_kv(Path(f"/proc/{pid}/smaps_rollup"), unit_kb=True)
    if roll:
        m["pss"] = roll.get("Pss")
        m["private"] = roll.get("Private_Clean", 0) + roll.get("Private_Dirty", 0)
        m["swap"] = roll.get("Swap")
    return m


def read_ints(path: Path) -> list[int]:
    try:
        return [int(x) for x in path.read_text().split()]
    except (OSError, ValueError):
        return []


def proc_name(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", "replace"
        ).strip()
    except OSError:
        return ""


def proc_cgroup(pid: int) -> str:
    """cgroup v2 path of a pid, as seen from our cgroup namespace."""
    try:
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
            f = line.split(":", 2)
            if len(f) == 3 and f[1] == "":  # 0::/path  -> the v2 hierarchy
                return f[2]
    except OSError:
        pass
    return ""


def all_pids() -> list[int]:
    return sorted(int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit())


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #

@dataclass
class ProcInfo:
    pid: int
    kind: str
    comm: str
    cgroup: str
    rss: int | None
    pss: int | None
    private: int | None
    swap: int | None
    container: str | None = None  # which container this shim/proxy belongs to
    inside_container_cgroup: bool = False
    samples: list[dict] = field(default_factory=list)


@dataclass
class CgroupMem:
    path: str = ""
    current: int = 0
    peak: int | None = None
    anon: int = 0
    file: int = 0
    inactive_file: int = 0
    kernel: int = 0
    slab: int = 0
    sock: int = 0
    pagetables: int = 0
    kernel_stack: int = 0
    swap: int = 0
    pids: int = 0

    @property
    def working_set(self) -> int:
        """Docker-style working-set heuristic: charged bytes minus inactive_file."""
        return max(0, self.current - self.inactive_file)


@dataclass
class Service:
    name: str
    lang: str
    base: str
    container_id: str
    image: str
    image_size: int = 0
    port: str = ""
    pid: int = 0
    status: str = ""
    http_ok: bool = False
    http_body: str = ""
    cg: CgroupMem = field(default_factory=CgroupMem)
    procs: list[ProcInfo] = field(default_factory=list)   # shim + proxy for this one
    processes: list[dict] = field(default_factory=list)   # in-container processes
    provenance: dict = field(default_factory=dict)

    @property
    def host_tax(self) -> int:
        """Shim + proxy cost attributable to this container (PSS where available)."""
        return sum(best(p) for p in self.procs if not p.inside_container_cgroup)

    @property
    def total(self) -> int:
        return self.cg.working_set + self.host_tax


def best(p: ProcInfo) -> int:
    v = p.pss if p.pss is not None else p.rss
    return v or 0


def memory_metric(procs) -> str:
    """Never label a partial PSS / RSS sum as PSS."""
    values = [p.pss if isinstance(p, ProcInfo) else p.get("pss") for p in procs]
    if not values:
        return "unavailable"
    if all(v is not None for v in values):
        return "PSS"
    return "mixed PSS/RSS" if any(v is not None for v in values) else "RSS"


# --------------------------------------------------------------------------- #
# collection
# --------------------------------------------------------------------------- #

def cgroup_dir_for(pid: int) -> Path | None:
    rel = proc_cgroup(pid)
    if rel and rel != "/":
        cand = CGROUP_ROOT / rel.lstrip("/")
        if (cand / "memory.current").exists():
            return cand
    return None


def cgroup_dir_by_id(cid: str) -> Path | None:
    """Fallback when /proc/<pid>/cgroup is unhelpful (nested cgroup namespace)."""
    for pat in (f"docker-{cid}.scope", cid):
        try:
            cp = subprocess.run(
                ["find", str(CGROUP_ROOT), "-maxdepth", "6", "-type", "d", "-name", pat],
                capture_output=True, text=True, timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in cp.stdout.split():
            if (Path(line) / "memory.current").exists():
                return Path(line)
    return None


def collect_cgroup(d: Path) -> CgroupMem:
    st = read_kv(d / "memory.stat")
    cur = read_int(d / "memory.current")
    if cur is None or not {"anon", "file", "inactive_file"} <= st.keys():
        die(f"missing required cgroup memory data: {d}")
    kernel = st.get("kernel")
    if kernel is None:  # older kernels: approximate from the components
        kernel = (
            st.get("slab", 0) + st.get("kernel_stack", 0)
            + st.get("pagetables", 0) + st.get("percpu", 0) + st.get("sock", 0)
        )
    return CgroupMem(
        path=str(d),
        current=cur,
        peak=read_int(d / "memory.peak"),
        anon=st.get("anon", 0),
        file=st.get("file", 0),
        inactive_file=st.get("inactive_file", 0),
        kernel=kernel,
        slab=st.get("slab", 0),
        sock=st.get("sock", 0),
        pagetables=st.get("pagetables", 0),
        kernel_stack=st.get("kernel_stack", 0),
        swap=read_int(d / "memory.swap.current") or 0,
        pids=read_int(d / "pids.current") or 0,
    )


def classify(pid: int) -> tuple[str, str] | None:
    comm = proc_name(pid)
    if not comm:
        return None
    for kind, names in DAEMON_PATTERNS.items():
        if comm in names:
            return kind, comm
    # snap packaging sometimes truncates comm; fall back to argv[0]
    cmd = proc_cmdline(pid)
    if not cmd:
        return None
    argv0 = cmd.split()[0].rsplit("/", 1)[-1]
    for kind, names in DAEMON_PATTERNS.items():
        if argv0 in names:
            return kind, comm
    return None


def _median(vals: list[int | None]) -> int | None:
    vs = sorted(v for v in vals if v is not None)
    if not vs:
        return None
    mid = len(vs) // 2
    return vs[mid] if len(vs) % 2 else (vs[mid - 1] + vs[mid]) // 2


def collect_docker_procs(samples: int = 3, interval: float = 2.0) -> list[ProcInfo]:
    """Sample host-side Docker processes several times and take the median.

    dockerd is a Go program: it allocates hard while restoring image state and
    then its scavenger hands pages back. Measured on this host, a freshly
    restarted dockerd read 96 MiB, then 42 MiB, then 50 MiB within ~15 seconds.
    A single sample is therefore worth +/-50 MiB of noise -- larger than most of
    the effects this report tries to resolve -- so never trust just one.
    """
    obs: dict[int, dict[str, list[int | None]]] = {}
    meta: dict[int, tuple[str, str, str]] = {}
    expected = None
    for i in range(max(1, samples)):
        if i:
            time.sleep(interval)
        observed = set()
        for pid in all_pids():
            c = classify(pid)
            if not c:
                continue
            kind, comm = c
            observed.add(pid)
            m = proc_mem(pid)
            if m["rss"] is None:
                die(f"cannot read RSS for Docker process {pid}; retry on a stable host")
            slot = obs.setdefault(pid, {k: [] for k in ("rss", "pss", "private", "swap")})
            for k in slot:
                slot[k].append(m[k])
            meta.setdefault(pid, (kind, comm, proc_cgroup(pid)))
        if expected is not None and observed != expected:
            die("Docker processes changed during sampling; retry on a stable host")
        expected = observed

    out = []
    for pid, slot in obs.items():
        kind, comm, cg = meta[pid]
        out.append(ProcInfo(
            pid=pid, kind=kind, comm=comm, cgroup=cg,
            rss=_median(slot["rss"]),
            pss=_median(slot["pss"]) if all(v is not None for v in slot["pss"]) else None,
            private=_median(slot["private"]), swap=_median(slot["swap"]),
            samples=[{k: slot[k][i] for k in slot} for i in range(len(slot["rss"]))],
        ))
    out.sort(key=lambda p: p.pid)
    return out


def in_container_procs(cid: str) -> list[dict]:
    raw = docker("top", cid, "-o", "pid,rss,comm", check=False)
    rows = []
    for line in raw.splitlines()[1:]:
        f = line.split(None, 2)
        if len(f) == 3 and f[1].isdigit():
            rows.append({"pid": int(f[0]), "rss": int(f[1]) * 1024, "comm": f[2].strip()})
    return rows


def discover_services() -> list[Service]:
    config = json.loads(docker("compose", "-f", str(ROOT / "docker-compose.yml"),
                               "config", "--format", "json"))
    fmt = "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Label \"memtest.lang\"}}\t{{.Label \"memtest.base\"}}\t{{.Status}}\t{{.Ports}}"
    raw = docker("ps", "-a", "--no-trunc", "--filter", "label=memtest.lang",
                 "--filter", f"label=com.docker.compose.project={config['name']}",
                 "--format", fmt)
    svcs = []
    for line in raw.splitlines():
        f = line.split("\t")
        if len(f) < 7:
            continue
        cid, name, image, lang, base, status, ports = f[:7]
        svcs.append(Service(
            name=name.removeprefix("memtest-"), lang=lang, base=base,
            container_id=cid, image=image, status=status,
            port=(re.search(r":(\d+)->", ports).group(1) if re.search(r":(\d+)->", ports) else ""),
        ))
    svcs.sort(key=lambda s: (s.lang, s.base))
    return svcs


def hit(port: str, timeout: float = 3.0) -> tuple[bool, str]:
    url = f"http://127.0.0.1:{port}/api/hello"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(64)
            return r.status == 200 and body == b"Hello World", body.decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return False, f"{type(e).__name__}: {e}"


def warm(svcs: list[Service], n: int) -> None:
    """Exercise the endpoint serially; this does not establish steady state."""
    for s in svcs:
        if not s.port:
            continue
        for _ in range(n):
            ok, body = hit(s.port, timeout=5.0)
            if not ok:
                die(f"{s.name}: warm-up failed: {body}")


def wait_ready(svcs: list[Service], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    pending = [s for s in svcs if s.port]
    while pending and time.monotonic() < deadline:
        pending = [s for s in pending if not hit(s.port, timeout=1.0)[0]]
        if pending:
            time.sleep(1.0)
    if pending:
        die("endpoints not ready: " + ", ".join(s.name for s in pending))


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #

def mib(b: int | None) -> str:
    if b is None:
        return "n/a"
    return f"{b / MIB:,.1f}"


def table(headers: list[str], rows: list[list[str]], align: str = "") -> str:
    align = align or "l" * len(headers)
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    def fmt(cells):
        body = " | ".join(
            c.rjust(widths[i]) if align[i] == "r" else c.ljust(widths[i])
            for i, c in enumerate(cells)
        )
        return f"| {body} |"
    sep = "|" + "|".join(
        (("-" * (widths[i] + 1)) + ":") if align[i] == "r" else (":" + "-" * (widths[i] + 1))
        for i in range(len(headers))
    ) + "|"
    return "\n".join([fmt(headers), sep] + [fmt(r) for r in rows])


# --------------------------------------------------------------------------- #
# environment
# --------------------------------------------------------------------------- #

def environment() -> dict:
    def d(*a):
        return docker(*a, check=False).strip()

    uname = subprocess.run(["uname", "-sr"], capture_output=True, text=True).stdout.strip()
    info = {}
    for k, tmpl in (
        ("server_version", "{{.ServerVersion}}"),
        ("storage_driver", "{{.Driver}}"),
        ("cgroup_driver", "{{.CgroupDriver}}"),
        ("cgroup_version", "{{.CgroupVersion}}"),
        ("runtime", "{{.DefaultRuntime}}"),
        ("id", "{{.ID}}"),
        ("rootless", "{{.SecurityOptions}}"),
    ):
        info[k] = d("info", "--format", tmpl)
    return {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "kernel": uname,
        "cpus": os.cpu_count(),
        "docker_cmd": " ".join(resolve_docker()),
        "docker": info,
        "host_mem": host_mem(),
        # Set properly in collect()/cmd_baseline() by probing a *daemon* pid:
        # our own smaps_rollup is always readable and would give a false positive.
        "have_pss": False,
        "euid": os.geteuid(),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "epoch": time.time(),
        "host_used_formula": "MemTotal-MemFree-Buffers-Cached-SReclaimable",
    }


# --------------------------------------------------------------------------- #
# baseline
# --------------------------------------------------------------------------- #

def cmd_baseline(args) -> int:
    svcs = [s for s in discover_services() if "Up" in s.status]
    if svcs:
        die("benchmark containers are running; stop them before taking a baseline")
    procs = collect_docker_procs(args.samples)
    env = environment()
    env["have_pss"] = memory_metric(procs) == "PSS"
    snap = {
        "env": env,
        "host_mem": host_mem(),
        "docker_procs": [asdict(p) for p in procs],
        "docker_procs_total": sum(best(p) for p in procs),
        "docker_procs_metric": memory_metric(procs),
        "samples": args.samples,
        "running_memtest_containers": len(svcs),
        "epoch": time.time(),
    }
    REPORTS.mkdir(exist_ok=True)
    BASELINE.write_text(json.dumps(snap, indent=2) + "\n")
    hm = snap["host_mem"]
    print(f"baseline saved -> {BASELINE.relative_to(ROOT)}")
    print(f"  host used          : {mib(hm['used'])} MiB of {mib(hm['total'])} MiB")
    print(f"  docker daemon procs: {len(procs)} process(es), "
          f"{mib(snap['docker_procs_total'])} MiB ({snap['docker_procs_metric']})")
    return 0


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def cmd_inventory(args) -> int:
    """Probe versions separately: exec would otherwise pollute measured cgroups."""
    images = {}
    for s in discover_services():
        if "Up" not in s.status:
            die("start the benchmark containers before recording inventory")
        ins = json.loads(docker("inspect", s.container_id))[0]
        record = {
            "os_release": docker("exec", s.container_id, "cat", "/etc/os-release").strip(),
        }
        version_cmd = {
            "python": ("python3", "--version"), "typescript": ("node", "--version"),
            "csharp": ("dotnet", "--list-runtimes"),
        }.get(s.lang)
        if version_cmd:
            record["runtime_version"] = docker("exec", s.container_id, *version_cmd).strip()
        else:
            record["runtime_version"] = "compiler version not collected"
            record["binary_sha256"] = docker(
                "exec", s.container_id, "sha256sum", "/usr/local/bin/hello").split()[0]
        images[ins["Image"]] = record
    if not images:
        die("no benchmark containers found")
    REPORTS.mkdir(exist_ok=True)
    INVENTORY.write_text(json.dumps({"epoch": time.time(), "images": images}, indent=2) + "\n")
    print("Inventory saved. Stop the stack, take a baseline, and start it again before reporting.")
    return 0


def attribute_procs(svcs: list[Service], procs: list[ProcInfo]) -> list[ProcInfo]:
    """Assign each shim/proxy to a container; return the leftover shared procs."""
    for s in svcs:
        s.procs.clear()
    long_ids = {}
    for s in svcs:
        full = docker("inspect", "-f", "{{.Id}}", s.container_id, check=False).strip()
        if full:
            long_ids[full] = s
    cgroup_paths = {s.cg.path: s for s in svcs if s.cg.path}

    shared = []
    for p in procs:
        p.container = None
        p.inside_container_cgroup = any(
            p.cgroup and (CGROUP_ROOT / p.cgroup.lstrip("/")) == Path(path)
            or p.cgroup and Path(path) in (CGROUP_ROOT / p.cgroup.lstrip("/")).parents
            for path in cgroup_paths
        )
        if p.kind in SHARED_KINDS:
            shared.append(p)
            continue
        cmd = proc_cmdline(p.pid)
        owner = None
        if p.kind == "shim":
            for full, s in long_ids.items():
                if full in cmd or full[:12] in cmd:
                    owner = s
                    break
            if owner is None:  # shims are also identifiable by their cgroup
                for path, s in cgroup_paths.items():
                    if Path(path).name.split("docker-")[-1].removesuffix(".scope")[:12] in p.cgroup:
                        owner = s
                        break
        elif p.kind == "proxy":
            m = re.search(r"-host-port\s+(\d+)", cmd)
            if m:
                owner = next((s for s in svcs if s.port == m.group(1)), None)
        if owner is None:
            shared.append(p)
            continue
        p.container = owner.name
        owner.procs.append(p)
    return shared


def collect(requests: int, settle: float = 2.0, samples: int = 3) -> dict:
    env = environment()
    if env["docker"]["cgroup_version"] != "2":
        die("this report requires a local Linux Docker daemon with cgroup v2")
    svcs = discover_services()
    config = json.loads(docker("compose", "-f", str(ROOT / "docker-compose.yml"),
                               "config", "--format", "json"))
    expected = set(config["services"])
    if {s.name for s in svcs} != expected or len(svcs) != len(expected):
        die("incomplete or duplicate benchmark matrix; expected: " + ", ".join(sorted(expected)))
    inventory = json.loads(INVENTORY.read_text()) if INVENTORY.exists() else None
    if not inventory:
        die("record versions with `scripts/report.sh inventory`, then stop/start the stack")
    for s in svcs:
        ins = json.loads(docker("inspect", s.container_id))[0]
        state = ins["State"]
        if not state["Running"] or state.get("Paused") or state.get("Restarting"):
            die(f"{s.name}: container must be running and unpaused")
        s.pid = state["Pid"]
        s.image = ins["Image"]  # immutable running image, not a possibly retagged name
        record = inventory["images"].get(s.image)
        if record is None:
            die(f"{s.name}: running image is absent from inventory; record inventory again")
        # Docker timestamps include nanoseconds, beyond strptime's precision.
        stamp = re.sub(r"(\.\d{6})\d+", r"\1", state["StartedAt"])
        started_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if started_at.timestamp() <= inventory["epoch"]:
            die(f"{s.name}: restart after inventory probes to avoid their cgroup cache charges")
        img = json.loads(docker("image", "inspect", s.image))[0]
        s.image_size = img["Size"]
        s.provenance = {
            **record, "inventory_epoch": inventory["epoch"],
            "image_id": s.image, "repo_digests": img.get("RepoDigests", []),
            "architecture": img.get("Architecture"), "started_at": state["StartedAt"],
            "restart_count": ins["RestartCount"], "user": ins["Config"].get("User"),
            "limits": {k: ins["HostConfig"].get(k) for k in
                       ("Memory", "MemorySwap", "NanoCpus", "CpuQuota", "CpusetCpus")},
        }
        labels = ins["Config"].get("Labels", {})
        expected_labels = config["services"][s.name]["labels"]
        if any(labels.get(k) != expected_labels[k] for k in ("memtest.lang", "memtest.base")):
            die(f"{s.name}: runtime/base labels do not match Compose")
        d = cgroup_dir_for(s.pid) or cgroup_dir_by_id(s.container_id)
        if not d or s.container_id not in str(d):
            die(f"{s.name}: cannot identify its local cgroup; run on the Docker host")
        s.cg.path = str(d)
        if not s.port:
            die(f"{s.name}: no published endpoint port")
    wait_ready(svcs)
    warm(svcs, requests)
    for s in svcs:
        s.http_ok, s.http_body = hit(s.port)
        if not s.http_ok:
            die(f"{s.name}: endpoint validation failed: {s.http_body}")
    # Version probes ran in an earlier container lifetime. No exec during report.
    time.sleep(settle)
    started = time.time()
    for s in svcs:
        s.cg = collect_cgroup(Path(s.cg.path))
        s.processes = in_container_procs(s.container_id)
    procs = collect_docker_procs(samples)
    if not any(p.kind == "dockerd" for p in procs):
        die("no local dockerd process visible; cannot measure host overhead")
    env["have_pss"] = memory_metric(procs) == "PSS"
    shared = attribute_procs(svcs, procs)
    env["host_mem"] = host_mem()
    env["epoch"] = time.time()
    env["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S %z")
    for s in svcs:
        ins = json.loads(docker("inspect", s.container_id))[0]
        if (not ins["State"]["Running"] or ins["State"].get("Paused")
                or ins["State"]["Pid"] != s.pid
                or ins["State"]["StartedAt"] != s.provenance["started_at"]
                or ins["RestartCount"] != s.provenance["restart_count"]):
            die(f"{s.name}: container changed during measurement; retry")
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else None
    return {
        "schema_version": 2, "env": env, "services": svcs,
        "shared_procs": shared, "all_docker_procs": procs, "baseline": base,
        "requests_per_service": requests, "settle_seconds": settle, "samples": samples,
        "sample_interval_seconds": 2.0, "measurement_started_epoch": started,
        "measurement_finished_epoch": env["epoch"],
    }


def accounting(data: dict) -> dict:
    up = [s for s in data["services"] if "Up" in s.status]
    outside = [p for p in data["all_docker_procs"] if not p.inside_container_cgroup]
    assigned = [p for p in outside if p.container is not None and p.kind in PER_CONTAINER_KINDS]
    shared = [p for p in outside if p.kind in SHARED_KINDS]
    unassigned = [p for p in outside if p.kind in PER_CONTAINER_KINDS and p.container is None]
    ws = sum(s.cg.working_set for s in up)
    tax, daemon = sum(map(best, assigned)), sum(map(best, shared))
    return {
        "containers_working_set": ws, "containers_current": sum(s.cg.current for s in up),
        "containers_swap": sum(s.cg.swap for s in up),
        "attributed_host_tax": tax, "shared_daemon": daemon,
        "unattributed_host_processes": sum(map(best, unassigned)),
        "estimated_total": ws + tax + daemon,
        "host_metric": memory_metric(assigned + shared),
    }


def baseline_issues(data: dict) -> list[str]:
    base = data.get("baseline")
    if not base:
        return ["No baseline recorded."]
    issues = []
    env, before = data["env"], base["env"]
    if base.get("running_memtest_containers") != 0:
        issues.append("Baseline was not taken with the benchmark stopped.")
    if not before.get("boot_id") or before.get("boot_id") != env.get("boot_id"):
        issues.append("Baseline boot identity is missing or differs.")
    if before.get("host_used_formula") != env.get("host_used_formula"):
        issues.append("Baseline host-memory formula is missing or differs.")
    if before.get("docker") != env.get("docker"):
        issues.append("Docker environment differs from baseline.")
    now_procs = data["all_docker_procs"]
    old_procs = base.get("docker_procs", [])
    metric = memory_metric(now_procs)
    if metric not in ("PSS", "RSS") or memory_metric(old_procs) != metric:
        issues.append("Baseline and current host metrics differ or have partial PSS coverage.")
    old_daemons = {(p["kind"], p["pid"]) for p in old_procs if p["kind"] in SHARED_KINDS}
    new_daemons = {(p.kind, p.pid) for p in now_procs if p.kind in SHARED_KINDS}
    if old_daemons != new_daemons or not new_daemons:
        issues.append("Shared daemon process identities differ or are unavailable.")
    if any(p["kind"] in PER_CONTAINER_KINDS for p in old_procs):
        issues.append("Baseline contains other container shims/proxies.")
    if any(p.kind in PER_CONTAINER_KINDS and p.container is None for p in now_procs):
        issues.append("Current sample contains unattributed shims/proxies.")
    if "epoch" not in base:
        issues.append("Baseline age is unknown.")
    elif base["epoch"] > env.get("epoch", 0):
        issues.append("Baseline is newer than the measurement.")
    return issues


def render(data: dict) -> str:
    env, svcs = data["env"], data["services"]
    up = [s for s in svcs if "Up" in s.status]
    totals = accounting(data)
    metric = totals["host_metric"]
    base = data.get("baseline")
    issues = baseline_issues(data)
    L: list[str] = []
    A = L.append
    A(f"# Docker memory report: {len(up)} minimal `/api/hello` services")
    A("")
    A(f"_{env['timestamp']}_ · kernel `{env['kernel']}` · {env['cpus']} CPUs · "
      f"Docker `{env['docker']['server_version']}` · cgroup {env['docker']['cgroup_version']}")
    A("")
    A(f"Idle after **{data['requests_per_service']} sequential warm-up requests per service**, "
      f"readiness/validation requests, and **{data.get('settle_seconds', 'unrecorded')} seconds** "
      "of settling. This does not establish steady state or memory under concurrent load.")
    A("")
    A(f"Container cgroups are sampled once, sequentially. Host processes use per-process "
      f"medians of **{data['samples']} samples**, {data.get('sample_interval_seconds', 2)}s apart. "
      "Their sum is not a simultaneous snapshot or a median of stack totals.")
    A("")
    if metric != "PSS":
        A(f"> Host totals use **{metric}**. RSS includes shared resident pages in every "
          "process; mixed totals must not be presented as PSS. Missing PSS is not zero.")
        A("")

    A("## 1. Per-container memory")
    A("")
    A("WS = max(0, memory.current − inactive_file), the usual Docker CLI cache adjustment. "
      "This is a charged-memory heuristic, not an application's minimum RAM requirement. "
      "anon/file/kernel describe raw current memory, not additive components of WS. "
      "Tasks include threads; peak is the cgroup's recorded lifetime peak, not this run's peak.")
    A("")
    rows = [[s.name, mib(s.cg.current), mib(s.cg.working_set), mib(s.cg.anon),
             mib(s.cg.file), mib(s.cg.kernel), mib(s.cg.swap), mib(s.cg.peak),
             str(s.cg.pids), mib(s.host_tax), mib(s.total), "ok" if s.http_ok else "FAIL"]
            for s in up]
    A(table(["service", "current", "WS", "anon", "file", "kernel", "swap", "peak",
             "tasks", "host tax", "estimate", "endpoint"], rows, "lrrrrrrrrrrl"))
    A("")
    A(f"Memory is MiB. Host tax is attributed shim/proxy {metric} outside container "
      "cgroups. Estimate = WS + host tax; shared daemons are added only in section 4.")
    A("")

    A("## 2. Alpine vs Ubuntu")
    A("")
    rows = []
    for lang in sorted({s.lang for s in up}):
        a = next((s for s in up if s.lang == lang and s.base == "alpine"), None)
        u = next((s for s in up if s.lang == lang and s.base == "ubuntu"), None)
        if a and u:
            rows.append([lang, mib(a.cg.working_set), mib(u.cg.working_set),
                         f"{(u.cg.working_set - a.cg.working_set) / MIB:+.1f}",
                         mib(a.image_size), mib(u.image_size)])
    A(table(["language", "Alpine WS", "Ubuntu WS", "Ubuntu − Alpine", "Alpine image", "Ubuntu image"],
            rows, "lrrrrr"))
    A("")
    A("Image figures are Docker image inspect .Size for the running image ID, in MiB. "
      "They are not unique disk usage or download sizes; shared layers prevent summing "
      "them into a physical disk total. These are packaged-runtime comparisons, not "
      "controlled tests of libc alone.")
    A("")

    A("## 3. Host-side processes")
    A("")
    A("All discovered Docker-named processes are shown for audit. Only attributed "
      "shims/proxies enter service totals. Shared dockerd/containerd memory is global "
      "to the host and may also serve unrelated workloads.")
    A("")
    rows = []
    for kind in DAEMON_PATTERNS:
        ps = [p for p in data["all_docker_procs"] if p.kind == kind]
        if ps:
            rows.append([kind, str(len(ps)), mib(sum(p.rss or 0 for p in ps)),
                         mib(sum(p.pss for p in ps)) if all(p.pss is not None for p in ps) else "n/a",
                         memory_metric(ps)])
    A(table(["process", "count", "RSS", "PSS (complete only)", "coverage"], rows, "lrrrl"))
    A("")
    excluded = [p for p in data["all_docker_procs"]
                if p.kind in PER_CONTAINER_KINDS and p.container is None]
    if excluded:
        A(f"> {len(excluded)} unattributed shim/proxy processes are excluded from the stack total "
          f"({mib(totals['unattributed_host_processes'])} MiB outside its cgroups).")
        A("")
    if any(p.inside_container_cgroup for p in data["all_docker_procs"]):
        A("> Host processes inside a measured container cgroup are excluded from host tax "
          "and the shared-daemon addition to avoid counting them twice.")
        A("")

    A("## 4. Estimated footprint and baseline comparison")
    A("")
    A(table(["component", "MiB"], [
        ["Container working sets", mib(totals["containers_working_set"])],
        ["Attributed host tax", mib(totals["attributed_host_tax"])],
        ["Shared daemons (entire measured footprint)", mib(totals["shared_daemon"])],
        ["Estimated total including shared daemons", mib(totals["estimated_total"])],
    ], "lr"))
    A("")
    A(f"Raw container current memory: **{mib(totals['containers_current'])} MiB**; "
      f"subtracted inactive_file: **{mib(totals['containers_current'] - totals['containers_working_set'])} MiB**; "
      f"container swap: **{mib(totals['containers_swap'])} MiB**, excluded from RAM totals.")
    A("")
    A("This combines cgroup charges with process PSS/RSS, which use different shared-page "
      "accounting. It excludes some host kernel/network allocations and is not an exact "
      "physical-memory total. The entire shared daemon footprint is not the incremental "
      "cost of starting these containers.")
    A("")
    hm = env["host_mem"]
    if base:
        bhm = base["host_mem"]
        delta = hm["used"] - bhm["used"]
        A(table(["host measure", "baseline MiB", "now MiB", "delta MiB"], [
            ["Cache-excluding used", mib(bhm["used"]), mib(hm["used"]), f"{delta / MIB:+.1f}"],
            ["MemAvailable", mib(bhm["available"]), mib(hm["available"]),
             f"{(hm['available'] - bhm['available']) / MIB:+.1f}"],
        ], "lrrr"))
        A("")
        A("Cache-excluding used = MemTotal − MemFree − Buffers − Cached − SReclaimable "
          "(not modern free(1)'s used). Host deltas include unrelated activity and are "
          "not ground truth for this stack.")
        A("")
        if issues:
            A("> **Incremental comparison unavailable:** " + " ".join(issues))
        else:
            baseline_daemon = sum(p["pss"] if metric == "PSS" else p["rss"]
                                  for p in base["docker_procs"] if p["kind"] in SHARED_KINDS)
            incremental = totals["estimated_total"] - baseline_daemon
            A(f"Estimated increase over the stopped-stack baseline: "
              f"**{mib(incremental)} MiB** = total {mib(totals['estimated_total'])} "
              f"− baseline shared daemons {mib(baseline_daemon)} ({metric}). "
              f"Compare this increase with the host delta **{delta / MIB:+.1f} MiB**, "
              "allowing for different accounting and host activity.")
        if base.get("epoch") is not None:
            age = env.get("epoch", base["epoch"]) - base["epoch"]
            A("")
            A(f"Baseline age at measurement: {age / 60:.1f} minutes. "
              "Long gaps, builds, and unrelated work weaken the comparison.")
    else:
        A("No baseline recorded; no incremental stack cost can be inferred.")
    A("")

    A("## 5. Provenance and limitations")
    A("")
    rows = []
    for s in up:
        p = s.provenance
        version = p.get("runtime_version", "unrecorded").replace("\n", "; ").replace("|", "/")
        rows.append([s.name, version, p.get("image_id", s.image)])
    A(table(["service", "runtime", "running image ID"], rows))
    A("")
    A("JSON also records OS release, start time, architecture, configured limits, and "
      "compiled-binary hashes when collected. Version probes are recorded by image ID "
      "before restarting the stack, to avoid polluting measured cgroups. Compiler "
      "versions for Rust/Go are not inferred from mutable Dockerfile tags.")
    A("")
    A("- Versions, package builds, allocator, linking, server implementation and GC policy "
      "can all affect a row. Matching a major version does not isolate the base OS.")
    A("- Shared file pages are charged to a cgroup, not divided like PSS; startup order "
      "and other workloads can change attribution. Settling does not guarantee cache aging.")
    A("- CPU/RAM visibility and limits can alter runtime behavior; lower limits do not "
      "guarantee proportionally lower usage. Repeat comparable runs before ranking small gaps.")
    A("- The benchmark measures this trivial endpoint while idle after serial traffic. "
      "It does not compare throughput, latency, or production concurrency.")
    A("")
    A("Accounting references: [Docker stats](https://docs.docker.com/reference/cli/docker/container/stats/), "
      "[cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html), "
      "[Linux /proc](https://docs.kernel.org/filesystems/proc.html).")
    A("")
    return "\n".join(L) + "\n"
def to_json(data: dict) -> str:
    out = dict(data)
    out["accounting"] = accounting(data)
    out["baseline_issues"] = baseline_issues(data)
    out["services"] = [
        {**{k: v for k, v in asdict(s).items() if k not in ("cg", "procs")},
         "cgroup": asdict(s.cg),
         "working_set": s.cg.working_set,
         "host_tax": s.host_tax,
         "total": s.total,
         "host_procs": [asdict(p) for p in s.procs]}
        for s in data["services"]
    ]
    out["shared_procs"] = [asdict(p) for p in data["shared_procs"]]
    out["all_docker_procs"] = [asdict(p) for p in data["all_docker_procs"]]
    return json.dumps(out, indent=2, default=str) + "\n"


def cmd_report(args) -> int:
    data = collect(args.requests, args.settle, args.samples)
    md, js = render(data), to_json(data)
    REPORTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path in (REPORTS / f"memory-{stamp}.md", REPORTS / "memory-latest.md"):
        path.write_text(md)
    for path in (REPORTS / f"memory-{stamp}.json", REPORTS / "memory-latest.json"):
        path.write_text(js)
    if not args.quiet:
        print(md)
    print(f"[written] reports/memory-{stamp}.md, reports/memory-latest.md "
          f"(+ .json)", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inventory", help="record runtime versions; restart the stack afterwards")
    b = sub.add_parser("baseline", help="snapshot host+daemon memory with the stack DOWN")
    b.add_argument("--samples", type=int, default=5,
                   help="median over N samples of host-side processes (default: 5)")
    r = sub.add_parser("report", help="measure the running stack and write a report")
    r.add_argument("--requests", type=int, default=50,
                   help="warm-up requests per service before measuring (default: 50)")
    r.add_argument("--settle", type=float, default=2.0,
                   help="seconds to wait after warm-up before sampling (default: 2). "
                        "Cache aging and runtime steady state are not guaranteed.")
    r.add_argument("--samples", type=int, default=5,
                   help="median over N samples of host-side processes, 2s apart "
                        "(default: 5). dockerd RSS swings tens of MiB; one sample is noise.")
    r.add_argument("--quiet", action="store_true", help="write files without printing")
    args = ap.parse_args()
    if hasattr(args, "samples") and args.samples < 1:
        ap.error("--samples must be at least 1")
    if args.cmd == "report":
        if args.requests < 0:
            ap.error("--requests must be nonnegative")
        if not math.isfinite(args.settle) or args.settle < 0:
            ap.error("--settle must be finite and nonnegative")
    return {"inventory": cmd_inventory, "baseline": cmd_baseline, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
