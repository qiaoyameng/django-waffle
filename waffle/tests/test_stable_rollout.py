from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.test import RequestFactory
from django.test.utils import override_settings

import waffle
from test_app import views
from waffle.middleware import WaffleMiddleware
from waffle.stable import STABLE_BUCKETS, stable_bucket, stable_is_active
from waffle.tests.base import TestCase


def request_for(user=None):
    request = RequestFactory().get('/foo')
    request.user = user if user is not None else AnonymousUser()
    return request


def process_request(request, view=views.flag_in_view):
    response = view(request)
    return WaffleMiddleware(get_response).process_response(request, response)


def get_response(request):
    return 'hello'


class StableBucketingTests(TestCase):
    def test_bucket_is_deterministic_and_in_range(self):
        bucket = stable_bucket('myflag', 'salt', 'user:1')
        self.assertEqual(bucket, stable_bucket('myflag', 'salt', 'user:1'))
        self.assertIn(bucket, range(STABLE_BUCKETS))

    def test_bucket_differs_by_flag_salt_and_identity(self):
        values = {
            stable_bucket('myflag', 'salt', 'user:1'),
            stable_bucket('other', 'salt', 'user:1'),
            stable_bucket('myflag', 'pepper', 'user:1'),
            stable_bucket('myflag', 'salt', 'user:2'),
        }
        self.assertGreater(len(values), 1)

    def test_extremes(self):
        self.assertFalse(stable_is_active('f', 's', 'user:1', Decimal('0.0')))
        self.assertTrue(stable_is_active('f', 's', 'user:1', Decimal('100.0')))


