# ADR 0001: Select a compiled-plan custom resolver, gated on lifecycle safety

Status: **accepted decision; lifecycle and concurrent executor implemented**.
The historical decision compares Dishka 1.10.1 with a deliberately unsafe
reference, not two production runtimes.
The issue #5 experiments live only under `experiments/backend_comparison/`;
none is exported or included in the distribution. The existing module and
contract facade remains backend-independent. There is no public backend choice.

## Decision

Select **one custom compiled-plan resolver** for the runtime. Do not
ship the reference implementation as-is: it fails exactly-once initialization
and cancellation tests. Complete the mandatory gates below before claiming a
working/safe runtime. Reconsider Dishka if a supported version demonstrates
these same gates with materially less owned machinery. This is not a claim that
mature libraries are generally unsafe: it is a result for this adapter, locked
version, and specified scope contract.

Dishka is preferred when it meets the safety contract with less maintenance.
Here its default APP lock and an explicitly enabled REQUEST lock serialize the
scope while awaiting user code. The REQUEST lock gives same-key exactly-once and
isolates a cancelled waiter in this probe, but an unrelated provider cannot
progress and a provider calling `get` on another key deadlocks. Disabling the
lock would forfeit the very same-scope guarantees being tested. Moving all
coordination above Dishka is not a demonstrated fix: Dishka resolves nested
dependencies internally, before our wrapper is entered. A safe design would
need evidence of per-key coordination at *all* initialization points, plus
ownership, cleanup and drain around Dishka. That would leave us owning the
hard runtime machinery as well as an adapter and a second graph. The reference
already consumes our validated keys, but adds significant correctness and
maintenance responsibility; it is a starting point, not evidence of readiness.

## Reproduction and scope

From a fresh clone, run `uv sync --locked`, then
`uv run python -m experiments.backend_comparison.run`. The runner prints every
named result and timing; it exits successfully even when a candidate fails a
contract assertion so the whole matrix is visible. `uv run pytest tests/test_backend_comparison.py -q` asserts the **observed** pass/fail matrix;
a change in observations requires investigation and ADR update. Each scenario
is bounded by a 2 s timeout; deadlock/progress probes use a 100 ms barrier.
No external archive is needed. Tests run identical business factories and
assertions against both candidates. The fake is selected by a fresh composition,
not an override of a live global container.

The reference is an original, bounded adaptation of the cache/check/build and
`AsyncExitStack` approach in the historical Modframe validation `modframe/core.py`
(see archive `ARCHIVE_README.md`, ADR-002, ADR-004 and PRD-002). Unlike that
prototype it consumes pymodulith's real compiled plan and explicitly enters
only `resource` declarations. It is not a verbatim copy, has no production
state machine, and inherits the unsynchronized cache race. Its restart-cache
failure is specific to this adaptation: the archive creates a fresh runtime and
cache for each lifespan, so the failure is not evidence against archive restart.
The Dishka adapter translates each plan-local key to a private synthetic type
and constructs Dishka declarations on the cold path; APP maps to APPLICATION
and REQUEST to OPERATION. It uses locked REQUEST scopes, eager APP resolution
in provider order, and an outer stack for module hooks. Dishka owns resource exits.
Plan-local types avoid token equality and private-module collisions in this
probe. Both use the same explicit business factories, resource declarations,
compiled arguments, hooks and operation boundary. Neither is a public API.

## Observed matrix

P = assertion passed; F = assertion failed. Windows 11, CPython 3.13.14,
Dishka 1.10.1. These are measurements, not claims about a future runtime.

| Named scenario | Reference | Dishka | Observation |
|---|:---:|:---:|---|
| `scopes_resources_eager` | P | P | APP eager; sync/async values and resources; scoped identity and reverse exits |
| `composition_local_fake` | P | P | independent real/fake compositions |
| `same_key_once` | F | P | reference initialized twice |
| `independent_keys_progress` | P | F | scope lock blocks unrelated provider until timeout |
| `reentrant_resolution` | P | F | scope lock deadlocks on another key; bounded timeout |
| `cancelled_waiter` | F | P | reference initialized twice |
| `cancelled_initializer` | F | F | both initialized twice after owner cancellation |
| `failed_initialization_retry` | P | P | sequential failure then successful retry, cached once |
| `startup_rollback` | P | P | entered APP resource exits after dependent startup failure |
| `resource_exit_receives_business_error` | P | P | operation and APP resources receive the original business exception |
| `cleanup_failure_preserves_business` | P | P | business and both operation/APP cleanup errors observable |
| `no_hot_path_reflection` | P | P | startup, first and cached gets, later operation and restart |
| `cancelled_waiter_resource` | F | P | reference started resource twice; one successful enter/close |
| `operation_rollback` | P | P | entered operation resource exits on dependent failure |
| `many_isolated_operations` | P | P | 100 overlapping operations isolate instances, share APP |
| `application_lifecycle_order` | P | P | eager async APP resource, ordered hooks, reverse exits |
| `restart_fresh_instances` | F | P | reference reuses APP cache after restart |
| `closed_operation_rejected` | F | F | both allow lookup after scope exit |
| `shutdown_drains_operation` | F | F | neither adapter waits for active operation cleanup |

