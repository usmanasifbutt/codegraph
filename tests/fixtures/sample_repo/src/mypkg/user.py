from typing import TYPE_CHECKING

import requests

from mypkg.models import Base as B

from . import models as m

if TYPE_CHECKING:
    from mypkg import models


class User(B):
    """A user account."""

    @property
    def value(self) -> int:
        return self._value

    @value.setter
    def value(self, new: int) -> None:
        self._value = new


class Admin(m.Base):
    pass


def fetch_user(user_id: int, *, timeout: float = 1.0) -> "User":
    return requests.get(f"/users/{user_id}", timeout=timeout).json()