class StableRolloutTests(TestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.users = User.objects.bulk_create(
            User(username=f'user{i}') for i in range(2000)
        )

    def flag(self, **kw):
        kw.setdefault('name', 'myflag')
        kw.setdefault('stable_rollout', True)
        kw.setdefault('percent', Decimal('10.0'))
        flag = waffle.get_waffle_flag_model().objects.create(**kw)
        if 'salt' not in kw:
            flag.salt = 'fixed-salt'
            flag.save()
        return flag

    def active_set(self, percent, flag=None):
        flag = flag or self.flag()
        flag.percent = Decimal(percent)
        flag.save()
        active = set()
        for user in self.users:
            request = request_for(user)
            if waffle.flag_is_active(request, 'myflag'):
                active.add(user.pk)
        return active

    def test_result_is_stable_across_requests_and_clients(self):
        self.flag(percent=Decimal('50.0'))
        user = self.users[0]
        results = {
            waffle.flag_is_active(request_for(user), 'myflag')
            for _ in range(5)
        }
        self.assertEqual(len(results), 1)

    def test_increasing_percent_keeps_previously_active_identities(self):
        flag = self.flag()
        ten = self.active_set('10.0', flag)
        twenty = self.active_set('20.0', flag)
        # Fixed identity set: this is the primary assertion.
        self.assertTrue(ten.issubset(twenty))
        self.assertLess(len(ten), len(twenty))
        # Distribution sanity, not the guarantee under test.
        self.assertAlmostEqual(len(ten) / len(self.users), 0.1, delta=0.05)
        self.assertAlmostEqual(len(twenty) / len(self.users), 0.2, delta=0.05)

    def test_ordinary_save_keeps_salt_and_groups(self):
        flag = self.flag(percent=Decimal('10.0'))
        salt = flag.salt
        before = {
            user.pk
            for user in self.users
            if waffle.flag_is_active(request_for(user), 'myflag')
        }
        flag.percent = Decimal('15.0')
        flag.note = 'edited in admin'
        flag.save()
        flag.refresh_from_db()
        self.assertEqual(flag.salt, salt)
        after = {
            user.pk
            for user in self.users
            if waffle.flag_is_active(request_for(user), 'myflag')
        }
        self.assertEqual(before, after & before)
        self.assertTrue(before.issubset(after))

    def test_salt_is_generated_when_enabled(self):
        flag = waffle.get_waffle_flag_model().objects.create(
            name='fresh', percent=Decimal('50.0'))
        self.assertEqual(flag.salt, '')
        flag.stable_rollout = True
        flag.save()
        self.assertTrue(flag.salt)
        # Saving again does not silently rotate the salt.
        salt = flag.salt
        flag.save()
        self.assertEqual(flag.salt, salt)

    def test_rotating_salt_explicitly_regroups_identities(self):
        flag = self.flag(percent=Decimal('50.0'))
        users = self.users[:300]

        def active(salt):
            flag.salt = salt
            flag.save()
            return {
                user.pk
                for user in users
                if waffle.flag_is_active(request_for(user), 'myflag')
            }

        before = active('salt-one')
        flag.rotate_salt('salt-two')
        self.assertEqual(flag.salt, 'salt-two')
        after = {
            user.pk
            for user in users
            if waffle.flag_is_active(request_for(user), 'myflag')
        }
        self.assertNotEqual(before, after)

    def test_anonymous_identity_misses_by_default(self):
        self.flag(percent=Decimal('99.9'))
        request = request_for()
        request.COOKIES['dwf_myflag'] = 'True'
        self.assertFalse(waffle.flag_is_active(request, 'myflag'))

    @override_settings(WAFFLE_STABLE_ROLLOUT_MISSING_IDENTITY='cookie')
    def test_anonymous_falls_back_to_cookie_when_configured(self):
        self.flag(percent=Decimal('99.9'))
        request = request_for()
        request.COOKIES['dwf_myflag'] = 'True'
        self.assertTrue(waffle.flag_is_active(request, 'myflag'))

        request = request_for()
        request.COOKIES['dwf_myflag'] = 'False'
        self.assertFalse(waffle.flag_is_active(request, 'myflag'))

    @override_settings(WAFFLE_STABLE_ROLLOUT_MISSING_IDENTITY='cookie')
    def test_authenticated_identity_never_uses_cookie_key(self):
        self.flag(percent=Decimal('50.0'))
        user = self.users[0]
        without_cookie = waffle.flag_is_active(request_for(user), 'myflag')
        request = request_for(user)
        request.COOKIES['dwf_myflag'] = str(not without_cookie)
        self.assertEqual(
            without_cookie,
            waffle.flag_is_active(request, 'myflag'),
        )

    def test_identity_switch_is_not_implicitly_mixed(self):
        # Anonymous requests miss by default; logging in switches to the
        # identity bucket. A pre-existing anonymous cookie never drives the
        # authenticated decision.
        self.flag(percent=Decimal('99.9'))
        anonymous = request_for()
        self.assertFalse(waffle.flag_is_active(anonymous, 'myflag'))

        user = self.users[0]
        logged_in = request_for(user)
        logged_in.COOKIES['dwf_myflag'] = 'False'
        expected = stable_is_active(
            'myflag', 'fixed-salt', f'user:{user.pk}', Decimal('99.9'))
        self.assertEqual(
            expected, waffle.flag_is_active(logged_in, 'myflag'))

    def test_stable_path_does_not_set_cookies(self):
        self.flag(percent=Decimal('50.0'))
        response = process_request(request_for(self.users[0]))
        self.assertNotIn('dwf_myflag', response.cookies)

    def test_percent_zero_still_falls_through_to_false(self):
        self.flag(percent=Decimal('0.0'))
        self.assertFalse(
            waffle.flag_is_active(request_for(self.users[0]), 'myflag'))

    def test_everyone_overrides_stable_percent(self):
        flag = self.flag(percent=Decimal('0.0'))
        flag.everyone = True
        flag.save()
        self.assertTrue(
            waffle.flag_is_active(request_for(self.users[0]), 'myflag'))

        flag.everyone = False
        flag.save()
        self.assertFalse(
            waffle.flag_is_active(request_for(self.users[0]), 'myflag'))

    def test_superuser_overrides_stable_percent(self):
        self.flag(percent=Decimal('0.0'), superusers=True)
        superuser = get_user_model()(username='root', is_superuser=True)
        self.assertTrue(waffle.flag_is_active(request_for(superuser), 'myflag'))

    def test_explicit_user_overrides_stable_percent(self):
        flag = self.flag(percent=Decimal('0.0'))
        user = self.users[0]
        flag.users.add(user)
        self.assertTrue(waffle.flag_is_active(request_for(user), 'myflag'))

    def test_group_overrides_stable_percent(self):
        flag = self.flag(percent=Decimal('0.0'))
        group = Group.objects.create(name='early-adopters')
        flag.groups.add(group)
        user = self.users[0]
        user.groups.add(group)
        self.assertTrue(waffle.flag_is_active(request_for(user), 'myflag'))

    def test_testing_override_precedes_stable_percent(self):
        self.flag(percent=Decimal('0.0'), testing=True)
        request = request_for(self.users[0])
        request.GET = {'dwft_myflag': '1'}
        self.assertTrue(waffle.flag_is_active(request, 'myflag'))

    def test_flags_with_own_salts_bucket_independently(self):
        first = self.flag(name='first', percent=Decimal('50.0'))
        second = waffle.get_waffle_flag_model().objects.create(
            name='second', stable_rollout=True, percent=Decimal('50.0'))
        second.salt = 'different-salt'
        second.save()
        decisions = {
            (
                waffle.flag_is_active(request_for(user), 'first'),
                waffle.flag_is_active(request_for(user), 'second'),
            )
            for user in self.users[:200]
        }
        self.assertIn((True, False), decisions)
        self.assertIn((False, True), decisions)

    @override_settings(WAFFLE_STABLE_IDENTITY=lambda request: 'custom-key')
    def test_custom_identity_source(self):
        flag = self.flag(percent=Decimal('99.9'))
        self.assertTrue(
            waffle.flag_is_active(request_for(self.users[0]), 'myflag'))

        flag.percent = Decimal('0.0')
        flag.save()
        self.assertFalse(
            waffle.flag_is_active(request_for(self.users[1]), 'myflag'))

    @override_settings(
        WAFFLE_STABLE_IDENTITY='waffle.stable._default_identity')
    def test_dotted_path_identity_source(self):
        self.flag(percent=Decimal('99.9'))
        user = self.users[0]
        expected = stable_is_active(
            'myflag', 'fixed-salt', f'user:{user.pk}', Decimal('99.9'))
        self.assertEqual(
            expected, waffle.flag_is_active(request_for(user), 'myflag'))
        self.assertFalse(
            waffle.flag_is_active(request_for(), 'myflag'))
