.. _types-flag:

=====
Flags
=====

Flags are the most robust, flexible method of rolling out a feature with
Waffle. Flags can be used to enable a feature for specific users,
groups, users meeting certain criteria (such as being authenticated, or
superusers) or a certain percentage of visitors.


How Flags Work
==============

Flags compare the current request_ to their criteria to decide whether
they are active. Consider this simple example::

    if flag_is_active(request, 'foo'):
        pass

The :ref:`flag_is_active <usage-views>` function takes two arguments, the
request, and the name of a flag. Assuming this flag (``foo``) is defined
in the database, Waffle will make roughly the following decisions:

- Is ``WAFFLE_OVERRIDE`` active and if so does this request specify a
  value for this flag? If so, use that value.
- If not, is the flag set to globally on or off (the *Everyone*
  setting)? If so, use that value.
- If not, is the flag in *Testing* mode, and does the request specify a
  value for this flag? If so, use that value and set a testing cookie.
- If not, does the current user meet any of our criteria? If so, the
  flag is active.
- If not, does the user have an existing cookie set for this flag? If
  so, use that value.
- If not, randomly assign a value for this user based on the
  *Percentage* and set a cookie.

When :ref:`WAFFLE_STABLE_ROLLOUT <starting-configuring>` is enabled, the
last two percentage steps are replaced by a single stable step: the
configured identity of the current user is hashed with the flag name
and the flag's :ref:`salt <types-flag-stable-rollout>` into a fixed
bucket, and the flag is active when that bucket falls within the
*Percentage*. Every check above keeps its precedence; only the random
percentage decision changes.


Flag Attributes
===============

Flags can be administered through the Django `admin site`_ or the
:ref:`command line <usage-cli>`. They have the following attributes:

:name:
    The name of the flag. Will be used to identify the flag everywhere.
:everyone:
    Globally set the Flag, **overriding all other criteria**. Leave as
    *Unknown* to use other criteria.
:testing:
    Can the flag be specified via a querystring parameter? :ref:`See
    below <types-flag-testing>`.
:percent:
    A percentage of users for whom the flag will be active, if no other
    criteria applies to them.
:superusers:
    Is this flag always active for superusers?
:staff:
    Is this flag always active for staff?
:authenticated:
    Is this flag always active for authenticated users?
:languages:
    Is the ``LANGUAGE_CODE`` of the request in this list?
    (Comma-separated values.)
:groups:
    A list of group IDs for which this flag will always be active.
:users:
    A list of user IDs for which this flag will always be active.
:rollout:
    Activate Rollout mode? :ref:`See below <types-flag-rollout>`.
:salt:
    Per-flag salt for :ref:`stable rollout <types-flag-stable-rollout>`.
    Generated when the flag is created and immutable on ordinary saves;
    it only changes when you rotate it explicitly, which reassigns all
    identities to new buckets.
:note:
    Describe where the flag is used.

A Flag will be active if *any* of the criteria are true for the current
user or request (i.e. they are combined with ``or``). For example, if a
Flag is active for superusers, a specific group, and 12% of visitors,
then it will be active if the current user is a superuser *or* if they
are in the group *or* if they are in the 12%.


.. note::

    Users are assigned randomly when using Percentages, so in practice
    the actual proportion of users for whom the Flag is active will
    probably differ slightly from the Percentage value.

Flag Methods
============

The Flag class has the following public methods:

:is_active:
    Determines if the flag is active for a given request. Returns a boolean value.
:is_active_for_user:
    Determines if the flag is active for a given user. Returns a boolean value.


.. _types-flag-custom-model:

Custom Flag Models
======================

For many cases, the default Flag model provides all the necessary functionality. It allows
flagging individual Users and Groups. If you would like flags to be applied to
different things, such as companies a User belongs to, you can use a custom flag model.

The functionality uses the same concepts as Django's custom user models, and a lot of this will
be immediately recognizable.

An application needs to define a ``WAFFLE_FLAG_MODEL`` settings. The default is ``waffle.Flag``
but can be pointed to an arbitrary object.

