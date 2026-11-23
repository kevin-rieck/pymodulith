# Issue 10: core baseline

**Status: measured; independent review pending.** The final run passes the
owner-amended 200 ms fresh-process import ceiling and all other encoded gates.
The original ceiling was 100 ms, approved before measurement and failed in
three runs. After seeing those failures, the owner explicitly changed only the
import ceiling to 200 ms, stating that import is not critical for operation.
This is a post-measurement policy amendment, not an originally preregistered
200 ms gate or a performance improvement. Import is a one-time process startup
cost, not an operation-path cost. These host-specific M1 guardrails are not
production promises.

## Reproduce

From the repository root, with the locked local environment (`uv sync --locked`):

```console
uv run python -m experiments.core_performance --json results.json
```

The command writes JSON even if a budget fails, and exits nonzero on a failed
budget. [The recorded JSON](core-baseline.json) includes each process's raw
samples, distributions, graph registration and validation observations,
throughput counts, I/O samples, allocation deltas, environment and source
hashes. It records base revision `4c7a638db3d926f0df59d1697c57f8fcc28e5f0b`
and the dirty worktree used for the measurement; hashes identify the measured
source. These results do not imply independent review or an accepted release.

The machine was Windows 11 build 26200, AMD Ryzen 5 3600X, 17,126,502,400 bytes
RAM, CPython 3.13.14 release build, default `ProactorEventLoop`, balanced power
scheme. Idle and plugged-in conditions were requested but not independently
verified. Timer-pair overhead median was 100 ns, reported and not subtracted.

Seven independent workload processes alternate manual/runtime order; a separate
minimal fresh interpreter measures each package import excluding interpreter
launch. Phase distributions have 100 warmups and 1,000 samples per process;
shop registration and validate-only use 30 fresh compositions in a warm
interpreter. Startup uses the same compiled plan and fresh scope instances.
Each throughput window has 100 warmups, admits for at least five seconds and
1,000 operations, drains all admitted work and includes drain in the denominator.
Loopback has 100 warmups and 1,000 timed requests at each concurrency. The
manual control explicitly constructs the same eager application services, and
lazily constructs/caches the same operation resource and service. Both use the
shop business classes and resource context managers. Full flow includes entry,
resolution, a business call, correctness checks and cleanup; it is directly
measured, not a sum of other phases. Reporting rows are discarded after each
checked receipt to bound unrelated retained data.

## Agreed gates and observed costs

Latency is the median of seven process medians, without tracemalloc. Runtime
figures are for the selected production executor; the manual figures are
report-only. Times below are microseconds except import, registration, and
validation, which are in milliseconds.

| Phase | Agreed maximum | Runtime | Manual |
|---|---:|---:|---:|
| Fresh-process package import | 200 ms (amended) | 170.649 ms | n/a |
| Shop registration | 5 ms | 0.0244 ms | n/a |
| Shop validate-only, fresh composition | 10 ms | 0.1640 ms | n/a |
| Application startup including eager providers | 10,000 us | 121.6 us | 2.9 us |
| Empty operation entry + exit | 100 us | 18.3 us | 1.4 us |
| Empty entry / exit separately | report only | 9.05 / 9.2 us | 0.7 / 0.7 us |
| First get | 500 us | 79.5 us | 2.2 us |
| Cached get | 20 us | 5.3 us | 0.4 us |
| Direct business call | report only | 3.8 us | 2.3 us |
| Operation cleanup | 500 us | 37.3 us | 1.7 us |
| Application cleanup | 10,000 us | 36.6 us | 1.5 us |
| Full warm no-I/O shop operation | 2,000 us | 143.4 us | 7.1 us |

