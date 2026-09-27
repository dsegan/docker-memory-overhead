# docker-mem-test

Ten minimal containers, each serving exactly one endpoint:

```
GET /api/hello  ->  200  "Hello World"
```

Five languages × two runtime bases (**alpine 3.22** and **Ubuntu 26.04**), plus
tooling that reports the *full* memory cost of running them — containers,
per-container daemon overhead, and the Docker daemon itself.

## Layout

```
rust/  go/  python/  typescript/  csharp/
    Dockerfile.alpine     # alpine 3.22 runtime base
    Dockerfile.ubuntu     # ubuntu 26.04 runtime base
    <sources>             # shared by both variants
docker-compose.yml        # all 10 services, ports 8001-8010
scripts/
    report.sh             # wrapper (handles sudo for PSS)
    memreport.py          # the measurement + report generator
reports/                  # generated: memory-latest.{md,json} + timestamped
```

| service | port | server |
|:--|--:|:--|
| `rust-alpine` / `rust-ubuntu` | 8001 / 8002 | axum 0.8 on tokio (Rust 1.93) |
| `go-alpine` / `go-ubuntu` | 8003 / 8004 | `net/http` stdlib (Go 1.26) |
| `python-alpine` / `python-ubuntu` | 8005 / 8006 | `http.server` stdlib (CPython 3.14) |
| `typescript-alpine` / `typescript-ubuntu` | 8007 / 8008 | `node:http` stdlib (Node 22) |
| `csharp-alpine` / `csharp-ubuntu` | 8009 / 8010 | ASP.NET Core minimal API (.NET 10) |

Each language uses its own platform's built-in HTTP server so the comparison is
about *runtime* footprint, not framework choice. Rust is the exception — it has
no stdlib HTTP server, so axum (the de-facto minimal choice) is used.

All containers run as uid 65534 (`nobody`) and are **deliberately
unconstrained** — no `mem_limit`, no `cpus`. The point is to measure what each
runtime *chooses* to allocate, not what a cap forced it into.

## Usage

```bash
# 1. baseline: host + daemon memory with nothing running
docker compose down
scripts/report.sh baseline

# 2. build and start all ten
docker compose up -d --build

# 3. measure
scripts/report.sh report                  # 50 warm-up requests per service
scripts/report.sh report --requests 500   # push runtimes further into steady state
```

The report lands in `reports/memory-latest.md` (human) and
`reports/memory-latest.json` (machine), plus a timestamped copy of each.

Sanity check one endpoint by hand:

```bash
curl -s localhost:8003/api/hello   # -> Hello World
```

## What the report measures

`docker stats` only shows layer 1 of three. All three are reported separately
and summed:

1. **Container cgroups** — read straight from cgroup v2 (`memory.current`,
   `memory.stat`, `memory.peak`), broken into `anon` (the runtime's real
   heap/stack), `file` (page cache charged to the cgroup — mapped binaries and
   libraries), and `kernel` (slab, page tables, socket buffers). *Working set* =
   `memory.current − inactive_file`, which is the number `docker stats` prints
   and the one to compare across rows.

2. **Per-container host tax** — Docker spawns a `containerd-shim-runc-v2` for
   every container and a `docker-proxy` for every published port. Neither lives
   in the container's cgroup, so neither appears in `docker stats`. At ten
   containers this is not a rounding error.

3. **Shared daemon** — `dockerd` + `containerd`, paid once regardless of scale.

Host-side processes are measured by **PSS** when the script can read
`/proc/<pid>/smaps_rollup` (root only). PSS divides shared pages by the number
of processes sharing them, which is the honest way to total twenty copies of the
same two binaries. Without root the script falls back to RSS, says so in the
report, and its host-tax figures are then an overestimate.

Finally the bottom-up total is **cross-checked** against the host's own
`MemUsed` delta versus the baseline snapshot. The two won't match exactly — the
host figure also absorbs unrelated activity and kernel allocations charged to no
cgroup — but they should agree to the same order of magnitude. A wide gap means
something outside the accounting moved.

## Fairness notes

Read these before quoting any number.

- **Runtime versions are matched across both bases.** Ubuntu 26.04 ships CPython
  3.14.3 and Node 22.22 — the same majors as `python:3.14-alpine3.22` and
  `node:22-alpine3.22` — so neither needs a third-party apt source, and no row
  is version-skewed. Rust, Go and .NET are each built from a single toolchain
  and produce the same output for both bases.
