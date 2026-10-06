from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.http import HttpRequest

from waffle.utils import get_setting

if TYPE_CHECKING:
    from waffle.models import FlagGroup, FlagGroupMembership

# Shared bucket space for every experiment group. Buckets are integers in
# the half-open range [0, BUCKET_COUNT); 100% of traffic == BUCKET_COUNT.
BUCKET_COUNT = 10000


class Assignment:
    """Why a group request ended up (or not) on a variant."""
    UNGROUPED = 'ungrouped'
    EMPTY_GROUP = 'empty_group'
    NO_IDENTITY = 'no_identity'
    EMPTY_BUCKET = 'empty_bucket'
    BUCKET = 'bucket'
    FORCED = 'forced'
    FORCED_OFF = 'forced_off'
    CONFLICT = 'conflict'
    MISSING_GROUP = 'missing_group'


@dataclass
class GroupDecision:
    """The result of evaluating a mutually exclusive experiment group."""
    group_name: str
    flag_name: str | None = None
    active: bool = False
    bucket: int | None = None
    identity: str | None = None
    reason: str = Assignment.UNGROUPED
    interval: tuple[int, int] | None = None
    forced_flags: tuple[str, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return self.active


def stable_bucket(group_name: str, identity: str,
                  bucket_count: int = BUCKET_COUNT) -> int:
    """Deterministic, uniform bucket for ``identity`` inside ``group_name``."""
    digest = hashlib.blake2b(
        f'waffle-group:{group_name}:{identity}'.encode('utf-8'),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, 'big') % bucket_count


def resolve_identity(request: HttpRequest, group_name: str,
                     identity: str | None = None,
                     read_only: bool = False) -> str | None:
    """Return a stable identity for the request.

    Precedence: explicit ``identity`` argument -> authenticated user id ->
    sticky group cookie -> (when allowed to write) a freshly minted identity
    that the middleware persists. Read-only anonymous traffic with no cookie
    cannot be bucketed and returns ``None``.
    """
    if identity is not None:
        return str(identity)

    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        return f'user:{user.pk}'

    cookie_name = get_setting('GROUP_COOKIE') % group_name
    if cookie_name in request.COOKIES:
        return f'anon:{request.COOKIES[cookie_name]}'

    if read_only:
        return None

    minted = uuid.uuid4().hex
    if not hasattr(request, 'waffle_groups'):
        request.waffle_groups = {}
    request.waffle_groups[cookie_name] = minted
    return f'anon:{minted}'


def evaluate_group(request: HttpRequest, group: "FlagGroup",
                   identity: str | None = None,
                   read_only: bool = False) -> GroupDecision:
    """Evaluate every rule for a group exactly once per request."""
    cache_key = (group.name, read_only) if identity is None else None
    if cache_key is not None:
        cached = getattr(request, '_waffle_group_decisions', None)
        if cached and cache_key in cached:
            return cached[cache_key]

    decision = _evaluate_group(request, group, identity, read_only)

    if cache_key is not None:
        if not hasattr(request, '_waffle_group_decisions'):
            request._waffle_group_decisions = {}
        request._waffle_group_decisions[cache_key] = decision
    return decision


def _evaluate_group(request: HttpRequest, group: "FlagGroup",
                    identity: str | None, read_only: bool) -> GroupDecision:
    intervals = group.intervals()
    if not intervals:
        return GroupDecision(
            group_name=group.name, reason=Assignment.EMPTY_GROUP)

    # Single deterministic, database-order-independent source for both the
    # precedence order and the bucket layout.
    ordered = [membership for membership, _start, _end in intervals]

    forced: list[FlagGroupMembership] = []
    forced_values: dict[str, bool] = {}
    for membership in ordered:
        value = membership.flag.forced_value(request)
        if value is not None:
            forced_values[membership.flag.name] = value
            if value:
                forced.append(membership)

    if len(forced) > 1:
        # Targeting/testing forces more than one variant on. The configured
        # policy decides; picking the database's first row is never allowed.
        if group.on_conflict == group.ConflictPolicy.PRIORITY:
            winner = forced[0]
            decision = GroupDecision(
                group_name=group.name,
                flag_name=winner.flag.name,
                active=True,
                reason=Assignment.FORCED,
                forced_flags=tuple(sorted(forced_values)),
            )
        else:
            decision = GroupDecision(
                group_name=group.name,
                active=False,
                reason=Assignment.CONFLICT,
                forced_flags=tuple(sorted(forced_values)),
            )
        return decision

    resolved_identity = resolve_identity(
        request, group.name, identity=identity, read_only=read_only)
    if resolved_identity is None:
        return GroupDecision(
            group_name=group.name, reason=Assignment.NO_IDENTITY)
    bucket = stable_bucket(group.name, resolved_identity)

    if forced:
        winner = forced[0]
        return GroupDecision(
            group_name=group.name,
            flag_name=winner.flag.name,
            active=True,
            bucket=bucket,
            identity=resolved_identity,
            reason=Assignment.FORCED,
            forced_flags=tuple(sorted(forced_values)),
        )

    for membership, start, end in intervals:
        if start <= bucket < end:
            if forced_values.get(membership.flag.name) is False:
                return GroupDecision(
                    group_name=group.name,
                    flag_name=membership.flag.name,
                    active=False,
                    bucket=bucket,
                    identity=resolved_identity,
                    reason=Assignment.FORCED_OFF,
                    interval=(start, end),
                    forced_flags=tuple(sorted(forced_values)),
                )
            return GroupDecision(
                group_name=group.name,
                flag_name=membership.flag.name,
                active=True,
                bucket=bucket,
                identity=resolved_identity,
                reason=Assignment.BUCKET,
                interval=(start, end),
                forced_flags=tuple(sorted(forced_values)),
            )

    # Bucket beyond the configured percentages: not taking part.
    return GroupDecision(
        group_name=group.name,
        active=False,
        bucket=bucket,
        identity=resolved_identity,
        reason=Assignment.EMPTY_BUCKET,
    )
