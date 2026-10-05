from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.http import HttpRequest

from waffle.utils import get_setting

if TYPE_CHECKING:
    from waffle.models import AbstractBaseFlag

CONTEXT_ATTR = 'waffle_prerequisite_context'

STATE_ON = 'on'
STATE_OFF = 'off'
VALID_STATES = (STATE_ON, STATE_OFF)

UNKNOWN_INACTIVE = 'inactive'
UNKNOWN_ERROR = 'error'
VALID_UNKNOWN_POLICIES = (UNKNOWN_INACTIVE, UNKNOWN_ERROR)


class PrerequisiteError(Exception):
    """Base error for flag prerequisite problems."""


class PrerequisiteCycleError(PrerequisiteError):
    """Raised when a cycle is detected while evaluating prerequisites."""


class UnknownPrerequisiteFlagError(PrerequisiteError):
    """Raised when a prerequisite references a flag that does not exist
    and the WAFFLE_PREREQUISITE_UNKNOWN_FLAG setting is 'error'."""


def _get_context(request: HttpRequest) -> dict[str, Any]:
    """Return the per-request evaluation context.

    The context lives on the request object, so cached dependency results
    are only shared within a single request and never leak across users.
    """
    context = getattr(request, CONTEXT_ATTR, None)
    if context is None:
        context = {'results': {}, 'in_progress': []}
        setattr(request, CONTEXT_ATTR, context)
    return context


def iter_flag_names(node: Any) -> Iterator[str]:
    """Yield every flag name referenced by a prerequisite spec."""
    if not isinstance(node, dict):
        return
    if 'all' in node or 'any' in node:
        children = node.get('all', node.get('any'))
        if isinstance(children, list):
            for child in children:
                yield from iter_flag_names(child)
    elif 'flag' in node:
        name = node['flag']
        if isinstance(name, str):
            yield name


def validate_spec(spec: Any) -> None:
    """Validate the structure of a prerequisite spec.

    Raises ValidationError if the spec is malformed.
    """
    _validate_node(spec, path='prerequisites')


def _validate_node(node: Any, path: str) -> None:
    if not isinstance(node, dict):
        raise ValidationError(
            f'{path}: each node must be an object, got {node!r}')
    kinds = [key for key in ('all', 'any', 'flag') if key in node]
    if len(kinds) != 1:
        raise ValidationError(
            f"{path}: each node must contain exactly one of "
            f"'all', 'any' or 'flag', got {sorted(node)}")
    kind = kinds[0]
    if kind in ('all', 'any'):
        children = node[kind]
        if not isinstance(children, list) or not children:
            raise ValidationError(
                f"{path}: '{kind}' must be a non-empty list of nodes")
        for index, child in enumerate(children):
            _validate_node(child, path=f'{path}.{kind}[{index}]')
    else:
        name = node['flag']
        if not isinstance(name, str) or not name:
            raise ValidationError(
                f"{path}: 'flag' must be a non-empty flag name")
        state = node.get('state', STATE_ON)
        if state not in VALID_STATES:
            raise ValidationError(
                f"{path}: 'state' must be one of {VALID_STATES}, "
                f"got {state!r}")


def find_cycle(flag_model: type[AbstractBaseFlag], root_name: str,
               root_spec: Any) -> list[str] | None:
    """Return a cycle path (e.g. ['a', 'b', 'a']) reachable from
    root_name given its (not yet saved) spec, or None.

    Specs for other flags are read straight from the database so the
    check reflects what would be persisted.
    """
    state: dict[str, str] = {}
    path: list[str] = []

    def spec_for(name: str) -> Any:
        if name == root_name:
            return root_spec
        try:
            other = flag_model.get_from_db(name)
        except flag_model.DoesNotExist:
            return None
        return other.prerequisites

    def visit(name: str) -> list[str] | None:
        state[name] = 'visiting'
        path.append(name)
        spec = spec_for(name)
        if spec:
            for dep in iter_flag_names(spec):
                if state.get(dep) == 'visiting':
                    return path[path.index(dep):] + [dep]
                if state.get(dep) is None:
                    cycle = visit(dep)
                    if cycle:
                        return cycle
        path.pop()
        state[name] = 'done'
        return None

    return visit(root_name)


def evaluate_prerequisites(flag: AbstractBaseFlag, request: HttpRequest,
                           read_only: bool = False) -> bool:
    """Evaluate a flag's prerequisite spec for this request.

    Results of dependency flags are memoized on the request, so each
    dependency is evaluated at most once per request. Raises
    PrerequisiteCycleError if a dependency cycle is detected at
    evaluation time (e.g. introduced by a concurrent config update).
    """
    context = _get_context(request)
    if flag.name in context['in_progress']:
        chain = context['in_progress']
        cycle = chain[chain.index(flag.name):] + [flag.name]
        raise PrerequisiteCycleError(
            'Prerequisite cycle detected: %s' % ' -> '.join(cycle))
    context['in_progress'].append(flag.name)
    try:
        return _evaluate_node(flag.prerequisites, type(flag), request,
                              context, read_only)
    finally:
        context['in_progress'].pop()


def _evaluate_node(node: dict, flag_model: type[AbstractBaseFlag],
                   request: HttpRequest, context: dict[str, Any],
                   read_only: bool) -> bool:
    # Short-circuiting is deliberate: branches that are never evaluated
    # must not evaluate their flags, so they cannot set percent cookies
    # or record exposure state on the request.
    if 'all' in node:
        for child in node['all']:
            if not _evaluate_node(child, flag_model, request, context,
                                  read_only):
                return False
        return True
    if 'any' in node:
        for child in node['any']:
            if _evaluate_node(child, flag_model, request, context,
                              read_only):
                return True
        return False
    return _evaluate_leaf(node, flag_model, request, context, read_only)


def _evaluate_leaf(node: dict, flag_model: type[AbstractBaseFlag],
                   request: HttpRequest, context: dict[str, Any],
                   read_only: bool) -> bool:
    name = node['flag']
    if name in context['results']:
        active = context['results'][name]
    else:
        dependency = flag_model.get(name)
        if not dependency.pk:
            active = _unknown_flag_active(name)
        else:
            active = bool(dependency.is_active(request, read_only=read_only))
        context['results'][name] = active
    state = node.get('state', STATE_ON)
    return active if state == STATE_ON else not active


def _unknown_flag_active(name: str) -> bool:
    policy = get_setting('PREREQUISITE_UNKNOWN_FLAG')
    if policy == UNKNOWN_ERROR:
        raise UnknownPrerequisiteFlagError(
            f'Prerequisite references unknown flag {name!r}')
    # Explicit fail-closed default: an unknown prerequisite flag is
    # treated as inactive.
    return False