.. note::

    It is not possible to change the Flag model and generate working migrations. Ideally, the flag
    model should be defined at the start of a new project. This is a limitation of the `swappable`
    Django magic. Please use magic responsibly.

The custom Flag model must inherit from `waffle.models.AbstractBaseFlag`. If you want the existing
``User`` and ``Group`` based flagging and would like to add more entities to it,
you may extend `waffle.models.AbstractUserFlag`.

If you use a custom flag model to apply to models beyond Users and Groups, you must run Django's
``makemigrations`` before running migrations as outlined in the :ref:`installation docs
<installation-settings-migrations>`.

If you need to reference the class that is being used as the `Flag` model in your project, use the
``get_waffle_flag_model()`` method. If you reference the Flag a lot, it may be convenient to add
``Flag = get_waffle_flag_model()`` right below your imports and reference the Flag model as if it had
been imported directly.

Example:

.. code-block:: python

    # settings.py
    WAFFLE_FLAG_MODEL = 'myapp.Flag'

    # models.py
    from waffle.models import AbstractUserFlag, CACHE_EMPTY
    from waffle.utils import get_setting, keyfmt, get_cache

    class Flag(AbstractUserFlag):
        FLAG_COMPANIES_CACHE_KEY = 'FLAG_COMPANIES_CACHE_KEY'
        FLAG_COMPANIES_CACHE_KEY_DEFAULT = 'flag:%s:companies'

        companies = models.ManyToManyField(
            Company,
            blank=True,
            help_text=_('Activate this flag for these companies.'),
        )

        def get_flush_keys(self, flush_keys=None):
            flush_keys = super(Flag, self).get_flush_keys(flush_keys)
            companies_cache_key = get_setting(Flag.FLAG_COMPANIES_CACHE_KEY, Flag.FLAG_COMPANIES_CACHE_KEY_DEFAULT)
            flush_keys.append(keyfmt(companies_cache_key, self.name))
            return flush_keys

        def is_active_for_user(self, user):
            is_active = super(Flag, self).is_active_for_user(user)
            if is_active:
                return is_active

            if getattr(user, 'company_id', None):
                company_ids = self._get_company_ids()
                if user.company_id in company_ids:
                    return True

        def _get_company_ids(self):
            cache = get_cache()
            cache_key = keyfmt(
                get_setting(Flag.FLAG_COMPANIES_CACHE_KEY, Flag.FLAG_COMPANIES_CACHE_KEY_DEFAULT),
                self.name
            )
            cached = cache.get(cache_key)
            if cached == CACHE_EMPTY:
                return set()
            if cached:
                return cached

            company_ids = set(self.companies.all().values_list('pk', flat=True))
            if not company_ids:
                cache.add(cache_key, CACHE_EMPTY)
                return set()

            cache.add(cache_key, company_ids)
            return company_ids

    # admin.py
    from waffle.admin import FlagAdmin as WaffleFlagAdmin

    class FlagAdmin(WaffleFlagAdmin):
        raw_id_fields = tuple(list(WaffleFlagAdmin.raw_id_fields) + ['companies'])
    admin.site.register(Flag, FlagAdmin)




.. _types-flag-testing:

Testing Mode
============

See :ref:`User testing with Waffle <testing-user>`.


.. _types-flag-rollout:

Rollout Mode
============

When a Flag is activated by chance, Waffle sets a cookie so the flag
will not flip back and forth on subsequent visits. This can present a
problem for gradually deploying new features: users can get "stuck" with
the Flag turned off, even as the percentage increases.

*Rollout mode* addresses this by changing the TTL of "off" cookies. When
Rollout mode is active, cookies setting the Flag to "off" are session
cookies, while those setting the Flag to "on" are still controlled by
:ref:`WAFFLE_MAX_AGE <starting-configuring>`.

Effectively, Rollout mode changes the *Percentage* from "percentage of
visitors" to "percent chance that the Flag will be activated per visit."


