from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.core.exceptions import ValidationError
from django.test import RequestFactory
from django.test.utils import override_settings

import waffle
from waffle.models import ExperimentConflictError, ExperimentGroup, Flag
from waffle.tests.base import TestCase


def get_request(user=None, **get):
    request = RequestFactory().get('/foo', data=get)
    request.user = user or AnonymousUser()
    return request


def bucket_for(group_name, user):
    return ExperimentGroup.get_bucket(group_name, f'user:{user.pk}')


class ExperimentGroupTestCase(TestCase):
    """A/B flags sharing one mutually exclusive bucket space.

    a_flag (name first) owns buckets [0, 300), b_flag owns [300, 500),
    and buckets [500, 1000) are not part of the experiment.
    """

    def setUp(self):
        super().setUp()
        self.group = ExperimentGroup.objects.create(name='exp')
        self.flag_a = Flag.objects.create(
            name='a_flag', percent=30, experiment_group=self.group)
        self.flag_b = Flag.objects.create(
            name='b_flag', percent=20, experiment_group=self.group)

    def make_user(self, username='user'):
        return get_user_model().objects.create(username=username)

    def make_user_with_bucket(self, bucket):
        for pk in range(1, 100000):
            if ExperimentGroup.get_bucket('exp', f'user:{pk}') == bucket:
                return get_user_model().objects.create(
                    pk=pk, username=f'user{pk}')
        self.fail(f'no identity found for bucket {bucket}')

    def expected_assignment(self, user):
        bucket = bucket_for('exp', user)
        if bucket < 300:
            return 'a_flag'
        if bucket < 500:
            return 'b_flag'
        return None


class IntervalTests(ExperimentGroupTestCase):
    def test_intervals_are_contiguous_in_name_order(self):
        intervals = self.group.get_intervals()
        self.assertEqual(
            [(flag.name, start, end) for flag, start, end in intervals],
            [('a_flag', 0, 300), ('b_flag', 300, 500)],
        )

    def test_total_percent(self):
        self.assertEqual(self.group.get_total_percent(), 50)

    def test_group_rejects_total_over_100(self):
        self.flag_b.percent = 80
        self.flag_b.save()
        with self.assertRaises(ValidationError):
            self.group.clean()

    def test_flag_rejects_total_over_100(self):
        flag = Flag(name='c_flag', percent=60, experiment_group=self.group)
        with self.assertRaises(ValidationError):
            flag.full_clean()

    def test_group_allows_total_of_exactly_100(self):
        self.flag_b.percent = 70
        self.flag_b.save()
        self.group.clean()


class BucketBoundaryTests(ExperimentGroupTestCase):
    """Fixed identities landing exactly on every interval boundary."""

    def assert_assignment(self, bucket, expected):
        user = self.make_user_with_bucket(bucket)
        request = get_request(user)
        self.assertEqual(bucket_for('exp', user), bucket)
        self.assertEqual(waffle.experiment_result(request, 'exp'), expected)
        self.assertEqual(
            waffle.flag_is_active(request, 'a_flag'), expected == 'a_flag')
        self.assertEqual(
            waffle.flag_is_active(request, 'b_flag'), expected == 'b_flag')

    def test_first_bucket_of_first_flag(self):
        self.assert_assignment(0, 'a_flag')

    def test_last_bucket_of_first_flag(self):
        self.assert_assignment(299, 'a_flag')

    def test_first_bucket_of_second_flag(self):
        self.assert_assignment(300, 'b_flag')

    def test_last_bucket_of_second_flag(self):
        self.assert_assignment(499, 'b_flag')

    def test_first_empty_bucket(self):
        self.assert_assignment(500, None)

    def test_last_empty_bucket(self):
        self.assert_assignment(999, None)


