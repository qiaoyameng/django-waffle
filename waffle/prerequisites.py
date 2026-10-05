from __future__ import annotations

from collections.abc import Callable
from typing import Any


UNKNOWN_PREREQUISITE = object()


class InvalidPrerequisiteError(ValueError):
    """The prerequisite expression is not well-formed."""


class CircularFlagDependencyError(Exception):
    """Evaluating a flag requires evaluating that same flag again."""

    def __init__(self, flag_name: str, path: list[str] | None = None):
        self.flag_name = flag_name
        self.path = path or [flag_name]
        super().__init__(
            f"Circular flag dependency: {' -> '.join(self.path + [flag_name])}"
        )


def parse_prerequisites(value: Any) -> dict[str, Any] | None:
    if value in (None, '', {}):
        return None
    return _parse_node(value)


def get_prerequisite_flag_names(value: Any) -> set[str]:
    condition = parse_prerequisites(value)
    if condition is None:
        return set()

    names: set[str] = set()
    nodes = [condition]
    while nodes:
        node = nodes.pop()
        if 'flag' in node:
            names.add(node['flag'])
        else:
            nodes.extend(node['conditions'])
    return names


def evaluate_prerequisites(
    value: Any,
    resolve_flag: Callable[[str], bool | object],
) -> bool | object:
    condition = parse_prerequisites(value)
    if condition is None:
        return True
    return _evaluate_node(condition, resolve_flag)


def _parse_node(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        if not value.strip():
            raise InvalidPrerequisiteError(
                "A prerequisite flag name cannot be empty."
            )
        return {'flag': value, 'active': True}

    if not isinstance(value, dict):
        raise InvalidPrerequisiteError('Prerequisites must be a JSON object.')

    operator_keys = ('all', 'any', 'not')
    present_operators = [key for key in operator_keys if key in value]
    if len(present_operators) > 1:
        raise InvalidPrerequisiteError(
            'A prerequisite node may contain only one boolean operator.'
        )

    operator = present_operators[0] if present_operators else None
    if operator in ('all', 'any'):
        if set(value) != {operator}:
            raise InvalidPrerequisiteError(
                f"The '{operator}' prerequisite may only contain a non-empty list."
            )
        children = value[operator]
        if not isinstance(children, (list, tuple)) or not children:
            raise InvalidPrerequisiteError(
                f"The '{operator}' prerequisite must contain a non-empty list."
            )
        return {
            'operator': operator,
            'conditions': [_parse_node(child) for child in children],
        }

    if operator == 'not':
        if set(value) != {'not'}:
            raise InvalidPrerequisiteError(
                "The 'not' prerequisite may only contain one condition."
            )
        return {'operator': 'not', 'conditions': [_parse_node(value['not'])]}

    flag_keys = ('flag', 'flag_name', 'name')
    present_flag_keys = [key for key in flag_keys if key in value]
    if len(present_flag_keys) != 1:
        raise InvalidPrerequisiteError(
            "A prerequisite must include exactly one 'flag' name."
        )
    flag_name = value[present_flag_keys[0]]
    if not isinstance(flag_name, str) or not flag_name.strip():
        raise InvalidPrerequisiteError(
            "A prerequisite must include a non-empty 'flag' name."
        )

    active: Any = True
    active_keys = (
        'active', 'is_active', 'enabled', 'on', 'state', 'expected', 'value'
    )
    present_active_keys = [key for key in active_keys if key in value]
    if len(present_active_keys) > 1:
        raise InvalidPrerequisiteError(
            'A prerequisite may include only one expected flag state.'
        )
    if present_active_keys:
        active = value[present_active_keys[0]]
    if not isinstance(active, bool):
        raise InvalidPrerequisiteError(
            f"The expected state of prerequisite flag '{flag_name}' must be a boolean."
        )
    allowed_keys = {present_flag_keys[0], *present_active_keys}
    if set(value) != allowed_keys:
        raise InvalidPrerequisiteError('Prerequisite contains unknown properties.')

    return {'flag': flag_name, 'active': active}


def _evaluate_node(
    node: dict[str, Any],
    resolve_flag: Callable[[str], bool | object],
) -> bool | object:
    if 'flag' in node:
        value = resolve_flag(node['flag'])
        if value is UNKNOWN_PREREQUISITE:
            return UNKNOWN_PREREQUISITE
        return bool(value) is node['active']

    operator = node['operator']
    if operator == 'not':
        value = _evaluate_node(node['conditions'][0], resolve_flag)
        if value is UNKNOWN_PREREQUISITE:
            return UNKNOWN_PREREQUISITE
        return not value

    if operator == 'all':
        for child in node['conditions']:
            value = _evaluate_node(child, resolve_flag)
            if value is UNKNOWN_PREREQUISITE:
                return UNKNOWN_PREREQUISITE
            if not value:
                return False
        return True

    for child in node['conditions']:
        value = _evaluate_node(child, resolve_flag)
        if value is UNKNOWN_PREREQUISITE:
            return UNKNOWN_PREREQUISITE
        if value:
            return True
    return False
