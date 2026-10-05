from collections import Counter
from unittest import mock

from django.contrib.auth.models import AnonymousUser, User
from django.core import cache
from django.core.exceptions import ValidationError
from django.test import RequestFactory
from django.test.utils import override_settings

import waffle
from waffle.models import Flag
from waffle.prerequisites import (
    PrerequisiteCycleError,
    UnknownPrerequisiteFlagError,
)
from waffle.tests.base import TestCase


def get(**kw):
    request = RequestFactory().get('/foo', data=kw)
    request.user = AnonymousUser()
    return request


def make_flag(name, **kwargs):
    return Flag.objects.create(name=name, **kwargs)


class count_active_calls:
    """Context manager counting Flag.is_active calls per flag name."""

    def __init__(self):
        self.calls = Counter()
        self.original = Flag.is_active

    def __enter__(self):
        counter = self.calls
        original = self.original

        def counting_is_active(self, request, read_only=False):
            counter[self.name] += 1
            return original(self, request, read_only=read_only)

        self._patch = mock.patch.object(Flag, 'is_active', counting_is_active)
        self._patch.start()
        return self.calls

    def __exit__(self, *exc):
        self._patch.stop()
        return False


class PrerequisiteEvaluationTests(TestCase):
    def test_basic_prerequisite_gates_flag(self):
        make_flag('base_settlement', everyone=True)
        make_flag('new_payment', everyone=True, prerequisites={
            'all': [{'flag': 'base_settlement', 'state': 'on'}],
        })
        assert waffle.flag_is_active(get(), 'new_payment') is True

        base = Flag.objects.get(name='base_settlement')
        base.everyone = False
        base.save()
        assert waffle.flag_is_active(get(), 'new_payment') is False

    def test_prerequisites_override_everyone(self):
        make_flag('dependency', everyone=False)
        make_flag('gated', everyone=True, prerequisites={
            'all': [{'flag': 'dependency'}],
        })
        assert waffle.flag_is_active(get(), 'gated') is False

    def test_negation_condition(self):
        make_flag('legacy_off', everyone=False)
        make_flag('new_payment', everyone=True, prerequisites={
            'all': [{'flag': 'legacy_off', 'state': 'off'}],
        })
        assert waffle.flag_is_active(get(), 'new_payment') is True

        legacy = Flag.objects.get(name='legacy_off')
        legacy.everyone = True
        legacy.save()
        assert waffle.flag_is_active(get(), 'new_payment') is False

    def test_and_or_combination(self):
        make_flag('base', everyone=True)
        make_flag('beta', everyone=False)
        make_flag('staff_only', everyone=True)
        make_flag('combo', everyone=True, prerequisites={
            'all': [
                {'flag': 'base', 'state': 'on'},
                {'any': [
                    {'flag': 'beta', 'state': 'on'},
                    {'flag': 'staff_only', 'state': 'off'},
                ]},
            ],
        })
        # beta is off, but staff_only is on so the negated branch fails.
        assert waffle.flag_is_active(get(), 'combo') is False

        beta = Flag.objects.get(name='beta')
        beta.everyone = True
        beta.save()
        assert waffle.flag_is_active(get(), 'combo') is True

    def test_diamond_dependency_evaluated_once_per_request(self):
        make_flag('d', everyone=True)
        make_flag('b', everyone=True, prerequisites={
            'all': [{'flag': 'd'}],
        })
        make_flag('c', everyone=True, prerequisites={
            'all': [{'flag': 'd'}],
        })
        make_flag('a', everyone=True, prerequisites={
            'all': [{'flag': 'b'}, {'flag': 'c'}],
        })

        with count_active_calls() as calls:
            assert waffle.flag_is_active(get(), 'a') is True

        assert calls['a'] == 1
        assert calls['b'] == 1
        assert calls['c'] == 1
        assert calls['d'] == 1

    def test_negated_diamond_evaluated_once_per_request(self):
        make_flag('d', everyone=False)
        make_flag('b', everyone=True, prerequisites={
            'all': [{'flag': 'd', 'state': 'off'}],
        })
        make_flag('c', everyone=True, prerequisites={
            'any': [{'flag': 'd'}, {'flag': 'd', 'state': 'off'}],
        })
        make_flag('a', everyone=True, prerequisites={
            'all': [{'flag': 'b'}, {'flag': 'c'}],
        })

        with count_active_calls() as calls:
            assert waffle.flag_is_active(get(), 'a') is True

        assert calls['d'] == 1

    def test_results_not_shared_across_requests(self):
        user = User.objects.create(username='alice')
        make_flag('for_users', authenticated=True)
        make_flag('gated', everyone=True, prerequisites={
            'all': [{'flag': 'for_users'}],
        })

        authed_request = get()
        authed_request.user = user
        anon_request = get()

        with count_active_calls() as calls:
            assert waffle.flag_is_active(authed_request, 'gated') is True
            assert waffle.flag_is_active(anon_request, 'gated') is False

        # Each request evaluated the dependency independently.
        assert calls['for_users'] == 2

    def test_short_circuited_and_branch_sets_no_percent_cookie(self):
        make_flag('off_flag', everyone=False)
        make_flag('pct', percent=99.9)
        make_flag('gated', everyone=True, prerequisites={
            'all': [
                {'flag': 'off_flag', 'state': 'on'},
                {'flag': 'pct', 'state': 'on'},
            ],
        })

        request = get()
        with count_active_calls() as calls:
            assert waffle.flag_is_active(request, 'gated') is False

        assert calls['pct'] == 0
        waffles = getattr(request, 'waffles', {})
        assert 'pct' not in waffles

    def test_short_circuited_or_branch_sets_no_percent_cookie(self):
        make_flag('on_flag', everyone=True)
        make_flag('pct', percent=99.9)
        make_flag('gated', everyone=True, prerequisites={
            'any': [
                {'flag': 'on_flag', 'state': 'on'},
                {'flag': 'pct', 'state': 'on'},
            ],
        })

        request = get()
        with count_active_calls() as calls:
            assert waffle.flag_is_active(request, 'gated') is True

        assert calls['pct'] == 0
        waffles = getattr(request, 'waffles', {})
        assert 'pct' not in waffles

    def test_unknown_prerequisite_flag_defaults_to_inactive(self):
        make_flag('gated_on', everyone=True, prerequisites={
            'all': [{'flag': 'does_not_exist', 'state': 'on'}],
        })
        make_flag('gated_off', everyone=True, prerequisites={
            'all': [{'flag': 'does_not_exist', 'state': 'off'}],
        })
        assert waffle.flag_is_active(get(), 'gated_on') is False
        assert waffle.flag_is_active(get(), 'gated_off') is True

    @override_settings(WAFFLE_PREREQUISITE_UNKNOWN_FLAG='error')
    def test_unknown_prerequisite_flag_error_policy(self):
        make_flag('gated', everyone=True, prerequisites={
            'all': [{'flag': 'does_not_exist'}],
        })
        with self.assertRaises(UnknownPrerequisiteFlagError):
            waffle.flag_is_active(get(), 'gated')

    def test_cycle_from_concurrent_update_raises_recognizable_error(self):
        make_flag('a', everyone=True, prerequisites={
            'all': [{'flag': 'b'}],
        })
        make_flag('b', everyone=True)

        # Simulate a concurrent config update that bypasses save()
        # validation (e.g. a bulk queryset update in another process).
        Flag.objects.filter(name='b').update(prerequisites={
            'all': [{'flag': 'a'}],
        })
        cache.cache.clear()

        with self.assertRaisesMessage(PrerequisiteCycleError, 'a -> b -> a'):
            waffle.flag_is_active(get(), 'a')

    def test_self_reference_cycle_raises_at_evaluation(self):
        make_flag('selfish', everyone=True)
        Flag.objects.filter(name='selfish').update(prerequisites={
            'all': [{'flag': 'selfish'}],
        })
        cache.cache.clear()

        with self.assertRaisesMessage(PrerequisiteCycleError,
                                      'selfish -> selfish'):
            waffle.flag_is_active(get(), 'selfish')