.. _types-flag-stable-rollout:

Stable Rollout
==============

The default percentage behavior samples independently on every request
(and on every client) until a cookie is set. That means the decision can
be re-sampled when a cookie is missing, expires, or never reaches the
server, and different devices are bucketed separately.

Set :ref:`WAFFLE_STABLE_ROLLOUT <starting-configuring>` to ``True`` to
replace the percentage step with deterministic bucketing:

#. The identity is taken from the authenticated user using
   :ref:`WAFFLE_ROLLOUT_IDENTIFIER <starting-configuring>` (the user's
   primary key by default).
#. It is hashed together with the flag name and the flag's salt
   (HMAC-SHA256) into one of
   :ref:`WAFFLE_ROLLOUT_BUCKET_COUNT <starting-configuring>` buckets.
#. The flag is active when the bucket is below the flag's *Percent*.

All earlier rules — *Everyone*, *Testing*, languages, superusers, staff,
*Authenticated*, groups and explicit users — keep their precedence.
Stable bucketing is only consulted when none of them matched.

Because the bucket is a pure function of the identity, flag name and
salt:

- The same user gets the same answer on every request and every client.
- Raising the *Percent* (e.g. 10% → 20%) is monotonic: every identity
  selected at 10% is still selected at 20%, and new identities are only
  added. This is verified against the fixed set of possible identities
(the buckets), not by sampling traffic.

Missing identity
----------------

Anonymous users have no identity. Configure the behavior with
:ref:`WAFFLE_ROLLOUT_MISSING_IDENTIFIER <starting-configuring>`:

- ``'miss'`` (default): the flag is inactive and no percent cookie is
  written.
- ``'cookie'``: anonymous visitors use the existing per-browser
  cookie/random percentage path.

Login and logout
----------------

The two bucket keys are never mixed within one request:

- An authenticated request is always bucketed by the configured
  identity. Waffle does not read the anonymous percent cookie and does
  not write one, even if such a cookie is present.
- An anonymous request (with ``'cookie'``) uses only the cookie path.

Consequently, logging in moves a visitor from their anonymous cookie
bucket to their identity bucket, and logging out moves them back to the
cookie bucket; neither value is carried over or overwritten.

Salt rotation
-------------

Buckets are keyed by the flag's salt, so the salt is the only thing that
can reassign identities. It is generated when the flag is created and
saving a flag for any ordinary reason (editing the percent, note, etc.)
never changes it, which means increasing a percentage does not reshuffle
existing users.

To deliberately re-bucket every identity — for example after a ramp-up
experiment — rotate the salt explicitly with
``Flag.rotate_salt()`` or with the *Rotate rollout salt* admin action.
Rotating the salt invalidates the stable property during a ramp-up: do
it on purpose, not as part of a routine edit.


.. _request: https://docs.djangoproject.com/en/dev/topics/http/urls/#how-django-processes-a-request
.. _admin site: https://docs.djangoproject.com/en/dev/ref/contrib/admin/


.. _types-flag-auto-create-missing:

Auto Create Missing
===================

When a flag is evaluated in code that is missing in the database the
flag returns the :ref:`WAFFLE_FLAG_DEFAULT <starting-configuring>`
value but does not create a flag in the database. If you'd like waffle
to create missing flags in the database whenever it encounters a
missing flag you can set :ref:`WAFFLE_CREATE_MISSING_FLAGS
<starting-configuring>` to ``True``. Missing flags will be created in
the database and the value of the ``Everyone`` flag attribute will be
set to :ref:`WAFFLE_FLAG_DEFAULT <starting-configuring>` in the
auto-created database record.


.. _types-flag-log-missing:

Log Missing
===================

Whether or not you enabled :ref:`Auto Create Missing Flags <types-flag-auto-create-missing>`,
it can be practical to be informed that a flag was or is missing.
If you'd like waffle to log a warning, error, ... you can set :ref:`WAFFLE_LOG_MISSING_FLAGS
<starting-configuring>` to any level known by Python default logger.
