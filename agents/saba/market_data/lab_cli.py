"""Saba's first reproducible research experiment.

Run from agents/saba:
python -m market_data.lab_cli run GC=F --interval 1d
python -m market_data.lab_cli recent
"""
import argparse
import json
import os
from pathlib import Path

from .archive import MarketArchive
from .laboratory import ExperimentRegistry, evaluate, moving_average_signal


def main(argv=None):
    parser = argparse.ArgumentParser(description="Saba strategy laboratory (research only)")
    parser.add_argument("--root", help="Archive root directory")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("symbol")
    run.add_argument("--interval", default="1d")
    run.add_argument("--short", type=int, default=10)
    run.add_argument("--long", type=int, default=30)
    run.add_argument("--fee-bps", type=float, default=2)
    run.add_argument("--spread-bps", type=float, default=3)
    run.add_argument("--slippage-bps", type=float, default=2)
    recent = sub.add_parser("recent")
    recent.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    archive = MarketArchive(args.root)
    journal = Path(archive.root) / "experiments.sqlite3"
    registry = ExperimentRegistry(journal)
    try:
        if args.command == "recent":
            print(json.dumps(registry.recent(args.limit), indent=2))
            return 0
        bars = archive.read(args.symbol, args.interval)
        signals = moving_average_signal(bars, args.short, args.long)
        config = {"symbol": args.symbol, "interval": args.interval, "short": args.short,
                  "long": args.long, "fee_bps": args.fee_bps, "spread_bps": args.spread_bps,
                  "slippage_bps": args.slippage_bps}
        result = evaluate(bars, signals, fee_bps=args.fee_bps,
                          spread_bps=args.spread_bps, slippage_bps=args.slippage_bps)
        experiment_id = registry.record("moving_average_baseline", bars, config, result)
        print(json.dumps({"experiment_id": experiment_id, "configuration": config, "results": result}, indent=2))
        return 0
    finally:
        registry.close()


if __name__ == "__main__":
    raise SystemExit(main())
