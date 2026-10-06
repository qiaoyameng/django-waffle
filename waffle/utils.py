from __future__ import annotations

import hashlib
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


def get_cache(using: str | None = None, force_real: bool = False) -> BaseCache:
    CACHE_NAME = get_setting('CACHE_NAME')
    cache = caches[CACHE_NAME]
    if force_real or using is None:
        return cache

    from waffle.transactions import get_transaction_cache

    transaction_cache = get_transaction_cache(using)
    return transaction_cache if transaction_cache.active else cache
