# Report review — 2026-09-27

The original measurements are useful observations of this endpoint, but the
original report overstated what they established. Its per-service arithmetic
reproduces from the saved JSON. Its incremental-memory comparison and several
causal explanations do not hold up.

The **21:42:06 +0200 rerun now has a compatible PSS baseline**. It records a
269.2 MiB combined footprint estimate and a 157.4 MiB estimated increase over
the stopped-stack baseline. This resolves the mixed-metric comparison; the
host-memory delta still does not corroborate the estimate (details below).

## Findings and corrections

| Finding | Evidence and consequence | Correction |
|:--|:--|:--|
| Total footprint was compared with an incremental host delta | The original 292.7 MiB includes the entire shared daemon footprint, although daemons were already present in the stopped-stack baseline. | Subtract compatible baseline daemon memory before making an incremental comparison. |
| RSS and PSS were mixed across snapshots | The original 20:54:57 PSS snapshot embeds a baseline with both daemon PSS values null. Its field named `docker_procs_total_pss` actually contains RSS. | Derive metric coverage from every process; disable incompatible comparisons. New baseline fields name the metric explicitly. |
| Runtime versions were not matched | Live inventory: Python 3.14.5/3.14.4, Node 22.22.3/22.22.1, .NET 10.0.3/10.0.12 (Alpine/Ubuntu). | Record actual versions and running image IDs; describe these as packaged-runtime comparisons. |
| Even Alpine patch releases differ | Inventory reports Alpine 3.22.3 for C#, 3.22.4 for Python/Node, and 3.22.6 for Go/Rust. | Preserve `/etc/os-release`; requested image tags are not exact provenance. |
| Missing data could look like a zero-memory winner | Missing cgroups defaulted to zero; incomplete matrices could still produce a report titled “10 services.” | Fail collection for missing required data or incomplete matrices. |
| HTTP success did not validate the specified endpoint | Any HTTP 200 was accepted; failed warm-up requests were ignored. | Require exact `Hello World` bytes and successful warm-up/readiness checks. |
| Host attribution could inflate totals | Unattributed shims/proxies entered the grand total. Inside-cgroup shims were excluded there but still added to service totals. | Exclude unattributed processes from stack totals and inside-cgroup processes from both additions. |
| Partial PSS availability was labeled PSS | Any readable PSS enabled the label; missing values contributed RSS or appeared as zero in the PSS column. | Explicit PSS/RSS/mixed labels; PSS sums require complete coverage. |
| Host and container sampling differed | Host memory was read before warm-up; container memory was sampled once while daemon values were medians. | Read host memory after sampling; report the timing and preserve raw host-process samples. |
| Thread counts were called processes | C# showed 22 and Rust 9 in a column sourced from `pids.current`. | Label the column tasks, including threads. |
| Short warm-up was called steady state | Sequential requests and a fixed delay do not demonstrate GC/JIT/cache stability or concurrent-load memory. | Describe idle-after-warm-up conditions, with one container sample per report. |
| The explanation for rising RSS was backwards | Reducing sharers changes the divisor in PSS, not RSS. | Correct the explanation; avoid treating the proxy before/after pair as controlled causal evidence. |
| OS-gap conclusions contradicted the table | Original C# gap: +11.9 MiB; image-size gaps span roughly +30 to +59 MiB. | Replace blanket “single-digit RAM / 42 MiB image” claims with the observed rows. |
| Image tags did not identify measured artifacts | Inspecting an image tag could inspect a different image after retagging. | Use the running container's immutable image ID. Do not call summed image sizes unique disk usage. |