- **The matrix is pinned to Ubuntu 26.04 and Alpine 3.22 together.** Those two
  are coupled: Microsoft publishes no Ubuntu 26.04 base for .NET 9
  (`9.0-resolute` does not exist), so C# runs on **.NET 10**, whose alpine
  runtime is `alpine3.22` — which is why the whole alpine side is 3.22 rather
  than 3.21. Changing either base means rechecking both.
- **musl vs glibc is a real variable, not just image size.** Alpine builds link
  musl, whose allocator behaves differently from glibc's. Rust and .NET on
  alpine are genuinely different binaries, not the same binary on a smaller
  base. Go is the exception — `CGO_ENABLED=0` puts one identical static binary
  on both, making it the cleanest base-only comparison here.
- **Runtimes size themselves against the host.** Go's GC, .NET's GC heap count
  and V8's heap all scale with visible CPU count and total RAM. Numbers from a
  32 GB / 8-core workstation do not transfer to a 2-vCPU VM.
- **Page cache ages.** `working set` subtracts only *inactive* file pages.
  Sampled seconds after start, every mapped page is still in the active LRU, so
  working set equals `memory.current` and reads high — the C# and TypeScript
  rows dropped ~15 MiB each between a 1.5 s and a 15 s settle. Use `--settle`.
- **RSS vs PSS.** Unprivileged, the script sums RSS over ~32 host-side
  processes that are mostly two distinct binaries, so shared text is counted
  once per process and the total is an upper bound. Run under `sudo` for PSS,
  or trust the host `MemUsed` delta, which counts each physical page once.
- **One-shot sample.** No averaging; a GC cycle landing mid-measurement moves a
  row by megabytes. Re-run before trusting anything surprising.

## Findings worth knowing

Measured on this host (8 CPUs, 31 GiB), 10 services, PSS for host-side processes.

- **Two `docker-proxy` processes per published port** when you write a bare
  `"8001:8080"` — Docker binds `0.0.0.0` and `::` separately. Writing
  `127.0.0.1:8001:8080` halves the count. Measured saving: proxy PSS **38.9 →
  24.9 MiB**, total host tax **80.4 → 68.2 MiB**. The `::` proxies were the cheap
  half (~1.3 MiB PSS each, versus ~2.6 for the IPv4 ones) because they never
  served a request, so the win is real but modest — about 1.2 MiB per container.
- **Measure this with PSS, not RSS.** RSS made the same change look like a
  −9.7 MiB saving *and* showed the surviving proxies "growing" 4.0 → 4.8 MiB.
  That growth was an artifact: with ten fewer processes sharing the same text
  pages, RSS attributes more to each survivor. PSS shows what actually happened
  — ten ~1.3 MiB processes disappeared and nothing else moved.
- **Per-container host tax is ~6.8 MiB** (shim + one proxy, PSS) and barely
  varies by language. For the lean runtimes it dominates: rust-alpine's
  container is ~1 MiB, so the shim and proxy cost it ~7× more than the service.
- **RSS overstates shims by 2.2× and proxies by 1.5×** (shim 92.3 → 41.5 PSS;
  proxy 58.8 → 38.9). Any "docker overhead" figure summing RSS over 20-30
  processes is roughly double the truth.
- **The alpine/ubuntu working-set gap is single-digit MiB; the image-size gap is
  ~42 MiB.** Base choice is a disk and pull-time decision, not a RAM one. Two
  exceptions: Python is *cheaper* on Ubuntu (musl costs CPython ~2 MiB), and Node
  is ~8 MiB cheaper on alpine, because Ubuntu's `nodejs` is a dfsg rebuild
  against system libraries rather than the official Node build.
- **Host `MemUsed` is too noisy for small effects.** Two baselines 18 minutes
  apart, stack down both times, differed by 360 MiB from unrelated activity.
  Use it as an order-of-magnitude check only; trust per-process figures below
  ~100 MiB.
- **dockerd's RSS swings tens of MiB.** Freshly restarted it read 96 → 42 → 50
  MiB within 15 seconds as Go's scavenger released startup allocations. Hence
  `--samples`, which takes a median.

## Requirements

- Docker with compose v2, cgroup v2 host, Python 3.9+ on the host.
- Network access at build time (crates.io, proxy.golang.org, npm, NuGet,
  archive.ubuntu.com, dl-cdn.alpinelinux.org). Package fetches are wrapped in
  bounded retries because some hosts resolve registries to unreachable IPv6
  addresses and fail intermittently.
- Docker socket access. If you installed the snap and get
  `permission denied ... /var/run/docker.sock`:

  ```bash
  sudo addgroup --system docker
  sudo adduser "$USER" docker
  sudo snap disable docker && sudo snap enable docker
  newgrp docker          # or log out and back in
  ```
