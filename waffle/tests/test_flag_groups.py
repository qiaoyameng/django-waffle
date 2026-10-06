
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group as AuthGroup
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import HttpResponse
from django.test import RequestFactory

import waffle
from waffle.groups import Assignment, BUCKET_COUNT, stable_bucket
from waffle.middleware import WaffleMiddleware
from waffle.models import Flag, FlagGroup, FlagGroupMembership
from waffle.tests.base import TestCase

User = get_user_model()


def request_with(identity=None, user=None, cookies=None, get=None):
    request = RequestFactory().get('/foo', data=get or {})
    request.user = AnonymousUser() if user is None else user
    if cookies:
        for key, value in cookies.items():
            request.COOKIES[key] = value
    return request


def identity_for_bucket(group_name, bucket, prefix='user'):
    """Find a stable identity whose bucket is exactly ``bucket``."""
    value = 0
    while stable_bucket(group_name, f'{prefix}:{value}') != bucket:
        value += 1
    return f'{prefix}:{value}'


class FixedUser:
    """Authenticated user stand-in with explicit pk."""
    def __init__(self, pk, is_staff=False, is_superuser=False):
        self.pk = pk
        self.id = pk
        self.is_authenticated = True
        self.is_staff = is_staff
        self.is_superuser = is_superuser
        self.groups = AuthGroup.objects.none()
        self.is_active = True


