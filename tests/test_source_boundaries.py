import os
import shutil
import subprocess
import sys
from pathlib import Path

import grimp

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "experiments/source_boundaries/importlinter.ini"


def test_source_boundaries_without_importing_shop(tmp_path, monkeypatch):
    examples = tmp_path / "examples"
    shutil.copytree(
        ROOT / "examples", examples, ignore=shutil.ignore_patterns("__pycache__")
    )
    shop = examples / "shop"
    marker = tmp_path / "imported.txt"
    (shop / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
        "raise RuntimeError('shop was imported')\n",
        encoding="utf-8",
    )
    orders = shop / "orders/place_order.py"
    inventory = shop / "inventory/domain.py"
    original_orders = orders.read_text(encoding="utf-8")
    original_inventory = inventory.read_text(encoding="utf-8")
    # A legal, one-way public package edge in addition to existing shared contracts.
    orders.write_text(
        original_orders + "\nimport examples.shop.reporting\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    # find_spec otherwise uses a root package loaded by earlier tests.
    monkeypatch.delitem(sys.modules, "examples", raising=False)
    graph = grimp.build_graph("examples", cache_dir=None)
    assert graph.direct_import_exists(
        importer="examples.shop.orders.place_order", imported="examples.shop.reporting"
    )
    assert graph.direct_import_exists(
        importer="examples.shop.orders.place_order", imported="examples.shop.contracts"
    )

    def check():
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from importlinter.cli import lint_imports_command; "
                "lint_imports_command()",
                "--config",
                str(CONFIG),
                "--no-cache",
                "--debug",
            ],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(tmp_path)},
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        print(result.stdout, result.stderr)
        assert not marker.exists()
        return result

    baseline = check()
    assert baseline.returncode == 0
    assert "2 kept, 0 broken" in baseline.stdout

    orders.write_text(
        original_orders + "\nfrom examples.shop.inventory.storage import Stock\n",
        encoding="utf-8",
    )
    private = check()
    assert private.returncode == 1
    assert "Other slices do not import inventory storage BROKEN" in private.stdout
    assert "examples.shop.orders.place_order" in private.stdout
    assert "examples.shop.inventory.storage" in private.stdout
    assert "l." in private.stdout

    orders.write_text(
        original_orders + "\nimport examples.shop.inventory\n", encoding="utf-8"
    )
    inventory.write_text(
        original_inventory + "\nimport examples.shop.orders\n", encoding="utf-8"
    )
    cycle = check()
    assert cycle.returncode == 1
    assert "Shop siblings are acyclic BROKEN" in cycle.stdout
    assert "No cycles are allowed in examples.shop" in cycle.stdout
    cycle_graph = grimp.build_graph("examples", cache_dir=None)
    breakers = cycle_graph.nominate_cycle_breakers("examples.shop")
    expected = {
        ("examples.shop.orders.place_order", "examples.shop.inventory"),
        ("examples.shop.inventory.domain", "examples.shop.orders"),
    }
    assert breakers and breakers <= expected
    for source, target in sorted(expected):
        details = cycle_graph.get_import_details(importer=source, imported=target)
        assert details and details[0]["line_number"]
        print(f"Cycle edge: {source} -> {target} (line {details[0]['line_number']})")
    print(f"Grimp nominated cycle breaker(s): {sorted(breakers)}")

    orders.write_text(
        original_orders
        + "\nfrom typing import TYPE_CHECKING\n"
        + "if TYPE_CHECKING:\n    from examples.shop.inventory.storage import Stock\n",
        encoding="utf-8",
    )
    inventory.write_text(original_inventory, encoding="utf-8")
    type_only = check()
    assert type_only.returncode == 1
    assert "Other slices do not import inventory storage BROKEN" in type_only.stdout

    imported = subprocess.run(
        [sys.executable, "-c", "import examples.shop"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert imported.returncode != 0
    assert "shop was imported" in imported.stderr
    assert marker.read_text(encoding="utf-8") == "imported"
