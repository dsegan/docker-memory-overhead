#!/usr/bin/env python3
"""Measure the *full* memory impact of the docker-mem-test stack.

Three layers are accounted for separately, because "how much RAM does this
cost me" is not answered by `docker stats` alone:

  1. container cgroups  -- what each service's processes actually charged
  2. per-container host tax -- the containerd shim + docker-proxy that the
     daemon spawns for every container / published port
  3. shared daemon      -- dockerd + containerd themselves

Layer 2 is the one people forget; with 10 containers it is not a rounding
error. All three are summed into a grand total, which is then cross-checked
against the host's own MemUsed delta versus a baseline snapshot.

Usage:
  memreport.py baseline                 # run with the stack DOWN
  memreport.py report [--requests N]    # run with the stack UP
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
BASELINE = REPORTS / "baseline.json"
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
        # "used" the way `free` computes it: total - free - buffers/cache reclaimable
        "used": total - free - mi.get("Buffers", 0) - mi.get("Cached", 0) - mi.get("SReclaimable", 0),
        "committed": mi.get("Committed_AS", 0),
    }


def proc_mem(pid: int) -> dict[str, int | None]:
    """RSS from statm (always readable) and PSS/USS from smaps_rollup (root only).

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
        """What `docker stats` shows: charged bytes minus reclaimable page cache."""
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

    @property
    def host_tax(self) -> int:
        """Shim + proxy cost attributable to this container (PSS where available)."""
        return sum(best(p) for p in self.procs)

    @property
    def total(self) -> int:
        return self.cg.working_set + self.host_tax


def best(p: ProcInfo) -> int:
    v = p.pss if p.pss is not None else p.rss
    return v or 0


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
    cur = read_int(d / "memory.current") or 0
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


def collect_docker_procs() -> list[ProcInfo]:
    out = []
    for pid in all_pids():
        c = classify(pid)
        if not c:
            continue
        kind, comm = c
        m = proc_mem(pid)
        out.append(ProcInfo(
            pid=pid, kind=kind, comm=comm, cgroup=proc_cgroup(pid),
            rss=m["rss"], pss=m["pss"], private=m["private"], swap=m["swap"],
        ))
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
    fmt = "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Label \"memtest.lang\"}}\t{{.Label \"memtest.base\"}}\t{{.Status}}\t{{.Ports}}"
    raw = docker("ps", "-a", "--filter", "label=memtest.lang", "--format", fmt)
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
            return r.status == 200, r.read(64).decode("utf-8", "replace").strip()
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return False, f"{type(e).__name__}: {e}"


def warm(svcs: list[Service], n: int) -> None:
    """Force lazy init (JIT tiers, GC heaps, thread pools) to actually happen.

    Without this the .NET and Node numbers are pre-JIT fiction.
    """
    for s in svcs:
        if not s.port:
            continue
        for _ in range(n):
            hit(s.port, timeout=5.0)


def wait_ready(svcs: list[Service], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    pending = [s for s in svcs if s.port]
    while pending and time.monotonic() < deadline:
        pending = [s for s in pending if not hit(s.port, timeout=1.0)[0]]
        if pending:
            time.sleep(1.0)


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
    }


# --------------------------------------------------------------------------- #
# baseline
# --------------------------------------------------------------------------- #

