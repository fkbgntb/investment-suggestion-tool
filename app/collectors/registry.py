"""Explicit adapter-name registry; source configuration never imports executable code."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import TypeAdapter

from app.domain.base import Identifier

_IDENTIFIER = TypeAdapter(Identifier)


class AdapterNotRegistered(ValueError):
    """A source references an adapter not approved by application code."""


class AdapterRegistry:
    def __init__(self, adapter_names: Iterable[str] = ()) -> None:
        self._names: set[str] = set()
        for name in adapter_names:
            self.register(name)

    def register(self, adapter_name: str) -> None:
        validated = _IDENTIFIER.validate_python(adapter_name)
        if validated in self._names:
            raise ValueError(f"adapter is already registered: {validated}")
        self._names.add(validated)

    def require(self, adapter_name: str) -> None:
        if adapter_name not in self._names:
            raise AdapterNotRegistered(f"adapter is not registered: {adapter_name}")

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._names))


def build_default_adapter_registry() -> AdapterRegistry:
    # Names are a code-owned whitelist; source rows cannot import arbitrary adapters.
    return AdapterRegistry(("alpha-vantage-news", "gdelt-doc", "mock-rss", "sec-submissions"))
