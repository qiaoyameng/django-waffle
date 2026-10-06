from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.cache.backends.locmem import LocMemCache
from django.test import TransactionTestCase
from django.test.utils import override_settings
from unittest import mock

import waffle
from test_app.models import Company, CompanyAwareFlag
from waffle import transactions
from waffle.utils import get_setting, keyfmt


class RecordingCache(LocMemCache):
    def __init__(self):
        super().__init__('locmem://', {})
        self.set_count = 0
        self.add_count = 0
        self.delete_count = 0
        self.delete_many_count = 0

    def set(self, key, value, timeout=None, version=None):
        self.set_count += 1
        return super().set(key, value, timeout, version=version)

    def add(self, key, value, timeout=None, version=None):
        self.add_count += 1
        return super().add(key, value, timeout, version=version)

    def delete(self, key, version=None):
        self.delete_count += 1
        return super().delete(key, version=version)

    def delete_many(self, keys, version=None):
        self.delete_many_count += 1
        return super().delete_many(keys, version=version)


class TransactionCacheTests(TransactionTestCase):
    databases = {'default', 'readonly'}

    def setUp(self):
        self.recording = RecordingCache()
        self.cache_patch = mock.patch(
            'waffle.transactions.get_shared_cache',
            return_value=self.recording,
        )
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        cache.clear()
        self.recording.clear()
        super().setUp()

    def test_commit_invalidates_after_flag_membership_update(self):
        flag = waffle.get_waffle_flag_model().objects.create(name='membership')
        group = Group.objects.create(name='staff')
        user = get_user_model().objects.create(username='member')
        flag.groups.add(group)
        flag.users.add(user)
        assert flag._get_group_ids() == {group.pk}
        assert flag._get_user_ids() == {user.pk}

        set_before = self.recording.set_count
        add_before = self.recording.add_count
        delete_before = self.recording.delete_count
        delete_many_before = self.recording.delete_many_count

        with transactions.atomic():
            flag.everyone = True
            flag.save()
            flag.users.remove(user)
            flag.groups.remove(group)

            assert waffle.flag_is_active(request(), flag.name)
            assert flag._get_group_ids() == set()
            assert flag._get_user_ids() == set()
            assert self.recording.set_count == set_before
            assert self.recording.add_count == add_before

        group_key = keyfmt(get_setting('FLAG_GROUPS_CACHE_KEY'), flag.name)
        user_key = keyfmt(get_setting('FLAG_USERS_CACHE_KEY'), flag.name)
        self.recording.add(group_key, {'fresh': 'group'})
        self.recording.add(user_key, {'fresh': 'user'})
        assert waffle.flag_is_active(request(), flag.name)
        assert self.recording.get(group_key) == {'fresh': 'group'}
        assert self.recording.get(user_key) == {'fresh': 'user'}
        add_after_commit = self.recording.add_count
        assert self.recording.set_count == set_before
        assert waffle.flag_is_active(request(), flag.name)
        assert self.recording.add_count == add_after_commit
        assert self.recording.delete_count > delete_before
        assert self.recording.delete_many_count > delete_many_before

    def test_rollback_does_not_publish_invalidation_or_private_values(self):
        flag = waffle.get_waffle_flag_model().objects.create(
            name='rolled-back', everyone=False
        )
        user = get_user_model().objects.create(username='rolled-back-user')
        assert not waffle.flag_is_active(request(), flag.name)
        delete_before = self.recording.delete_count
        add_before = self.recording.add_count

        with self.assertRaises(RuntimeError):
            with transactions.atomic():
                flag.everyone = True
                flag.save()
                flag.users.add(user)
                assert waffle.flag_is_active(request(), flag.name)
                assert flag._get_user_ids() == {user.pk}
                raise RuntimeError('rollback')

        assert not waffle.flag_is_active(request(), flag.name)
        assert flag._get_user_ids() == set()
        assert self.recording.delete_count == delete_before
        add_after_rollback = self.recording.add_count
        assert flag._get_user_ids() == set()
        assert self.recording.add_count == add_after_rollback

    def test_nested_savepoint_rollback_discards_only_that_layer(self):
        flag = waffle.get_waffle_flag_model().objects.create(
            name='nested', everyone=False
        )

        with transactions.atomic():
            flag.everyone = None
            flag.save()

            with self.assertRaises(ValueError):
                with transactions.atomic():
                    flag.everyone = True
                    flag.save()
                    assert waffle.flag_is_active(request(), flag.name)
                    raise ValueError('savepoint rollback')

            refreshed = waffle.get_waffle_flag_model().objects.using('default').get(
                name=flag.name
            )
            assert refreshed.everyone is None

            refreshed.everyone = False
            refreshed.save()
            assert not waffle.flag_is_active(request(), flag.name)

        assert not waffle.flag_is_active(request(), flag.name)

    def test_nested_savepoint_read_keeps_outer_invalidation(self):
        flag = waffle.get_waffle_flag_model().objects.create(
            name='nested-invalidation', everyone=False
        )

        with transactions.atomic():
            flag.everyone = True
            flag.save()

            with transactions.atomic():
                assert waffle.flag_is_active(request(), flag.name)

        assert self.recording.get(cache_key(flag.name)) is None

    def test_get_all_sees_private_membership_changes(self):
        flag = waffle.get_waffle_flag_model().objects.create(name='all-membership')
        group = Group.objects.create(name='all-group')
        flag.groups.add(group)

        with transactions.atomic():
            flag.groups.remove(group)
            flags = waffle.get_waffle_flag_model().get_all()

            refreshed = next(item for item in flags if item.name == flag.name)
            assert refreshed._prefetched_group_ids == set()

    @override_settings(
        WAFFLE_FLAG_MODEL='test_app.CompanyAwareFlag',
        AUTH_USER_MODEL='test_app.CompanyUser',
    )
    def test_custom_flag_m2m_flush_uses_transaction_cache(self):
        flag = CompanyAwareFlag.objects.create(name='company-membership')
        company = Company.objects.create(name='Acme')

        with self.assertRaises(RuntimeError):
            with transactions.atomic():
                flag.companies.add(company)
                assert flag._get_company_ids() == {company.pk}
                raise RuntimeError('rollback')

        assert flag._get_company_ids() == set()

    def test_nested_non_savepoint_uses_outer_editing_context(self):
        flag = waffle.get_waffle_flag_model().objects.create(
            name='nested-non-savepoint', everyone=False
        )

        with transactions.atomic():
            with transactions.atomic(savepoint=False):
                flag.everyone = True
                flag.save()

            assert transactions.is_active('default')

    def test_uncommitted_created_object_is_not_written_to_shared_cache(self):
        shared_key = cache_key('uncommitted-create')

        with self.assertRaises(RuntimeError):
            with transactions.atomic():
                flag = waffle.get_waffle_flag_model().objects.create(
                    name='uncommitted-create', everyone=True
                )
                assert waffle.flag_is_active(request(), flag.name)
                assert self.recording.get(shared_key) is None
                raise RuntimeError('rollback')

        assert not waffle.get_waffle_flag_model().objects.filter(
            name='uncommitted-create'
        ).exists()
        assert self.recording.get(shared_key) is None

    def test_uncommitted_deleted_object_stays_in_shared_cache_until_rollback(self):
        flag = waffle.get_waffle_flag_model().objects.create(
            name='uncommitted-delete', everyone=True
        )
        got = waffle.get_waffle_flag_model().get(flag.name)
        assert got
        assert waffle.flag_is_active(request(), flag.name)

        with self.assertRaises(RuntimeError):
            with transactions.atomic():
                flag.delete()
                assert not waffle.get_waffle_flag_model().objects.filter(
                    name=flag.name
                ).exists()
                assert self.recording.get(cache_key(flag.name)) is not None
                raise RuntimeError('rollback')

        assert waffle.flag_is_active(request(), flag.name)

    def test_switch_and_sample_rollback_does_not_publish_changes(self):
        switch = waffle.get_waffle_switch_model().objects.create(
            name='transaction-switch', active=False
        )
        sample = waffle.get_waffle_sample_model().objects.create(
            name='transaction-sample', percent=0
        )

        with self.assertRaises(RuntimeError):
            with transactions.atomic():
                switch.active = True
                switch.save()
                sample.percent = 100
                sample.save()
                assert waffle.switch_is_active(switch.name)
                assert waffle.sample_is_active(sample.name)
                raise RuntimeError('rollback')

        assert not waffle.switch_is_active(switch.name)
        assert not waffle.sample_is_active(sample.name)

    def test_database_aliases_have_independent_cache_identities(self):
        key = cache_key('alias')
        self.recording.set(key, 'shared value')
        default_context = transactions.atomic('default')
        readonly_context = transactions.atomic('readonly')
        default_context.__enter__()
        readonly_context.__enter__()

        try:
            transactions.get_cache('default').set(key, 'default value')
            transactions.get_cache('readonly').set(key, 'readonly value')
            assert transactions.get_cache('default').get(key) == 'default value'
            assert transactions.get_cache('readonly').get(key) == 'readonly value'
            assert self.recording.get(key) == 'shared value'
        finally:
            readonly_context.__exit__(RuntimeError, RuntimeError(), None)
            default_context.__exit__(RuntimeError, RuntimeError(), None)

        assert self.recording.get(key) == 'shared value'