The exception-forwarding scenario uses non-suppressing resources. Dishka's
container does not propagate a resource's suppression decision; the result does
not establish equivalent behavior for suppressing context managers.

The reflection test patches `inspect.signature`, `typing.get_type_hints` and
loaded Dishka and pymodulith aliases **after** plan validation and adapter
construction; it exercises backend internals, first resolution, cached hits,
new scopes and restart. Dishka does lazily compile its factories during first
get in this version, but this probe observed no prohibited reflection there.
It does not prove absence of every possible reflection API, dynamic import,
rare provider kind or future-version behavior. Translation/validation is
cold-path work; production must not redo it at each lifespan. Other gaps include
cancellation during resource enter/exit and shutdown, concurrent failure
broadcast, invalid resource diagnostics, every startup fault position, complete
operation-drain races, cross-loop use, broad graph shapes, and external I/O. The
matrix is a bounded decision probe, not the full M1 acceptance suite.

## Selected runtime acceptance matrix

`uv run pytest tests/test_selected_runtime_matrix.py -q` runs all 19 named
scenarios above against the actual compiled-plan runtime, using a test-only
adapter that supplies the scenario's already compiled plan. All 19 pass in the
issue #8 implementation; the original reference and Dishka observations above
are unchanged. The matrix remains a bounded probe, not exhaustive evidence.
`tests/test_concurrent_resolution.py` separately uses event barriers and bounded
waits for shared failure/retry, first and later waiter cancellation, transitive
fan-in, independent progress, dynamic cycles, concurrent rollback, in-flight
scope exit, cancellation and cross-loop/thread rejection. The existing runtime
and lifecycle tests cover 100 overlapping isolated operations, eager order,
reverse cleanup, error preservation, task affinity and context isolation.

Provider tasks use a snapshot of the creating task's context; their changes are
not propagated back to callers. Entry and exit of each provider resource use
its owner task. Module hooks remain host-task-owned: a host cancellation can
interrupt an individual hook exit, and a resource or hook whose own entry fails
must release its own partially acquired state. The host must allow settlement
and cleanup to finish. There is no guarantee for abrupt termination, forcibly
cancelled owner-task exit code, multiprocess sharing or use after scope exit.
Cycle checks also conservatively treat a child task spawned by an active provider
as part of that initializer; a detached child can therefore receive a cycle
error for a circular lookup even if its parent would not await it. Provenance
expires when the initializer finishes; non-cyclic child lookups and ordinary
host task sharing remain supported. See the README for the exact failure, retry
and cancellation policy.

## Surface, diagnosability, dependencies and timings

The spike has 173 adapter lines total: common call/enter helpers, approximately
59 lines for the reference classes and 88 for Dishka classes (including
whitespace and both operation facades). This is not a production LOC forecast:
reference omits the concurrency, lifecycle, error and state machinery, and the
Dishka translation depends on generated signatures, synthetic types and
Dishka's own implementation. Dishka graph errors show generated `PlanKeyN`
labels and backend frames unless mapped back to module/token/factory. The
reference errors expose user frames more directly but currently lack provider
context entirely. Both require meaningful contextual errors and preservation of
original exceptions; no developer-trial or troubleshooting-time claim is made.

Dishka is locked **only in the dev dependency group** for reproducibility;
there is no package runtime dependency, import from `src`, or business interface
coupling. At Python 3.13 its installed metadata has no applicable transitive
runtime dependency (its exceptiongroup dependency is conditional on Python
<3.11). On this machine its installed distribution occupied about 407 KB;
platform, wheel and tooling can change that number. Upgrade/review and adapter
maintenance still have a cost. The selected custom design adds no DI dependency.

A representative run of the command above (microseconds, one cold observation,
medians of 300 no-delay or 50 delayed operations):

