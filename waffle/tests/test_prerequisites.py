import threading
import traceback
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import JSONField, Value
from django.http import HttpResponse
from django.test import RequestFactory, TransactionTestCase

import waffle
from waffle.middleware import WaffleMiddleware
from waffle.prerequisites import (
    CircularFlagDependencyError,
    InvalidPrerequisiteError,
    evaluate_prerequisites,
)
from waffle.tests.base import TestCase


def request():
    req = RequestFactory().get('/')
    req.user = mock.Mock(is_authenticated=False, is_staff=False, is_superuser=False)
    return req


def response_for(req):
    middleware = WaffleMiddleware(lambda request: 'ok')
    return middleware.process_response(req, HttpResponse('ok'))


class PrerequisiteTests(TestCase):
    def test_boolean_expressions(self):
        def resolve_flag(name):
            return {
                'base_checkout': True,
                'legacy_checkout': False,
            }[name]

        self.assertTrue(evaluate_prerequisites('base_checkout', resolve_flag))
        self.assertTrue(
            evaluate_prerequisites(
                {'flag': 'legacy_checkout', 'active': False},
                resolve_flag,
            )
        )
        self.assertFalse(
            evaluate_prerequisites(
                {'any': [
                    'legacy_checkout',
                    {'flag': 'base_checkout', 'active': False},
                ]},
                resolve_flag,
            )
        )

    def test_blank_prerequisite_name_is_invalid(self):
        with self.assertRaises(InvalidPrerequisiteError):
            evaluate_prerequisites('   ', lambda name: True)

    def test_payment_requires_base_checkout_flag(self):
        Flag = waffle.get_waffle_flag_model()
        base = Flag.objects.create(name='base_checkout', everyone=True)
        payment = Flag.objects.create(name='new_payment', percent='50.0')

        payment.prerequisites = 'base_checkout'
        payment.save()
        req = request()
        with mock.patch('waffle.models.random.uniform', return_value=10):
            self.assertTrue(waffle.flag_is_active(req, 'new_payment'))

        base.everyone = False
        base.save()
        req = request()
        self.assertFalse(waffle.flag_is_active(req, 'new_payment'))
        self.assertNotIn('new_payment', getattr(req, 'waffles', {}))

    def test_negative_prerequisite(self):
        Flag = waffle.get_waffle_flag_model()
        Flag.objects.create(name='legacy_checkout', everyone=False)
        payment = Flag.objects.create(name='new_payment', everyone=True)
        payment.prerequisites = {'flag': 'legacy_checkout', 'active': False}
        payment.save()

        self.assertTrue(waffle.flag_is_active(request(), 'new_payment'))

    def test_and_or_combinations_short_circuit(self):
        Flag = waffle.get_waffle_flag_model()
        Flag.objects.create(name='base_checkout', everyone=True)
        Flag.objects.create(name='region_rollout', everyone=True)
        Flag.objects.create(name='legacy_checkout', everyone=False)
        unvisited = Flag.objects.create(
            name='unvisited_percent', percent='50.0'
        )
        payment = Flag.objects.create(name='new_payment', everyone=True)
        payment.prerequisites = {
            'all': [
                'base_checkout',
                {'any': [
                    'region_rollout',
                    {'flag': 'legacy_checkout', 'active': True},
                    'unvisited_percent',
                ]},
                {'not': {'flag': 'legacy_checkout'}},
            ]
        }
        payment.save()

        req = request()
        with mock.patch.object(type(unvisited), 'get', wraps=type(unvisited).get) as get_flag:
            self.assertTrue(waffle.flag_is_active(req, 'new_payment'))
            self.assertFalse(
                any(call.args[0] == 'unvisited_percent' for call in get_flag.call_args_list)
            )

        self.assertNotIn('unvisited_percent', getattr(req, 'waffles', {}))
        response = response_for(req)
        self.assertNotIn('dwf_unvisited_percent', response.cookies)

    def test_and_false_condition_short_circuits(self):
        Flag = waffle.get_waffle_flag_model()
        base = Flag.objects.create(name='base_checkout', percent='50.0')
        unvisited = Flag.objects.create(name='unvisited_percent', percent='50.0')
        payment = Flag.objects.create(
            name='new_payment',
            prerequisites={'all': ['base_checkout', 'unvisited_percent']},
        )

        req = request()
        with mock.patch('waffle.models.random.uniform', return_value=70):
            with mock.patch.object(Flag, 'get', wraps=Flag.get) as get_flag:
                self.assertFalse(waffle.flag_is_active(req, 'new_payment'))

        self.assertFalse(
            any(call.args[0] == 'unvisited_percent' for call in get_flag.call_args_list)
        )
        self.assertEqual(1, get_flag.call_args_list.count(mock.call(base.name)))
        self.assertNotIn('unvisited_percent', getattr(req, 'waffles', {}))
        response = response_for(req)
        self.assertNotIn('dwf_unvisited_percent', response.cookies)

    def test_not_inverts_dependency(self):
        Flag = waffle.get_waffle_flag_model()
        Flag.objects.create(name='base_checkout', everyone=False)
        payment = Flag.objects.create(
            name='new_payment',
            everyone=True,
            prerequisites={
                'all': [{'not': {'flag': 'base_checkout'}}]
            },
        )

        req = request()
        self.assertTrue(waffle.flag_is_active(req, 'new_payment'))

    def test_diamond_dependency_is_evaluated_once_per_request(self):
        Flag = waffle.get_waffle_flag_model()
        shared = Flag.objects.create(name='shared_dependency', everyone=True)
        left = Flag.objects.create(name='left_dependency', everyone=True)
        right = Flag.objects.create(name='right_dependency', everyone=True)
        root = Flag.objects.create(name='root', everyone=True)

        left.prerequisites = 'shared_dependency'
        left.save()
        right.prerequisites = 'shared_dependency'
        right.save()
        root.prerequisites = {'all': ['left_dependency', 'right_dependency']}
        root.save()

        req = request()
        with mock.patch.object(
            Flag,
            '_is_active_without_prerequisites',
            autospec=True,
            wraps=Flag._is_active_without_prerequisites,
        ) as evaluate:
            self.assertTrue(waffle.flag_is_active(req, 'root'))
            self.assertEqual(
                1,
                sum(
                    call.args[0].name == 'shared_dependency'
                    for call in evaluate.mock_calls
                ),
            )

    def test_request_context_does_not_leak_between_users(self):
        Flag = waffle.get_waffle_flag_model()
        base = Flag.objects.create(name='base_checkout')
        payment = Flag.objects.create(name='new_payment', everyone=True)
        payment.prerequisites = 'base_checkout'
        payment.save()

        active_user = mock.Mock()
        active_user.is_authenticated = True
        inactive_request = request()
        active_request = request()
        active_request.user = active_user

        base.authenticated = False
        base.save()
        self.assertFalse(waffle.flag_is_active(inactive_request, 'new_payment'))

        cache.clear()
        base.authenticated = True
        base.save()
        self.assertTrue(waffle.flag_is_active(active_request, 'new_payment'))
        self.assertFalse(waffle.flag_is_active(request(), 'new_payment'))

    def test_unknown_prerequisite_fails_closed(self):
        Flag = waffle.get_waffle_flag_model()
        payment = Flag.objects.create(name='new_payment', everyone=True)
        payment.prerequisites = 'missing_checkout'

        with self.assertRaises(ValidationError):
            payment.save()

        Flag.objects.filter(name='new_payment').update(
            prerequisites=Value(
                '"missing_checkout"', output_field=JSONField()
            )
        )
        self.assertFalse(waffle.flag_is_active(request(), 'new_payment'))

    def test_invalid_prerequisite_is_rejected_on_save(self):
        Flag = waffle.get_waffle_flag_model()
        flag = Flag.objects.create(name='payment')
        flag.prerequisites = {'all': []}

        with self.assertRaises(ValidationError):
            flag.save()

    def test_synchronous_cycle_is_rejected_on_save(self):
        Flag = waffle.get_waffle_flag_model()
        base = Flag.objects.create(name='base_checkout')
        payment = Flag.objects.create(name='new_payment')
        base.prerequisites = 'new_payment'
        base.save()
        payment.prerequisites = 'base_checkout'

        with self.assertRaises(ValidationError):
            payment.save()


