import inspect
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Any, Protocol, TypeVar

import pytest

from pymodulith import (
    Application,
    Module,
    RegistrationError,
    Scope,
    ValidationError,
    ValidationIssue,
)


class A:
    pass


class B:
    pass


class C:
    pass


class Contract(Protocol):
    def run(self) -> None: ...


def issues(*modules: Module) -> tuple[ValidationIssue, ...]:
    with pytest.raises(ValidationError) as error:
        Application("app").include(*modules).validate()
    return error.value.issues


def test_valid_graph_is_order_independent_and_cold() -> None:
    calls: list[str] = []
    owner = Module("owner")
    consumer = Module("consumer", requires=[Contract])

    @owner.provide(Contract, scope=Scope.APPLICATION, export=True)
    async def value() -> "NonexistentReturn":  # noqa: F821  # ty: ignore[unresolved-reference]
        calls.append("value")
        raise AssertionError

    @owner.resource(A, scope=Scope.APPLICATION)
    @contextmanager
    def resource(contract: "Contract") -> Generator[A]:
        calls.append("resource")
        yield A()

    @consumer.resource(B, scope=Scope.OPERATION, export=True)
    @asynccontextmanager
    async def async_resource(contract: Contract) -> AsyncGenerator[B]:
        calls.append("async resource")
        yield B()

    @consumer.lifespan
    @asynccontextmanager
    async def lifespan(*, contract: Contract) -> AsyncGenerator[None]:
        calls.append("lifespan")
        yield

    assert owner.providers[0].factory is value
    assert owner.providers[1].factory is resource
    assert consumer.providers[0].factory is async_resource
    assert consumer.lifespan_factory is lifespan
    assert Application("one").include(owner, consumer).validate()
    assert Application("two").include(consumer, owner).validate()
    assert calls == []


def test_local_precedence_and_unused_requirements() -> None:
    owner, consumer = Module("owner"), Module("consumer", requires=[B])
    owner.provide(B, scope=Scope.APPLICATION, export=True)(lambda: None)
    consumer.provide(A, scope=Scope.APPLICATION)(lambda: None)

    @consumer.provide(C, scope=Scope.APPLICATION)
    def factory(a: A) -> None:
        pass

    Application("app").include(consumer, owner).validate()


@pytest.mark.parametrize(
    "case, expected",
    [
        ("missing", "missing_export"),
        ("duplicate", "duplicate_export"),
        ("self", "self_requirement"),
        ("private_redundant", "redundant_requirement"),
        ("repeated", "redundant_requirement"),
        ("undeclared", "undeclared_dependency"),
        ("private_external", "undeclared_dependency"),
        ("provider_self", "self_dependency"),
    ],
)
def test_graph_defects(case: str, expected: str) -> None:
    owner = Module("owner")
    consumer = Module(
        "consumer",
        requires=[A]
        if case in {"missing", "self", "private_redundant", "repeated"}
        else [],
    )
    if case == "duplicate":
        owner.provide(A, scope=Scope.APPLICATION, export=True)(lambda: None)
        consumer.provide(A, scope=Scope.APPLICATION, export=True)(lambda: None)
    if case in {"self", "private_redundant"}:
        consumer.provide(A, scope=Scope.APPLICATION, export=case == "self")(
            lambda: None
        )
    if case == "repeated":
        consumer = Module("consumer", requires=[A, A])
        owner.provide(A, scope=Scope.APPLICATION, export=True)(lambda: None)
    if case == "private_external":
        owner.provide(A, scope=Scope.APPLICATION)(lambda: None)
    if case in {"undeclared", "private_external", "provider_self"}:

        def factory(a: A) -> None:
            pass

        consumer.provide(A if case == "provider_self" else B, scope=Scope.APPLICATION)(
            factory
        )
    found = issues(owner, consumer)
    assert expected in [issue.code for issue in found]
    assert all(issue.message for issue in found)
    assert all(isinstance(issue, ValidationIssue) for issue in found)


def test_identity_even_for_unhashable_equal_and_same_named_tokens() -> None:
    class Equal(type):
        def __eq__(cls, other: object) -> bool:
            return True

        __hash__ = None  # type: ignore[assignment]

    first = Equal("Token", (), {"__module__": "same"})
    second = Equal("Token", (), {"__module__": "same"})
    owner, consumer = Module("owner"), Module("consumer", requires=[first, second])
    owner.provide(first, scope=Scope.APPLICATION, export=True)(lambda: None)
    found = issues(owner, consumer)
    assert [issue.code for issue in found] == ["missing_export"]
    assert found[0].token == "same.Token"
    owner.provide(second, scope=Scope.APPLICATION, export=True)(lambda: None)
    Application("app").include(owner, consumer).validate()