def test_other_sqlite_connection_only_sees_values_after_commit():
    with tempfile.TemporaryDirectory() as directory:
        path = f'{directory}/visibility.sqlite3'
        with closing(sqlite3.connect(path)) as setup:
            setup.execute('PRAGMA journal_mode=WAL')
            setup.execute(
                '''
                CREATE TABLE waffle_flag (
                    id integer primary key,
                    name varchar(100) not null unique,
                    everyone bool,
                    percent decimal,
                    testing bool not null,
                    superusers bool not null,
                    staff bool not null,
                    authenticated bool not null,
                    languages text not null,
                    rollout bool not null,
                    note text not null,
                    created datetime not null,
                    modified datetime not null
                )
                '''
            )
            setup.execute(
                'INSERT INTO waffle_flag VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (1, 'visible', 0, 0, 0, 1, 0, 0, '', 0, '', '2026-01-01', '2026-01-01'),
            )
            setup.commit()

        with closing(sqlite3.connect(path)) as writer, closing(
            sqlite3.connect(path)
        ) as reader:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute(
                'UPDATE waffle_flag SET everyone = 1 WHERE name = ?',
                ('visible',),
            )
            row = reader.execute(
                'SELECT everyone FROM waffle_flag WHERE name = ?',
                ('visible',),
            ).fetchone()
            assert bool(row[0]) is False

            writer.commit()
            row = reader.execute(
                'SELECT everyone FROM waffle_flag WHERE name = ?',
                ('visible',),
            ).fetchone()
            assert bool(row[0]) is True


def cache_key(name):
    return keyfmt(get_setting('FLAG_CACHE_KEY'), name)


def request():
    class User:
        is_authenticated = False
        is_staff = False
        is_superuser = False

    value = type('Request', (), {'GET': {}, 'COOKIES': {}, 'headers': {}})()
    value.user = User()
    return value
