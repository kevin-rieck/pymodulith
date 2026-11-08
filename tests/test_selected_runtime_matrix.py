"""Run the historical backend contract against the selected production executor."""

import asyncio

import pytest

from experiments.backend_comparison.run import run_scenario
from experiments.backend_comparison.scenarios import SCENARIOS
from pymodulith import Application


class SelectedRuntime:
    def __init__(self, plan):
        self.app = Application("matrix")
        self.app._plan = plan

    def start(self):
        return self.app.lifespan()

    def operation(self):
        return self.app.operation()


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_selected_runtime_backend_matrix(name):
    assert asyncio.run(run_scenario(SelectedRuntime, SCENARIOS[name])) == "PASS"