The cache subtraction and task labels follow [Docker's stats documentation](https://docs.docker.com/reference/cli/docker/container/stats/).
Shared-page charge ownership is documented in [cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html).
The RSS/PSS distinction follows [Linux `/proc` documentation](https://docs.kernel.org/filesystems/proc.html).

## Preserved evidence

[Original snapshot JSON](evidence/original-205526.json) is copied unchanged from
`reports/memory-20260927-205526.json`. A regression test reconstructs its service
totals and verifies the 292.7 MiB combined estimate and 68.2 MiB attributed host
tax while rejecting its baseline comparison. This preserves useful observations
without endorsing the old interpretation.

The first corrected run with a fresh stopped-stack RSS baseline completed at
21:35. A subsequent user-collected PSS report at **21:39:26 +0200** is preserved
as [Markdown](evidence/pss-213927.md) and [raw JSON](evidence/pss-213927.json).
It recorded 163.8 MiB container WS, 64.4 MiB attributed host tax and 103.8 MiB
shared daemon PSS, totaling **331.9 MiB**. All ten endpoints passed, no
unattributed shims/proxies were found, and container swap was zero. Its embedded
baseline still used RSS, so the new generator correctly withheld an incremental
comparison. The next run supplied the matching sudo/PSS baseline.

### Rerun with a matching PSS baseline

The user-collected baseline at **21:40:33 +0200** and report at
**21:42:06 +0200** are preserved in [raw JSON](evidence/pss-214208.json), including
the embedded baseline and individual host-process samples, and
[Markdown](evidence/pss-214208.md). The embedded baseline matches the saved
baseline file. Both snapshots have complete PSS coverage, the same host boot
and daemon identities, and no compatibility issues. The baseline contains zero
running benchmark containers and only the two shared daemons.

The report used 200 sequential warm-up requests per service, 15 seconds of
settling, and five host-process samples. All ten endpoints returned the exact
expected body. Every recorded host-process PSS median was recomputed from its
raw samples and matched. There were no unattributed shims/proxies.

| Measure | MiB |
|:--|--:|
| Container working sets | 115.2 |
| Attributed shim/proxy PSS | 63.6 |
| Shared daemon PSS, stack running | 90.4 |
| Combined footprint estimate | 269.2 |
| Shared daemon PSS, baseline | 111.9 |
| Estimated increase over baseline | **157.4** |
| Host cache-excluding used-memory delta | **−28.9** |
| Host MemAvailable delta | +1.5 |

The incremental calculation uses unrounded bytes: 282,310,656 − 117,297,152 =
165,013,504 bytes, or 157.4 MiB. Baseline age was 93.6 seconds. Raw container
current memory totaled 155.1 MiB, of which 39.9 MiB was subtracted as
`inactive_file`. Containers also used 88 KiB of swap, excluded from RAM totals.

The matched metrics make the subtraction internally consistent, but do not
establish a fixed marginal cost. Shared daemon PSS fell by 21.5 MiB between
snapshots. Meanwhile, host used memory moved in the opposite direction to the
bottom-up estimate. The host-level check therefore **does not corroborate** the
157.4 MiB estimate; these samples cannot separate unrelated host activity from
differences in memory accounting. Neither figure should be presented as an
exact physical-RAM increase attributable to the stack.

The per-language conclusions also remain sample-dependent: the C# Ubuntu-minus-
Alpine WS gap is now −0.4 MiB, versus +11.9 MiB in the original snapshot.
Runtime versions remain mismatched. The new baseline resolves metric
compatibility, not repeatability or isolation of the base OS.

Version probes are a separate `inventory` command. They run before a required
container restart, because execing interpreters or hashing binaries inside the
measured cgroup can itself add file-cache charges. Reports reuse this inventory
by running image ID, without execing probe processes during measurement. The
Go executable hashes match across these two images; the Rust hashes differ.

## Validation and remaining limits

For the next proxy comparison, Compose now uses bare `8001:8080`-style mappings
instead of explicit 127.0.0.1 bindings. The preserved 21:42 report above is the
single-proxy reference: ten proxies, 22.1 MiB proxy PSS and 63.6 MiB combined
shim/proxy PSS. The new configuration is expected to create twenty proxies on
this host; verify the measured count before comparing. Keep the same images and
200-request / 15-second / five-sample settings, and take another PSS baseline
with the stack stopped. Use `docker compose up -d --no-build` after the baseline
so Compose recreates containers with the changed bindings; `start` or `restart`
alone will not apply the port changes. Compare proxy PSS and total host tax
separately from changes in container working sets and shared daemons. The
warm-up still uses IPv4, so the IPv6 proxies will not receive benchmark traffic.

Nineteen standard-library regression tests cover accounting, mixed metrics,
incompatible baselines, missing data, endpoint bodies, process churn, invalid
arguments, incomplete matrices, and separation of inventory probes from the
measured container lifetime. The collector was also exercised against all ten
live services. No application code, runtime tuning, image tags, or dependency
versions were changed during this review.

The updated report can be trusted as an explicitly scoped, reproducible
calculation over the recorded samples. It is still not an exact physical-RAM
total or a causal test of Alpine versus Ubuntu. Cgroup charges and PSS have
different sharing rules; host kernel allocations are incomplete; daemon costs
are global; and the host is doing unrelated work. RSS-based totals additionally
double-count shared process mappings.

Exact compiler versions and dependency resolutions are not available from the
current build records. Mutable tags, unpinned apt packages and missing Rust/npm
lockfiles prevent reproducible future rebuilds. A publication-quality OS-only
comparison needs matched runtime patch/build settings, pinned artifacts,
repeated fresh starts with varied ordering, and reported variation. The current
results support observations about these particular images and this workload.
