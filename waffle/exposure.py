"""Explicit exposure tracking for flags.

Calling :func:`waffle.flag_is_active` only computes a decision; it does not
prove the flag was ever shown to a user. To measure real exposure, callers
evaluate a flag to get a :class:`FlagDecision` handle and explicitly
:meth:`~FlagDecision.confirm` it once the feature is actually used.

The handle is bound to the request that produced it, the flag name and the
final decision. It cannot be confirmed from a different request and
repeated confirmations within the same request are de-duplicated.

Recorded events only ever contain identity fields the caller explicitly
approved via ``WAFFLE_EXPOSURE_IDENTITY_FIELDS``; the request and user
objects are never copied into an event.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable

from django.http import HttpRequest

from waffle.utils import get_setting

logger = logging.getLogger('waffle')

STATE_ATTR = '_waffle_exposure_state'

Recorder = Callable[['ExposureEvent'], Any]


class ExposureError(RuntimeError):
    """Base class for exposure tracking errors."""


class ExposureStateClosed(ExposureError):
    """Raised when a decision is confirmed outside of its request."""


class ReadOnlyExposureError(ExposureError):
    """Raised when a read-only decision is asked to record exposure."""


@dataclass(frozen=True)
class ExposureEvent:
    """A single confirmed exposure.

    Only primitive, caller-approved values are stored here -- never the
    request or the user object.
    """

    flag_name: str
    active: bool
    identity: dict[str, Any] = field(default_factory=dict)


def default_user_id(request: HttpRequest) -> Any:
    """Return the authenticated user's pk, if available."""
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        return getattr(user, 'pk', None)
    return None


def default_session_key(request: HttpRequest) -> Any:
    """Return the current session key, if a session exists."""
    session = getattr(request, 'session', None)
    if session is not None:
        return session.session_key
    return None


def _load_callable(value: Any) -> Callable[..., Any] | None:
    if value is None:
        return None
    if callable(value):
        return value
    if isinstance(value, str):
        module_path, _, attr = value.rpartition('.')
        if not module_path:
            raise ValueError(f"'{value}' is not a dotted import path")
        return getattr(import_module(module_path), attr)
    raise TypeError(f"Expected a callable or dotted path, got {value!r}")


def get_recorder() -> Recorder | None:
    return _load_callable(get_setting('EXPOSURE_RECORDER'))


def approved_identity_fields() -> tuple[str, ...]:
    fields = get_setting('EXPOSURE_IDENTITY_FIELDS', ())
    return tuple(fields)


def get_exposure_state(request: HttpRequest, create: bool = False) -> 'ExposureState | None':
    state = getattr(request, STATE_ATTR, None)
    if state is None and create:
        state = ExposureState(request)
        setattr(request, STATE_ATTR, state)
    return state


class ExposureState:
    """Per-request bookkeeping for pending confirmed exposures."""

    def __init__(self, request: HttpRequest) -> None:
        self.request = request
        self.closed = False
        self._pending: dict[str, ExposureEvent] = {}
        self._recorded: set[str] = set()

    def add(self, event: ExposureEvent) -> bool:
        """Buffer a confirmed exposure; return False on de-duplication."""
        if self.closed:
            raise ExposureStateClosed(
                'This flag decision belongs to a request that has already '
                'finished; it cannot be confirmed from another request.'
            )
        if event.flag_name in self._pending or event.flag_name in self._recorded:
            return False
        self._pending[event.flag_name] = event
        return True

    def flush(
        self,
        *,
        submit: bool = True,
        recorder: Recorder | None = None,
    ) -> None:
        """Submit buffered exposures through the recorder and seal the state.

        Recorder failures are logged but never propagated: recording an
        exposure must never alter or overturn the flag decision.
        """
        recorder = recorder if recorder is not None else get_recorder()
        if submit and recorder is not None:
            for flag_name, event in list(self._pending.items()):
                try:
                    recorder(event)
                except Exception:
                    logger.exception(
                        'Exposure recorder failed for flag %r; the flag '
                        'decision is unaffected.', flag_name
                    )
                self._recorded.add(flag_name)
        self._pending.clear()
        self.closed = True


class FlagDecision:
    """A confirmable handle for a flag evaluation.

    ``bool(decision)`` is the final decision. The handle is bound to the
    originating request and is used once the feature is actually rendered
    or otherwise consumed.
    """

    __slots__ = ('flag_name', 'active', 'read_only', '_state')

    def __init__(
        self,
        flag_name: str,
        active: bool,
        state: ExposureState,
        read_only: bool = False,
    ) -> None:
        self.flag_name = flag_name
        self.active = bool(active)
        self.read_only = read_only
        self._state = state

    def __bool__(self) -> bool:
        return self.active

    def _build_event(self, identity: dict[str, Any]) -> ExposureEvent:
        return ExposureEvent(
            flag_name=self.flag_name,
            active=self.active,
            identity=identity,
        )

    def _resolve_identity(self) -> dict[str, Any]:
        """Pull approved identity fields off the request."""
        resolvers = get_setting('EXPOSURE_IDENTITY_RESOLVERS', {}) or {}
        identity: dict[str, Any] = {}
        for field_name in approved_identity_fields():
            path = resolvers.get(field_name)
            if path is None:
                continue
            resolver = _load_callable(path)
            if resolver is None:
                continue
            value = resolver(self._state.request)
            if value is not None:
                identity[field_name] = value
        return identity

    def confirm(self, **identity: Any) -> bool:
        """Register that the feature was actually used.

        Keyword arguments are recorded as the exposure identity, but only
        keys present in ``WAFFLE_EXPOSURE_IDENTITY_FIELDS`` are accepted;
        any other key raises ``ValueError``. With no arguments the approved
        fields are resolved from the request.

        Returns ``True`` when this is the first confirmation for the flag
        in this request and ``False`` when it is de-duplicated.
        """
        if self.read_only:
            raise ReadOnlyExposureError(
                f"Flag {self.flag_name!r} was evaluated read-only; read-only "
                "checks and background prewarming never record exposure."
            )
        approved = approved_identity_fields()
        unexpected = set(identity) - set(approved)
        if unexpected:
            raise ValueError(
                'Identity field(s) not approved by '
                'WAFFLE_EXPOSURE_IDENTITY_FIELDS: '
                + ', '.join(sorted(unexpected))
            )
        if not identity:
            identity = self._resolve_identity()
        return self._state.add(self._build_event(identity))
