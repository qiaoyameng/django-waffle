from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable, Mapping

from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest

from waffle.utils import get_setting

logger = logging.getLogger('waffle')

# Per-request state attribute names.
PENDING_ATTR = '_waffle_exposure_pending'
SEEN_ATTR = '_waffle_exposure_seen'
ID_ATTR = '_waffle_exposure_request_id'

ERROR_RESPONSE = 'error_response'
EXCEPTION = 'exception'


class ExposureError(ValueError):
    """Raised when a decision handle is confirmed outside its own request."""


@dataclass(frozen=True)
class ExposureRecord:
    """The data handed to the configured exposure logger.

    Only the flag name, the final decision and explicitly approved identity
    fields are included. The ``request`` and ``user`` objects are never
    passed through.
    """
    flag_name: str
    decision: bool
    identity: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            'flag_name': self.flag_name,
            'decision': self.decision,
            'identity': dict(self.identity),
        }


@dataclass
class FlagDecision:
    """A flag evaluation result bound to one request.

    Evaluating a flag does not record an exposure. Call :meth:`confirm`
    (or :func:`confirm_exposure`) only once the feature was actually used.
    """
    request: HttpRequest
    flag_name: str
    decision: bool
    request_id: str

    def __bool__(self) -> bool:
        return self.decision

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FlagDecision):
            return self.decision == other.decision
        if isinstance(other, bool):
            return self.decision == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.decision)

    def confirm(self, identity: Mapping[str, Any] | None = None) -> bool:
        """Register the exposure for the request this decision came from.

        Returns ``True`` the first time, ``False`` when the exposure was
        already confirmed for this request (deduplication).
        """
        return stage_exposure(
            self.request, self.flag_name, self.decision,
            identity=identity, request_id=self.request_id,
        )


def evaluate(request: HttpRequest, flag_name: str, decision: bool) -> FlagDecision:
    """Build a decision handle bound to the current request."""
    request_id = getattr(request, ID_ATTR, None)
    if request_id is None:
        request_id = uuid.uuid4().hex
        setattr(request, ID_ATTR, request_id)
    return FlagDecision(
        request=request,
        flag_name=flag_name,
        decision=bool(decision),
        request_id=request_id,
    )


def _approved_identity(request: HttpRequest,
                       identity: Mapping[str, Any] | None) -> dict[str, Any]:
    """Collect only caller-approved identity fields.

    An explicit ``identity`` mapping wins outright. Otherwise the project
    must opt in by listing user attributes in
    ``WAFFLE_EXPOSURE_IDENTITY_ATTRIBUTES``; only those attributes are read.
    """
    if identity is not None:
        return dict(identity)

    attributes = get_setting('EXPOSURE_IDENTITY_ATTRIBUTES', ()) or ()
    user = getattr(request, 'user', None)
    approved: dict[str, Any] = {}
    if user is not None:
        for attribute in attributes:
            if hasattr(user, attribute):
                approved[attribute] = getattr(user, attribute)
    return approved


def stage_exposure(request: HttpRequest, flag_name: str, decision: bool,
                   identity: Mapping[str, Any] | None = None,
                   request_id: str | None = None) -> bool:
    """Stash a confirmed exposure on the originating request.

    The logger is not invoked until the response phase (see
    :mod:`waffle.middleware`), so a failing logger can never alter the
    feature decision. Repeated confirmations within the same request are
    deduplicated on ``(flag name, decision)``.
    """
    bound_id = getattr(request, ID_ATTR, None)
    if request_id is not None and bound_id != request_id:
        raise ExposureError(
            f"Flag decision for {flag_name!r} was evaluated for a different "
            "request and cannot be confirmed against this one."
        )

    seen: dict[tuple[str, bool], ExposureRecord] = getattr(request, SEEN_ATTR, None)
    if seen is None:
        seen = {}
        setattr(request, SEEN_ATTR, seen)

    decision = bool(decision)
    key = (flag_name, decision)
    if key in seen:
        return False

    record = ExposureRecord(
        flag_name=flag_name,
        decision=decision,
        identity=_approved_identity(request, identity),
    )
    seen[key] = record

    pending: list[ExposureRecord] = getattr(request, PENDING_ATTR, None)
    if pending is None:
        pending = []
        setattr(request, PENDING_ATTR, pending)
    pending.append(record)
    return True


def confirm_exposure(request: HttpRequest, flag_name: str, decision: bool,
                     identity: Mapping[str, Any] | None = None,
                     request_id: str | None = None) -> bool:
    """Public entry point for confirming an evaluated flag decision."""
    return stage_exposure(request, flag_name, decision,
                          identity=identity, request_id=request_id)


def take_pending(request: HttpRequest) -> list[ExposureRecord]:
    """Return and clear all confirmed-but-unlogged exposures on a request."""
    pending = getattr(request, PENDING_ATTR, None) or []
    if hasattr(request, PENDING_ATTR):
        delattr(request, PENDING_ATTR)
    if hasattr(request, SEEN_ATTR):
        delattr(request, SEEN_ATTR)
    return pending


def _resolve_logger() -> Callable[[ExposureRecord], Any] | None:
    configured = get_setting('EXPOSURE_LOGGER')
    if configured is None:
        return None
    if callable(configured):
        return configured
    if isinstance(configured, str):
        module_path, separator, attribute = configured.rpartition('.')
        if not separator:
            raise ImproperlyConfigured(
                f"WAFFLE_EXPOSURE_LOGGER {configured!r} is not a valid dotted path"
            )
        try:
            resolved = getattr(import_module(module_path), attribute)
        except (ImportError, AttributeError) as exc:
            raise ImproperlyConfigured(
                f"WAFFLE_EXPOSURE_LOGGER {configured!r} could not be imported"
            ) from exc
        if not callable(resolved):
            raise ImproperlyConfigured(
                f"WAFFLE_EXPOSURE_LOGGER {configured!r} is not callable"
            )
        return resolved
    raise ImproperlyConfigured(
        "WAFFLE_EXPOSURE_LOGGER must be a callable or a dotted import path"
    )


def flush(records: list[ExposureRecord]) -> None:
    """Hand confirmed exposures to the configured logger.

    Logger failures are swallowed and logged; they can never change the
    feature decision or the response.
    """
    if not records:
        return
    log_exposure = _resolve_logger()
    if log_exposure is None:
        return
    for record in records:
        try:
            log_exposure(record)
        except Exception:  # noqa: BLE001 - callbacks must not alter the decision
            logger.exception(
                'Exposure logger failed for flag %r', record.flag_name,
            )


def should_submit(kind: str) -> bool:
    """Whether configuration asks to submit exposures for a failure mode."""
    if kind == ERROR_RESPONSE:
        setting = 'LOG_EXPOSURES_ON_ERROR_RESPONSE'
    elif kind == EXCEPTION:
        setting = 'LOG_EXPOSURES_ON_EXCEPTION'
    else:
        raise ValueError(f'Unknown exposure flush kind: {kind!r}')
    value = get_setting(setting, False)
    if not isinstance(value, bool):
        raise ImproperlyConfigured(f'WAFFLE_{setting} must be a boolean')
    return value
