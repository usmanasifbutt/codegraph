"""Data models."""
from pydantic import BaseModel


class Base:
    """Root of the model hierarchy."""

    def save(self) -> None:
        pass


class Config(BaseModel):
    debug: bool = False
