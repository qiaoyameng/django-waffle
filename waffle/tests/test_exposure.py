import asyncio
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory, override_settings

from test_app import views
import waffle
from waffle.exposure import (
    ExposureStateClosed,
    ReadOnlyExposureError,
    get_exposure_state,
)
from waffle.middleware import WaffleMiddleware
from waffle.tests.base import TestCase

RECORDER = 'waffle.tests.test_exposure.fake_recorder'


def fake_recorder(event):
    """Test recorder; replaced per test via a Mock, but usable as a path."""


def make_request():
    request = RequestFactory().get('/foo')
    request.user = AnonymousUser()
    return request


def run_through_middleware(view, request):
    def get_response(req):
        return view(req)

    return WaffleMiddleware(get_response)(request)


@override_settings(WAFFLE_EXPOSURE_RECORDER=RECORDER)
class ExposureDecisionTests(TestCase):
    def setUp(self):
        super().setUp()
        self.recorder = mock.Mock(side_effect=fake_recorder)
        # Point the resolved dotted path at our per-test mock.
        import waffle.tests.test_exposure as mod
        self._patch = mock.patch.object(mod, 'fake_recorder', self.recorder)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.flag_model = waffle.get_waffle_flag_model()

    def test_evaluate_without_confirm_records_nothing(self):
        self.flag_model.objects.create(name='myflag', everyone=True)
        request = make_request()
        response = run_through_middleware(
            views.evaluate_without_confirm_view, request
        )
        self.assertEqual(b'on', response.content)
        self.recorder.assert_not_called()

    def test_decision_is_truthy_and_equals_final_decision(self):
        self.flag_model.objects.create(name='myflag', everyone=True)
        decision = waffle.evaluate_flag(make_request(), 'myflag')
        self.assertTrue(decision)
        self.assertTrue(decision.active)

        self.flag_model.objects.create(name='offflag', everyone=False)
        off = waffle.evaluate_flag(make_request(), 'offflag')
        self.assertFalse(off)
        self.assertFalse(off.active)

    def test_confirm_records_once_on_success(self):
        self.flag_model.objects.create(name='myflag', everyone=True)
        request = make_request()
        response = run_through_middleware(views.evaluate_confirm_view, request)
        self.assertEqual(b'on', response.content)
        self.assertEqual(1, self.recorder.call_count)
        event = self.recorder.call_args.args[0]
        self.assertEqual('myflag', event.flag_name)
        self.assertTrue(event.active)

    def test_confirm_multiple_times_is_deduplicated(self):
        self.flag_model.objects.create(name='myflag', everyone=True)
        request = make_request()
        decision = waffle.evaluate_flag(request, 'myflag')
        self.assertTrue(decision.confirm())
        # Same handle, same request: de-duplicated and returns False.
        self.assertFalse(decision.confirm())
        self.assertFalse(decision.confirm())

        # A fresh evaluation of the same flag in the same request also
        # de-duplicates.
        self.assertFalse(waffle.evaluate_flag(request, 'myflag').confirm())

        waffle.flush_exposures(request)
        self.assertEqual(1, self.recorder.call_count)

    def test_handle_cannot_be_confirmed_in_another_request(self):
        self.flag_model.objects.create(name='myflag', everyone=True)
        first = make_request()
        decision = waffle.evaluate_flag(first, 'myflag')
        self.assertTrue(decision.confirm())
        waffle.flush_exposures(first)

        second = make_request()
        # The handle is sealed to its original request: confirming it while
        # "processing" another request raises instead of recording there.
        with self.assertRaises(ExposureStateClosed):
            decision.confirm()

        # The second request gets its own clean state and records nothing.
        waffle.flush_exposures(second, submit=False)
        self.assertEqual(1, self.recorder.call_count)

    def test_different_flags_in_same_request_are_separate(self):
        self.flag_model.objects.create(name='a', everyone=True)
        self.flag_model.objects.create(name='b', everyone=False)
        request = make_request()
        waffle.evaluate_flag(request, 'a').confirm()
        waffle.evaluate_flag(request, 'b').confirm()
        waffle.flush_exposures(request)
        self.assertEqual(2, self.recorder.call_count)
        names = {c.args[0].flag_name for c in self.recorder.call_args_list}
        self.assertEqual({'a', 'b'}, names)

    def test_nested_template_usage_confirms_and_deduplicates(self):
        self.flag_model.objects.create(name='myflag', everyone=True)
        request = make_request()
        response = run_through_middleware(
            views.flag_nested_in_template, request
        )
        body = response.content.decode()
        self.assertIn('outer-flag-on', body)
        self.assertIn('inner-flag-on', body)
        # The flag was rendered three times across outer + include.
        self.assertEqual(1, self.recorder.call_count)

    def test_event_does_not_contain_request_or_user(self):
        self.flag_model.objects.create(name='myflag', everyone=True)
        request = make_request()
        request.user = get_user_model()(username='bob', pk=42)
        waffle.evaluate_flag(request, 'myflag').confirm()
        waffle.flush_exposures(request)
        event = self.recorder.call_args.args[0]
        self.assertEqual({}, event.identity)
        self.assertFalse(hasattr(event, 'request'))
        self.assertFalse(hasattr(event, 'user'))

    @override_settings(WAFFLE_EXPOSURE_IDENTITY_FIELDS=('user_id',))
    def test_explicit_approved_identity_field_is_recorded(self):
        self.flag_model.objects.create(name='myflag', everyone=True)
        request = make_request()
        request.user = get_user_model()(username='bob', pk=7)
        waffle.evaluate_flag(request, 'myflag').confirm(user_id=7)
        waffle.flush_exposures(request)
        event = self.recorder.call_args.args[0]
        self.assertEqual({'user_id': 7}, event.identity)

    @override_settings(WAFFLE_EXPOSURE_IDENTITY_FIELDS=('user_id',))
    def test_unapproved_identity_field_is_rejected(self):
        self.flag_model.objects.create(name='myflag', everyone=True)
        decision = waffle.evaluate_flag(make_request(), 'myflag')
        with self.assertRaises(ValueError):
            decision.confirm(email='bob@example.com')
        self.assertEqual(0, self.recorder.call_count)

    @override_settings(WAFFLE_EXPOSURE_IDENTITY_FIELDS=('user_id',))
    def test_template_confirms_resolve_approved_fields_only(self):
        self.flag_model.objects.create(name='myflag', everyone=True)
        request = make_request()
        request.user = get_user_model()(username='bob', pk=9)
        run_through_middleware(views.flag_nested_in_template, request)
        event = self.recorder.call_args.args[0]
        self.assertEqual({'user_id': 9}, event.identity)

    def test_anonymous_identity_field_resolves_to_nothing(self):
        self.flag_model.objects.create(name='myflag', everyone=True)
        request = make_request()
        with override_settings(WAFFLE_EXPOSURE_IDENTITY_FIELDS=('user_id',)):
            waffle.evaluate_flag(request, 'myflag').confirm()
        waffle.flush_exposures(request)
        event = self.recorder.call_args.args[0]
        self.assertEqual({}, event.identity)


