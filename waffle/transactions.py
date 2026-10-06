from __future__ import annotations

from contextlib import ContextDecorator
from threading import local
from typing import Any, Callable, Iterable

from django.db import transaction

from waffle.utils import get_cache

_MISSING = object()


class TransactionCacheLayer:
    def __init__(self) -> None:
        self.values: dict[tuple[Any, str], Any] = {}
        self.deleted_keys: set[tuple[Any, str]] = set()
        self.cleared = False


class TransactionCache:
    def __init__(self, using: str) -> None:
        self.using = using
        self.layers: list[TransactionCacheLayer] = []

    @property
    def active(self) -> bool:
        return bool(self.layers)

    def push(self) -> None:
        self.layers.append(TransactionCacheLayer())

    def merge(self) -> None:
        layer = self.layers.pop()
        if self.layers:
            parent = self.layers[-1]
            parent.values.update(layer.values)
            parent.deleted_keys.update(layer.deleted_keys)
            parent.cleared = parent.cleared or layer.cleared

    def discard(self) -> None:
        self.layers.pop()

    def flush(self) -> None:
        layers = self.layers
        deleted_keys: set[tuple[Any, str]] = set()
        cleared = False

        for layer in reversed(layers):
            deleted_keys.update(layer.deleted_keys)
            cleared = cleared or layer.cleared

        cache = get_cache(force_real=True)
        if cleared:
            cache.clear()
        if deleted_keys:
            keys_by_version: dict[Any, set[str]] = {}
            for version, key in deleted_keys:
                keys_by_version.setdefault(version, set()).add(key)
            for version, keys in keys_by_version.items():
                if version is None:
                    cache.delete_many(keys)
                else:
                    cache.delete_many(keys, version=version)

        layers.clear()

    def get(self, key: str, default: Any = None, version: Any = None) -> Any:
        cache_key = (version, key)
        found = False
        value = _MISSING

        for layer in reversed(self.layers):
            if cache_key in layer.values:
                found = True
                value = layer.values[cache_key]
                break
            if cache_key in layer.deleted_keys:
                return default
            if layer.cleared:
                return default

        if found:
            return value
        result = get_cache(force_real=True).get(key, _MISSING, version=version)
        return default if result is _MISSING else result

    def get_many(self, keys: Iterable[str], version: Any = None) -> dict[str, Any]:
        result = {}
        for key in keys:
            value = self.get(key, _MISSING, version=version)
            if value is not _MISSING:
                result[key] = value
        return result

    def set(
        self,
        key: str,
        value: Any,
        timeout: float | None = None,
        version: Any = None,
    ) -> None:
        self.layers[-1].values[(version, key)] = value

    def add(
        self,
        key: str,
        value: Any,
        timeout: float | None = None,
        version: Any = None,
    ) -> bool:
        existing = self.get(key, _MISSING, version=version)
        if existing is not _MISSING:
            return False
        self.set(key, value, timeout, version)
        return True

    def delete(self, key: str, version: Any = None) -> bool:
        layer = self.layers[-1]
        cache_key = (version, key)
        layer.deleted_keys.add(cache_key)
        layer.values.pop(cache_key, None)
        return True

    def delete_many(self, keys: Iterable[str], version: Any = None) -> None:
        for key in keys:
            self.delete(key, version=version)

    def clear(self) -> None:
        layer = self.layers[-1]
        layer.cleared = True
        layer.deleted_keys.clear()
        layer.values.clear()

    def __getattr__(self, name: str) -> Any:
        return getattr(get_cache(force_real=True), name)


_state = local()


def _get_state() -> dict[str, TransactionCache]:
    caches = getattr(_state, 'caches', None)
    if caches is None:
        caches = {}
        _state.caches = caches
    return caches


def get_transaction_cache(using: str) -> TransactionCache:
    caches = _get_state()
    cache = caches.get(using)
    if cache is None:
        cache = TransactionCache(using)
        caches[using] = cache
    return cache


class transactional_atomic(ContextDecorator):
    def __init__(
        self,
        using: str = 'default',
        savepoint: bool = True,
        durable: bool = False,
    ) -> None:
        self.using = using
        self.atomic = transaction.atomic(using, savepoint, durable)
        self.transaction_cache = get_transaction_cache(using)
        self._outermost = False
        self._savepoint_id = None

    def __enter__(self) -> transactional_atomic:
        self.atomic.__enter__()
        self._outermost = len(self.transaction_cache.layers) == 0
        savepoint_ids = transaction.get_connection(self.using).savepoint_ids
        self._savepoint_id = savepoint_ids[-1] if savepoint_ids else None
        self.transaction_cache.push()
        if self._outermost:
            transaction.on_commit(self.transaction_cache.flush, using=self.using)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool | None:
        connection = transaction.get_connection(self.using)
        discard_layer = (
            exc_type is not None
            or (
                connection.needs_rollback
                and (self._outermost or self._savepoint_id is not None)
            )
        )
        try:
            result = self.atomic.__exit__(exc_type, exc_value, traceback)
        except BaseException:
            self.transaction_cache.discard()
            raise
        if discard_layer:
            self.transaction_cache.discard()
        elif not self._outermost:
            self.transaction_cache.merge()
        return result


def atomic(
    using: str | Callable[..., Any] | None = 'default',
    savepoint: bool = True,
    durable: bool = False,
):
    if callable(using):
        view = using
        return transactional_atomic()(view)
    if using is None:
        using = 'default'
    return transactional_atomic(using, savepoint, durable)


transaction_atomic = atomic
transaction_edit = transactional_atomic