class AssignmentConsistencyTests(ExperimentGroupTestCase):
    def test_flag_is_active_matches_group_result_for_many_identities(self):
        users = [self.make_user(username=f'user{i}') for i in range(100)]
        assigned = {'a_flag': 0, 'b_flag': 0, None: 0}
        for user in users:
            request = get_request(user)
            expected = self.expected_assignment(user)
            assigned[expected] += 1
            self.assertEqual(
                waffle.experiment_result(request, 'exp'), expected)
            self.assertEqual(
                waffle.flag_is_active(request, 'a_flag'),
                expected == 'a_flag')
            self.assertEqual(
                waffle.flag_is_active(request, 'b_flag'),
                expected == 'b_flag')
        # All regions of the bucket space are exercised.
        self.assertTrue(all(count > 0 for count in assigned.values()))

    def test_result_does_not_depend_on_check_order(self):
        user = self.make_user()
        expected = self.expected_assignment(user)

        request_a_first = get_request(user)
        waffle.flag_is_active(request_a_first, 'a_flag')
        waffle.flag_is_active(request_a_first, 'b_flag')

        request_b_first = get_request(user)
        waffle.flag_is_active(request_b_first, 'b_flag')
        waffle.flag_is_active(request_b_first, 'a_flag')

        self.assertEqual(
            waffle.experiment_result(request_a_first, 'exp'), expected)
        self.assertEqual(
            waffle.experiment_result(request_b_first, 'exp'), expected)

    def test_flags_are_mutually_exclusive(self):
        users = [self.make_user(username=f'user{i}') for i in range(100)]
        for user in users:
            request = get_request(user)
            self.assertFalse(
                waffle.flag_is_active(request, 'a_flag')
                and waffle.flag_is_active(request, 'b_flag')
            )

    def test_empty_buckets_get_no_flag(self):
        user = self.make_user_with_bucket(700)
        request = get_request(user)
        self.assertIsNone(waffle.experiment_result(request, 'exp'))
        self.assertFalse(waffle.flag_is_active(request, 'a_flag'))
        self.assertFalse(waffle.flag_is_active(request, 'b_flag'))

    def test_missing_group_gives_no_assignment(self):
        self.assertIsNone(waffle.experiment_result(get_request(), 'missing'))

    def test_anonymous_request_is_internally_consistent(self):
        request = get_request()
        result = waffle.experiment_result(request, 'exp')
        self.assertEqual(
            waffle.flag_is_active(request, 'a_flag'), result == 'a_flag')
        self.assertEqual(
            waffle.flag_is_active(request, 'b_flag'), result == 'b_flag')


