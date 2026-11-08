"""Run fault matrix and a small, decomposed equivalent-work timing probe."""

import asyncio
import importlib.metadata
import platform
import statistics
import sys
import time

from .candidates import Dishka, Reference
from .scenarios import SCENARIOS, Service, normal_plan


async def run_scenario(candidate, scenario):
    try:
        await asyncio.wait_for(scenario(candidate), 2)
    except Exception as exc:
        message = str(exc).encode("ascii", "backslashreplace").decode("ascii")
        return f"FAIL {type(exc).__name__}: {message}"
    return "PASS"


async def timings(candidate, delay, iterations):
    start = time.perf_counter_ns()
    plan = normal_plan([], delay=delay)
    compiled = time.perf_counter_ns()
    runtime = candidate(plan)
    adapted = time.perf_counter_ns()
    async with runtime.start():
        started = time.perf_counter_ns()
        entries, firsts, hits, business, exits = [], [], [], [], []
        for _ in range(iterations):
            a = time.perf_counter_ns()
            async with runtime.operation() as op:
                b = time.perf_counter_ns()
                service = await op.get(Service)
                c = time.perf_counter_ns()
                assert await op.get(Service) is service
                d = time.perf_counter_ns()
                assert service.count() == 7
                business_end = time.perf_counter_ns()
            e = time.perf_counter_ns()
            entries.append(b - a)
            firsts.append(c - b)
            hits.append(d - c)
            business.append(business_end - d)
            exits.append(e - business_end)
    print(
        f"{candidate.__name__} delay={delay * 1000:g}ms: "
        f"compile={(compiled - start) / 1000:.1f}us "
        f"adapter={(adapted - compiled) / 1000:.1f}us "
        f"startup={(started - adapted) / 1000:.1f}us "
        f"median operation entry={statistics.median(entries) / 1000:.1f}us "
        f"first get={statistics.median(firsts) / 1000:.1f}us "
        f"cache hit={statistics.median(hits) / 1000:.1f}us "
        f"business={statistics.median(business) / 1000:.1f}us "
        f"exit={statistics.median(exits) / 1000:.1f}us (n={iterations})"
    )


async def main():
    print(
        f"Python {sys.version.split()[0]} {platform.platform()} "
        f"Dishka {importlib.metadata.version('dishka')}"
    )
    for name, scenario in SCENARIOS.items():
        results = [
            await run_scenario(candidate, scenario) for candidate in (Reference, Dishka)
        ]
        print(f"{name}: reference={results[0]} | dishka={results[1]}")
    print("Timing probe (one cold sample, warm medians; simulated resource wait):")
    for delay, iterations in ((0, 300), (0.001, 50)):
        for candidate in (Reference, Dishka):
            await timings(candidate, delay, iterations)


if __name__ == "__main__":
    asyncio.run(main())
