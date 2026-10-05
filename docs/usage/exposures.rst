.. _usage-exposures:

==========================
Confirming flag exposures
==========================

Calling :func:`flag_is_active` only tells you what the decision for a
request was. It does **not** mean the feature was actually shown: a view
may evaluate a flag and then not render it, a read-only check may be made
for analytics, and flags may be warmed up in the background. Counting
those evaluations as exposures skews experiment statistics.

Waffle therefore separates *evaluation* from *exposure*. An evaluation
returns a confirmable decision handle, and the application registers the
exposure only once the feature is actually used.


Evaluating and confirming
=========================

::

    decision = waffle.evaluate_flag(request, 'flag_name')
    if decision:
        decision.confirm(identity={'user_id': request.user.id})
        # ... run the new feature ...

The handle works in a boolean context (``if decision:``) and exposes the
final value as ``decision.decision``. Confirmation can also be done
through the module-level function::

    waffle.confirm_exposure(
        request, 'flag_name', bool(decision),
        identity={'user_id': request.user.id},
        request_id=decision.request_id,
    )

The handle is bound to the request, the flag and the evaluated decision.
It cannot be confirmed against a different request, and confirming the
same decision more than once within a request is a no-op (the exposure
is deduplicated).

Confirmed exposures are buffered on the request and handed to the
configured logger by :class:`~waffle.middleware.WaffleMiddleware` once
the response is produced, so the logger is never called while the
feature decision is being made.


Template usage
==============

Rendering a ``{% flag %}`` block (or ``waffle.flag(...)`` in Jinja2) is
actual usage, so the template tags confirm the exposure automatically for
whichever branch is rendered. Nested or repeated ``{% flag %}`` blocks
for the same flag confirm only once. Offline templates rendered without a
request in the context keep working but never record an exposure.


What is not an exposure
=======================

- Plain ``waffle.flag_is_active(request, name)`` calls.
- Read-only checks, ``flag_is_active(..., read_only=True)``.
- Background warm-up, e.g. evaluating every flag to pre-populate caches.

Use :func:`~waffle.evaluate_flag` only where a confirmation can follow.


Failed and cancelled responses
==============================

By default a confirmed exposure is submitted only when the response
succeeds (status code below 400). If the view raises an exception or the
request is cancelled before a response, the buffered exposures are
discarded as well, because a feature shown on a failed page may never
have reached the user.

Configure submission explicitly if your analytics should count these:

- ``WAFFLE_LOG_EXPOSURES_ON_ERROR_RESPONSE = True`` submits exposures
  even for responses with a status code of 400 or higher.
- ``WAFFLE_LOG_EXPOSURES_ON_EXCEPTION = True`` submits exposures when the
  view raised (or the request was cancelled).

The exposure callback is isolated: any exception it raises is caught and
logged by Waffle. A failing callback can never change the feature
decision or alter the HTTP response.


Identity fields
===============

The record passed to the logger contains only:

- ``flag_name`` — the name of the flag,
- ``decision`` — the final boolean decision,
- ``identity`` — a mapping of explicitly approved identity fields.

Waffle never copies the whole ``request`` or ``user`` object. Pass the
exact fields your analytics backend needs when confirming::

    decision.confirm(identity={'user_id': request.user.id, 'org': org_id})

Alternatively, let Waffle read a fixed allowlist of user attributes by
setting ``WAFFLE_EXPOSURE_IDENTITY_ATTRIBUTES``::

    WAFFLE_EXPOSURE_IDENTITY_ATTRIBUTES = ('id', 'email')

Attributes that do not exist on the user are skipped.


Configuring the logger
======================

Set ``WAFFLE_EXPOSURE_LOGGER`` to a callable or a dotted import path. The
callable receives one
``waffle.exposures.ExposureRecord`` per confirmed exposure::

    def log_exposure(record):
        analytics.track(
            event='flag_exposure',
            user_id=record.identity.get('user_id'),
            properties={
                'flag': record.flag_name,
                'decision': record.decision,
            },
        )

    WAFFLE_EXPOSURE_LOGGER = log_exposure

When the setting is unset (the default), confirming an exposure is a
harmless no-op, so application code can ship confirmations before the
analytics backend is wired up.
