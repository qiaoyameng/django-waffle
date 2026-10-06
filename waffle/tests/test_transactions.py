import sqlite3
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connections
from django.test import TransactionTestCase
from django.test.utils import override_settings

import waffle
from waffle import transactions
from waffle.utils import get_cache, get_setting


class TransactionRouter:
    def db_for_read(self, model, **hints):
        return 'transaction'

    def db_for_write(self, model, **hints):
        return 'transaction'


class RecordingCache:
    def __init__(self, cache):
        self.cache = cache
        self.set_count = 0
        self.delete_count = 0

    def __getattr__(self, name):
        return getattr(self.cache, name)

    def set(self, *args, **kwargs):
        self.set_count += 1
        return self.cache.set(*args, **kwargs)

    def add(self, *args, **kwargs):
        return self.cache.add(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.delete_count += 1
        return self.cache.delete(*args, **kwargs)

    def delete_many(self, keys, *args, **kwargs):
        self.delete_count += len(keys)
        return self.cache.delete_many(keys, *args, **kwargs)


@override_settings(
    WAFFLE_READ_FROM_WRITE_DB=True,
    DATABASE_ROUTERS=['waffle.tests.test_transactions.TransactionRouter'],
)
class TransactionCacheTests(TransactionTestCase):
    databases = {'default', 'transaction'}
    alias = 'transaction'

    def setUp(self):
        super().setUp()
        get_cache().clear()
        self.db_connection = connections[self.alias]
        with self.db_connection.cursor() as cursor:
            cursor.execute('PRAGMA journal_mode=WAL')

        self.recording_cache = RecordingCache(get_cache(force_real=True))
        cache_patcher = mock.patch(
            'waffle.utils.caches',
            {get_setting('CACHE_NAME'): self.recording_cache},
        )
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)

        self.flag_model = waffle.get_waffle_flag_model()
        self.user_model = get_user_model()
        self.flag = self.flag_model.objects.using(self.alias).create(name='managed-flag', everyone=False)
        self.user = self.user_model.objects.using(self.alias).create(username='alice')
        self.group = Group.objects.using(self.alias).create(name='managed-group')

        flag = self.flag_model.get('managed-flag')
        flag._get_user_ids()
        flag._get_group_ids()
        self.recording_cache.set_count = 0
        self.recording_cache.delete_count = 0

    def second_connection(self):
        db_connection = sqlite3.connect(self.db_connection.settings_dict['NAME'], timeout=1)
        db_connection.execute('PRAGMA journal_mode=WAL')
        self.addCleanup(db_connection.close)
        return db_connection

    def flag_everyone_from_db(self, db_connection):
        row = db_connection.execute(
            'SELECT everyone FROM waffle_flag WHERE name = ?',
            ['managed-flag'],
        ).fetchone()
        return bool(row[0])

    def assertCacheUnchanged(self):
        self.assertEqual(self.recording_cache.set_count, 0)
        self.assertEqual(self.recording_cache.delete_count, 0)

    def test_commit_publishes_only_committed_state(self):
        other_connection = self.second_connection()

        with transactions.atomic(self.alias):
            self.flag.everyone = True
            self.flag.save(using=self.alias)

            self.assertTrue(self.flag_model.get('managed-flag').everyone)
            self.assertFalse(self.flag_everyone_from_db(other_connection))
            self.assertCacheUnchanged()

        self.assertTrue(self.flag_everyone_from_db(other_connection))
        self.assertEqual(self.recording_cache.set_count, 0)
        self.assertEqual(self.recording_cache.delete_count, 4)

    def test_rollback_does_not_publish(self):
        other_connection = self.second_connection()

        with self.assertRaises(RuntimeError):
            with transactions.atomic(self.alias):
                self.flag.everyone = True
                self.flag.save(using=self.alias)
                self.assertTrue(self.flag_model.get('managed-flag').everyone)
                raise RuntimeError('rollback')

        self.assertFalse(self.flag_everyone_from_db(other_connection))
        self.assertFalse(self.flag_model.get('managed-flag').everyone)
        self.assertCacheUnchanged()

    def test_m2m_changes_are_private_until_commit(self):
        other_connection = self.second_connection()

        with transactions.atomic(self.alias):
            self.flag.users.add(self.user)
            self.flag.groups.add(self.group)

            self.assertTrue(self.flag_model.get('managed-flag').is_active_for_user(self.user))
            user_membership_count = other_connection.execute(
                'SELECT COUNT(*) FROM waffle_flag_users '
                'WHERE flag_id = ? AND user_id = ?',
                [self.flag.pk, self.user.pk],
            ).fetchone()[0]
            group_membership_count = other_connection.execute(
                'SELECT COUNT(*) FROM waffle_flag_groups '
                'WHERE flag_id = ? AND group_id = ?',
                [self.flag.pk, self.group.pk],
            ).fetchone()[0]
            self.assertEqual(user_membership_count, 0)
            self.assertEqual(group_membership_count, 0)
            self.assertCacheUnchanged()

        user_membership_count = other_connection.execute(
            'SELECT COUNT(*) FROM waffle_flag_users '
            'WHERE flag_id = ? AND user_id = ?',
            [self.flag.pk, self.user.pk],
        ).fetchone()[0]
        group_membership_count = other_connection.execute(
            'SELECT COUNT(*) FROM waffle_flag_groups '
            'WHERE flag_id = ? AND group_id = ?',
            [self.flag.pk, self.group.pk],
        ).fetchone()[0]
        self.assertEqual(user_membership_count, 1)
        self.assertEqual(group_membership_count, 1)
        self.assertEqual(self.recording_cache.set_count, 0)
        self.assertEqual(self.recording_cache.delete_count, 4)

    def test_savepoint_rollback_discards_only_that_layer(self):
        with transactions.atomic(self.alias):
            self.flag.everyone = True
            self.flag.save(using=self.alias)

            with self.assertRaises(RuntimeError):
                with transactions.atomic(self.alias):
                    self.flag.everyone = False
                    self.flag.save(using=self.alias)
                    raise RuntimeError('savepoint rollback')

            self.assertTrue(self.flag_model.get('managed-flag').everyone)

        self.assertTrue(self.flag_model.get('managed-flag').everyone)
        self.assertEqual(self.recording_cache.set_count, 0)
        self.assertEqual(self.recording_cache.delete_count, 4)

    def test_delete_invalidation_is_private_until_commit(self):
        other_connection = self.second_connection()

        with transactions.atomic(self.alias):
            self.flag.delete(using=self.alias)
            self.assertFalse(self.flag_model.get('managed-flag').pk)
            row = other_connection.execute(
                'SELECT COUNT(*) FROM waffle_flag WHERE name = ?',
                ['managed-flag'],
            ).fetchone()
            self.assertEqual(row[0], 1)
            self.assertCacheUnchanged()

        row = other_connection.execute(
            'SELECT COUNT(*) FROM waffle_flag WHERE name = ?',
            ['managed-flag'],
        ).fetchone()
        self.assertEqual(row[0], 0)
        self.assertEqual(self.recording_cache.set_count, 0)
        self.assertEqual(self.recording_cache.delete_count, 4)

    def test_alias_identities_are_separate(self):
        default_cache = transactions.get_transaction_cache('default')
        other_cache = transactions.get_transaction_cache('other')

        with transactions.atomic('default'):
            default_cache.delete('cache-key')
            self.assertIn((None, 'cache-key'), default_cache.layers[-1].deleted_keys)
            self.assertEqual(other_cache.layers, [])

    def test_pending_none_value_remains_visible(self):
        with transactions.atomic(self.alias):
            transaction_cache = transactions.get_transaction_cache(self.alias)
            transaction_cache.set('nullable', None)
            self.assertIsNone(transaction_cache.get('nullable', 'missing'))

    def test_atomic_works_as_decorator(self):
        @transactions.atomic(self.alias)
        def update_flag():
            self.flag.everyone = True
            self.flag.save(using=self.alias)
            return self.flag_model.get('managed-flag').everyone

        self.assertTrue(update_flag())
        self.assertTrue(self.flag_model.get('managed-flag').everyone)

    @override_settings(WAFFLE_CREATE_MISSING_SWITCHES=True)
    def test_rollback_does_not_publish_dynamically_created_switch(self):
        switch_model = waffle.get_waffle_switch_model()
        other_connection = self.second_connection()

        with self.assertRaises(RuntimeError):
            with transactions.atomic(self.alias):
                switch_model.objects.using(self.alias).create(
                    name='pending-switch', active=True
                )
                self.assertTrue(waffle.switch_is_active('pending-switch'))
                raise RuntimeError('rollback')

        row = other_connection.execute(
            'SELECT COUNT(*) FROM waffle_switch WHERE name = ?',
            ['pending-switch'],
        ).fetchone()
        self.assertEqual(row[0], 0)
        with override_settings(WAFFLE_CREATE_MISSING_SWITCHES=False):
            self.assertFalse(waffle.switch_is_active('pending-switch'))
        self.assertEqual(self.recording_cache.set_count, 0)
