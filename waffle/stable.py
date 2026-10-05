from __future__ import annotations

import hashlib
import secrets
from decimal import Decimal
from typing import Callable

from django.http import HttpRequest

from waffle.utils import get_setting


STABLE_BUCKETS = 10_000


def generate_salt() -> str:
    """Return a new random salt for stable bucketing."""
    return secrets.token_hex(16)


def _default_identity(request: HttpRequest) -> str | None:
    """Default stable-rollout identity: the authenticated user's primary key.

    Anonymous (or unsaved) users have no stable identity.
    """
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False) and user.pk is not None:
        return f'user:{user.pk}'
    return None


def _get_identity_source() -> Callable[[HttpRequest], str | None]:
    source = get_setting('STABLE_IDENTITY')
    if callable(source):
        return source
    from django.utils.module_loading import import_string
    return import_string(source)


def get_stable_identity(request: HttpRequest) -> str | None:
    """Return the stable identity for a request, or ``None`` if unavailable."""
    identity = _get_identity_source()(request)
    if identity is None:
        return None
    return str(identity)


def stable_bucket(flag_name: str, salt: str, identity: str) -> int:
    """Deterministically map a flag/salt/identity pair to a bucket.

    Buckets are integers in the half-open range ``[0, STABLE_BUCKETS)``.
    """
    digest = hashlib.sha256(
        f'{flag_name}.{salt}.{identity}'.encode('utf-8')
    ).hexdigest()
    return int(digest[:8], 16) % STABLE_BUCKETS


def stable_is_active(flag_name: str, salt: str, identity: str, percent: Decimal) -> bool:
    """Return whether a stable rollout flag is active for an identity.

    Bucketing depends only on the flag name, flag salt and identity, so the
    result is stable across requests and clients. Increasing ``percent`` only
    adds identities to the active set; identities already active stay active.
    """
    cutoff = int(Decimal(STABLE_BUCKETS) * percent / Decimal(100))
    return stable_bucket(flag_name, salt, identity) < cutoff
