"""Run with an isolated wheel environment, from outside the source checkout."""

import asyncio
import io
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from importlib import metadata, resources
from pathlib import Path

import pymodulith
from pymodulith import (
    Application,
    Module,
    Operation,
    Provider,
    RegistrationError,
    Scope,
    ValidationError,
    ValidationIssue,
)

readme = Path(sys.argv[1]).resolve()
package_path = Path(pymodulith.__file__).resolve()
assert package_path.is_relative_to(Path(sys.prefix).resolve()), package_path
assert not package_path.is_relative_to(readme.parent), package_path
assert set(pymodulith.__all__) == {
    "Application",
    "Module",
    "Operation",
    "Provider",
    "RegistrationError",
    "Scope",
    "ValidationError",
    "ValidationIssue",
}
assert all(
    getattr(pymodulith, item.__name__) is item
    for item in (
        Application,
        Module,
        Operation,
        Provider,
        RegistrationError,
        Scope,
        ValidationError,
        ValidationIssue,
    )
)
assert resources.files("pymodulith").joinpath("py.typed").is_file()
assert not metadata.requires("pymodulith")


class Missing:
    pass


try:
    Application("invalid").include(Module("consumer", requires=[Missing])).validate()
except ValidationError as exc:
    assert any(issue.code == "missing_export" for issue in exc.issues)
else:
    raise AssertionError("validation accepted a missing export")


class FileStore:
    def __init__(self, path: Path):
        self.file = path.open("w+", encoding="utf-8")


async def exercise_resource(path: Path) -> None:
    module = Module("storage")
    closed = []

    @module.resource(FileStore, scope=Scope.OPERATION, export=True)
    @contextmanager
    def store():
        value = FileStore(path)
        try:
            yield value
        finally:
            value.file.close()
            closed.append(value.file.closed)

    app = Application("smoke").include(module)
    app.validate()
    async with app.lifespan():
        async with app.operation() as operation:
            value = await operation.get(FileStore)
            value.file.write("wheel works")
            assert not value.file.closed
        assert value.file.closed
    assert closed == [True]
    assert path.read_text(encoding="utf-8") == "wheel works"


with tempfile.TemporaryDirectory() as directory:
    asyncio.run(exercise_resource(Path(directory) / "data.txt"))

example = (
    readme.read_text(encoding="utf-8").split("```python\n", 1)[1].split("\n```", 1)[0]
)
output = io.StringIO()
with redirect_stdout(output):
    exec(compile(example, str(readme), "exec"), {"__name__": "__main__"})
assert output.getvalue() == "1\n", output.getvalue()
print(f"Installed wheel smoke passed: {package_path}")
