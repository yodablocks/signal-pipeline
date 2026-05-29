"""
sources/base.py — SignalSource ABC.

Every source implements this. The pipeline never calls source internals directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from signal_pipeline.schema import SignalEvent


class SignalSource(ABC):
    """
    Abstract base for all signal sources.

    Implementors must:
    - declare SOURCE_NAME and SOURCE_TYPE as class attributes
    - declare TRUST_TIER as 1, 2, or 3
    - implement fetch() returning a list of SignalEvent
    """

    SOURCE_NAME: str = ""
    SOURCE_TYPE: str = ""
    TRUST_TIER: int = 2

    @abstractmethod
    async def fetch(self, asset: str) -> list[SignalEvent]:
        """
        Fetch signals for the given asset.
        Must never raise — absorb errors and return [] with a log entry.
        """
        ...

    def supported_assets(self) -> list[str] | None:
        """
        Return a list of supported assets, or None if the source supports all.
        Used by the pipeline to skip fetch() for unsupported assets.
        """
        return None
