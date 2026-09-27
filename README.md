# docker-mem-test

Ten containers serve `GET /api/hello` with HTTP 200 and the exact body
`Hello World`. Five languages run on two runtime bases: Alpine 3.22 and
Ubuntu 26.04. The report estimates **idle memory after serial warm-up**,
including container cgroups, attributed shims/proxies, and shared Docker daemons.

This is a comparison of these server implementations and packaged runtime
builds. It is not a production capacity test or an isolated experiment on libc.
See [the report review](docs/report-review.md) for corrections to earlier findings.

## Matrix

| Services | Host ports | Server / requested toolchain |
|:--|--:|:--|
| `rust-alpine` / `rust-ubuntu` | 8001 / 8002 | axum 0.8 + tokio, Rust 1.93 |
| `go-alpine` / `go-ubuntu` | 8003 / 8004 | net/http, Go 1.26 |
| `python-alpine` / `python-ubuntu` | 8005 / 8006 | ThreadingHTTPServer, CPython 3.14 |
| `typescript-alpine` / `typescript-ubuntu` | 8007 / 8008 | node:http, Node 22 |
| `csharp-alpine` / `csharp-ubuntu` | 8009 / 8010 | ASP.NET Core minimal API, .NET 10 |

Both variants share application source, but libraries, linking, build flags and
patch versions can differ. All run as UID/GID 65534. Compose sets no CPU or memory
limit; inherited host/cgroup constraints can still apply. Ports bind to
127.0.0.1. On the measured Docker configuration this creates one IPv4 proxy per
service; proxy behavior depends on daemon and network configuration.

## Usage

Run on the local Linux Docker host, with Compose v2, cgroup v2 and Python 3.9+.
The script requires Docker socket access and host `/proc` and `/sys/fs/cgroup`
visibility. Remote daemons and Docker Desktop VM measurements are not supported.

Build **before** taking a baseline so builds are outside the comparison window:

```bash
docker compose build
docker compose up -d --no-build
scripts/report.sh inventory
docker compose down
scripts/report.sh baseline --samples 5
docker compose up -d --no-build
scripts/report.sh report --requests 200 --settle 15 --samples 5
```

The wrapper uses passwordless sudo if available to read PSS. Otherwise it uses
RSS and labels the result. Use the same privilege level for baseline and report.
For PSS when sudo requires a password, run both measurement commands explicitly
under `sudo`.

Inventory probes runtime versions in the containers and saves them by image ID.
The stack must be stopped and started afterwards: those probes can populate its
cgroup's file cache. Report generation does not exec extra processes inside the
containers. Re-record inventory after rebuilding images; later measurements of
the same images can reuse it.

Reports are written to `reports/memory-latest.{md,json}` and timestamped copies.
JSON preserves the raw measurements, running image IDs, OS/runtime information,
configured limits, sampling window, and calculated totals. Generated files are
gitignored; retain the JSON alongside any results you share.

Repeat the report with identical arguments to assess variation. `--samples`
only repeats **host-process** measurements; each container cgroup is read once.
Fresh-start replicates require another down/baseline/up cycle. Neither repeated
warm-ups nor a fixed settling time proves steady state.

The collector rejects incomplete matrices, stopped/paused containers, wrong
endpoint bodies, failed warm-up requests, unavailable required cgroup data,
container restarts, and host process churn during sampling. An invalid baseline
does not suppress current measurements; it disables the incremental comparison.

```bash
curl -s localhost:8003/api/hello
python3 -m unittest discover -s tests -v
```

## What the numbers mean

