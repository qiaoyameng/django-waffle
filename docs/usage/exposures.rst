.. _usage-exposures:

============================
Confirming flag exposures
============================

Calling ``waffle.flag_is_active(request, name)`` computes a decision, but
it does **not** prove the feature was ever shown to a user: a view may
check a flag and then return early, render a different template, or error
out before the feature is used. Counting evaluations therefore inflates
experiment numbers.

To measure real exposures, Waffle provides an explicit two-step API:
evaluate the flag to get a *decision handle*, then confirm the handle
once the feature is actually used.

Evaluating and confirming
=========================

::

    import waffle

    def my_view(request):
        decision = waffle.evaluate_flag(request, 'new_onboarding')
        if decision:
            # The new experience is actually being rendered now.
            decision.confirm(user_id=request.user.pk)
            ...

``evaluate_flag`` returns a :class:`~waffle.exposure.FlagDecision`. It
behaves as the final boolean decision, but holds off on recording
anything until ``confirm()`` is called. Evaluating without confirming
never records an exposure.

Handles are bound to the originating request, the flag name and the
final decision:

* A handle cannot be confirmed from a different request; doing so after
  the original request has finished raises
  :class:`~waffle.exposure.ExposureStateClosed`.
* Confirming the same flag more than once in a request is
  de-duplicated -- only the first ``confirm()`` records, and repeated
  calls return ``False``.

Templates
=========

The Django ``{% flag %}`` template tag confirms the exposure
automatically when the block is rendered (that is, when the decision is
actually used on the page). Repeated usage of one flag in a request,
including through ``{% include %}`` and other nested templates, is
recorded once.

Read-only checks and prewarming
===============================

Read-only checks and background prewarming never produce exposures::

    waffle.flag_is_active(request, name, read_only=True)
    decision = waffle.evaluate_flag(request, name, read_only=True)
    decision.confirm()  # raises ReadOnlyExposureError

Failed and cancelled responses
==============================

Confirmed exposures are buffered for the request and submitted by
``WaffleMiddleware`` once the response is known. What happens when the
view fails is controlled by settings:

* ``WAFFLE_EXPOSURE_SUBMIT_ON_ERROR`` (default ``False``): submit when a
  regular exception propagates.
* ``WAFFLE_EXPOSURE_SUBMIT_ON_CANCEL`` (default ``False``): submit when
  the request is cancelled (e.g. the client disconnected).

On a normal, successful response confirmed exposures are always
submitted.

Recorder failures are logged and swallowed: a broken analytics backend
can never change or overturn a flag decision.

Privacy: approved identity fields
=================================

Exposure events contain the flag name, the boolean decision and a small
``identity`` mapping. Waffle never copies the ``request`` or ``user``
object onto an event.

Only fields explicitly approved via
``WAFFLE_EXPOSURE_IDENTITY_FIELDS`` can ever be recorded. Passing any
other keyword to ``confirm()`` raises ``ValueError``::

    # settings.py
    WAFFLE_EXPOSURE_IDENTITY_FIELDS = ('user_id', 'session_key')

    decision.confirm(user_id=request.user.pk)  # allowed
    decision.confirm(email=request.user.email)  # ValueError

When ``confirm()`` is called with no arguments (as the template tag
does), approved fields are pulled off the request using the resolvers in
``WAFFLE_EXPOSURE_IDENTITY_RESOLVERS``. Defaults resolve ``user_id`` from
``request.user.pk`` (authenticated users only) and ``session_key`` from
``request.session``.

The recorder
============

Set ``WAFFLE_EXPOSURE_RECORDER`` to a dotted path (or callable) that
accepts a single :class:`~waffle.exposure.ExposureEvent`::

    # analytics/recording.py
    def record_flag_exposure(event):
        analytics.track(
            'flag_exposure',
            flag=event.flag_name,
            active=event.active,
            identity=event.identity,
        )

::

    WAFFLE_EXPOSURE_RECORDER = 'analytics.recording.record_flag_exposure'

Outside of a request/response cycle (for example rendering a template to
a string in a management command), call
``waffle.flush_exposures(request)`` explicitly to submit buffered
exposures.