def test_cycles_and_independent_defects_are_deterministic() -> None:
    left = Module("left", requires=[B])
    right = Module("right", requires=[C])
    third = Module("third", requires=[A])
    broken = Module("broken", requires=[str])

    @left.provide(A, scope=Scope.APPLICATION, export=True)
    def a(b: B) -> None:
        pass

    @right.provide(B, scope=Scope.APPLICATION, export=True)
    def b(c: C) -> None:
        pass

    @third.provide(C, scope=Scope.APPLICATION, export=True)
    def c(a: A) -> None:
        pass

    found = issues(left, right, third, broken)
    assert found == issues(broken, third, left, right)
    assert [issue.code for issue in found] == [
        "missing_export",
        "module_cycle",
        "provider_cycle",
    ]
    assert found[1].path == ("left", "right", "third", "left")
    assert found[2].path == tuple(
        f"{module}:test_validation.{token}"
        for module, token in [
            ("left", "A"),
            ("right", "B"),
            ("third", "C"),
            ("left", "A"),
        ]
    )


def test_direct_and_transitive_scope_edges_and_lifespan() -> None:
    module = Module("scope")
    module.provide(C, scope=Scope.OPERATION)(lambda: None)

    @module.provide(B, scope=Scope.APPLICATION)
    def b(c: C) -> None:
        pass

    @module.provide(A, scope=Scope.APPLICATION)
    def a(b: B) -> None:
        pass

    @module.lifespan
    def lifespan(c: C) -> None:
        pass

    found = issues(module)
    assert [issue.code for issue in found] == [
        "invalid_lifespan_scope",
        "invalid_scope",
        "invalid_scope",
    ]
    assert found[0].parameter == "c"
    assert any(len(issue.path) == 3 for issue in found if issue.code == "invalid_scope")


T = TypeVar("T")


@pytest.mark.parametrize(
    "annotation, expected",
    [
        (None, "missing_annotation"),
        ("DoesNotExist", "unresolved_annotation"),
        (Any, "unsupported_annotation"),
        (T, "unsupported_annotation"),
        (A | B, "unsupported_annotation"),
        (Annotated[A, "qualifier"], "unsupported_annotation"),
        (list[A], "unsupported_annotation"),
    ],
)
def test_invalid_annotations(annotation: object, expected: str) -> None:
    module = Module("annotations")

    def factory(dependency):
        pass

    if annotation is not None:
        factory.__annotations__["dependency"] = annotation
    module.provide(A, scope=Scope.APPLICATION)(factory)
    found = issues(module)
    assert len(found) == 1
    assert (found[0].code, found[0].module, found[0].parameter) == (
        expected,
        "annotations",
        "dependency",
    )
    assert found[0].provider and found[0].provider.endswith(".A")


@pytest.mark.parametrize(
    "factory",
    [
        lambda dependency, /: None,
        lambda *dependency: None,
        lambda **dependency: None,
        lambda dependency=None: None,
    ],
)
def test_unsupported_parameters(factory: Any) -> None:
    module = Module("parameters")
    module.provide(A, scope=Scope.APPLICATION)(factory)
    found = issues(module)
    assert [(issue.code, issue.parameter) for issue in found] == [
        ("unsupported_parameter", "dependency")
    ]


def test_invalid_signature_and_independent_parameter_failures() -> None:
    class BadSignature:
        @property
        def __signature__(self) -> Any:
            raise RuntimeError("private detail")

        def __call__(self) -> None:
            pass

    module = Module("bad")
    module.provide(A, scope=Scope.APPLICATION)(BadSignature())

    def bad(first, second: "NotFound") -> None:  # noqa: F821  # ty: ignore[unresolved-reference]
        pass

    module.provide(B, scope=Scope.APPLICATION)(bad)
    found = issues(module)
    assert [issue.code for issue in found] == [
        "invalid_signature",
        "missing_annotation",
        "unresolved_annotation",
    ]
    assert "private detail" not in str(found)
    assert not issubclass(ValidationError, RegistrationError)


def test_local_provider_takes_precedence_over_external_export() -> None:
    owner, consumer = Module("owner"), Module("consumer")
    owner.provide(A, scope=Scope.OPERATION, export=True)(lambda: None)
    consumer.provide(A, scope=Scope.APPLICATION)(lambda: None)

    @consumer.provide(B, scope=Scope.APPLICATION)
    def factory(a: A) -> None:
        pass

    Application("app").include(owner, consumer).validate()