class PrerequisiteCycleTransactionTests(TransactionTestCase):
    reset_sequences = True

    def test_cycle_created_by_concurrent_saves_raises_identifiable_error(self):
        Flag = waffle.get_waffle_flag_model()
        first = Flag.objects.create(name='first_checkout')
        second = Flag.objects.create(name='second_checkout')
        cache.clear()

        start = threading.Barrier(2)
        written = threading.Barrier(2)
        barrier_after_validation = threading.Barrier(2)
        errors = []
        original_validate_prerequisites = Flag.validate_prerequisites

        def validate_after_barrier(self):
            original_validate_prerequisites(self)
            barrier_after_validation.wait()

        def update(name, dependency):
            try:
                flag = Flag.objects.get(name=name)
                start.wait()
                flag.prerequisites = dependency
                flag.save()
                written.wait()
            except Exception:
                errors.append(traceback.format_exc())
                for barrier in (start, barrier_after_validation, written):
                    try:
                        barrier.abort()
                    except threading.BrokenBarrierError:
                        pass

        with mock.patch.object(
            Flag,
            'validate_prerequisites',
            side_effect=validate_after_barrier,
            autospec=True,
        ) as validate:
            threads = [
                threading.Thread(
                    target=update,
                    args=('first_checkout', 'second_checkout'),
                ),
                threading.Thread(
                    target=update,
                    args=('second_checkout', 'first_checkout'),
                ),
            ]
            for thread in threads:
                thread.start()

            thread_timeout = 5
            for thread in threads:
                thread.join(thread_timeout)
                self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            self.assertGreaterEqual(validate.call_count, 2)

        cache.clear()
        with self.assertRaises(CircularFlagDependencyError) as raised:
            waffle.flag_is_active(request(), 'first_checkout')

        self.assertIn('Circular flag dependency', str(raised.exception))
