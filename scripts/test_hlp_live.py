import asyncio
import sys
sys.path.insert(0, "src")

from signal_pipeline.sources.hlp import HLPSource

async def main():
    asset = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    source = HLPSource()
    signals = await source.fetch(asset)
    for s in signals:
        print(s)
        print(s.summary)

asyncio.run(main())
