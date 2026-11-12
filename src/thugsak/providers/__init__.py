"""Game-data providers. See `base` for the interface and why it exists."""

from .base import (  # noqa: F401
    GameDataProvider,
    ProviderUnavailable,
    available,
    get,
    providers_for,
    register,
)

__all__ = [
    "GameDataProvider",
    "ProviderUnavailable",
    "available",
    "get",
    "providers_for",
    "register",
]