@pytest.mark.parametrize("kind", ["missing", "unresolved", "default", "operation"])
def test_invalid_lifespan_dependencies_are_cold(kind: str) -> None:
    calls: list[str] = []
    module = Module("hooks")
    module.provide(A, scope=Scope.OPERATION)(lambda: calls.append("provider"))

    @contextmanager
    def hook(dependency):
        calls.append("hook")
        yield

    original = inspect.unwrap(hook)
    if kind == "unresolved":
        original.__annotations__["dependency"] = "Unknown"
    elif kind == "default":
        original.__annotations__["dependency"] = A
        original.__defaults__ = (None,)
    elif kind == "operation":
        original.__annotations__["dependency"] = A
    module.lifespan(hook)
    found = issues(module)
    expected = {
        "missing": "missing_annotation",
        "unresolved": "unresolved_annotation",
        "default": "unsupported_parameter",
        "operation": "invalid_lifespan_scope",
    }
    assert [(issue.code, issue.provider, issue.parameter) for issue in found] == [
        (expected[kind], "lifespan", "dependency")
    ]
    if kind != "operation":
        assert found[0].factory and "hook" in found[0].factory
    assert calls == []


def test_equivalent_declaration_order_and_parameter_locations() -> None:
    def build(reverse: bool) -> Module:
        module = Module("module", requires=[str, int] if reverse else [int, str])

        def a(dep: C) -> None:
            pass

        def b(dep: C) -> None:
            pass

        declarations = [(A, a), (B, b)]
        for token, factory in reversed(declarations) if reverse else declarations:
            module.provide(token, scope=Scope.APPLICATION)(factory)
        return module

    assert issues(build(False)) == issues(build(True))


def test_scope_diagnostics_identify_first_parameter() -> None:
    module = Module("scope")
    module.provide(B, scope=Scope.OPERATION)(lambda: None)

    def factory(dependency: B) -> None:
        pass

    module.provide(A, scope=Scope.APPLICATION)(factory)
    found = issues(module)
    assert [(issue.code, issue.parameter) for issue in found] == [
        ("invalid_scope", "dependency")
    ]


def test_forward_references_in_callable_and_partial_factories() -> None:
    from functools import partial

    class CallableFactory:
        Dependency = A

        def __call__(self, dependency: "Dependency") -> B:
            raise AssertionError

    def factory(unused: int, dependency: "A") -> C:
        raise AssertionError

    module = Module("callables")
    module.provide(A, scope=Scope.APPLICATION)(lambda: None)
    module.provide(B, scope=Scope.APPLICATION)(CallableFactory())
    module.provide(C, scope=Scope.APPLICATION)(partial(factory, 1))
    Application("app").include(module).validate()


@pytest.mark.parametrize("override", [False, True])
def test_inherited_callable_factory_annotation_namespace(override: bool) -> None:
    class Base:
        Alias = A

        def __call__(self, dependency: "Alias") -> None:
            raise AssertionError("validation must not invoke the factory")

    class Other:
        Alias = B

    class Child(Base, Other):
        pass

    class Override(Child):
        Alias = B

    module = Module("callables")
    module.provide(B if override else A, scope=Scope.APPLICATION)(lambda: None)
    module.provide(C, scope=Scope.APPLICATION)(Override() if override else Child())
    Application("app").include(module).validate()


def test_same_label_cycle_paths_are_canonical_across_registration_order() -> None:
    first = type("Same", (), {"__module__": "tokens"})
    second = type("Same", (), {"__module__": "tokens"})

    def build(reverse: bool) -> Module:
        module = Module("module")
        declarations = [(first, B), (B, second), (second, C), (C, first)]
        for token, dependency in reversed(declarations) if reverse else declarations:

            def factory(value):
                pass

            factory.__annotations__["value"] = dependency
            module.provide(token, scope=Scope.APPLICATION)(factory)
        return module

    assert issues(build(False)) == issues(build(True))
    found = issues(build(False))
    assert [issue.code for issue in found] == ["provider_cycle"]
    assert found[0].path == (
        "module:test_validation.B",
        "module:tokens.Same",
        "module:test_validation.C",
        "module:tokens.Same",
        "module:test_validation.B",
    )


def test_cycle_detection_handles_deep_and_branching_acyclic_graphs() -> None:
    from pymodulith.validation import _cycles

    chain = [{index + 1} for index in range(1199)] + [set()]
    assert _cycles(chain) == []
    chain[-1].add(0)
    assert _cycles(chain) == [(*range(1200), 0)]
    branching = [{index + 1, index + 2} for index in range(45)] + [set(), set()]
    assert _cycles(branching) == []


class ConstructorDependency:
    pass


