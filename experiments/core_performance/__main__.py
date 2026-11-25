"""Reproduce the host-scoped issue 10 core baseline (not a transport SLO)."""

import argparse
import asyncio
import gc
import hashlib
import inspect
import json
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from examples.shop.composition import (
        build_app,
        inventory_module,
        orders_module,
        reporting_module,
    )
    from examples.shop.inventory.domain import InventoryService
    from examples.shop.orders.place_order import PlaceOrder
    from examples.shop.reporting.service import ReportingService, ReportRepository
    from examples.shop.resources import order_session, stock_resource
    from pymodulith import Application, Module, Scope


class Events:
    def __init__(self):
        self.counts = {}

    def append(self, event):
        self.counts[event] = self.counts.get(event, 0) + 1

    def check(self):
        assert self.counts.get("stock.open", 0) == self.counts.get("stock.close", 0)
        assert self.counts.get("session.open", 0) == self.counts.get("session.close", 0)


def now():
    return time.perf_counter_ns()


def distribution(samples):
    ordered = sorted(samples)
    return {
        "unit": "ns",
        "count": len(samples),
        "median": statistics.median(samples),
        "min": ordered[0],
        "max": ordered[-1],
        "p95": ordered[(len(ordered) - 1) * 95 // 100],
        "samples": samples,
    }


def imports():
    global Application, Module, Scope, build_app, inventory_module, orders_module
    global reporting_module, InventoryService, ReportingService, ReportRepository
    global stock_resource, order_session, PlaceOrder
    from examples.shop.composition import (
        build_app,
        inventory_module,
        orders_module,
        reporting_module,
    )
    from examples.shop.inventory.domain import InventoryService
    from examples.shop.orders.place_order import PlaceOrder
    from examples.shop.reporting.service import ReportingService, ReportRepository
    from examples.shop.resources import order_session, stock_resource
    from pymodulith import Application, Module, Scope


def make_app(events):
    return build_app(
        inventory_module({"widget": 10**9}, events),
        reporting_module(),
        orders_module(events),
    )


class Manual:
    """Explicit eager application and lazy cached operation resource ownership."""

    def __init__(self, events):
        self.events = events

    async def start(self):
        self.stack = ExitStack()
        stock = self.stack.enter_context(stock_resource({"widget": 10**9}, self.events))
        self.stock = stock
        self.inventory = InventoryService(stock)
        self.reporting = ReportingService(ReportRepository())

    async def stop(self):
        self.stack.close()
        assert self.stock.closed

    async def enter(self):
        return ManualOperation(self)

    async def exit(self, operation):
        operation.stack.close()
        if operation.service is not None:
            assert operation.service.session.closed


class ManualOperation:
    def __init__(self, shop):
        self.shop = shop
        self.stack = ExitStack()
        self.service = None

    async def get(self):
        if self.service is None:
            session = self.stack.enter_context(order_session(self.shop.events))
            self.service = PlaceOrder(self.shop.inventory, self.shop.reporting, session)
        return self.service


class Runtime:
    def __init__(self, events, app=None):
        self.events = events
        self.app = app if app is not None else make_app(events)
        self.app.validate()

    async def start(self):
        self.context = self.app.lifespan()
        await self.context.__aenter__()

    async def stop(self):
        await self.context.__aexit__(None, None, None)
        assert self.app._runtime is None

    async def enter(self):
        context = self.app.operation()
        return context, await context.__aenter__()

    async def get(self, operation):
        return await operation[1].get(PlaceOrder)

    async def exit(self, operation):
        await operation[0].__aexit__(None, None, None)


async def get(control, operation):
    if isinstance(control, Manual):
        return await operation.get()
    return await control.get(operation)


def clear_rows(service):
    service.reporting.repository.rows.clear()


async def one(control, live=None):
    operation = await control.enter()
    service = await get(control, operation)
    assert await get(control, operation) is service
    session = service.session
    if live is not None:
        assert id(session) not in live
        live.add(id(session))
        await asyncio.sleep(0)
    receipt = await service.place("widget", 1)
    assert receipt.sku == "widget" and receipt.quantity == 1 and session.placed == 1
    assert service.reporting.repository.rows[-1] is receipt
    clear_rows(service)
    await control.exit(operation)
    assert session.closed
    if live is not None:
        live.remove(id(session))


async def phases(kind, warmups, count):
    events = Events()
    control = Manual(events) if kind == "manual" else Runtime(events)
    samples = {
        key: []
        for key in (
            "startup",
            "empty_enter",
            "empty_exit",
            "empty_enter_exit",
            "first_get",
            "cached_get",
            "direct_business",
            "operation_cleanup",
            "full_flow",
            "application_cleanup",
        )
    }
    # Startup and shutdown use fresh application instances but the same compiled plan.
    for index in range(warmups + count):
        start = now()
        await control.start()
        started = now()
        operation = await control.enter()
        entered = now()
        await control.exit(operation)
        empty_exited = now()
        operation = await control.enter()
        before_get = now()
        service = await get(control, operation)
        after_get = now()
        assert await get(control, operation) is service
        after_hit = now()
        receipt = await service.place("widget", 1)
        after_business = now()
        assert receipt.quantity == 1
        assert service.reporting.repository.rows[-1] is receipt
        clear_rows(service)
        before_exit = now()
        await control.exit(operation)
        after_exit = now()
        assert service.session.closed
        before_full = now()
        await one(control)
        after_full = now()
        await control.stop()
        stopped = now()
        if index >= warmups:
            for key, value in (
                ("startup", started - start),
                ("empty_enter", entered - started),
                ("empty_exit", empty_exited - entered),
                ("empty_enter_exit", empty_exited - started),
                ("first_get", after_get - before_get),
                ("cached_get", after_hit - after_get),
                ("direct_business", after_business - after_hit),
                ("operation_cleanup", after_exit - before_exit),
                ("full_flow", after_full - before_full),
                ("application_cleanup", stopped - after_full),
            ):
                samples[key].append(value)
    events.check()
    return {key: distribution(value) for key, value in samples.items()}


async def throughput(kind, concurrency, seconds, minimum):
    events = Events()
    control = Manual(events) if kind == "manual" else Runtime(events)
    live = set()
    await control.start()
    for _ in range(100):
        await one(control)
    started = now()
    deadline = started + int(seconds * 1e9)
    admitted = completed = 0

    async def worker():
        nonlocal admitted, completed
        while now() < deadline or admitted < minimum:
            admitted += 1
            await one(control, live)
            completed += 1

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed = (now() - started) / 1e9
    assert admitted == completed and not live
    await control.stop()
    events.check()
    return {
        "completed": completed,
        "failures": 0,
        "seconds_including_drain": elapsed,
        "operations_per_second": completed / elapsed,
        "resources": events.counts,
    }


async def loopback(kind, concurrency, count):
    events = Events()
    control = Manual(events) if kind == "manual" else Runtime(events)
    payload = b"pymodulith-core-10"
    response = b"ack-10"
    served = 0
    connections = set()

    async def serve(reader, writer):
        nonlocal served
        task = asyncio.current_task()
        connections.add(task)
        try:
            assert await reader.readexactly(len(payload)) == payload
            writer.write(response)
            await writer.drain()
            served += 1
        finally:
            writer.close()
            await writer.wait_closed()
            connections.remove(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    await control.start()
    latency = []
    live = set()
    admitted = 0
    limit = 100

    async def worker():
        nonlocal admitted
        while admitted < limit:
            admitted += 1
            before = now()
            operation = await control.enter()
            service = await get(control, operation)
            assert id(service.session) not in live
            live.add(id(service.session))
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(payload)
                await writer.drain()
                assert await reader.readexactly(len(response)) == response
                receipt = await service.place("widget", 1)
                assert receipt.quantity == 1
                assert service.reporting.repository.rows[-1] is receipt
                clear_rows(service)
            finally:
                writer.close()
                await writer.wait_closed()
                await control.exit(operation)
            assert service.session.closed
            live.remove(id(service.session))
            latency.append(now() - before)

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    while connections:
        await asyncio.gather(*tuple(connections))
    assert served == 100 and not live
    admitted = served = 0
    latency.clear()
    limit = count
    started = now()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    while connections:
        await asyncio.gather(*tuple(connections))
    elapsed = (now() - started) / 1e9
    server.close()
    await server.wait_closed()
    await control.stop()
    events.check()
    assert served == count == len(latency) and not live and not connections
    return {
        "latency": distribution(latency),
        "completed": served,
        "failures": 0,
        "seconds_including_drain": elapsed,
        "operations_per_second": served / elapsed,
        "payload_bytes": len(payload),
        "response_bytes": len(response),
        "resources": events.counts,
    }


def graph_inputs(size, shape):
    tokens = [type(f"Token{i:04}", (), {}) for i in range(size)]
    declarations = []
    edges = 0
    for i, token in enumerate(tokens):
        if shape == "broad":
            dependencies = [tokens[0]] if i else []
        else:
            # Four bounded layers; each node points only into the preceding layer.
            width = max(1, (size + 3) // 4)
            layer = i // width
            base = (layer - 1) * width
            dependencies = (
                [tokens[base + ((i + j) % width)] for j in range(min(3, width))]
                if layer
                else []
            )
        edges += len(dependencies)

        def factory(**kwargs):
            return object()

        factory.__signature__ = inspect.Signature(  # ty: ignore[unresolved-attribute]
            [
                inspect.Parameter(
                    f"dep{j}", inspect.Parameter.KEYWORD_ONLY, annotation=dep
                )
                for j, dep in enumerate(dependencies)
            ]
        )
        declarations.append((token, factory))
    assert edges <= 3 * size
    return declarations, edges


def register_graph(declarations):
    module = Module("graph")
    for token, factory in declarations:
        module.provide(token, scope=Scope.OPERATION)(factory)
    return Application("graph").include(module)


def cold_and_graphs():
    registration, compilation = [], []
    for _ in range(30):
        events = Events()
        start = now()
        app = make_app(events)
        registered = now()
        app.validate()
        finished = now()
        registration.append(registered - start)
        compilation.append(finished - registered)
    graphs = {}
    for shape in ("broad", "layered"):
        for size in (1, 10, 100, 1000):
            declarations, edges = graph_inputs(size, shape)
            start = now()
            app = register_graph(declarations)
            registered = now()
            app.validate()
            finished = now()
            graphs[f"{shape}_{size}"] = {
                "providers": size,
                "edges": edges,
                "registration_ns": registered - start,
                "validate_ns": finished - registered,
            }
    return {
        "shop_registration": distribution(registration),
        "shop_fresh_composition_validate": distribution(compilation),
        "graphs": graphs,
    }


def snapshot_total():
    gc.collect()
    snapshot = tracemalloc.take_snapshot()
    stats = snapshot.statistics("traceback")
    return {
        "bytes": sum(item.size for item in stats),
        "allocations": sum(item.count for item in stats),
    }


def difference(after, before):
    return {key: after[key] - before[key] for key in before}


async def memory():
    gc.collect()
    tracemalloc.start()
    empty = snapshot_total()
    declarations, edges = graph_inputs(1000, "layered")
    graph = register_graph(declarations)
    plan = graph.validate()
    graph_retained = snapshot_total()
    assert len(plan.providers) == 1000 and edges <= 3000
    graph_delta = difference(graph_retained, empty)
    del graph, plan, declarations
    gc.collect()

    events = Events()
    app = make_app(events)
    app.validate()
    control = Runtime(events, app)
    gc.collect()
    before_peak = tracemalloc.get_traced_memory()[0]
    tracemalloc.reset_peak()
    await control.start()
    live = set()
    await asyncio.gather(*(one(control, live) for _ in range(100)))
    assert not live
    await control.stop()
    peak = tracemalloc.get_traced_memory()[1] - before_peak
    events.check()

    # Warm a stopped lifespan before each independent churn comparison.
    await control.start()
    await one(control)
    await control.stop()
    warm_operations = snapshot_total()
    await control.start()
    for _ in range(10000):
        await one(control)
    await control.stop()
    after_operations = snapshot_total()
    await control.start()
    await one(control)
    await control.stop()
    warm_restarts = snapshot_total()
    for _ in range(100):
        await control.start()
        for _ in range(100):
            await one(control)
        await control.stop()
    after_restarts = snapshot_total()
    events.check()
    await asyncio.sleep(0)
    assert not [
        task for task in asyncio.all_tasks() if task is not asyncio.current_task()
    ]
    assert app._runtime is None and app.validate() is app._plan
    tracemalloc.stop()
    return {
        "graph_1000_retained_delta": graph_delta,
        "startup_and_100_operations_peak_delta_bytes": peak,
        "operations_10000_retained_delta": difference(
            after_operations, warm_operations
        ),
        "restarts_100_x_100_retained_delta": difference(after_restarts, warm_restarts),
        "resources": events.counts,
        "owned_tasks_after": 0,
    }


def environment():
    def command(*args):
        try:
            return subprocess.check_output(
                args, text=True, stderr=subprocess.STDOUT
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return "unavailable"

    ram = "unavailable"
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.DWORD),
                ("load", wintypes.DWORD),
                ("total_phys", ctypes.c_ulonglong),
                ("avail_phys", ctypes.c_ulonglong),
                ("total_page", ctypes.c_ulonglong),
                ("avail_page", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("avail_virtual", ctypes.c_ulonglong),
                ("avail_extended", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            ram = status.total_phys
    return {
        "cpu": platform.processor(),
        "cpu_model": (
            command(
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Processor | "
                "Select-Object -First 1 -ExpandProperty Name)",
            )
            if sys.platform == "win32"
            else platform.processor()
        ),
        "ram_bytes": ram,
        "os": platform.platform(),
        "python": sys.version,
        "python_build": platform.python_build(),
        "executable": sys.executable,
        "power_scheme": command("powercfg", "/getactivescheme"),
        "git_revision": command("git", "rev-parse", "HEAD"),
        "git_status": command("git", "status", "--short"),
        "source_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for directory in (
                Path("src/pymodulith"),
                Path("examples/shop"),
                Path("experiments/core_performance"),
            )
            for path in sorted(directory.rglob("*.py"))
        },
        "conditions": "idle/plugged-in requested; not independently verified",
    }


async def worker_async(index, warmups, count, seconds, minimum, io_count):
    result = {"phases": {}, "throughput": {}, "loopback": {}}
    order = ("manual", "runtime") if index % 2 == 0 else ("runtime", "manual")
    for kind in order:
        result["phases"][kind] = await phases(kind, warmups, count)
        result["throughput"][kind] = {}
        result["loopback"][kind] = {}
        for concurrency in (1, 10, 100):
            result["throughput"][kind][str(concurrency)] = await throughput(
                kind, concurrency, seconds, minimum
            )
            result["loopback"][kind][str(concurrency)] = await loopback(
                kind, concurrency, io_count
            )
    result["order"] = order
    result["event_loop"] = type(asyncio.get_running_loop()).__qualname__
    await asyncio.sleep(0)
    assert not [
        task for task in asyncio.all_tasks() if task is not asyncio.current_task()
    ]
    return result


def child(index):
    import_ns = int(
        subprocess.check_output(
            [
                sys.executable,
                "-c",
                "import time; start=time.perf_counter_ns(); import pymodulith; "
                "print(time.perf_counter_ns()-start)",
            ],
            text=True,
        )
    )
    imports()
    overhead = []
    for _ in range(1000):
        start = now()
        overhead.append(now() - start)
    cold = cold_and_graphs()
    result = asyncio.run(worker_async(index, 100, 1000, 5, 1000, 1000))
    return {
        "index": index,
        "package_import_ns": import_ns,
        "timer_overhead": distribution(overhead),
        "cold": cold,
        **result,
    }


def evaluate(runs, memory_result):
    gates = {}

    def maximum(name, values, limit, unit):
        observed = statistics.median(values)
        gates[name] = {
            "observed": observed,
            "limit": limit,
            "unit": unit,
            "pass": observed <= limit,
        }

    maximum("package_import", [r["package_import_ns"] for r in runs], 200e6, "ns")
    for phase, limit in (
        ("shop_registration", 5e6),
        ("shop_fresh_composition_validate", 10e6),
    ):
        maximum(phase, [r["cold"][phase]["median"] for r in runs], limit, "ns")
    for phase, limit in (
        ("startup", 10e6),
        ("empty_enter_exit", 100e3),
        ("first_get", 500e3),
        ("cached_get", 20e3),
        ("operation_cleanup", 500e3),
        ("application_cleanup", 10e6),
        ("full_flow", 2e6),
    ):
        maximum(
            phase, [r["phases"]["runtime"][phase]["median"] for r in runs], limit, "ns"
        )
    for concurrency in (1, 10, 100):
        values = [
            r["throughput"]["runtime"][str(concurrency)]["operations_per_second"]
            for r in runs
        ]
        observed = statistics.median(values)
        gates[f"throughput_{concurrency}"] = {
            "observed": observed,
            "limit": 500,
            "unit": "operations/s",
            "pass": observed >= 500,
        }
    for shape in ("broad", "layered"):
        for size, limit in ((1, 5e6), (10, 10e6), (100, 100e6), (1000, 1000e6)):
            name = f"{shape}_{size}"
            maximum(
                name,
                [r["cold"]["graphs"][name]["validate_ns"] for r in runs],
                limit,
                "ns",
            )
    for name, value, limit in (
        (
            "graph_retained",
            memory_result["graph_1000_retained_delta"]["bytes"],
            16 * 1024**2,
        ),
        (
            "runtime_peak",
            memory_result["startup_and_100_operations_peak_delta_bytes"],
            16 * 1024**2,
        ),
        (
            "operations_retained",
            memory_result["operations_10000_retained_delta"]["bytes"],
            1024**2,
        ),
        (
            "restarts_retained",
            memory_result["restarts_100_x_100_retained_delta"]["bytes"],
            1024**2,
        ),
    ):
        maximum(name, [value], limit, "bytes")
    return gates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path)
    parser.add_argument("--child", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--memory", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child is not None:
        print(json.dumps(child(args.child)))
        return
    if args.memory:
        imports()
        print(json.dumps(asyncio.run(memory())))
        return
    if args.json is None:
        parser.error("--json is required")
    runs = []
    for index in range(7):
        print(f"process {index + 1}/7", file=sys.stderr, flush=True)
        output = subprocess.check_output(
            [
                sys.executable,
                "-m",
                "experiments.core_performance",
                "--child",
                str(index),
            ],
            text=True,
        )
        runs.append(json.loads(output))
    print("separate tracemalloc experiment", file=sys.stderr, flush=True)
    output = subprocess.check_output(
        [sys.executable, "-m", "experiments.core_performance", "--memory"], text=True
    )
    memory_result = json.loads(output)
    result = {
        "protocol": {
            "processes": 7,
            "warmups": 100,
            "phase_samples": 1000,
            "fresh_compositions_per_process": 30,
            "throughput_admission_seconds": 5,
            "minimum_completions": 1000,
            "loopback_requests_per_concurrency": 1000,
            "timing_unit": "ns; timer overhead reported, not subtracted",
        },
        "environment": environment(),
        "runs": runs,
        "memory": memory_result,
        "gates": evaluate(runs, memory_result),
    }
    args.json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    failed = [name for name, gate in result["gates"].items() if not gate["pass"]]
    print(f"wrote {args.json}; failed budgets: {failed}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
