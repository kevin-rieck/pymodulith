"""Freeze observed spike outcomes; a changed result requires revisiting the ADR."""

import asyncio

import pytest

from experiments.backend_comparison.candidates import Dishka, Reference
from experiments.backend_comparison.run import run_scenario
from experiments.backend_comparison.scenarios import SCENARIOS

EXPECTED_FAILURES = {
    (Reference, "same_key_once"): "initializations=2",
    (Reference, "cancelled_waiter"): "initializations=2",
    (Reference, "cancelled_initializer"): "initializations=2",
    (Reference, "cancelled_waiter_resource"): "'start', 'start'",
    (Dishka, "independent_keys_progress"): "TimeoutError",
    (Dishka, "reentrant_resolution"): "TimeoutError",
    (Dishka, "cancelled_initializer"): "initializations=2",
    (Reference, "restart_fresh_instances"): "application instance survived restart",
    (Reference, "closed_operation_rejected"): "closed operation still resolved",
    (Dishka, "closed_operation_rejected"): "closed operation still resolved",
    (Reference, "shutdown_drains_operation"): "shutdown completed before",
    (Dishka, "shutdown_drains_operation"): "shutdown completed before",
}


@pytest.mark.parametrize("candidate", [Reference, Dishka], ids=["reference", "dishka"])
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_backend_comparison_observed_matrix(candidate, name):
    result = asyncio.run(run_scenario(candidate, SCENARIOS[name]))
    failure = EXPECTED_FAILURES.get((candidate, name))
    if failure:
        assert result.startswith("FAIL ") and failure in result, result
    else:
        assert result == "PASS", result