class ReadOnlyAndPrewarmTests(TestCase):
    def setUp(self):
        super().setUp()
        self.flag_model = waffle.get_waffle_flag_model()

    def test_readonly_evaluate_cannot_be_confirmed(self):
        self.flag_model.objects.create(name='myflag', everyone=True)
        decision = waffle.evaluate_flag(make_request(), 'myflag', read_only=True)
        self.assertTrue(decision)
        with self.assertRaises(ReadOnlyExposureError):
            decision.confirm()

    def test_readonly_check_and_flag_is_active_record_nothing(self):
        self.flag_model.objects.create(name='myflag', everyone=True)
        request = make_request()
        # Plain read-only checks.
        waffle.flag_is_active(request, 'myflag', read_only=True)
        waffle.evaluate_flag(request, 'myflag', read_only=True)
        # Background prewarming: load every flag without confirming.
        list(self.flag_model.get_all())
        state = get_exposure_state(request)
        if state is not None:
            # A state may exist from the read-only evaluate, but it must
            # never hold a confirmable/recordable exposure.
            self.assertEqual({}, state._pending)
            waffle.flush_exposures(request)

    def test_readonly_view_records_nothing_through_middleware(self):
        self.flag_model.objects.create(name='myflag', everyone=True)
        request = make_request()
        with override_settings(WAFFLE_EXPOSURE_RECORDER=RECORDER):
            import waffle.tests.test_exposure as mod
            recorder = mock.Mock(side_effect=fake_recorder)
            with mock.patch.object(mod, 'fake_recorder', recorder):
                response = run_through_middleware(
                    views.evaluate_readonly_view, request
                )
        self.assertEqual(b'on', response.content)
        recorder.assert_not_called()


