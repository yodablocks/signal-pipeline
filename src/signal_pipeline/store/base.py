"""
store/base.py — SignalStore ABC (ClickHouse-ready interface).

Define the interface correctly now. Swapping memory.py for a
ClickHouse or TimescaleDB implementation is one file change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from signal_pipeline.schema import SignalEvent


class SignalStore(ABC):

    @abstractmethod
    async def save(self, events: list[SignalEvent]) -> None:
        """Persist a batch of SignalEvents."""
        ...

    @abstractmethod
    async def get(
        self,
        asset: str,
        signal_types: list[str] | None = None,
        max_age_seconds: float | None = None,
        include_invalid: bool = False,
    ) -> list[SignalEvent]:
        """
        Retrieve signals for an asset.
        Filters by signal_type and age if provided.
        Excludes invalid signals by default.
        """
        ...

    @abstractmethod
    async def clear(self, asset: str | None = None) -> None:
        """Clear signals. If asset is None, clear all."""
        ...
