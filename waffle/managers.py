from typing import TYPE_CHECKING, Any, Generic, TypeVar

from django.db import models


if TYPE_CHECKING:
    from waffle.models import _BaseModelType, AbstractBaseFlag, AbstractBaseSample, AbstractBaseSwitch  # noqa: F401,PLC0415
else:
    _BaseModelType = TypeVar("_BaseModelType")


class BaseManager(models.Manager, Generic[_BaseModelType]):
    def get_by_natural_key(self, name: str) -> _BaseModelType:
        return self.get(name=name)

    def create(self, *args: Any, **kwargs: Any) -> _BaseModelType:
        return super().create(*args, **kwargs)


class FlagManager(BaseManager['AbstractBaseFlag']):
    pass


class SwitchManager(BaseManager['AbstractBaseSwitch']):
    pass


class SampleManager(BaseManager['AbstractBaseSample']):
    pass
