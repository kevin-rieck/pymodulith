"""Cold-path validation of declared composition graphs."""

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, get_origin, get_type_hints

from .registration import Module, Provider, Scope


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A graph validation diagnostic; labels describe tokens, not their identity."""

    code: str
    message: str
    module: str | None = None
    provider: str | None = None
    parameter: str | None = None
    token: str | None = None
    path: tuple[str, ...] = ()
    factory: str | None = None


class ValidationError(ValueError):
    """One or more invalid declarations in an application graph."""

    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__(
            "\n".join(f"{issue.code}: {issue.message}" for issue in issues)
        )


def _label(token: type) -> str:
    return f"{token.__module__}.{token.__qualname__}"


def _dependencies(
    factory: Callable[..., object],
    module: str,
    provider: str,
    issues: list[ValidationIssue],
) -> list[tuple[str, type]]:
    factory_name = f"{type(factory).__module__}.{type(factory).__qualname__}"
    try:
        factory_name = (
            f"{getattr(factory, '__module__', type(factory).__module__)}."
            f"{getattr(factory, '__qualname__', type(factory).__qualname__)}"
        )
        signature = inspect.signature(factory)
        target = inspect.unwrap(factory)
        while isinstance(target, partial):
            target = inspect.unwrap(target.func)
        localns_class = None
        if inspect.isclass(target):
            localns_class = target
            call = inspect.unwrap(type(target).__call__)
            methods = {
                name: inspect.unwrap(getattr(target, name))
                for name in ("__new__", "__init__")
            }
            if inspect.isfunction(call):
                target = call
            else:
                for base in target.__mro__:
                    method = next(
                        (
                            methods[name]
                            for name in ("__new__", "__init__")
                            if name in vars(base) and inspect.isfunction(methods[name])
                        ),
                        None,
                    )
                    if method is not None:
                        target = method
                        break
                else:
                    target = methods["__init__"]
        elif not inspect.isfunction(target) and not inspect.ismethod(target):
            localns_class = type(target)
            target = inspect.unwrap(target.__call__)
        localns = None
        if localns_class is not None:
            localns = {}
            for base in reversed(localns_class.__mro__):
                localns.update(vars(base))
        globalns = getattr(target, "__globals__", {})
    except Exception as exc:
        issues.append(
            ValidationIssue(
                "invalid_signature",
                f"{module}: {provider} factory {factory_name}: "
                f"cannot inspect signature ({type(exc).__name__})",
                module,
                provider,
                factory=factory_name,
            )
        )
        return []

    dependencies: list[tuple[str, type]] = []
    for parameter in signature.parameters.values():
        location = f"{module}: {provider} factory {factory_name}.{parameter.name}"
        if (
            parameter.kind
            not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            or parameter.default is not inspect.Parameter.empty
        ):
            issues.append(
                ValidationIssue(
                    "unsupported_parameter",
                    f"{location}: parameter kind or default is unsupported",
                    module,
                    provider,
                    parameter.name,
                    factory=factory_name,
                )
            )
            continue
        if parameter.annotation is inspect.Parameter.empty:
            issues.append(
                ValidationIssue(
                    "missing_annotation",
                    f"{location}: dependency annotation is missing",
                    module,
                    provider,
                    parameter.name,
                    factory=factory_name,
                )
            )
            continue

        # Return hints and unrelated bad hints must not interfere.
        def annotation_holder() -> None:
            pass

        annotation_holder.__annotations__ = {"dependency": parameter.annotation}
        try:
            token = get_type_hints(
                annotation_holder,
                globalns=globalns,
                localns=localns,
                include_extras=True,
            )["dependency"]
        except Exception as exc:
            issues.append(
                ValidationIssue(
                    "unresolved_annotation",
                    f"{location}: cannot resolve annotation ({type(exc).__name__})",
                    module,
                    provider,
                    parameter.name,
                    factory=factory_name,
                )
            )
            continue
        if not isinstance(token, type) or token is Any or get_origin(token) is not None:
            issues.append(
                ValidationIssue(
                    "unsupported_annotation",
                    f"{location}: dependency annotation is not a class or Protocol",
                    module,
                    provider,
                    parameter.name,
                    factory=factory_name,
                )
            )
            continue
        dependencies.append((parameter.name, token))
    return dependencies


def _cycles(edges: list[set[int]]) -> list[tuple[int, ...]]:
    """Enumerate directed simple cycles once, starting at their least index."""
    reverse: list[set[int]] = [set() for _ in edges]
    for node, targets in enumerate(edges):
        for target in targets:
            reverse[target].add(node)
    cycles: list[tuple[int, ...]] = []
    for start in range(len(edges)):
        reachable = {start}
        pending = [start]
        while pending:
            for node in reverse[pending.pop()]:
                if node > start and node not in reachable:
                    reachable.add(node)
                    pending.append(node)
        paths: list[tuple[int, tuple[int, ...]]] = [(start, (start,))]
        while paths:
            node, path = paths.pop()
            for target in sorted(edges[node]):
                if target == start and len(path) > 1:
                    cycles.append((*path, start))
                elif target > start and target in reachable and target not in path:
                    paths.append((target, (*path, target)))
    return cycles


def validate(modules: Sequence[Module]) -> None:
    modules = sorted(modules, key=lambda module: module.name)
    issues: list[ValidationIssue] = []
    providers: list[tuple[Module, Provider]] = [
        (module, provider)
        for module in modules
        for provider in sorted(
            module.providers, key=lambda provider: _label(provider.token)
        )
    ]
    exports: dict[int, list[int]] = {}
    for index, (_, provider) in enumerate(providers):
        if provider.export:
            exports.setdefault(id(provider.token), []).append(index)
    for owners in exports.values():
        if len(owners) > 1:
            token = _label(providers[owners[0]][1].token)
            names = tuple(providers[index][0].name for index in owners)
            issues.append(
                ValidationIssue(
                    "duplicate_export",
                    f"{token}: exported by {', '.join(names)}",
                    token=token,
                    path=names,
                )
            )

    module_indices = {module.name: index for index, module in enumerate(modules)}
    module_edges: list[set[int]] = [set() for _ in modules]
    provider_edges: list[set[int]] = [set() for _ in providers]
    edge_parameters: dict[tuple[int, int], str] = {}
    for module in modules:
        seen: set[int] = set()
        for token in sorted(module.requires, key=_label):
            key = id(token)
            label = _label(token)
            local = next(
                (provider for provider in module.providers if provider.token is token),
                None,
            )
            if key in seen:
                issues.append(
                    ValidationIssue(
                        "redundant_requirement",
                        f"{module.name}: repeated requirement {label}",
                        module.name,
                        token=label,
                    )
                )
                continue
            seen.add(key)
            if local is not None:
                code = "self_requirement" if local.export else "redundant_requirement"
                issues.append(
                    ValidationIssue(
                        code,
                        f"{module.name}: requirement {label} is locally provided",
                        module.name,
                        token=label,
                    )
                )
                continue
            owners = exports.get(key, [])
            if not owners:
                issues.append(
                    ValidationIssue(
                        "missing_export",
                        f"{module.name}: no export for required {label}",
                        module.name,
                        token=label,
                    )
                )
            elif len(owners) == 1:
                module_edges[module_indices[module.name]].add(
                    module_indices[providers[owners[0]][0].name]
                )

    def resolve(
        module: Module,
        provider_label: str,
        factory: Callable[..., object],
        source: int | None,
    ) -> None:
        for parameter, token in _dependencies(
            factory, module.name, provider_label, issues
        ):
            label = _label(token)
            local = next(
                (
                    index
                    for index, (owner, item) in enumerate(providers)
                    if owner is module and item.token is token
                ),
                None,
            )
            target = local
            if target is None and any(
                required is token for required in module.requires
            ):
                owners = exports.get(id(token), [])
                if len(owners) == 1:
                    target = owners[0]
                else:
                    continue  # The missing or ambiguous export is already diagnosed.
            if target is None:
                issues.append(
                    ValidationIssue(
                        "undeclared_dependency",
                        f"{module.name}: {provider_label}.{parameter} "
                        f"cannot resolve {label}",
                        module.name,
                        provider_label,
                        parameter,
                        label,
                    )
                )
                continue
            if source == target:
                issues.append(
                    ValidationIssue(
                        "self_dependency",
                        f"{module.name}: {provider_label}.{parameter} "
                        "depends on itself",
                        module.name,
                        provider_label,
                        parameter,
                        label,
                    )
                )
                continue
            if source is not None:
                provider_edges[source].add(target)
                edge = (source, target)
                edge_parameters[edge] = min(
                    parameter, edge_parameters.get(edge, parameter)
                )
            if source is None and providers[target][1].scope is Scope.OPERATION:
                issues.append(
                    ValidationIssue(
                        "invalid_lifespan_scope",
                        f"{module.name}: lifespan.{parameter} depends on "
                        f"operation provider {label}",
                        module.name,
                        provider_label,
                        parameter,
                        label,
                    )
                )

    for index, (module, provider) in enumerate(providers):
        resolve(module, _label(provider.token), provider.factory, index)
    for module in modules:
        if module.lifespan_factory is not None:
            resolve(module, "lifespan", module.lifespan_factory, None)

    provider_names = [
        f"{module.name}:{_label(provider.token)}" for module, provider in providers
    ]
    for index, (module, provider) in enumerate(providers):
        if provider.scope is not Scope.APPLICATION:
            continue
        visited = {index}
        pending: list[tuple[int, tuple[int, ...]]] = [(index, (index,))]
        while pending:
            node, path = pending.pop()
            for target in sorted(
                provider_edges[node],
                key=lambda target: (
                    provider_names[target],
                    edge_parameters[node, target],
                ),
                reverse=True,
            ):
                if target in visited:
                    continue
                visited.add(target)
                target_path = (*path, target)
                if providers[target][1].scope is Scope.OPERATION:
                    labels = tuple(provider_names[item] for item in target_path)
                    issues.append(
                        ValidationIssue(
                            "invalid_scope",
                            "application provider depends on operation provider: "
                            f"{' -> '.join(labels)}",
                            module.name,
                            _label(provider.token),
                            parameter=edge_parameters[index, target_path[1]],
                            token=_label(providers[target][1].token),
                            path=labels,
                        )
                    )
                pending.append((target, target_path))
    for code, edges, names in (
        ("provider_cycle", provider_edges, provider_names),
        ("module_cycle", module_edges, [module.name for module in modules]),
    ):
        for cycle in _cycles(edges):
            labels = tuple(names[index] for index in cycle[:-1])
            path = min(labels[index:] + labels[:index] for index in range(len(labels)))
            path = (*path, path[0])
            issues.append(
                ValidationIssue(
                    code, f"{code.replace('_', ' ')}: {' -> '.join(path)}", path=path
                )
            )

    if issues:
        issues.sort(
            key=lambda issue: (
                issue.code,
                issue.module or "",
                issue.provider or "",
                issue.parameter or "",
                issue.token or "",
                issue.path,
                issue.message,
            )
        )
        raise ValidationError(issues)
