from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.db import transaction
from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from waffle import get_waffle_flag_model, transactions


def _flag_relation_field(sender: Any, flag_model: type[Any]) -> Any:
    for field in sender._meta.get_fields():
        if getattr(field.remote_field, 'model', None) is flag_model:
            return field
    return None


def _flags_for_related_instance(sender: Any, instance: Any, using: str, flag_model: type[Any]):
    field = _flag_relation_field(sender, flag_model)
    flag_ids = sender._base_manager.using(using).filter(
        **{field.name: instance}
    ).values_list(f'{field.name}_id', flat=True)
    return flag_model.objects.using(using).filter(pk__in=flag_ids)


def _flush_after_commit(flag: Any, using: str) -> Callable[[], None]:
    def flush() -> None:
        flag.flush(using=using)
    return flush


@receiver(m2m_changed)
def flag_membership_changed(sender, instance, action, using=None, **kwargs):
    flag_model = get_waffle_flag_model()
    through_models = {
        field.remote_field.through
        for field in flag_model._meta.get_fields()
        if getattr(field, 'many_to_many', False)
    }
    if sender not in through_models:
        return

    using = using or transactions.db_for_write(flag_model)

    if action == 'pre_clear' and not isinstance(instance, flag_model):
        instance._waffle_cleared_flag_ids = list(
            _flags_for_related_instance(
                sender, instance, using, flag_model
            ).values_list('pk', flat=True)
        )
        return

    if action not in ('post_add', 'post_remove', 'post_clear'):
        return

    if isinstance(instance, flag_model):
        flags = [instance]
    elif action == 'post_clear':
        flag_ids = getattr(instance, '_waffle_cleared_flag_ids', [])
        flags = flag_model.objects.using(using).filter(pk__in=flag_ids)
        instance.__dict__.pop('_waffle_cleared_flag_ids', None)
    else:
        flags = flag_model.objects.using(using).filter(pk__in=kwargs['pk_set'])

    for flag in flags:
        if transactions.is_active(using):
            flag.flush(using=using)
        else:
            transaction.on_commit(_flush_after_commit(flag, using))