The amended-policy fresh imports in milliseconds were 144.9219, 136.6224,
176.7719, 154.6747, 170.6485, 267.2558, 181.6359; the gate is the median,
not a per-process maximum. The last run under the original 100 ms policy had
152.6953, 194.9757, 136.1413, 140.3453, 142.7275, 186.7358, 148.7184 ms
(median 148.718 ms). The first run also failed (149.915 ms median). An
intermediate run after adding workload warmups failed (144.552 ms median); its
console summary was retained, but its raw JSON was overwritten by a later run.
Profiling the first run with `python -X importtime -c "import pymodulith"`
attributed approximately 167.7 ms cumulative of 186.5 ms total to importing
`asyncio`; independent imports of asyncio alone took 175–190 ms.
Import profiling changes timing and does not isolate filesystem, antivirus or
scheduling effects. No production code was changed: deferring required imports
past this timer into an already-warm benchmark would shift the cost rather than
improve the actual startup cost. The full initial and last pre-amendment runs
and importtime output are preserved outside the worktree in the issue-10
artifacts directory for review.

| Concurrency | Minimum runtime ops/s | Runtime ops/s | Manual ops/s |
|---:|---:|---:|---:|
| 1 | 500 | 5,953 | 66,073 |
| 10 | 500 | 9,013 | 80,182 |
| 100 | 500 | 8,821 | 81,508 |

These are medians of process-level completed-and-cleaned throughput, not
admission rates. Each operation verifies its receipt, reporting, per-operation
session identity and cleanup; application resources and owned tasks are checked
at shutdown. No wall-clock assertion is part of the normal test suite.

For synthetic graphs, factories and tokens are made outside the separately
reported registration timer. The broad graph uses at most one dependency per
node; the layered graph has four bounded layers and at most three edges per
node. Validate-only medians (ms), versus the pre-agreed ceiling for each N:

| Providers | Ceiling | Broad | Layered |
|---:|---:|---:|---:|
| 1 | 5 | 0.043 | 0.070 |
| 10 | 10 | 0.176 | 0.232 |
| 100 | 100 | 1.447 | 2.350 |
| 1000 | 1000 | 13.242 | 67.682 |

Separate tracemalloc experiments (Python allocations, not RSS):

| Boundary | Limit | Observed |
|---|---:|---:|
| 1000-provider graph declarations/tokens/plan retained over post-import empty baseline | 16 MiB | 3,985,018 bytes; +34,982 allocations |
| Peak over validated pre-lifespan baseline including startup and 100 concurrent operations through cleanup | 16 MiB | 1,582,823 bytes |
| Retained growth after 10,000 operations, post-GC warmed stopped baselines | 1 MiB | 336 bytes; +7 allocations |
| Retained growth after 100 restarts x 100 operations, post-GC warmed stopped baselines | 1 MiB | 8,400 bytes; +3 allocations |

The compiled application remains retained in both churn comparisons; result,
event and operation handles are discarded equivalently. The experiment checked
20,102 operation resource opens and closes, 104 application resource opens and
closes, and zero leftover owned tasks after shutdown.

## Bounded loopback evidence — report only

This is real localhost TCP with an 18-byte request and 6-byte response, one
connection per operation, verified response count and connection cleanup; it is
not a database, network or production transport proxy. Medians below are
medians of process medians for per-operation elapsed latency and of
process-level throughput. Runtime throughput was about 19.0%, 19.8%, and 18.8%
lower than manual at concurrency 1, 10, and 100 respectively; there is no ratio
gate.

| Concurrency | Manual median ms | Runtime median ms | Manual ops/s | Runtime ops/s |
|---:|---:|---:|---:|---:|
| 1 | 0.695 | 0.844 | 1,325 | 1,073 |
| 10 | 7.319 | 9.062 | 1,339 | 1,075 |
| 100 | 71.632 | 87.146 | 1,352 | 1,098 |

Archive measurements with different measurement windows are **not comparable
speedup claims**. Transport p95/p99/SLO validation and production readiness are
later work. The historical two-candidate evidence remains in
[ADR 0001](../adrs/0001-runtime-backend.md); it is not another production backend.