class PrerequisiteValidationTests(TestCase):
    def test_save_rejects_cycle(self):
        make_flag('a', prerequisites={'all': [{'flag': 'b'}]})
        b = make_flag('b')
        b.prerequisites = {'all': [{'flag': 'a'}]}
        with self.assertRaises(ValidationError):
            b.save()

    def test_save_rejects_indirect_cycle(self):
        make_flag('a', prerequisites={'all': [{'flag': 'b'}]})
        make_flag('b', prerequisites={'all': [{'flag': 'c'}]})
        c = make_flag('c')
        c.prerequisites = {'any': [{'flag': 'a'}]}
        with self.assertRaisesMessage(ValidationError, 'c -> a -> b -> c'):
            c.save()

    def test_save_rejects_self_reference(self):
        with self.assertRaises(ValidationError):
            make_flag('a', prerequisites={'all': [{'flag': 'a'}]})

    def test_save_allows_unknown_prerequisite_flag(self):
        # Unknown flags are an evaluation-time policy concern, not a
        # save-time cycle.
        make_flag('a', prerequisites={'all': [{'flag': 'missing'}]})
        assert Flag.objects.filter(name='a').exists()

    def test_clean_rejects_cycle(self):
        make_flag('a', prerequisites={'all': [{'flag': 'b'}]})
        b = make_flag('b')
        b.prerequisites = {'all': [{'flag': 'a'}]}
        with self.assertRaises(ValidationError):
            b.full_clean()

    def test_invalid_specs_rejected(self):
        bad_specs = [
            {},
            {'all': []},
            {'all': [{'flag': 'a'}], 'any': [{'flag': 'b'}]},
            {'flag': ''},
            {'flag': 'a', 'state': 'maybe'},
            {'all': [{'flag': 'a', 'state': 'on'}, 'not-a-node']},
            {'all': 'a'},
        ]
        for spec in bad_specs:
            flag = Flag(name='flag_%d' % abs(hash(str(spec))),
                        prerequisites=spec)
            with self.assertRaises(ValidationError, msg=str(spec)):
                flag.save()

    def test_valid_nested_spec_accepted(self):
        make_flag('x')
        flag = make_flag('y', prerequisites={
            'any': [
                {'flag': 'x'},
                {'all': [
                    {'flag': 'x', 'state': 'off'},
                    {'flag': 'missing', 'state': 'on'},
                ]},
            ],
        })
        flag.full_clean()
