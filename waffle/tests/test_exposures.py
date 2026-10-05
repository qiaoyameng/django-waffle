from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse, HttpResponseServerError
from django.template import Context, Template
from django.template import engines
from django.test import RequestFactory, override_settings

import waffle
from waffle import exposures
from waffle.exposures import ExposureError
from waffle.middleware import WaffleMiddleware
from waffle.tests.base import TestCase


def get_response(request):
    return HttpResponse('ok')


def make_request():
    request = RequestFactory().get('/foo')
    request.user = AnonymousUser()
    return request


class ExposureLogger:
    """Stand-in analytics backend."""

    def __init__(self, raises=False):
        self.records = []
        self.raises = raises

    def __call__(self, record):
        if self.raises:
            raise RuntimeError('analytics backend is down')
        self.records.append(record)


dotted_records: list = []


def dotted_sink(record):
    dotted_records.append(record)


class EvaluateFlagTests(TestCase):
    def setUp(self):
        super().setUp()
        self.flag = waffle.get_waffle_flag_model().objects.create(
            name='myflag', everyone=True)

    def test_handle_is_truthy_and_returns_decision(self):
        request = make_request()
        decision = waffle.evaluate_flag(request, 'myflag')
        self.assertTrue(bool(decision))
        if decision:
            chosen = True
        else:
            chosen = False
        self.assertTrue(chosen)
        self.assertEqual(decision.decision, True)
        self.assertEqual(decision.flag_name, 'myflag')

    def test_handle_for_inactive_flag(self):
        self.flag.everyone = False
        self.flag.save()
        self.flag.flush()
        decision = waffle.evaluate_flag(make_request(), 'myflag')
        self.assertFalse(bool(decision))
        self.assertEqual(decision, False)

    def test_evaluate_without_confirm_records_nothing(self):
        request = make_request()
        sink = ExposureLogger()
        with override_settings(WAFFLE_EXPOSURE_LOGGER=sink):
            waffle.evaluate_flag(request, 'myflag')
            WaffleMiddleware(get_response).process_response(
                request, HttpResponse('ok'))
        self.assertEqual(sink.records, [])

    def test_plain_flag_is_active_never_exposes(self):
        request = make_request()
        sink = ExposureLogger()
        with override_settings(WAFFLE_EXPOSURE_LOGGER=sink):
            self.assertTrue(waffle.flag_is_active(request, 'myflag'))
            WaffleMiddleware(get_response).process_response(
                request, HttpResponse('ok'))
        self.assertEqual(sink.records, [])

    def test_read_only_check_does_not_expose(self):
        self.flag.everyone = None
        self.flag.percent = '99.9'
        self.flag.save()
        self.flag.flush()

        request = make_request()
        sink = ExposureLogger()
        with override_settings(WAFFLE_EXPOSURE_LOGGER=sink):
            waffle.flag_is_active(request, 'myflag', read_only=True)
            waffle.evaluate_flag(request, 'myflag', read_only=True)
            WaffleMiddleware(get_response).process_response(
                request, HttpResponse('ok'))
        self.assertEqual(sink.records, [])
        self.assertFalse(hasattr(request, exposures.PENDING_ATTR))

    def test_background_warmup_does_not_expose(self):
        request = make_request()
        # Prefetching / warming every flag is a background concern, not an
        # impression shown to a user.
        for flag in waffle.get_waffle_flag_model().get_all():
            flag.is_active(request, read_only=True)
        self.assertFalse(hasattr(request, exposures.PENDING_ATTR))


