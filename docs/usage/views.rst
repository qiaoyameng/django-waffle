.. _usage-views:

=====================
Using Waffle in views
=====================

Waffle provides simple methods to test :ref:`flags <types-flag>`,
:ref:`switches <types-switch>`, or :ref:`samples <types-sample>` in
views (or, for switches and samples, anywhere else you're writing
Python).


Flags
=====

::

    waffle.flag_is_active(request, 'flag_name')

Returns ``True`` if the flag is active for this request, else ``False``.
For example::

    import waffle

    def my_view(request):
        if waffle.flag_is_active(request, 'flag_name'):
            """Behavior if flag is active."""
        else:
            """Behavior if flag is inactive."""

Note that evaluating a flag does not mean the feature was actually
shown. To measure real experiment exposures, use
``waffle.evaluate_flag(request, 'flag_name')`` and explicitly confirm the
returned decision handle once the feature is used. See
:ref:`usage-exposures`.


Switches
========

::

    waffle.switch_is_active('switch_name')

Returns ``True`` if the switch is active, else ``False``.


Samples
=======

::

    waffle.sample_is_active('sample_name')

Returns ``True`` if the sample is active, else ``False``.

.. warning::

    See the warning in the :ref:`Sample chapter <types-sample>`.
