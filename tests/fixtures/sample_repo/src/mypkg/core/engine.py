from ..models import Base
from .helpers import retry
from ...outside import thing


class Service(Base):
    @retry
    def run(self, n: int) -> int:
        def helper(x):
            return x * 2

        return helper(n)


async def fetch(url: str) -> bytes:
    import json

    return json.dumps(url).encode()