class NewConstructor:
    def __new__(cls, dependency: "ConstructorDependency"):
        raise AssertionError("validation must not instantiate the class")


class InitConstructor:
    def __init__(self, dependency: "ConstructorDependency") -> None:
        raise AssertionError("validation must not instantiate the class")


@pytest.mark.parametrize("factory", [NewConstructor, InitConstructor])
def test_class_constructor_forward_reference(factory: type) -> None:
    module = Module("constructors")
    module.provide(ConstructorDependency, scope=Scope.APPLICATION)(
        ConstructorDependency
    )
    module.provide(factory, scope=Scope.APPLICATION)(factory)
    Application("app").include(module).validate()


@pytest.mark.parametrize("override", [False, True])
def test_inherited_constructor_annotation_namespace(override: bool) -> None:
    class Base:
        Alias = A

        def __init__(self, dependency: "Alias") -> None:
            raise AssertionError("validation must not instantiate the class")

    class Other:
        Alias = B

    class Child(Base, Other):
        pass

    class Override(Child):
        Alias = B

    factory = Override if override else Child
    module = Module("constructors")
    module.provide(B if override else A, scope=Scope.APPLICATION)(lambda: None)
    module.provide(factory, scope=Scope.APPLICATION)(factory)
    Application("app").include(module).validate()


@pytest.mark.parametrize("child_method", ["__new__", "__init__"])
def test_constructor_signature_uses_closest_method_namespace(child_method: str) -> None:
    from types import ModuleType

    base_module = ModuleType("base_constructor")
    child_module = ModuleType("child_constructor")
    vars(base_module)["Dependency"] = A
    vars(child_module)["Dependency"] = B
    base_method = "__init__" if child_method == "__new__" else "__new__"
    exec(
        f"class Base:\n    def {base_method}(self, dependency: 'Dependency'): pass",
        vars(base_module),
    )
    vars(child_module)["Base"] = vars(base_module)["Base"]
    exec(
        "class Child(Base):\n"
        f"    def {child_method}(self, dependency: 'Dependency'): pass",
        vars(child_module),
    )
    module = Module("constructors")
    module.provide(B, scope=Scope.APPLICATION)(B)
    module.provide(vars(child_module)["Child"], scope=Scope.APPLICATION)(
        vars(child_module)["Child"]
    )
    Application("app").include(module).validate()


def test_class_metaclass_call_uses_its_signature_namespace() -> None:
    from types import ModuleType

    meta_module = ModuleType("constructor_meta")
    class_module = ModuleType("constructor_class")
    vars(meta_module)["Dependency"] = B
    vars(class_module)["Dependency"] = A
    exec(
        "class Meta(type):\n    def __call__(cls, dependency: 'Dependency'): pass",
        vars(meta_module),
    )
    vars(class_module)["Meta"] = vars(meta_module)["Meta"]
    exec(
        "class Constructed(metaclass=Meta):\n"
        "    def __init__(self, dependency: 'Dependency'): pass",
        vars(class_module),
    )
    module = Module("constructors")
    module.provide(B, scope=Scope.APPLICATION)(B)
    module.provide(vars(class_module)["Constructed"], scope=Scope.APPLICATION)(
        vars(class_module)["Constructed"]
    )
    Application("app").include(module).validate()


@pytest.mark.parametrize("as_lifespan", [False, True])
@pytest.mark.parametrize("async_factory", [False, True])
def test_decorated_callable_instance_forward_reference_is_cold(
    as_lifespan: bool, async_factory: bool
) -> None:
    calls: list[str] = []

    class SyncFactory:
        @contextmanager
        def __call__(self, dependency: "A") -> Generator[B]:
            calls.append("entered")
            yield B()

    class AsyncFactory:
        @asynccontextmanager
        async def __call__(self, dependency: "A") -> AsyncGenerator[B]:
            calls.append("entered")
            yield B()

    def dependency() -> A:
        calls.append("dependency")
        return A()

    module = Module("callable_instances")
    module.provide(A, scope=Scope.APPLICATION)(dependency)
    factory = AsyncFactory() if async_factory else SyncFactory()
    if as_lifespan:
        assert module.lifespan(factory) is factory
    else:
        assert module.resource(B, scope=Scope.APPLICATION)(factory) is factory
    assert Application("app").include(module).validate()
    assert calls == []