The container table includes raw `memory.current`, anonymous memory, file cache,
kernel charges, swap, lifetime peak and task count. Tasks include threads.
WS is `max(0, memory.current - inactive_file)`, following the usual Docker CLI
adjustment. Anonymous/file/kernel values describe raw current memory, so they
do not add up to WS. These sequential kernel reads are not atomic.
[Docker documents the cache adjustment and task count.](https://docs.docker.com/reference/cli/docker/container/stats/)

WS is a useful comparison metric, but it is not all physical RAM attributable
to an application. Shared pages are charged to a cgroup rather than divided
among users, so startup order and other workloads can affect the rows. File
cache includes more than mapped binaries; inactive does not mean unused.
[Linux documents memory ownership and cgroup counters.](https://docs.kernel.org/admin-guide/cgroup-v2.html)

Host tax adds only shims/proxies attributed to the benchmark and outside its
cgroups. Unattributed shims/proxies appear separately. Shared dockerd/containerd
memory is reported in full and may support unrelated work.

PSS divides shared resident pages among the processes mapping them. RSS counts
the whole resident mapping in each process. Fewer sharers can increase a
survivor's **PSS**, not its RSS merely through a change in the divisor.
PSS availability is checked for every sampled process; partial coverage is
labeled mixed PSS/RSS. Neither metric includes all host kernel overhead.
[Linux defines RSS and PSS here.](https://docs.kernel.org/filesystems/proc.html)

The combined estimate is:

```text
container working sets + attributed outside-cgroup host tax + shared daemon footprint
```

It mixes cgroup charges and process residency accounting and is not an exact
physical-memory total. Swap is reported separately. The comparable incremental
estimate subtracts the **baseline shared daemon footprint**, using the same
metric, from this total. Baselines must identify the same boot and daemon
processes and have no running benchmark containers or other shims/proxies.

Host cache-excluding used memory is explicitly calculated as
`MemTotal - MemFree - Buffers - Cached - SReclaimable`. This is not modern
`free`'s used value. Its delta and the change in `MemAvailable` are contextual
checks, not ground truth: host activity and different cache accounting can
dominate small effects. The host snapshot is taken after measurement.

Image sizes are Docker's `image inspect .Size` for the running image ID, with
storage-driver-dependent semantics. They are not unique disk usage or transfer
sizes, and summing them double-counts shared layers.

## What the existing evidence supports

The original PSS snapshot at **2026-09-27 20:54:57 +0200** recorded 124.3 MiB of
container WS, 68.2 MiB of attributed host processes, and 100.2 MiB of shared
daemons: a combined estimate of **292.7 MiB**. The baseline used RSS, so the
original incremental comparison was invalid. These are historical observations,
not fixed costs.

| Language | Alpine WS MiB | Ubuntu WS MiB | Ubuntu − Alpine MiB |
|:--|--:|--:|--:|
| C# | 22.1 | 34.1 | +11.9 |
| Go | 3.6 | 3.4 | −0.2 |
| Python | 11.1 | 9.6 | −1.5 |
| Rust | 0.8 | 1.0 | +0.2 |
| TypeScript / Node | 15.9 | 22.9 | +7.0 |

Differences are computed before rounding. The C# gap is not single-digit MiB.
The earlier two-family proxy sample used 80.4 MiB of host tax versus 68.2 MiB
with IPv4-only bindings, but those runs were not a controlled repeated
experiment. They do not establish a universal 1.2 MiB saving per container.

Live version checks during review found Python **3.14.5 vs 3.14.4** and Node
**22.22.3 vs 22.22.1**, and .NET **10.0.3 vs 10.0.12** (Alpine vs Ubuntu).
Matching major versions does not make
these base-only comparisons. Claims that musl alone caused Python's gap, or
Ubuntu packaging alone caused Node's gap, are not established by these data.

Docker tags, apt packages and Rust/npm dependency ranges are mutable; no
Cargo.lock or package-lock.json is committed. Image IDs identify the measured
artifacts, but rebuilding later need not reproduce them. A controlled follow-up
should pin image digests and dependency locks, record compiler versions, repeat
fresh starts in varied order, and report distributions. Go's two Dockerfiles
request the same static build; recorded binary hashes can verify equality for
the actual images.

## Layout

```text
rust/ go/ python/ typescript/ csharp/  # sources + Alpine/Ubuntu Dockerfiles
docker-compose.yml                  # ten services
scripts/report.sh                   # optional sudo wrapper
scripts/memreport.py                 # collector + Markdown/JSON renderer
tests/test_memreport.py              # accounting and failure-path regression tests
docs/report-review.md               # audit findings and evidence
reports/                            # generated local artifacts
```

Builds require registry and package-repository access. Some apt/apk commands
retry downloads; not all dependency fetches do.
