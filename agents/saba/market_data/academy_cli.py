"""Saba Academy CLI (research only; no orders).

Run from agents/saba:
  python -m market_data.academy_cli train --symbol GC=F --interval 1d --capital 1000 --experiments 100
  python -m market_data.academy_cli final-test --symbol GC=F --interval 1d --experiment-id 101
  python -m market_data.academy_cli export --owner-id <supabase-user-uuid> --out ~/academy_export
"""
import argparse
import json
from pathlib import Path

from .archive import MarketArchive
from .laboratory import ExperimentRegistry
from .academy.execution import CostModel
from .academy.experiments import run_final_test, run_training
from .academy.simulator import SimConfig
from .academy.ledger import LedgerStore
from .academy.export import export_all


def main(argv=None):
    p = argparse.ArgumentParser(description="Saba Academy replay engine (historical simulation only)")
    p.add_argument("--root", help="Archive root directory")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("train", "final-test", "export"):
        s = sub.add_parser(name)
        if name != "export":
            s.add_argument("--symbol", default="GC=F")
            s.add_argument("--interval", default="1d")
        s.add_argument("--owner-id", default="local", help="Supabase user UUID for exports (default: local)")
        s.add_argument("--org-id", default="personal")
    e = sub.choices["export"]
    e.add_argument("--out", help="Output directory (default: <root>/academy_export; must be outside any git repo)")
    e.add_argument("--include-abstentions", action="store_true")
    e.add_argument("--allow-in-repo", action="store_true")
    t = sub.choices["train"]
    t.add_argument("--capital", type=float, default=1000.0)
    t.add_argument("--experiments", type=int, default=100)
    t.add_argument("--seed", type=int, default=42)
    t.add_argument("--fee-bps", type=float, default=2.0)
    t.add_argument("--spread-bps", type=float, default=3.0)
    t.add_argument("--slippage-bps", type=float, default=2.0)
    t.add_argument("--max-exposure", type=float, default=1.0)
    t.add_argument("--control-runs", type=int, default=30)
    f = sub.choices["final-test"]
    f.add_argument("--experiment-id", type=int, required=True)
    f.add_argument("--force", action="store_true")
    f.add_argument("--ledger-dir", help="Write trades/decisions/equity CSVs here")
    args = p.parse_args(argv)
    if args.command == "train" and not 1 <= args.experiments <= 10_000:
        p.error("--experiments must be 1..10000")
    archive = MarketArchive(args.root)
    registry = ExperimentRegistry(Path(archive.root) / "experiments.sqlite3")
    try:
        ledger = LedgerStore(archive.root, args.owner_id, args.org_id)
        if args.command == "export":
            out = export_all(ledger, args.out or Path(archive.root) / "academy_export",
                             include_abstentions=args.include_abstentions, allow_in_repo=args.allow_in_repo)
            print(json.dumps(out, indent=2))
            return 0
        bars = archive.read(args.symbol, args.interval)
        if args.command == "train":
            cfg = SimConfig(args.capital, CostModel(args.fee_bps, args.spread_bps, args.slippage_bps), args.max_exposure)
            out = run_training(bars, registry, symbol=args.symbol, interval=args.interval,
                               experiments=args.experiments, seed=args.seed, sim_config=cfg,
                               control_runs=args.control_runs, ledger=ledger)
        else:
            out, result = run_final_test(bars, registry, args.experiment_id, force=args.force, keep_result=True, ledger=ledger)
            if args.ledger_dir:
                import pandas as pd
                d = Path(args.ledger_dir)
                d.mkdir(parents=True, exist_ok=True)
                pd.DataFrame(result.trades).to_csv(d / "trades.csv", index=False)
                pd.DataFrame(result.decisions).to_csv(d / "decisions.csv", index=False)
                pd.DataFrame(result.orders).to_csv(d / "orders.csv", index=False)
                result.equity.to_csv(d / "equity.csv")
        print(json.dumps(out, indent=2, default=str))
        return 0
    except (ValueError, FileNotFoundError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    finally:
        registry.close()


if __name__ == "__main__":
    raise SystemExit(main())
