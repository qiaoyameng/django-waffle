import asyncio

from asgiref.sync import (
    iscoroutinefunction,
    markcoroutinefunction,
    sync_to_async,
)
from django.http import HttpRequest, HttpResponse
from django.utils.deprecation import MiddlewareMixin
from django.utils.encoding import smart_str

from waffle.exposure import get_exposure_state
from waffle.utils import get_setting


def _set_waffle_cookies(request: HttpRequest, response: HttpResponse) -> HttpResponse:
    secure = get_setting('SECURE')
    max_age = get_setting('MAX_AGE')

    if hasattr(request, 'waffles'):
        for k in request.waffles:
            name = smart_str(get_setting('COOKIE') % k)
            active, rollout = request.waffles[k]
            if rollout and not active:
                # "Inactive" is a session cookie during rollout mode.
                age = None
            else:
                age = max_age
            response.set_cookie(name, value=active, max_age=age,
                                secure=secure)
    if hasattr(request, 'waffle_tests'):
        for k in request.waffle_tests:
            name = smart_str(get_setting('TEST_COOKIE') % k)
            value = request.waffle_tests[k]
            response.set_cookie(name, value=value)

    return response


class WaffleMiddleware(MiddlewareMixin):
    def __init__(self, get_response):
        super().__init__(get_response)
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    def _flush_exposures(self, request: HttpRequest, *, submit: bool) -> None:
        state = get_exposure_state(request)
        if state is not None and not state.closed:
            state.flush(submit=submit)

    def process_response(self, request: HttpRequest, response: HttpResponse) -> HttpResponse:
        # Successful response: always offer confirmed exposures to the
        # recorder; flush() itself de-duplicates and swallows recorder
        # failures so the response/decision are never affected.
        self._flush_exposures(request, submit=True)
        return _set_waffle_cookies(request, response)

    def _process_exception(self, request: HttpRequest, exc: BaseException) -> None:
        if isinstance(exc, asyncio.CancelledError):
            submit = get_setting('EXPOSURE_SUBMIT_ON_CANCEL')
        else:
            submit = get_setting('EXPOSURE_SUBMIT_ON_ERROR')
        self._flush_exposures(request, submit=submit)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if self.async_mode:
            return self.__acall__(request)
        try:
            response = self.get_response(request)
        except BaseException as exc:
            self._process_exception(request, exc)
            raise
        return self.process_response(request, response)

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        try:
            response = await self.get_response(request)
        except BaseException as exc:
            # Flush directly: wrapping in sync_to_async would deadlock when
            # the task is being torn down by a CancelledError.
            self._process_exception(request, exc)
            raise
        return await sync_to_async(
            self.process_response, thread_sensitive=True
        )(request, response)