def cmd_baseline(_args) -> int:
    svcs = [s for s in discover_services() if "Up" in s.status]
    if svcs:
        print(
            "warning: %d memtest container(s) are still running; a baseline taken now\n"
            "         will already include them. Run `docker compose down` first.\n"
            % len(svcs),
            file=sys.stderr,
        )
    procs = collect_docker_procs()
    env = environment()
    env["have_pss"] = any(p.pss is not None for p in procs)
    snap = {
        "env": env,
        "host_mem": host_mem(),
        "docker_procs": [asdict(p) for p in procs],
        "docker_procs_total_pss": sum(best(p) for p in procs),
        "running_memtest_containers": len(svcs),
    }
    REPORTS.mkdir(exist_ok=True)
    BASELINE.write_text(json.dumps(snap, indent=2) + "\n")
    hm = snap["host_mem"]
    print(f"baseline saved -> {BASELINE.relative_to(ROOT)}")
    print(f"  host used          : {mib(hm['used'])} MiB of {mib(hm['total'])} MiB")
    print(f"  docker daemon procs: {len(procs)} process(es), "
          f"{mib(snap['docker_procs_total_pss'])} MiB")
    return 0


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def attribute_procs(svcs: list[Service], procs: list[ProcInfo]) -> list[ProcInfo]:
    """Assign each shim/proxy to a container; return the leftover shared procs."""
    by_id = {s.container_id: s for s in svcs}
    long_ids = {}
    for s in svcs:
        full = docker("inspect", "-f", "{{.Id}}", s.container_id, check=False).strip()
        if full:
            long_ids[full] = s
    cgroup_paths = {s.cg.path: s for s in svcs if s.cg.path}

    shared = []
    for p in procs:
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
        p.inside_container_cgroup = bool(
            owner.cg.path and p.cgroup and Path(owner.cg.path).name in p.cgroup
        )
        owner.procs.append(p)
    return shared


def collect(requests: int, settle: float = 2.0) -> dict:
    env = environment()
    svcs = discover_services()
    if not svcs:
        die("no memtest containers found -- run `docker compose up -d --build` first")

    running = [s for s in svcs if "Up" in s.status]
    wait_ready(running)
    if requests > 0:
        warm(running, requests)
        time.sleep(settle)

    for s in svcs:
        s.image_size = int(
            docker("image", "inspect", "-f", "{{.Size}}", s.image, check=False).strip() or 0
        )
        if "Up" not in s.status:
            continue
        s.pid = int(docker("inspect", "-f", "{{.State.Pid}}", s.container_id).strip() or 0)
        d = (cgroup_dir_for(s.pid) if s.pid else None) or cgroup_dir_by_id(s.container_id)
        if d:
            s.cg = collect_cgroup(d)
        s.processes = in_container_procs(s.container_id)
        if s.port:
            s.http_ok, s.http_body = hit(s.port)

    procs = collect_docker_procs()
    env["have_pss"] = any(p.pss is not None for p in procs)
    shared = attribute_procs(svcs, procs)

    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else None

    return {
        "env": env,
        "services": svcs,
        "shared_procs": shared,
        "all_docker_procs": procs,
        "baseline": base,
        "requests_per_service": requests,
    }


