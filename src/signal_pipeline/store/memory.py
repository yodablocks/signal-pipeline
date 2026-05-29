"""
store/memory.py — In-memory SignalStore implementation.

Suitable for CLI use and testing. Not persistent across process restarts.
Replace with a ClickHouse or TimescaleDB implementation for production.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone

from signal_pipeline.schema import SignalEvent
from signal_pipeline.store.base import SignalStore


class MemoryStore(SignalStore):

    def __init__(self) -> None:
        self._store: dict[str, list[SignalEvent]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def save(self, events: list[SignalEvent]) -> None:
        async with self._lock:
            for event in events:
                self._store[event.asset].append(event)

    async def get(
        self,
        asset: str,
        signal_types: list[str] | None = None,
        max_age_seconds: float | None = None,
        include_invalid: bool = False,
    ) -> list[SignalEvent]:
        async with self._lock:
            events = list(self._store.get(asset, []))

        if not include_invalid:
            events = [e for e in events if e.is_valid]
        if signal_types:
            events = [e for e in events if e.signal_type in signal_types]
        if max_age_seconds is not None:
            events = [e for e in events if e.age_seconds() <= max_age_seconds]

        return events

    async def clear(self, asset: str | None = None) -> None:
        async with self._lock:
            if asset:
                self._store.pop(asset, None)
            else:
                self._store.clear()

    def size(self, asset: str | None = None) -> int:
        if asset:
            return len(self._store.get(asset, []))
        return sum(len(v) for v in self._store.values())