class ConfirmExposureTests(TestCase):
    def setUp(self):
        super().setUp()
        waffle.get_waffle_flag_model().objects.create(name='myflag', everyone=True)

    def test_confirm_then_submitted_on_response(self):
        request = make_request()
        sink = ExposureLogger()
        with override_settings(WAFFLE_EXPOSURE_LOGGER=sink):
            decision = waffle.evaluate_flag(request, 'myflag')
            self.assertTrue(decision.confirm())
            WaffleMiddleware(get_response).process_response(
                request, HttpResponse('ok'))

        self.assertEqual(len(sink.records), 1)
        record = sink.records[0]
        self.assertEqual(record.flag_name, 'myflag')
        self.assertIs(record.decision, True)

    def test_repeated_confirmation_is_deduplicated(self):
        request = make_request()
        sink = ExposureLogger()
        with override_settings(WAFFLE_EXPOSURE_LOGGER=sink):
            decision = waffle.evaluate_flag(request, 'myflag')
            self.assertTrue(decision.confirm())
            self.assertFalse(decision.confirm())
            self.assertFalse(decision.confirm())
            # Module-level confirmation for the same decision also dedupes.
            self.assertFalse(waffle.confirm_exposure(
                request, 'myflag', True, request_id=decision.request_id))
            WaffleMiddleware(get_response).process_response(
                request, HttpResponse('ok'))
        self.assertEqual(len(sink.records), 1)

    def test_handle_cannot_be_confirmed_against_another_request(self):
        origin = make_request()
        other = make_request()
        decision = waffle.evaluate_flag(origin, 'myflag')
        with self.assertRaises(ExposureError):
            waffle.confirm_exposure(
                other, 'myflag', True, request_id=decision.request_id)

    def test_handle_is_always_staged_on_originating_request(self):
        origin = make_request()
        other = make_request()
        decision = waffle.evaluate_flag(origin, 'myflag')
        # Even if the handle object leaks into work done for another
        # request, the exposure is bound to the originating request.
        decision.confirm()
        self.assertTrue(hasattr(origin, exposures.PENDING_ATTR))
        self.assertFalse(hasattr(other, exposures.PENDING_ATTR))

    def test_explicit_identity_contains_only_approved_fields(self):
        request = make_request()
        request.secret_header = 'do-not-copy'
        sink = ExposureLogger()
        with override_settings(WAFFLE_EXPOSURE_LOGGER=sink):
            decision = waffle.evaluate_flag(request, 'myflag')
            decision.confirm(identity={'user_id': 42, 'bucket': 'test'})
            WaffleMiddleware(get_response).process_response(
                request, HttpResponse('ok'))

        data = sink.records[0].as_dict()
        self.assertEqual(set(data.keys()), {'flag_name', 'decision', 'identity'})
        self.assertEqual(data['identity'], {'user_id': 42, 'bucket': 'test'})
        self.assertNotIn('request', data)
        self.assertNotIn('user', data)
        self.assertNotIn('secret_header', data['identity'])

    def test_identity_allowlist_copies_only_named_user_attributes(self):
        user = get_user_model().objects.create_user(
            username='alice', password='super-secret')
        request = make_request()
        request.user = user
        sink = ExposureLogger()
        settings = override_settings(
            WAFFLE_EXPOSURE_LOGGER=sink,
            WAFFLE_EXPOSURE_IDENTITY_ATTRIBUTES=('id', 'username'),
        )
        with settings:
            decision = waffle.evaluate_flag(request, 'myflag')
            decision.confirm()
            WaffleMiddleware(get_response).process_response(
                request, HttpResponse('ok'))

        identity = sink.records[0].identity
        self.assertEqual(identity, {'id': user.id, 'username': 'alice'})
        self.assertNotIn('password', identity)
        self.assertNotIn('request', identity)

    def test_no_identity_by_default(self):
        user = get_user_model().objects.create_user(
            username='bob', password='super-secret')
        request = make_request()
        request.user = user
        sink = ExposureLogger()
        with override_settings(WAFFLE_EXPOSURE_LOGGER=sink):
            waffle.evaluate_flag(request, 'myflag').confirm()
            WaffleMiddleware(get_response).process_response(
                request, HttpResponse('ok'))
        self.assertEqual(dict(sink.records[0].identity), {})


class FailingCallbackTests(TestCase):
    def setUp(self):
        super().setUp()
        waffle.get_waffle_flag_model().objects.create(name='myflag', everyone=True)

    def test_callback_failure_does_not_change_decision_or_response(self):
        request = make_request()
        sink = ExposureLogger(raises=True)
        with override_settings(WAFFLE_EXPOSURE_LOGGER=sink):
            decision = waffle.evaluate_flag(request, 'myflag')
            self.assertTrue(decision.decision)
            decision.confirm(identity={'user_id': 7})
            with self.assertLogs('waffle', level='ERROR'):
                response = WaffleMiddleware(get_response).process_response(
                    request, HttpResponse('feature-content'))
        # Feature decision and rendered response are untouched.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'feature-content')
        self.assertTrue(waffle.flag_is_active(make_request(), 'myflag'))