@override_settings(WAFFLE_EXPOSURE_RECORDER=RECORDER)
class FailureResponseTests(TestCase):
    def setUp(self):
        super().setUp()
        self.recorder = mock.Mock(side_effect=fake_recorder)
        import waffle.tests.test_exposure as mod
        self._patch = mock.patch.object(mod, 'fake_recorder', self.recorder)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.flag_model = waffle.get_waffle_flag_model()
        self.flag_model.objects.create(name='myflag', everyone=True)

    def test_error_response_does_not_submit_by_default(self):
        request = make_request()
        with self.assertRaises(RuntimeError):
            run_through_middleware(views.boom_view, request)
        self.recorder.assert_not_called()

    @override_settings(WAFFLE_EXPOSURE_SUBMIT_ON_ERROR=True)
    def test_error_response_submits_when_configured(self):
        request = make_request()
        with self.assertRaises(RuntimeError):
            run_through_middleware(views.boom_view, request)
        self.assertEqual(1, self.recorder.call_count)

    def test_cancelled_request_does_not_submit_by_default(self):
        request = make_request()
        with self.assertRaises(asyncio.CancelledError):
            run_through_middleware(views.cancelled_view, request)
        self.recorder.assert_not_called()

    @override_settings(WAFFLE_EXPOSURE_SUBMIT_ON_CANCEL=True)
    def test_cancelled_request_submits_when_configured(self):
        request = make_request()
        with self.assertRaises(asyncio.CancelledError):
            run_through_middleware(views.cancelled_view, request)
        self.assertEqual(1, self.recorder.call_count)

    @override_settings(WAFFLE_EXPOSURE_SUBMIT_ON_CANCEL=True)
    async def test_async_cancelled_request_honours_configuration(self):
        from django.test import AsyncRequestFactory

        async def get_response(req):
            return await views.async_cancelled_view(req)

        request = AsyncRequestFactory().get('/foo')
        request.user = AnonymousUser()
        middleware = WaffleMiddleware(get_response)
        with self.assertRaises(asyncio.CancelledError):
            await middleware(request)
        self.assertEqual(1, self.recorder.call_count)

    def test_recorder_failure_does_not_change_decision_or_response(self):
        self.recorder.side_effect = RuntimeError('analytics down')
        request = make_request()
        # The recorder blows up, but the view response is untouched.
        response = run_through_middleware(views.evaluate_confirm_view, request)
        self.assertEqual(b'on', response.content)
        # State is still sealed despite the recorder failure: the original
        # handle cannot smuggle a late exposure into another request.
        decision = waffle.evaluate_flag(request, 'myflag')
        with self.assertRaises(ExposureStateClosed):
            decision.confirm()
        self.assertEqual(1, self.recorder.call_count)

    def test_recorder_return_value_is_ignored(self):
        self.recorder.return_value = False
        request = make_request()
        response = run_through_middleware(views.evaluate_confirm_view, request)
        # Even a "Falsey" recorder result cannot flip the feature decision.
        self.assertEqual(b'on', response.content)

    def test_recorder_failure_during_error_flush_is_swallowed(self):
        self.recorder.side_effect = RuntimeError('analytics down')
        request = make_request()
        with override_settings(WAFFLE_EXPOSURE_SUBMIT_ON_ERROR=True):
            with self.assertRaises(RuntimeError):
                run_through_middleware(views.boom_view, request)
        # The original view exception (not the recorder's) is what raised.


class RecorderConfigurationTests(TestCase):
    def test_no_recorder_configured_is_a_noop(self):
        # No WAFFLE_EXPOSURE_RECORDER at all: confirm + flush stay quiet.
        flag_model = waffle.get_waffle_flag_model()
        flag_model.objects.create(name='myflag', everyone=True)
        request = make_request()
        self.assertTrue(waffle.evaluate_flag(request, 'myflag').confirm())
        # Seals without error.
        waffle.flush_exposures(request)


@override_settings(WAFFLE_EXPOSURE_RECORDER=RECORDER)
class ExposureClientTests(TestCase):
    """End-to-end behaviour through the real middleware stack."""

    def setUp(self):
        super().setUp()
        self.recorder = mock.Mock(side_effect=fake_recorder)
        import waffle.tests.test_exposure as mod
        self._patch = mock.patch.object(mod, 'fake_recorder', self.recorder)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.client = Client()

    def test_confirmed_through_full_request_stack(self):
        waffle.get_waffle_flag_model().objects.create(name='myflag', everyone=True)
        response = self.client.get('/evaluate_confirm')
        self.assertEqual(200, response.status_code)
        self.assertEqual(b'on', response.content)
        self.assertEqual(1, self.recorder.call_count)

    def test_evaluated_but_not_confirmed_records_nothing(self):
        waffle.get_waffle_flag_model().objects.create(name='myflag', everyone=True)
        response = self.client.get('/evaluate_without_confirm')
        self.assertEqual(200, response.status_code)
        self.recorder.assert_not_called()

    def test_nested_template_records_once_through_client(self):
        waffle.get_waffle_flag_model().objects.create(name='myflag', everyone=True)
        response = self.client.get('/flag_nested_in_template')
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, self.recorder.call_count)
