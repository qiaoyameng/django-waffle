from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.cache import BaseCache, caches

import waffle
from waffle import defaults


def get_setting(name: str, default: Any = None) -> Any:
    try:
        return getattr(settings, 'WAFFLE_' + name)
    except AttributeError:
        return getattr(defaults, name, default)


def keyfmt(k: str, v: str | None = None) -> str:
    prefix = get_setting('CACHE_PREFIX') + waffle.__version__
    if v is None:
        key = prefix + k
    else:
        key = prefix + hashlib.md5((k % v).encode('utf-8')).hexdigest()
    return key


def get_cache() -> BaseCache:
    CACHE_NAME = get_setting('CACHE_NAME')
    return caches[CACHE_NAME]


def rollout_bucket(identifier: str, flag_name: str, salt: str) -> int:
    """Return a stable bucket in [0, bucket_count) for an identity.

    The bucket is a keyed hash of the flag name and the identity, so it is
    stable across requests and clients. Rotating ``salt`` deliberately
    reassigns every identity to a new bucket.
    """
    bucket_count = get_setting('ROLLOUT_BUCKET_COUNT')
    payload = f'{flag_name}\n{identifier}'.encode('utf-8')
    digest = hmac.new(salt.encode('utf-8'), payload, hashlib.sha256).hexdigest()
    return int(digest, 16) % int(bucket_count)


def identity_in_percent(bucket: int, percent: Any) -> bool:
    """Compare a bucket against a percentage using the configured scale."""
    bucket_count = Decimal(get_setting('ROLLOUT_BUCKET_COUNT'))
    return Decimal(bucket) < Decimal(str(percent)) * bucket_count / Decimal(100)
