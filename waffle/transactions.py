from __future__ import annotations

from contextlib import contextmanager
from threading import local
from typing import Any, Iterator

from django.core.cache import BaseCache
from django.db import DEFAULT_DB_ALIAS, connections, models, router, transaction

from waffle.utils import get_cache as get_shared_cache


_state = local()
_missing = object()
_deleted = object()
CACHE_EMPTY = '-'


class CacheLayer:
    def __init__(self) -> None:
        self.values: dict[tuple[str, int | None], Any] = {}
        self.deletes: set[tuple[str, int | None]] = set()
        self.cleared = False

    def lookup(self, key: tuple[str, int | None]) -> Any:
        if key in self.deletes:
            return _deleted
        if key in self.values:
            return self.values[key]
        return None

    def commit(self, parent: "CacheLayer") -> None:
        if self.cleared:
            parent.cleared = True
            parent.values = {**parent.values, **self.values}
            parent.deletes.update(self.deletes)
            return

        for key, value in self.values.items():
            parent.values[key] = value
        for key in self.deletes:
            parent.values.pop(key, None)
            parent.deletes.add(key)


class TransactionalCache:
    def __init__(self, cache: BaseCache, layers: list[CacheLayer]) -> None:
        self.cache = cache
        self.layers = layers

    def _key(self, key: str, version: int | None = None) -> tuple[str, int | None]:
        return key, version

    def _lookup(self, key: str, version: int | None = None) -> Any:
        cache_key = self._key(key, version)
        for layer in reversed(self.layers):
            value = layer.lookup(cache_key)
            if value is not None:
                return value
        return _missing

    def get(self, key: str, default: Any = None, version: int | None = None) -> Any:
        value = self._lookup(key, version)
        if value is _deleted:
            return default
        if value is not _missing:
            return value
        value = self.cache.get(key, _missing, version=version)
        return default if value is _missing else value

    def get_many(self, keys: list[str], version: int | None = None) -> dict[str, Any]:
        result = {}
        missing = []
        for key in keys:
            value = self._lookup(key, version)
            if value is _missing:
                missing.append(key)
            elif value is not _deleted:
                result[key] = value

        for layer in reversed(self.layers):
            if layer.cleared:
                return result

        result.update(self.cache.get_many(missing, version=version))
        return result

    def get_deleted_many(
        self,
        keys: list[str],
        deleted_value: Any,
        version: int | None = None,
    ) -> dict[str, Any]:
        return {
            key: deleted_value
            for key in keys
            if self._lookup(key, version) is _deleted
        }

    def add(self, key: str, value: Any, timeout: Any = None, version: int | None = None) -> bool:
        existing = self._lookup(key, version)
        if existing is not _missing and existing is not _deleted:
            return False
        self.layers[-1].values[self._key(key, version)] = value
        return True

    def set(self, key: str, value: Any, timeout: Any = None, version: int | None = None) -> None:
        self.layers[-1].values[self._key(key, version)] = value

    def delete(self, key: str, version: int | None = None) -> None:
        cache_key = self._key(key, version)
        self.layers[-1].values.pop(cache_key, None)
        self.layers[-1].deletes.add(cache_key)

    def delete_many(self, keys: list[str], version: int | None = None) -> None:
        for key in keys:
            self.delete(key, version=version)

    def clear(self) -> None:
        layer = self.layers[-1]
        layer.values.clear()
        layer.deletes.clear()
        layer.cleared = True

    def has_key(self, key: str, version: int | None = None) -> bool:
        value = self._lookup(key, version)
        return value is not _missing and value is not _deleted

    def __getattr__(self, name: str) -> Any:
        return getattr(self.cache, name)

    def commit(self) -> None:
        layer = self.layers[0]
        if layer.cleared:
            self.cache.clear()
            return

        by_version: dict[int | None, list[str]] = {}
        for key, version in layer.deletes:
            by_version.setdefault(version, []).append(key)
        for version, keys in by_version.items():
            self.cache.delete_many(keys, version=version)


def _stacks() -> dict[str, list[CacheLayer]]:
    stacks = getattr(_state, 'stacks', None)
    if stacks is None:
        stacks = {}
        _state.stacks = stacks
    return stacks


def is_active(using: str = DEFAULT_DB_ALIAS) -> bool:
    return bool(_stacks().get(using))


def get_cache(using: str | None = None) -> BaseCache:
    alias = using or DEFAULT_DB_ALIAS
    cache = get_shared_cache()
    layers = _stacks().get(alias)
    if layers:
        return TransactionalCache(cache, layers)
    return cache


def get_deleted_many(
    using: str,
    keys: list[str],
    deleted_value: Any,
    version: int | None = None,
) -> dict[str, Any]:
    cache = get_cache(using)
    if not isinstance(cache, TransactionalCache):
        return {}
    return cache.get_deleted_many(keys, deleted_value, version=version)


def db_for_read(model: type[models.Model] | None = None, **hints: Any) -> str:
    write_alias = router.db_for_write(model, **hints)
    if is_active(write_alias):
        return write_alias

    read_alias = router.db_for_read(model, **hints)
    return read_alias or write_alias or DEFAULT_DB_ALIAS


def db_for_write(model: type[models.Model] | None = None, **hints: Any) -> str:
    return router.db_for_write(model, **hints) or DEFAULT_DB_ALIAS


@contextmanager
def atomic(
    using: str | None = None,
    savepoint: bool = True,
) -> Iterator[None]:
    alias = using or DEFAULT_DB_ALIAS
    stacks = _stacks()
    stack = stacks.setdefault(alias, [])
    add_layer = savepoint or not connections[alias].in_atomic_block
    layer = CacheLayer() if add_layer else None
    if layer is not None:
        stack.append(layer)

    try:
        with transaction.atomic(using=alias, savepoint=savepoint):
            yield
    except BaseException:
        if layer is not None and stack and stack[-1] is layer:
            stack.pop()
        if not stack:
            stacks.pop(alias, None)
        raise
    else:
        if layer is not None and stack and stack[-1] is layer:
            stack.pop()
            if stack:
                layer.commit(stack[-1])
            else:
                TransactionalCache(get_shared_cache(), [layer]).commit()
        if not stacks.get(alias):
            stacks.pop(alias, None)
