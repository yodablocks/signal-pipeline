"""
cli.py — Entry point: signal-pipeline --asset BTC --top 10 [--position long:50000] [--explain]

Runs the full pipeline:
  1. Fetch from all enabled sources concurrently
  2. Validate
  3. Store
  4. Rank
  5. Score (model layer — directional verdict)
  6. Assemble agent-ready payload
  7. Print to stdout (or write to file)

Flags:
  --explain   Print a human-readable breakdown of the model's scoring to stderr.
              Shows each signal's vote, weight, and contribution to the verdict.
              Does not affect the JSON payload on stdout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

import click
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
log = logging.getLogger(__name__)


def _default_sources():
    from signal_pipeline.sources.deribit import DeribitSource
    from signal_pipeline.sources.liquidation import LiquidationSource
    from signal_pipeline.sources.dune import DuneSource
    from signal_pipeline.sources.perp import PerpSource
    from signal_pipeline.sources.polymarket import PolymarketSource
    from signal_pipeline.sources.social import SocialSource
    return [DeribitSource(), LiquidationSource(), PerpSource(), DuneSource(), PolymarketSource(), SocialSource()]


def _parse_position(position_str: str | None) -> dict[str, Any]:
    """Parse 'long:50000' or 'short:25000' into position context dict."""
    if not position_str:
        return {}
    parts = position_str.split(":")
    side = parts[0].lower()
    size_usd = float(parts[1]) if len(parts) > 1 else 0.0
    return {"side": side, "size_usd": size_usd}


async def _run(
    asset: str,
    top: int,
    token_budget: int,
    position_str: str | None,
    output_file: str | None,
    verbose: bool,
    explain: bool,
) -> None:
    from signal_pipeline.assembler import assemble_json
    from signal_pipeline.model import score as model_score
    from signal_pipeline.ranking import rank
    from signal_pipeline.store.memory import MemoryStore
    from signal_pipeline.validation import validate_batch

    if verbose:
        logging.getLogger().setLevel(logging.INFO)

    sources = _default_sources()
    store = MemoryStore()
    position_context = _parse_position(position_str)
    position_assets = {asset} if position_context else set()

    # 1. Fetch concurrently
    click.echo(f"Fetching signals for {asset} from {len(sources)} sources...", err=True)
    results = await asyncio.gather(
        *[source.fetch(asset) for source in sources],
        return_exceptions=True,
    )

    all_events = []
    for source, result in zip(sources, results):
        if isinstance(result, Exception):
            click.echo(f"  {source.SOURCE_NAME}: ERROR — {result}", err=True)
        else:
            click.echo(f"  {source.SOURCE_NAME}: {len(result)} signals", err=True)
            all_events.extend(result)

    # 2. Validate
    validated = validate_batch(all_events)
    invalid_count = sum(1 for e in validated if not e.is_valid)
    if invalid_count:
        click.echo(f"  Validation: {invalid_count} signals flagged invalid", err=True)

    # 3. Store
    await store.save(validated)

    # 4. Rank
    stored = await store.get(asset)
    ranked = rank(stored, position_assets=position_assets, token_budget=token_budget, max_signals=top)
    click.echo(f"  Ranked {len(ranked)} signals (budget: {token_budget} tokens)", err=True)

    # 5. Model layer — directional verdict
    # Pass ALL validated events, not just the ranked top-N.
    # Ranking controls the agent payload (token budget).
    # The model scores everything — ranking cutoff should not blind it.
    model_result = model_score(validated, asset=asset)
    click.echo(
        f"  Model: {model_result.direction.upper()}  "
        f"confidence={model_result.confidence:.1%}  "
        f"confluence={model_result.confluence.summary()}",
        err=True,
    )

    if explain:
        click.echo("", err=True)
        click.echo(model_result.explain(), err=True)
        click.echo("", err=True)

    # 6. Assemble
    payload_json = assemble_json(
        ranked,
        asset=asset,
        position_context=position_context,
        model_output=model_result.to_dict(),
    )

    # 7. Output
    if output_file:
        with open(output_file, "w") as f:
            f.write(payload_json)
        click.echo(f"Payload written to {output_file}", err=True)
    else:
        click.echo(payload_json)


@click.command()
@click.option("--asset", "-a", default="BTC", show_default=True, help="Asset to analyze (e.g. BTC, ETH, HYPE)")
@click.option("--top", "-n", default=10, show_default=True, help="Max signals to include in payload")
@click.option("--token-budget", default=2000, show_default=True, help="Token budget for signal context")
@click.option("--position", default=None, help="Open position context, e.g. long:50000 or short:25000")
@click.option("--output", "-o", default=None, help="Write payload JSON to file instead of stdout")
@click.option("--verbose", "-v", is_flag=True, help="Enable INFO logging")
@click.option(
    "--explain",
    is_flag=True,
    help=(
        "Print a human-readable model breakdown to stderr. "
        "Shows each signal's direction, strength, weight, and reason. "
        "Does not affect stdout JSON output."
    ),
)
def main(asset, top, token_budget, position, output, verbose, explain):
    """
    signal-pipeline — fetch, validate, rank, score, and assemble crypto signals for an AI agent.

    Outputs a JSON payload ready for injection into a Hermes (or compatible) agent context.
    The payload includes a model.direction field with the directional verdict.

    Examples:
        signal-pipeline --asset BTC
        signal-pipeline --asset HYPE --top 5 --position long:50000
        signal-pipeline --asset ETH --output payload.json
        signal-pipeline --asset BTC --explain
        signal-pipeline --asset BTC --explain --position long:50000 --verbose
    """
    asyncio.run(_run(asset, top, token_budget, position, output, verbose, explain))


if __name__ == "__main__":
    main()
