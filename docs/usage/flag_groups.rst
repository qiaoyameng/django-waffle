.. _usage-flag-groups:

======================
Mutually exclusive A/B groups
======================

A page with two experiment variants (``variant_a`` and ``variant_b``)
must never show both. A **flag group** places its member flags in a single
shared bucket space of 10,000 buckets (0-100%). Each member owns a
non-overlapping percentage interval; buckets beyond the configured
percentages simply do not take part in the experiment.

Setup
=====

Create a group and assign flags with percentages. The total must not
exceed 100%; a flag can belong to at most one group.

::

    from waffle.models import Flag, FlagGroup, FlagGroupMembership

    group = FlagGroup.objects.create(name='homepage_hero')
    a = Flag.objects.create(name='hero_variant_a')
    b = Flag.objects.create(name='hero_variant_b')
    FlagGroupMembership.objects.create(group=group, flag=a, percent='40')
    FlagGroupMembership.objects.create(group=group, flag=b, percent='30')

Here ``hero_variant_a`` covers buckets 0-4000, ``hero_variant_b``
4000-7000, and the remaining 30% of identities see neither variant.

Evaluating
==========

Call :func:`waffle.flag_group_result` once for the whole group, or keep
calling :func:`waffle.flag_is_active` for individual flags. Both paths
use the exact same evaluation, so the answer never depends on which flag
you check first::

    import waffle

    decision = waffle.flag_group_result(request, 'homepage_hero')
    if decision:
        render_template(decision.flag_name)

    # These always agree with the group result above, in either order:
    waffle.flag_is_active(request, 'hero_variant_a')
    waffle.flag_is_active(request, 'hero_variant_b')

``GroupDecision`` exposes ``flag_name``, ``active``, ``bucket``,
``identity``, ``interval``, ``reason`` and ``forced_flags`` so
allocations are explainable. An explicit identity can be passed for
deterministic evaluation::

    waffle.flag_group_result(request, 'homepage_hero', identity='user:42')

Targeting, testing, and conflicts
=================================

User/group targeting, *Everyone*, *Testing* overrides, language, staff
and superuser rules all take precedence over the bucket. Bucketing is
only used when no variant is forced on.

If several variants are simultaneously forced on for one request, the
group's conflict policy decides (it is never based on the first database
row):

- ``reject`` (default): no variant wins, every member flag is inactive.
- ``priority``: the variant with the lowest ``priority`` number wins;
  ties are broken by flag name.

A flag forced *off* (e.g. *Everyone: No*) never wins, even for an
identity whose bucket falls inside its interval. The decision carries
reason ``forced_off``.

Changing percentages
====================

Intervals are laid out in the fixed ``(priority, flag name)`` order, so
growing a variant only consumes buckets from the trailing empty space;
shrinking a variant moves its tail buckets back to "not in experiment"
rather than reassigning them to another variant. Existing buckets keep
their identity hash, so the effect of each change is fully explainable
from the reported intervals.