| Candidate/workload | compile | adapter | startup | scope enter | first get | hit | business | exit |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| reference, no delay | 194.3 | 1.4 | 16.5 | 3.0 | 11.6 | 0.8 | 0.7 | 4.1 |
| Dishka, no delay | 208.8 | 1506.1 | 142.1 | 4.5 | 17.8 | 2.0 | 0.7 | 7.6 |
| reference, 1 ms simulated wait | 214.8 | 1.3 | 16.5 | 8.4 | 15565.6 | 3.2 | 1.4 | 12.6 |
| Dishka, 1 ms simulated wait | 289.6 | 2001.4 | 170.6 | 10.1 | 15499.4 | 5.3 | 1.4 | 18.1 |

Compilation includes composition registration and validation; adapter creation
is separate; startup eagerly creates the APP value and enters the hook. Every
warm operation creates the same sync and async resources, resolves the same
service, checks its cached identity, sums 128 values and closes resources. The
wait is `asyncio.sleep(0.001)` in the async resource, not real I/O; this Windows
run's timer granularity makes it roughly 15 ms. No concurrency safety is
inferred from these serial timings. They are absolute local observations, not a
production latency, throughput, p95/p99, memory or toy-ratio decision. Profile
representative real I/O and equivalent **fully correct** behavior after the
runtime gates, with workload budgets and repeated distributions.

## Mandatory implementation gates and supported execution

The selected runtime must preserve the full `m1-contract.md`; these constraints
are not relaxed to make the reference pass:

* Exactly one in-flight initialization **per provider key per scope**, including
  transitive dependencies; no scope-wide lock across user awaits. Independent
  keys must progress. Reentrant resolution of an independent key must work;
  detect and reject dynamic wait cycles with actionable errors rather than hang.
* Scope-owned initialization survives cancellation of any single waiting task,
  including the first caller; that caller still receives cancellation. Other
  waiters receive the same result or failure. Failed attempts are evicted only
  after their waiters observe failure; a later request may retry. Scope exit must
  settle outstanding initialization before closing successfully entered resources.
* Preserve business/startup exceptions together with every cleanup error; unwind
  successful acquisitions in reverse order. Reject admission during drain, wait
  through operation cleanup and only then release APP resources. Cancellation
  and host timeouts must not silently abandon owned cleanup; specify/test the
  exact shutdown cancellation boundary before release.
* Support multiple asyncio tasks sharing a live operation on **one event loop**,
  and independent operations on that loop. No sharing a runtime, operation,
  locks, or acquired resources across threads or event loops; no multiprocess
  shared state; no guarantees after abrupt process termination. Each runtime
  and scope has one loop owner; fail clearly on unsupported cross-loop/thread
  use. Do not claim safety for task completion after its scope has exited.
* Compile/translate once from validated metadata. Startup, restarts, scope entry,
  first resolution, cache hits, cleanup and backend internals must not inspect
  signatures/type hints or revalidate/search the graph. No dynamic override of
  a compiled graph; use a new composition for a fake.

## Concrete follow-up issue updates for the maintainer

These are instructions for updating existing tracked issues, not completed work:

* **#6 scoped resolution:** implement only the selected custom plan executor;
  resolve plan-local keys and argument names, private/export boundaries and
  identity (including same-named/unhashable tokens). Distinguish value factories
  from explicit sync/async resources, contextualize invalid results, cache per
  correct scope, and use fresh application/operation caches. Test two modules
  with identical private tokens, fake compositions, 100 overlapping operations,
  sync/async factories, retained/closed scopes and restart. Do not add Dishka or
  a public backend selector to the package.
* **#7 lifecycle/drain:** implement eager APP initialization, dependency-ordered
  hooks and reverse successful-acquisition cleanup, startup/operation rollback,
  ready/draining/stopped admission and active-operation drain through cleanup.
  Test failure at each acquisition, restart, overlapping lifespan rejection,
  shutdown races, cleanup failure groups and cancellation during enter/exit;
  preserve the business/startup exception as well as cleanup failures.
* **#8 concurrency/cancellation:** implement and test the per-key,
  scope-owned initialization and retry policy above. Use bounded event barriers
  for same/different keys, transitive resolution, dynamic cycles/reentrancy,
  cancellation of first and later waiters, shared failures, retry, scope exit
  during initialization and cleanup. Test unsupported cross-thread/loop calls
  fail rather than corrupting state; never await arbitrary user code under a
  global/scope lock.
* **#10 no-reflection/performance:** guard stdlib and imported reflection aliases
  through startup, first/cached resolution and restart, including any internal
  executor code; ensure adapter/plan construction is not repeated per lifespan.
  Measure cold compilation/startup separately from first get, hit, scope and
  resource teardown on representative I/O and concurrent correct behavior;
  establish explicit latency/throughput/memory budgets and record environment,
  distributions and regressions rather than relying on this probe's ratio.
