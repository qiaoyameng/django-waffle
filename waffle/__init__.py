from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest

from waffle.utils import get_setting
from django.apps import apps as django_apps

if TYPE_CHECKING:
    from waffle.models import AbstractBaseFlag, AbstractBaseSample, AbstractBaseSwitch
    from waffle.groups import GroupDecision

__version__ = '5.0.0'


def flag_is_active(request: HttpRequest, flag_name: str, read_only: bool = False) -> bool | None:
    flag = get_waffle_flag_model().get(flag_name)
    return flag.is_active(request, read_only=read_only)


def switch_is_active(switch_name: str) -> bool:
    switch = get_waffle_switch_model().get(switch_name)
    return switch.is_active()


def sample_is_active(sample_name: str) -> bool:
    sample = get_waffle_sample_model().get(sample_name)
    return sample.is_active()


def flag_group_result(request: HttpRequest, group_name: str,
                      identity: str | None = None,
                      read_only: bool = False) -> "GroupDecision":
    """Evaluate a mutually exclusive experiment group in one call.

    Independent ``flag_is_active`` calls for any of the group's flags use the
    very same evaluation, so callers never need to remember which flag to
    check first.
    """
    from waffle.groups import Assignment, evaluate_group
    group = get_waffle_flag_group_model().get(group_name)
    if not group.pk:
        from waffle.groups import GroupDecision
        return GroupDecision(
            group_name=group_name, reason=Assignment.MISSING_GROUP)
    return evaluate_group(request, group, identity=identity,
                          read_only=read_only)


def get_waffle_flag_model() -> type[AbstractBaseFlag]:
    return get_waffle_model('FLAG_MODEL')


def get_waffle_switch_model() -> type[AbstractBaseSwitch]:
    return get_waffle_model('SWITCH_MODEL')


def get_waffle_sample_model() -> type[AbstractBaseSample]:
    return get_waffle_model('SAMPLE_MODEL')


def get_waffle_flag_group_model():
    from waffle.models import FlagGroup
    return FlagGroup


def get_waffle_model(setting_name: str) -> (
    type[AbstractBaseFlag | AbstractBaseSwitch | AbstractBaseSample]
):
    """
    Returns the waffle Flag model that is active in this project.
    """
    default_model = {
        'FLAG_MODEL': 'waffle.Flag',
        'SWITCH_MODEL': 'waffle.Switch',
        'SAMPLE_MODEL': 'waffle.Sample',
    }

    # Add backwards compatibility by not requiring adding of model setting
    # for everyone who upgrades.  At some point it would be helpful to
    # require this to be defined explicitly, but no for now, to remove
    # pain from upgrading.
    default = default_model[setting_name]
    flag_model_name = get_setting(setting_name, default)

    try:
        return django_apps.get_model(flag_model_name)
    except ValueError:
        raise ImproperlyConfigured(f"WAFFLE_{setting_name} must be of the form 'app_label.model_name'")
    except LookupError:
        raise ImproperlyConfigured(
            f"WAFFLE_{setting_name} refers to model '{flag_model_name}' that has not been installed"
        )