class ForcedHitTests(ExperimentGroupTestCase):
    """User/group targeting and overrides take priority over buckets."""

    def test_user_targeting_forces_hit_outside_bucket(self):
        user = self.make_user_with_bucket(700)  # empty bucket
        self.flag_b.users.add(user)
        request = get_request(user)
        self.assertEqual(waffle.experiment_result(request, 'exp'), 'b_flag')
        self.assertFalse(waffle.flag_is_active(request, 'a_flag'))
        self.assertTrue(waffle.flag_is_active(request, 'b_flag'))

    def test_group_targeting_forces_hit(self):
        user = self.make_user_with_bucket(700)
        group = Group.objects.create(name='testers')
        user.groups.add(group)
        self.flag_a.groups.add(group)
        request = get_request(user)
        self.assertEqual(waffle.experiment_result(request, 'exp'), 'a_flag')

    def test_multiple_forced_hits_resolve_by_fixed_name_priority(self):
        user = self.make_user()
        self.flag_a.users.add(user)
        self.flag_b.users.add(user)
        request = get_request(user)
        # a_flag sorts before b_flag: a fixed, documented winner, not
        # whichever row the database happens to return first.
        self.assertEqual(waffle.experiment_result(request, 'exp'), 'a_flag')
        self.assertTrue(waffle.flag_is_active(request, 'a_flag'))
        self.assertFalse(waffle.flag_is_active(request, 'b_flag'))

    def test_multiple_forced_hits_priority_does_not_depend_on_order(self):
        user = self.make_user()
        self.flag_a.users.add(user)
        self.flag_b.users.add(user)
        request = get_request(user)
        waffle.flag_is_active(request, 'b_flag')
        self.assertEqual(waffle.experiment_result(request, 'exp'), 'a_flag')

    @override_settings(WAFFLE_EXPERIMENT_CONFLICT='error')
    def test_multiple_forced_hits_can_be_rejected_by_configuration(self):
        user = self.make_user()
        self.flag_a.users.add(user)
        self.flag_b.users.add(user)
        with self.assertRaises(ExperimentConflictError):
            waffle.experiment_result(get_request(user), 'exp')

    def test_everyone_true_wins_the_whole_group(self):
        self.flag_b.everyone = True
        self.flag_b.save()
        user = self.make_user_with_bucket(0)  # a_flag bucket
        request = get_request(user)
        self.assertEqual(waffle.experiment_result(request, 'exp'), 'b_flag')
        self.assertFalse(waffle.flag_is_active(request, 'a_flag'))
        self.assertTrue(waffle.flag_is_active(request, 'b_flag'))

    def test_everyone_false_excludes_flag_but_keeps_its_interval(self):
        self.flag_a.everyone = False
        self.flag_a.save()
        user = self.make_user_with_bucket(100)  # a_flag bucket
        request = get_request(user)
        self.assertIsNone(waffle.experiment_result(request, 'exp'))
        # b_flag's interval does not move into the dead space.
        other = self.make_user_with_bucket(400)
        self.assertEqual(
            waffle.experiment_result(get_request(other), 'exp'), 'b_flag')

    def test_testing_override_forces_hit(self):
        self.flag_a.testing = True
        self.flag_a.save()
        user = self.make_user_with_bucket(700)
        request = get_request(user, dwft_a_flag='1')
        self.assertEqual(waffle.experiment_result(request, 'exp'), 'a_flag')

    def test_testing_override_off_excludes_flag(self):
        self.flag_a.testing = True
        self.flag_a.save()
        user = self.make_user_with_bucket(100)  # a_flag bucket
        request = get_request(user, dwft_a_flag='0')
        self.assertIsNone(waffle.experiment_result(request, 'exp'))

    def test_language_forces_hit(self):
        self.flag_b.languages = 'en'
        self.flag_b.save()
        user = self.make_user_with_bucket(700)
        request = get_request(user)
        request.LANGUAGE_CODE = 'en'
        self.assertEqual(waffle.experiment_result(request, 'exp'), 'b_flag')


class PercentChangeTests(ExperimentGroupTestCase):
    """Ratio changes only move interval boundaries, never reshuffle."""

    def assignments(self, users):
        return {
            user.pk: waffle.experiment_result(get_request(user), 'exp')
            for user in users
        }

    def test_growing_later_flag_keeps_earlier_flag_members(self):
        users = [self.make_user(username=f'user{i}') for i in range(200)]
        before = self.assignments(users)

        self.flag_b.percent = 40  # b_flag: [300, 500) -> [300, 700)
        self.flag_b.save()

        after = self.assignments(users)
        for user in users:
            bucket = bucket_for('exp', user)
            if before[user.pk] == 'a_flag':
                # a_flag's interval is untouched: members are stable.
                self.assertEqual(after[user.pk], 'a_flag')
            elif 500 <= bucket < 700:
                # Only buckets newly covered by b_flag change hands.
                self.assertEqual(after[user.pk], 'b_flag')
            elif bucket >= 700:
                self.assertIsNone(after[user.pk])

    def test_shrinking_first_flag_only_moves_boundary_buckets(self):
        users = [self.make_user(username=f'user{i}') for i in range(200)]
        before = self.assignments(users)

        self.flag_a.percent = 10  # a_flag: [0, 100), b_flag: [100, 300)
        self.flag_a.save()

        after = self.assignments(users)
        for user in users:
            bucket = bucket_for('exp', user)
            if bucket < 100:
                self.assertEqual(after[user.pk], 'a_flag')
            elif 100 <= bucket < 300 and before[user.pk] == 'a_flag':
                # Buckets between the old and new boundary move to b_flag.
                self.assertEqual(after[user.pk], 'b_flag')
            elif bucket >= 500:
                self.assertIsNone(after[user.pk])