class FailedResponseTests(TestCase):
    def setUp(self):
        super().setUp()
        waffle.get_waffle_flag_model().objects.create(name='myflag', everyone=True)

    def _confirmed_request(self):
        request = make_request()
        decision = waffle.evaluate_flag(request, 'myflag')
        decision.confirm(identity={'user_id': 1})
        return request

    def test_error_response_discards_by_default(self):
        sink = ExposureLogger()
        request = self._confirmed_request()
        with override_settings(WAFFLE_EXPOSURE_LOGGER=sink):
            WaffleMiddleware(get_response).process_response(
                request, HttpResponseServerError('boom'))
        self.assertEqual(sink.records, [])

    def test_error_response_submits_when_configured(self):
        sink = ExposureLogger()
        request = self._confirmed_request()
        with override_settings(
            WAFFLE_EXPOSURE_LOGGER=sink,
            WAFFLE_LOG_EXPOSURES_ON_ERROR_RESPONSE=True,
        ):
            WaffleMiddleware(get_response).process_response(
                request, HttpResponseServerError('boom'))
        self.assertEqual(len(sink.records), 1)

    def test_exception_discards_by_default(self):
        sink = ExposureLogger()
        request = self._confirmed_request()
        middleware = WaffleMiddleware(get_response)
        with override_settings(WAFFLE_EXPOSURE_LOGGER=sink):
            middleware.process_exception(request, RuntimeError('cancelled'))
            # A later response phase must not submit the taken records.
            middleware.process_response(request, HttpResponse('ok'))
        self.assertEqual(sink.records, [])

    def test_exception_submits_when_configured(self):
        sink = ExposureLogger()
        request = self._confirmed_request()
        middleware = WaffleMiddleware(get_response)
        with override_settings(
            WAFFLE_EXPOSURE_LOGGER=sink,
            WAFFLE_LOG_EXPOSURES_ON_EXCEPTION=True,
        ):
            middleware.process_exception(request, RuntimeError('cancelled'))
        self.assertEqual(len(sink.records), 1)
        self.assertIs(sink.records[0].decision, True)


class NestedTemplateTests(TestCase):
    NESTED = Template(
        '{% load waffle_tags %}'
        '{% flag "myflag" %}'
        'outer-on '
        '{% flag "myflag" %}inner-on{% endflag %}'
        '{% endflag %}'
    )

    def _render_through_middleware(self, request, template):
        content = template.render(Context({'request': request}))
        response = HttpResponse(content)
        return WaffleMiddleware(get_response).process_response(request, response)

    def test_nested_flag_usage_confirms_once(self):
        waffle.get_waffle_flag_model().objects.create(name='myflag', everyone=True)
        request = make_request()
        sink = ExposureLogger()
        with override_settings(WAFFLE_EXPOSURE_LOGGER=sink):
            response = self._render_through_middleware(request, self.NESTED)
        self.assertContains(response, 'outer-on')
        self.assertContains(response, 'inner-on')
        self.assertEqual(len(sink.records), 1)

    def test_inactive_branch_records_inactive_exposure_once(self):
        waffle.get_waffle_flag_model().objects.create(name='myflag', everyone=False)
        template = Template(
            '{% load waffle_tags %}'
            '{% flag "myflag" %}on{% else %}off{% endflag %}'
            '{% flag "myflag" %}on{% else %}off{% endflag %}'
        )
        request = make_request()
        sink = ExposureLogger()
        with override_settings(WAFFLE_EXPOSURE_LOGGER=sink):
            response = self._render_through_middleware(request, template)
        self.assertContains(response, 'off')
        self.assertEqual(len(sink.records), 1)
        self.assertIs(sink.records[0].decision, False)

    def test_template_without_request_keeps_working(self):
        waffle.get_waffle_flag_model().objects.create(name='myflag', everyone=True)
        template = Template(
            '{% load waffle_tags %}'
            '{% flag "myflag" %}on{% else %}off{% endflag %}'
        )
        # No request in context (e.g. offline emails): falls back to plain
        # evaluation and never touches exposure state.
        content = template.render(Context({}))
        self.assertEqual(content.strip(), 'on')

    def test_render_without_logger_configured(self):
        waffle.get_waffle_flag_model().objects.create(name='myflag', everyone=True)
        request = make_request()
        response = self._render_through_middleware(request, self.NESTED)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(hasattr(request, exposures.PENDING_ATTR))


class DottedPathLoggerTests(TestCase):
    def setUp(self):
        super().setUp()
        waffle.get_waffle_flag_model().objects.create(name='myflag', everyone=True)

    def test_logger_imported_from_dotted_path(self):
        dotted_records.clear()
        request = make_request()
        with override_settings(
            WAFFLE_EXPOSURE_LOGGER='waffle.tests.test_exposures.dotted_sink',
        ):
            waffle.evaluate_flag(request, 'myflag').confirm()
            WaffleMiddleware(get_response).process_response(
                request, HttpResponse('ok'))
        self.assertEqual(len(dotted_records), 1)


class JinjaExposureTests(TestCase):
    @skipUnless(settings.JINJA_INSTALLED, 'django-jinja is not installed')
    def test_nested_jinja_flag_confirms_once(self):
        waffle.get_waffle_flag_model().objects.create(name='myflag', everyone=True)
        request = make_request()
        sink = ExposureLogger()
        template = engines['backend'].from_string(
            "{% if waffle.flag('myflag') %}"
            "outer "
            "{% if waffle.flag('myflag') %}inner{% endif %}"
            "{% endif %}"
        )
        with override_settings(WAFFLE_EXPOSURE_LOGGER=sink):
            content = template.render({'request': request})
            WaffleMiddleware(get_response).process_response(
                request, HttpResponse(content))
        self.assertIn('outer', content)
        self.assertIn('inner', content)
        self.assertEqual(len(sink.records), 1)