@pytest.mark.parametrize("method_kind", ["instance", "class"])
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("override", [False, True])
def test_bound_method_annotation_namespace(
    method_kind: str, wrapped: bool, override: bool
) -> None:
    from functools import partial, wraps

    class Base:
        Alias = A

        def instance(self, dependency: "Alias") -> None:
            raise AssertionError("validation must not invoke the factory")

        @classmethod
        def class_method(cls, dependency: "Alias") -> None:
            raise AssertionError("validation must not invoke the factory")

    class Child(Base):
        Alias = B

    owner = Child if override else Base
    factory = owner().instance if method_kind == "instance" else owner.class_method
    if wrapped:

        @wraps(factory)
        def wrapper(*args, **kwargs):
            raise AssertionError("validation must not invoke the factory")

        factory = partial(wrapper)
    module = Module("methods")
    module.provide(B if override else A, scope=Scope.APPLICATION)(lambda: None)
    module.provide(C, scope=Scope.APPLICATION)(factory)
    Application("app").include(module).validate()


def test_metaclass_call_annotation_namespace() -> None:
    class BaseMeta(type):
        Alias = A

        def __call__(cls, dependency: "Alias", other: "ClassAlias") -> object:  # noqa: F821  # ty: ignore[unresolved-reference]
            raise AssertionError("validation must not invoke the factory")

    class Meta(BaseMeta):
        pass

    class Factory(metaclass=Meta):
        ClassAlias = A

    module = Module("metaclass")
    module.provide(A, scope=Scope.APPLICATION)(lambda: None)
    module.provide(B, scope=Scope.APPLICATION)(Factory)
    Application("app").include(module).validate()


def test_decorated_bound_method_annotation_namespace() -> None:
    from functools import wraps

    def decorate(method):
        @wraps(method)
        def wrapper(*args, **kwargs):
            raise AssertionError("validation must not invoke the factory")

        return wrapper

    class Factory:
        Alias = A

        @decorate
        def make(self, dependency: "Alias") -> B:
            raise AssertionError("validation must not invoke the factory")

    module = Module("decorated_method")
    module.provide(A, scope=Scope.APPLICATION)(lambda: None)
    module.provide(B, scope=Scope.APPLICATION)(Factory().make)
    Application("app").include(module).validate()


def test_lazy_annotations_ignore_return_and_aggregate_parameters() -> None:
    import sys

    if sys.version_info < (3, 14):
        pytest.skip("lazy annotations require Python 3.14")

    namespace: dict[str, Any] = {"A": A}
    exec("def valid() -> MissingReturn: pass", namespace)
    exec(
        "def invalid(first, second: MissingDependency, third: AnotherMissing, "
        "fourth: A) -> MissingReturn: pass",
        namespace,
    )
    module = Module("lazy_annotations")
    module.provide(A, scope=Scope.APPLICATION)(namespace["valid"])
    module.provide(B, scope=Scope.APPLICATION)(namespace["invalid"])
    found = issues(module)
    assert [(issue.code, issue.parameter) for issue in found] == [
        ("missing_annotation", "first"),
        ("unresolved_annotation", "second"),
        ("unresolved_annotation", "third"),
    ]
    assert all(issue.module == "lazy_annotations" and issue.factory for issue in found)


@pytest.mark.parametrize(
    "parameters, return_annotation",
    [
        ("first, second: MissingDependency", " -> (1 / 0)"),
        ("first: (1 / 0), second: MissingDependency", ""),
    ],
    ids=["return", "parameter"],
)
def test_lazy_failing_annotation_expression_is_invalid_signature(
    parameters: str, return_annotation: str
) -> None:
    import sys

    if sys.version_info < (3, 14):
        pytest.skip("lazy annotations require Python 3.14")

    calls: list[str] = []
    namespace: dict[str, Any] = {"__name__": "lazy_factory", "calls": calls}
    exec(
        f"def factory({parameters}){return_annotation}: calls.append('called')",
        namespace,
    )
    module = Module("failing_annotations")
    module.provide(A, scope=Scope.APPLICATION)(namespace["factory"])
    with pytest.raises(ValidationError) as error:
        Application("app").include(module).validate()
    assert [
        (issue.code, issue.module, issue.factory, issue.parameter)
        for issue in error.value.issues
    ] == [("invalid_signature", "failing_annotations", "lazy_factory.factory", None)]
    assert "ZeroDivisionError" in str(error.value)
    assert "Traceback" not in str(error.value)
    assert calls == []


def test_lazy_function_local_concrete_token_annotation() -> None:
    import sys

    if sys.version_info < (3, 14):
        pytest.skip("lazy annotations require Python 3.14")

    class LocalToken:
        pass

    def factory(dependency: LocalToken) -> B:
        raise AssertionError("validation must not invoke the factory")

    module = Module("local_token")
    module.provide(LocalToken, scope=Scope.APPLICATION)(lambda: None)
    module.provide(B, scope=Scope.APPLICATION)(factory)
    Application("app").include(module).validate()