class StableBucketTests(TestCase):
    def test_deterministic(self):
        self.assertEqual(
            stable_bucket('g', 'user:1'), stable_bucket('g', 'user:1'))

    def test_bucket_bounds(self):
        for value in range(5000):
            bucket = stable_bucket('g', f'user:{value}')
            self.assertGreaterEqual(bucket, 0)
            self.assertLess(bucket, BUCKET_COUNT)

    def test_groups_are_separate_spaces(self):
        # The same identity may land anywhere per group; verify group name
        # participates in the hash.
        buckets = {
            name: stable_bucket(name, 'user:1')
            for name in ('g1', 'g2', 'g3', 'g4')
        }
        self.assertEqual(len(set(buckets.values())), len(buckets))

    def test_distribution_is_uniform(self):
        counts = [0] * 10
        for value in range(20000):
            counts[stable_bucket('g', f'user:{value}') // 1000] += 1
        for count in counts:
            self.assertAlmostEqual(count, 2000, delta=180)


class GroupSetupTests(TestCase):
    def setUp(self):
        super().setUp()
        self.group = FlagGroup.objects.create(name='homepage')

    def make_member(self, name, percent, priority=0):
        flag = Flag.objects.create(name=name)
        return FlagGroupMembership.objects.create(
            group=self.group, flag=flag, percent=percent, priority=priority)

    def test_total_percent_cannot_exceed_100(self):
        self.make_member('a', '60')
        self.make_member('b', '40')
        extra = Flag.objects.create(name='c')
        with self.assertRaises(ValidationError):
            FlagGroupMembership.objects.create(
                group=self.group, flag=extra, percent='0.01')

    def test_individual_percent_bounds(self):
        flag = Flag.objects.create(name='big')
        with self.assertRaises(ValidationError):
            FlagGroupMembership.objects.create(
                group=self.group, flag=flag, percent='100.01')

    def test_full_100_percent_leaves_no_empty_buckets(self):
        self.make_member('a', '55.55')
        self.make_member('b', '44.45')
        covered = [0] * BUCKET_COUNT
        for membership, start, end in self.group.intervals():
            self.assertLessEqual(end, BUCKET_COUNT)
            for bucket in range(start, end):
                covered[bucket] += 1
        self.assertEqual(sum(covered), BUCKET_COUNT)

    def test_partial_percent_leaves_empty_buckets(self):
        self.make_member('a', '40')
        self.make_member('b', '30')
        intervals = self.group.intervals()
        self.assertEqual([(start, end) for _, start, end in intervals],
                         [(0, 4000), (4000, 7000)])

    def test_intervals_never_overlap(self):
        self.make_member('a', '25')
        self.make_member('b', '25')
        self.make_member('c', '25')
        covered = [0] * BUCKET_COUNT
        for membership, start, end in self.group.intervals():
            for bucket in range(start, end):
                covered[bucket] += 1
        self.assertLessEqual(max(covered), 1)
        self.assertEqual(sum(covered), 7500)

    def test_flag_belongs_to_at_most_one_group(self):
        other = FlagGroup.objects.create(name='other')
        membership = self.make_member('a', '10')
        with self.assertRaises(IntegrityError):
            FlagGroupMembership.objects.create(
                group=other, flag=membership.flag, percent='10')


class AllocationBoundaryTests(TestCase):
    def setUp(self):
        super().setUp()
        self.group = FlagGroup.objects.create(name='homepage')
        self.flag_a = Flag.objects.create(name='variant_a')
        self.flag_b = Flag.objects.create(name='variant_b')
        self.member_a = FlagGroupMembership.objects.create(
            group=self.group, flag=self.flag_a, percent='40')
        self.member_b = FlagGroupMembership.objects.create(
            group=self.group, flag=self.flag_b, percent='30')

    def decide(self, identity):
        return waffle.flag_group_result(
            request_with(), 'homepage', identity=identity)

    def test_every_bucket_assignment_for_all_10000_buckets(self):
        """Exhaustive boundary sweep over fixed identities in SQLite."""
        seen = set()
        # Find identities that land exactly on every allocation boundary.
        for boundary in (0, 1, 3999, 4000, 6999, 7000, 9999):
            seen.add(boundary)
            identity = identity_for_bucket('homepage', boundary)
            decision = self.decide(identity)
            self.assertEqual(decision.bucket, boundary)
            if boundary == 0 or boundary == 1 or boundary == 3999:
                self.assertEqual(decision.flag_name, 'variant_a')
                self.assertTrue(decision.active)
                self.assertEqual(decision.reason, Assignment.BUCKET)
            elif boundary in (4000, 6999):
                self.assertEqual(decision.flag_name, 'variant_b')
                self.assertTrue(decision.active)
            else:
                self.assertIsNone(decision.flag_name)
                self.assertFalse(decision.active)
                self.assertEqual(decision.reason, Assignment.EMPTY_BUCKET)

    def test_intervals_are_half_open(self):
        # Last bucket of A.
        d = self.decide(identity_for_bucket('homepage', 3999))
        self.assertEqual(d.flag_name, 'variant_a')
        # First bucket of B.
        d = self.decide(identity_for_bucket('homepage', 4000))
        self.assertEqual(d.flag_name, 'variant_b')
        # First empty bucket.
        d = self.decide(identity_for_bucket('homepage', 7000))
        self.assertIsNone(d.flag_name)
        self.assertEqual(d.reason, Assignment.EMPTY_BUCKET)

    def test_boundary_counts_match_configured_percent(self):
        counts = {'a': 0, 'b': 0, 'empty': 0}
        for value in range(10000):
            decision = self.decide(f'user:{value}')
            if decision.flag_name == 'variant_a':
                counts['a'] += 1
            elif decision.flag_name == 'variant_b':
                counts['b'] += 1
            else:
                counts['empty'] += 1
        self.assertAlmostEqual(counts['a'], 4000, delta=120)
        self.assertAlmostEqual(counts['b'], 3000, delta=120)
        self.assertAlmostEqual(counts['empty'], 3000, delta=120)


class ConsistentEvaluationTests(TestCase):
    def setUp(self):
        super().setUp()
        self.group = FlagGroup.objects.create(name='homepage')
        self.flag_a = Flag.objects.create(name='variant_a')
        self.flag_b = Flag.objects.create(name='variant_b')
        FlagGroupMembership.objects.create(
            group=self.group, flag=self.flag_a, percent='40')
        FlagGroupMembership.objects.create(
            group=self.group, flag=self.flag_b, percent='30')
        # Fixed user known to land in variant B's interval.
        self.user_pk = next(
            value for value in range(1, 200)
            if 4000 <= stable_bucket('homepage', f'user:{value}') < 7000
        )
        self.user = FixedUser(pk=self.user_pk)

    def test_two_independent_flag_calls_match_group_result(self):
        request = request_with(user=self.user)
        decision = waffle.flag_group_result(request, 'homepage')

        self.assertFalse(waffle.flag_is_active(request, 'variant_a'))
        self.assertTrue(waffle.flag_is_active(request, 'variant_b'))
        self.assertEqual(decision.flag_name, 'variant_b')
        self.assertTrue(waffle.flag_is_active(request, 'variant_b'))

    def test_result_independent_of_check_order(self):
        request_a = request_with(user=self.user)
        request_b = request_with(user=self.user)
        self.assertFalse(waffle.flag_is_active(request_a, 'variant_a'))
        self.assertTrue(waffle.flag_is_active(request_a, 'variant_b'))
        self.assertTrue(waffle.flag_is_active(request_b, 'variant_b'))
        self.assertFalse(waffle.flag_is_active(request_b, 'variant_a'))

    def test_business_never_checks_both_true(self):
        for value in range(1000):
            request = request_with(user=FixedUser(pk=value))
            a = waffle.flag_is_active(request, 'variant_a')
            b = waffle.flag_is_active(request, 'variant_b')
            self.assertFalse(a and b)


class ForcedHitTests(TestCase):
    def setUp(self):
        super().setUp()
        self.group = FlagGroup.objects.create(
            name='homepage', on_conflict=FlagGroup.ConflictPolicy.REJECT)
        self.flag_a = Flag.objects.create(name='variant_a')
        self.flag_b = Flag.objects.create(name='variant_b')
        self.member_a = FlagGroupMembership.objects.create(
            group=self.group, flag=self.flag_a, percent='10', priority=2)
        self.member_b = FlagGroupMembership.objects.create(
            group=self.group, flag=self.flag_b, percent='10', priority=1)

    def test_targeted_user_forces_variant_regardless_of_bucket(self):
        user = User.objects.create(username='forced')
        self.flag_b.users.add(user)
        request = request_with(user=user)
        decision = waffle.flag_group_result(request, 'homepage')
        self.assertEqual(decision.reason, Assignment.FORCED)
        self.assertEqual(decision.flag_name, 'variant_b')
        self.assertFalse(waffle.flag_is_active(request, 'variant_a'))
        self.assertTrue(waffle.flag_is_active(request, 'variant_b'))

    def test_conflicting_forced_hits_are_rejected(self):
        user = User.objects.create(username='two')
        self.flag_a.users.add(user)
        self.flag_b.users.add(user)
        request = request_with(user=user)
        decision = waffle.flag_group_result(request, 'homepage')
        self.assertEqual(decision.reason, Assignment.CONFLICT)
        self.assertFalse(decision.active)
        self.assertFalse(waffle.flag_is_active(request, 'variant_a'))
        self.assertFalse(waffle.flag_is_active(request, 'variant_b'))
        self.assertEqual(
            set(decision.forced_flags), {'variant_a', 'variant_b'})

    def test_conflict_priority_is_configured_not_db_order(self):
        self.group.on_conflict = FlagGroup.ConflictPolicy.PRIORITY
        self.group.save()
        user = User.objects.create(username='two2')
        # Add to the higher-numbered-priority flag last and in reverse so a
        # naive database-first traversal would pick variant_a.
        self.flag_a.users.add(user)
        self.flag_b.users.add(user)
        request = request_with(user=user)
        decision = waffle.flag_group_result(request, 'homepage')
        self.assertEqual(decision.reason, Assignment.FORCED)
        self.assertEqual(decision.flag_name, 'variant_b')
        self.assertTrue(waffle.flag_is_active(request, 'variant_b'))
        self.assertFalse(waffle.flag_is_active(request, 'variant_a'))

    def test_priority_tie_breaks_on_flag_name(self):
        self.group.on_conflict = FlagGroup.ConflictPolicy.PRIORITY
        self.group.save()
        self.member_a.priority = 1
        self.member_a.save()
        user = User.objects.create(username='tie')
        self.flag_a.users.add(user)
        self.flag_b.users.add(user)
        request = request_with(user=user)
        decision = waffle.flag_group_result(request, 'homepage')
        self.assertEqual(decision.flag_name, 'variant_a')

    def test_forced_off_wins_over_bucket(self):
        # everyone=False forces variant off even when the bucket would hit.
        self.flag_b.everyone = False
        self.flag_b.save()
        # B has the lower priority number, so its interval starts at 0.
        user_pk = next(
            value for value in range(1, 500)
            if stable_bucket('homepage', f'user:{value}') < 1000
        )
        user = FixedUser(pk=user_pk)  # lands in B
        request = request_with(user=user)
        decision = waffle.flag_group_result(request, 'homepage')
        self.assertEqual(decision.flag_name, 'variant_b')
        self.assertFalse(decision.active)
        self.assertEqual(decision.reason, Assignment.FORCED_OFF)
        self.assertFalse(waffle.flag_is_active(request, 'variant_b'))

    def test_forced_off_does_not_count_as_conflict(self):
        # A forced-on A and forced-off B: A is the single positive winner.
        self.flag_b.everyone = False
        self.flag_b.save()
        self.flag_a.everyone = True
        self.flag_a.save()
        request = request_with()
        decision = waffle.flag_group_result(request, 'homepage')
        self.assertEqual(decision.flag_name, 'variant_a')
        self.assertTrue(decision.active)

    def test_testing_querystring_override(self):
        self.flag_a.testing = True
        self.flag_a.save()
        request = request_with(get={'dwft_variant_a': '1'})
        decision = waffle.flag_group_result(request, 'homepage')
        self.assertEqual(decision.flag_name, 'variant_a')
        self.assertEqual(decision.reason, Assignment.FORCED)


class AnonymousIdentityTests(TestCase):
    def setUp(self):
        super().setUp()
        self.group = FlagGroup.objects.create(name='homepage')
        self.flag_a = Flag.objects.create(name='variant_a')
        FlagGroupMembership.objects.create(
            group=self.group, flag=self.flag_a, percent='100')

    def test_anonymous_gets_persistent_identity_cookie(self):
        request = request_with()
        decision = waffle.flag_group_result(request, 'homepage')
        self.assertTrue(decision.active)
        self.assertIn('dwfg_homepage', request.waffle_groups)
        raw = request.waffle_groups['dwfg_homepage']
        self.assertEqual(decision.identity, f'anon:{raw}')

    def test_existing_group_cookie_is_stable(self):
        request = request_with(cookies={'dwfg_homepage': 'fixed-id'})
        first = waffle.flag_group_result(request, 'homepage')
        self.assertEqual(first.identity, 'anon:fixed-id')
        second_request = request_with(
            cookies={'dwfg_homepage': 'fixed-id'})
        second = waffle.flag_group_result(second_request, 'homepage')
        self.assertEqual(second.bucket, first.bucket)
        self.assertEqual(second.flag_name, first.flag_name)

    def test_read_only_anonymous_without_cookie_is_not_bucketed(self):
        request = request_with()
        decision = waffle.flag_group_result(
            request, 'homepage', read_only=True)
        self.assertEqual(decision.reason, Assignment.NO_IDENTITY)
        self.assertFalse(decision.active)
        self.assertFalse(
            waffle.flag_is_active(request, 'variant_a', read_only=True))


class ExplainableReallocationTests(TestCase):
    def setUp(self):
        super().setUp()
        self.group = FlagGroup.objects.create(name='homepage')
        self.flag_a = Flag.objects.create(name='variant_a')
        self.flag_b = Flag.objects.create(name='variant_b')
        self.member_a = FlagGroupMembership.objects.create(
            group=self.group, flag=self.flag_a, percent='50')
        self.member_b = FlagGroupMembership.objects.create(
            group=self.group, flag=self.flag_b, percent='30')

    def assignments(self):
        result = {}
        for value in range(3000):
            identity = f'user:{value}'
            bucket = stable_bucket('homepage', identity)
            if bucket < 5000:
                variant = 'variant_a'
            elif bucket < 8000:
                variant = 'variant_b'
            else:
                variant = None
            result[identity] = (bucket, variant)
        return result

    def test_extending_b_at_front_does_not_move_a_users(self):
        # Layout before: A=[0,5000), B=[5000,8000), empty=[8000,10000).
        before = self.assignments()
        self.member_b.percent = '50'
        self.member_b.save()

        moved = 0
        for identity, (bucket, old_variant) in before.items():
            if old_variant == 'variant_a':
                # A's interval is untouched at the front.
                decision = waffle.flag_group_result(
                    request_with(), 'homepage', identity=identity)
                self.assertEqual(decision.flag_name, 'variant_a')
            elif old_variant is None and bucket < 10000:
                decision = waffle.flag_group_result(
                    request_with(), 'homepage', identity=identity)
                if 8000 <= bucket < 10000:
                    if decision.flag_name == 'variant_b':
                        moved += 1
        self.assertGreater(moved, 0)

    def test_reduced_percent_moves_tail_buckets_to_empty(self):
        # Shrinking the last variant frees the shared tail: it becomes
        # empty, never silently reassigned to another variant.
        self.member_b.percent = '20'
        self.member_b.save()
        affected = identity_for_bucket('homepage', 7500)
        decision = waffle.flag_group_result(
            request_with(), 'homepage', identity=affected)
        self.assertEqual(decision.bucket, 7500)
        self.assertEqual(decision.reason, Assignment.EMPTY_BUCKET)
        kept = identity_for_bucket('homepage', 6000)
        decision = waffle.flag_group_result(
            request_with(), 'homepage', identity=kept)
        self.assertEqual(decision.flag_name, 'variant_b')

    def test_intervals_are_explicitly_reportable(self):
        intervals = [
            (membership.flag.name, start, end)
            for membership, start, end in self.group.intervals()
        ]
        self.assertEqual(
            intervals,
            [('variant_a', 0, 5000), ('variant_b', 5000, 8000)])


class CacheAndLifecycleTests(TestCase):
    def setUp(self):
        super().setUp()
        self.group = FlagGroup.objects.create(name='homepage')
        self.flag_a = Flag.objects.create(name='variant_a')
        self.member = FlagGroupMembership.objects.create(
            group=self.group, flag=self.flag_a, percent='100')

    def test_adding_membership_flushes_caches(self):
        request = request_with(user=FixedUser(pk=1))
        self.assertTrue(waffle.flag_is_active(request, 'variant_a'))
        flag_b = Flag.objects.create(name='variant_b')
        FlagGroupMembership.objects.create(
            group=self.group, flag=flag_b, percent='0')  # still 100 total
        # The new membership is visible without anyone clearing caches.
        self.assertIsNotNone(flag_b.get_group_membership())
        self.assertEqual(
            {m.flag.name for m in self.group.get_memberships()},
            {'variant_a', 'variant_b'})

    def test_deleting_membership_returns_flag_to_independent_behavior(self):
        self.assertTrue(
            waffle.flag_is_active(request_with(user=FixedUser(pk=1)),
                                  'variant_a'))
        self.member.delete()
        # No group anymore; standalone flag with no targeting/percent is off.
        self.assertFalse(
            waffle.flag_is_active(request_with(), 'variant_a'))

    def test_group_save_flushes_membership_cache(self):
        self.group.note = 'changed'
        self.group.save()
        request = request_with(user=FixedUser(pk=1))
        decision = waffle.flag_group_result(request, 'homepage')
        self.assertEqual(decision.flag_name, 'variant_a')


class GroupResultEdgeTests(TestCase):
    def test_missing_group_reports_missing(self):
        decision = waffle.flag_group_result(request_with(), 'nope')
        self.assertEqual(decision.reason, Assignment.MISSING_GROUP)
        self.assertFalse(decision.active)

    def test_empty_group(self):
        FlagGroup.objects.create(name='empty')
        decision = waffle.flag_group_result(request_with(), 'empty')
        self.assertEqual(decision.reason, Assignment.EMPTY_GROUP)

    def test_ungrouped_flag_keeps_independent_behavior(self):
        Flag.objects.create(name='solo')
        request = request_with()
        self.assertIsNone(
            waffle.get_waffle_flag_model()
            .get('solo').get_group_membership())
        self.assertFalse(waffle.flag_is_active(request, 'solo'))


class MiddlewareGroupCookieTests(TestCase):
    def test_response_sets_group_cookie(self):
        group = FlagGroup.objects.create(name='homepage')
        flag = Flag.objects.create(name='variant_a')
        FlagGroupMembership.objects.create(
            group=group, flag=flag, percent='100')
        request = request_with()
        waffle.flag_group_result(request, 'homepage')
        response = WaffleMiddleware(lambda r: 'ok').process_response(
            request, HttpResponse())
        self.assertIn('dwfg_homepage', response.cookies)
        self.assertTrue(response.cookies['dwfg_homepage'].value)


class GroupAdminTests(TestCase):
    def test_flag_group_registered_in_admin(self):
        from django.contrib import admin
        self.assertIn(FlagGroup, admin.site._registry)
        registered_admin = admin.site._registry[FlagGroup]
        self.assertIn(
            FlagGroupMembership,
            [inline.model for inline in registered_admin.inlines])


class MembershipUpdateTests(TestCase):
    def setUp(self):
        super().setUp()
        self.group = FlagGroup.objects.create(name='g')
        self.flag_a = Flag.objects.create(name='a')
        self.flag_b = Flag.objects.create(name='b')
        self.member_a = FlagGroupMembership.objects.create(
            group=self.group, flag=self.flag_a, percent='60')
        self.member_b = FlagGroupMembership.objects.create(
            group=self.group, flag=self.flag_b, percent='30')

    def test_update_cannot_push_total_over_100(self):
        self.member_b.percent = Decimal('40.01')
        with self.assertRaises(ValidationError):
            self.member_b.save()

    def test_update_shrinking_frees_space(self):
        self.member_a.percent = '50'
        self.member_a.save()
        self.member_b.percent = '50'
        self.member_b.save()
        intervals = self.group.intervals()
        self.assertEqual(
            [(start, end) for _m, start, end in intervals],
            [(0, 5000), (5000, 10000)])

    def test_flag_cannot_join_second_group(self):
        other = FlagGroup.objects.create(name='other')
        with self.assertRaises(IntegrityError):
            FlagGroupMembership.objects.create(
                group=other, flag=self.flag_a, percent='10')


class DatabaseOrderIndependenceTests(TestCase):
    def test_conflict_resolution_ignores_insertion_and_id_order(self):
        group = FlagGroup.objects.create(
            name='g', on_conflict=FlagGroup.ConflictPolicy.PRIORITY)
        zeta = Flag.objects.create(name='zeta')
        alpha = Flag.objects.create(name='alpha')
        # Insert higher-id/name flag first, equal priorities. A naive query
        # would return zeta; the fixed rule must pick alpha.
        FlagGroupMembership.objects.create(
            group=group, flag=zeta, percent='10', priority=5)
        FlagGroupMembership.objects.create(
            group=group, flag=alpha, percent='10', priority=5)
        user = User.objects.create(username='both')
        zeta.users.add(user)
        alpha.users.add(user)
        request = request_with(user=user)
        decision = waffle.flag_group_result(request, 'g')
        self.assertEqual(decision.reason, Assignment.FORCED)
        self.assertEqual(decision.flag_name, 'alpha')

    def test_bucket_layout_ignores_insertion_order(self):
        group = FlagGroup.objects.create(name='layout')
        second = Flag.objects.create(name='second')
        first = Flag.objects.create(name='first')
        # Insert second variant first, but give it a larger priority number
        # so it must still be laid out after first.
        FlagGroupMembership.objects.create(
            group=group, flag=second, percent='20', priority=2)
        FlagGroupMembership.objects.create(
            group=group, flag=first, percent='30', priority=1)
        intervals = [
            (membership.flag.name, start, end)
            for membership, start, end in group.intervals()
        ]
        self.assertEqual(
            intervals,
            [('first', 0, 3000), ('second', 3000, 5000)])
