"""
sources/social.py — Social signal source stub (trust_tier=3).

Interface is fully defined. Implementation deferred — Twitter/X API v2
bearer token required, and KOL scoring against Polymarket is a separate
pipeline step (see ranking.py for the credibility multiplier).

This stub returns [] with a log warning so the pipeline degrades
gracefully rather than raising on import or fetch.

When implemented:
  - Ingest KOL signal (tweet/post)
  - Extract the claim (asset + direction + timeframe)
  - Find best matching Polymarket market (PolymarketSource)
  - Compute implied prob delta (KOL claim vs market price)
  - Assign kol_credibility_score (rolling weighted accuracy over N prior calls)
  - Map to SignalEvent(signal_type=SignalType.KOL_CALL, trust_tier=3)
"""

from __future__ import annotations

import logging

from signal_pipeline.schema import SignalEvent
from signal_pipeline.sources.base import SignalSource

log = logging.getLogger(__name__)


class SocialSource(SignalSource):
    """
    Stub — returns [] until Twitter/X API integration is implemented.

    trust_tier=3: social signals are fully adversarial. They never outrank
    tier 1 or tier 2 signals of similar magnitude (enforced by ranking.py).
    """

    SOURCE_NAME = "social"
    SOURCE_TYPE = "social"
    TRUST_TIER = 3

    async def fetch(self, asset: str) -> list[SignalEvent]:
        log.warning(
            "SocialSource is a stub. Twitter/X API v2 bearer token required. "
            "Set TWITTER_BEARER_TOKEN env var and implement fetch(). Returning []."
        )
        return []
