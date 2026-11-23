import asyncio

from experiments.core_performance import __main__ as benchmark


def test_core_performance_workloads_and_graph_boundaries():
    benchmark.imports()
    for shape in ("broad", "layered"):
        for size in (1, 10, 100):
            declarations, edges = benchmark.graph_inputs(size, shape)
            assert edges <= 3 * size
            plan = benchmark.register_graph(declarations).validate()
            assert len(plan.providers) == size
            assert (
                sum(len(provider.arguments) for provider in plan.providers.values())
                == edges
            )

    async def run():
        for kind in ("manual", "runtime"):
            phases = await benchmark.phases(kind, 1, 2)
            assert all(phase["count"] == 2 for phase in phases.values())
            throughput = await benchmark.throughput(kind, 10, 0, 20)
            assert throughput["completed"] == 20
            io = await benchmark.loopback(kind, 10, 20)
            assert io["completed"] == io["latency"]["count"] == 20
        await asyncio.sleep(0)
        assert not [
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        ]

    asyncio.run(run())