def render(data: dict) -> str:
    env, svcs = data["env"], data["services"]
    shared, base = data["shared_procs"], data["baseline"]
    up = [s for s in svcs if "Up" in s.status]
    L: list[str] = []
    A = L.append

    metric = "PSS" if env["have_pss"] else "RSS"

    A("# Docker memory report: 10 minimal `/api/hello` services")
    A("")
    A(f"_{env['timestamp']}_ &middot; kernel `{env['kernel']}` &middot; "
      f"{env['cpus']} CPUs &middot; docker `{env['docker']['server_version']}` "
      f"(cgroup {env['docker']['cgroup_version']}, driver `{env['docker']['cgroup_driver']}`, "
      f"storage `{env['docker']['storage_driver']}`)")
    A("")
    A(f"Each service was warmed with **{data['requests_per_service']} requests** before "
      "measurement, so runtimes that allocate lazily (.NET JIT tiers, V8, CPython "
      "imports) are shown in their real steady state, not pre-first-request.")
    if not env["have_pss"]:
        A("")
        A("> **Running unprivileged:** `/proc/<pid>/smaps_rollup` is unreadable, so "
          "host-side daemon processes are reported as **RSS**. Because the 10 shims and "
          "10 proxies are the same binaries, RSS double-counts their shared text — the "
          "host-tax figures below are therefore an **over**estimate. Re-run under "
          "`sudo` for PSS.")
    A("")

    # ---- 1. per-service ---------------------------------------------------- #
    A("## 1. Per-container memory")
    A("")
    A("`working set` is what `docker stats` reports (`memory.current` less reclaimable "
      "page cache) and is the number to compare. `anon` is the runtime's own heap/stack; "
      "`file` is page cache charged to the cgroup (mostly the mapped binary and libs); "
      "`kernel` is slab + page tables + socket buffers the kernel charged to it.")
    A("")
    rows = []
    for s in up:
        rows.append([
            s.name, s.lang, s.base,
            mib(s.cg.working_set), mib(s.cg.anon), mib(s.cg.file), mib(s.cg.kernel),
            mib(s.cg.peak), str(s.cg.pids), mib(s.host_tax), mib(s.total),
            "ok" if s.http_ok else "FAIL",
        ])
    A(table(
        ["service", "lang", "base", "working set", "anon", "file", "kernel",
         "peak", "procs", "host tax", "TOTAL", "/api/hello"],
        rows,
        align="lllrrrrrrrrl",
    ))
    A("")
    A(f"All memory values are MiB. `host tax` = this container's containerd-shim + "
      f"docker-proxy ({metric}); `TOTAL` = working set + host tax.")
    A("")

    down = [s for s in svcs if "Up" not in s.status]
    if down:
        A(f"> Not running: {', '.join(f'`{s.name}` ({s.status})' for s in down)}")
        A("")

    bad = [s for s in up if not s.http_ok]
    if bad:
        A("> **Endpoint failures:** " + "; ".join(f"`{s.name}`: {s.http_body}" for s in bad))
        A("")

    # ---- 2. alpine vs ubuntu ---------------------------------------------- #
    A("## 2. Alpine vs Ubuntu, per language")
    A("")
    rows = []
    langs = sorted({s.lang for s in up})
    for lang in langs:
        a = next((s for s in up if s.lang == lang and s.base == "alpine"), None)
        u = next((s for s in up if s.lang == lang and s.base == "ubuntu"), None)
        if not (a and u):
            continue
        dws = u.cg.working_set - a.cg.working_set
        pct = (dws / a.cg.working_set * 100) if a.cg.working_set else 0
        rows.append([
            lang, mib(a.cg.working_set), mib(u.cg.working_set),
            f"{dws / MIB:+,.1f}", f"{pct:+.1f}%",
            f"{a.image_size / MIB:,.0f}", f"{u.image_size / MIB:,.0f}",
            f"{(u.image_size - a.image_size) / MIB:+,.0f}",
        ])
    A(table(
        ["lang", "alpine WS", "ubuntu WS", "Δ WS", "Δ %", "alpine img", "ubuntu img", "Δ img"],
        rows, align="lrrrrrrr",
    ))
    A("")
    A("WS = working set (MiB), img = image size on disk (MiB).")
    A("")
    for bname in ("alpine", "ubuntu"):
        grp = [s for s in up if s.base == bname]
        if grp:
            A(f"- **{bname}** ({len(grp)} services): "
              f"working set {mib(sum(s.cg.working_set for s in grp))} MiB, "
              f"images {sum(s.image_size for s in grp) / MIB:,.0f} MiB")
    A("")

    # ---- 3. language ranking ---------------------------------------------- #
    A("## 3. Language ranking (working set, lower is better)")
    A("")
    rows = [
        [str(i), s.name, mib(s.cg.working_set), mib(s.cg.anon), str(s.cg.pids),
         f"{s.image_size / MIB:,.0f}"]
        for i, s in enumerate(sorted(up, key=lambda x: x.cg.working_set), 1)
    ]
    A(table(["#", "service", "working set", "anon", "procs", "image"], rows, align="rlrrrr"))
    A("")

    # ---- 4. host / daemon overhead ---------------------------------------- #
    A("## 4. Host-side Docker overhead")
    A("")
    A("Memory the stack costs **outside** every container cgroup. `dockerd` and "
      "`containerd` are paid once; shims and proxies are paid *per container* and "
      "*per published port* respectively, which is what makes a 10-service stack more "
      "expensive than `docker stats` suggests.")
    A("")
    by_kind: dict[str, list[ProcInfo]] = {}
    for p in data["all_docker_procs"]:
        by_kind.setdefault(p.kind, []).append(p)
    rows = []
    for kind in ("dockerd", "containerd", "shim", "proxy"):
        ps = by_kind.get(kind, [])
        if not ps:
            continue
        rows.append([
            kind, str(len(ps)),
            mib(sum(p.rss or 0 for p in ps)),
            mib(sum(p.pss or 0 for p in ps)) if env["have_pss"] else "n/a",
            mib(sum(best(p) for p in ps)),
            "once" if kind in SHARED_KINDS else "per container/port",
        ])
    A(table(["process", "count", "Σ RSS", "Σ PSS", f"Σ {metric} (used)", "scaling"],
            rows, align="lrrrrl"))
    A("")
    nproxy = len(by_kind.get("proxy", []))
    nport = len([s for s in up if s.port])
    if nproxy > nport:
        A(f"> **{nproxy} `docker-proxy` processes for {nport} published ports** — Docker "
          "starts one per address family, so every `ports:` entry costs two, binding "
          "`0.0.0.0` and `::` separately. That doubling is pure overhead on a host with "
          "no working IPv6. Publishing on an explicit `127.0.0.1:PORT:8080`, or using "
          "`network_mode: host`, avoids it.")
        A("")

    shim_dbl = [p for p in data["all_docker_procs"] if p.inside_container_cgroup]
    if shim_dbl:
        A(f"> Note: {len(shim_dbl)} shim/proxy process(es) live *inside* a container's "
          "own cgroup on this host, so their memory is already counted in that "
          "container's working set. They are excluded from the grand total below to "
          "avoid double-counting.")
        A("")

    if base:
        bd = base["docker_procs_total_pss"]
        A(f"With the stack **down**, the same processes used {mib(bd)} MiB "
          f"({len(base['docker_procs'])} process(es)). Bringing 10 containers up added "
          f"{mib(sum(best(p) for p in data['all_docker_procs']) - bd)} MiB of host-side "
          "Docker memory.")
        A("")

    # ---- 5. grand total --------------------------------------------------- #
    A("## 5. Full-stack total")
    A("")
    containers_ws = sum(s.cg.working_set for s in up)
    containers_cur = sum(s.cg.current for s in up)
    per_ct = sum(best(p) for p in data["all_docker_procs"]
                 if p.kind in PER_CONTAINER_KINDS and not p.inside_container_cgroup)
    shared_mem = sum(best(p) for p in shared if p.kind in SHARED_KINDS)
    grand = containers_ws + per_ct + shared_mem

    rows = [
        [f"Containers ({len(up)}) — working set", mib(containers_ws),
         f"{containers_ws / grand * 100:.0f}%" if grand else "-"],
        ["Per-container host tax — shims + proxies", mib(per_ct),
         f"{per_ct / grand * 100:.0f}%" if grand else "-"],
        ["Shared daemon — dockerd + containerd", mib(shared_mem),
         f"{shared_mem / grand * 100:.0f}%" if grand else "-"],
        ["**Grand total**", f"**{mib(grand)}**", "100%"],
    ]
    A(table(["layer", f"MiB ({metric} for host procs)", "share"], rows, align="lrr"))
    A("")
    A(f"Containers' raw `memory.current` (page cache included) totals "
      f"{mib(containers_cur)} MiB; the working-set total above excludes "
      f"{mib(containers_cur - containers_ws)} MiB of reclaimable cache.")
    A("")

    hm = env["host_mem"]
    if base:
        delta = hm["used"] - base["host_mem"]["used"]
        A("### Cross-check against host RAM")
        A("")
        A(table(
            ["measure", "baseline (stack down)", "now (stack up)", "Δ"],
            [
                ["MemUsed", mib(base["host_mem"]["used"]), mib(hm["used"]),
                 f"{delta / MIB:+,.1f}"],
                ["MemAvailable", mib(base["host_mem"]["available"]), mib(hm["available"]),
                 f"{(hm['available'] - base['host_mem']['available']) / MIB:+,.1f}"],
            ],
            align="lrrr",
        ))
        A("")
        A(f"Bottom-up accounting says **{mib(grand)} MiB**; the host's own MemUsed moved "
          f"by **{delta / MIB:+,.1f} MiB**.")
        A("")
        nhost = len([p for p in data["all_docker_procs"]])
        if not env["have_pss"] and grand > delta:
            A(f"The bottom-up figure is the **upper bound**, and the gap is expected: "
              f"without root it sums **RSS** over {nhost} host-side processes that are "
              "mostly the same two binaries, so every shared text page is counted once "
              "per process. The host's MemUsed delta counts each physical page once and "
              f"is the figure to trust — treat **{delta / MIB:,.0f} MiB** as the real cost "
              "of this stack. Re-run as `sudo scripts/report.sh report` to replace RSS "
              "with PSS and close the gap.")
        else:
            A("These will not match exactly — the host figure also absorbs unrelated "
              "activity and kernel allocations no cgroup is charged for — but they should "
              "agree closely. A large gap means something outside this accounting moved.")
    else:
        A("### Cross-check against host RAM")
        A("")
        A(f"Host MemUsed is currently {mib(hm['used'])} MiB of {mib(hm['total'])} MiB. "
          "No baseline found, so the delta attributable to this stack cannot be shown — "
          "run `scripts/report.sh baseline` with the stack down, then bring it up and "
          "re-report.")
    A("")

    # ---- 6. caveats ------------------------------------------------------- #
    A("## 6. How to read this / caveats")
    A("")
    A("- **Working set is not a reservation.** `anon` is real; `file` is page cache the "
      "kernel will drop under pressure. Set `mem_limit` and these numbers shrink.")
    A("- **Runtimes size themselves against the host.** Go's GC, .NET's GC heap count "
      f"and V8's heap all scale with visible CPUs ({env['cpus']} here) and total RAM "
      f"({mib(hm['total'])} MiB). The same images on a 2-vCPU box measure lower.")
    A("- **Runtime versions are matched across both bases.** Ubuntu 26.04 ships CPython "
      "3.14 and Node 22, the same majors as `python:3.14-alpine3.22` and "
      "`node:22-alpine3.22`; Rust, Go and .NET are built from one toolchain each. So a "
      "row's alpine/ubuntu gap is base image and libc, not a version difference.")
    A("- **musl vs glibc is a real variable,** not just image size: alpine builds link "
      "musl, which allocates differently from glibc. Rust and .NET on alpine are "
      "genuinely different binaries, not the same binary on a smaller base. Go is the "
      "exception -- `CGO_ENABLED=0` puts one identical static binary on both.")
    A("- **Page cache ages.** `working set` excludes only *inactive* file pages. Sampled "
      "seconds after start, every mapped page is still in the active LRU, so working set "
      "equals `memory.current` and is at its maximum. Raise `--settle` for a figure "
      "closer to steady state.")
    A("- **One-shot sample.** No averaging over time; a GC cycle mid-measurement moves "
      "a row by megabytes. Re-run to confirm anything surprising.")
    A("")
    return "\n".join(L) + "\n"


def to_json(data: dict) -> str:
    out = dict(data)
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
    data = collect(args.requests, args.settle)
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
    sub.add_parser("baseline", help="snapshot host+daemon memory with the stack DOWN")
    r = sub.add_parser("report", help="measure the running stack and write a report")
    r.add_argument("--requests", type=int, default=50,
                   help="warm-up requests per service before measuring (default: 50)")
    r.add_argument("--settle", type=float, default=2.0,
                   help="seconds to wait after warm-up before sampling (default: 2). "
                        "Higher values let file pages age into the inactive LRU, which is "
                        "what makes working set < memory.current.")
    r.add_argument("--quiet", action="store_true", help="write files without printing")
    args = ap.parse_args()
    return {"baseline": cmd_baseline, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
