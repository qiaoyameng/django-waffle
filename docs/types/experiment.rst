.. _types-experiment:

=================
Experiment Groups
=================

An *Experiment Group* makes a set of flags mutually exclusive, which is
what you want when one page has several experiment variants (A/B/n)
that must never be active at the same time.

All flags in a group share a single bucket space of 1000 buckets (0.1%
granularity). Each flag's *percent* defines a contiguous,
non-overlapping interval of that space, allocated in flag-name order.
For example, with ``a_flag`` at 30% and ``b_flag`` at 20%::

    buckets    0 - 299   -> a_flag
    buckets  300 - 499   -> b_flag
    buckets  500 - 999   -> not in the experiment

The sum of all flag percents in a group may not exceed 100; the
remainder of the bucket space is the part of your audience that does
not take part in the experiment. Both the group and the individual
flags are validated against this rule.


How Assignment Works
====================

Each visitor is mapped to a bucket by a stable hash of the group name
and their identity (the user ID for authenticated users, otherwise
the session key). Because the bucket only depends on the identity:

- Assignments are stable across requests without any cookies.
- Changing a flag's percent only re-assigns the buckets between the
  old and the new interval boundaries. Every other visitor keeps their
  previous assignment, so the impact of a ratio change is exactly the
  moved boundary range - nothing gets reshuffled.

Use ``waffle.experiment_result(request, group_name)`` to get the name
of the flag a request is assigned to (or ``None``). Calling
``flag_is_active()`` for each flag in the group always agrees with
that result, no matter which flag you check first - at most one
grouped flag is ever active for a request.


Priority and Conflicts
======================

For grouped flags, the decision order is:

- ``WAFFLE_OVERRIDE`` querystring values (per flag, debugging only).
- Direct targeting: *Everyone*, *Testing* overrides, *Languages*, and
  the user criteria (users, groups, authenticated, staff, superusers).
  These are evaluated for **all** flags in the group together.
- Bucket assignment as described above.

If direct targeting forces more than one flag of the same group active
for one request, the winner is decided by
``WAFFLE_EXPERIMENT_CONFLICT``:

:``priority`` (default):
    The flag that sorts first by name wins - a fixed, documented
    priority, never whichever row the database returns first.
:``error``:
    Raise ``waffle.models.ExperimentConflictError`` so the conflicting
    configuration is rejected loudly.

Flags forced *off* (``everyone = False``, or a Testing override of
``0``) are excluded from the assignment, but their interval stays in
place so the other flags' buckets do not move.
