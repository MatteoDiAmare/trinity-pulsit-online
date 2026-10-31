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
from .academy.instruments import available_instruments, load_instrument
from .academy.risk import RiskProfile, StressModel
from .academy.ledger import LedgerStore
from .academy.export import export_all
from .academy.compare import compare_markets
from .academy.spec_strategy import load_specs_dir


def main(argv=None):
    p = argparse.ArgumentParser(description="Saba Academy replay engine (historical simulation only)")
    p.add_argument("--root", help="Archive root directory")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("resume", help="Operator only: lift a halt set by Saba or by the operator")
    sub.add_parser("halt", help="Stop all new research cycles").add_argument("--reason", required=True)
    for name in ("train", "train-specs", "compare", "final-test", "export"):
        s = sub.add_parser(name)
        if name not in ("export", "compare"):
            s.add_argument("--symbol", default="GC=F")
            s.add_argument("--interval", default="1d")
        s.add_argument("--owner-id", default="local", help="Supabase user UUID for exports (default: local)")
        s.add_argument("--org-id", default="personal")
    e = sub.choices["export"]
    e.add_argument("--out", help="Output directory (default: <root>/academy_export; must be outside any git repo)")
    e.add_argument("--include-abstentions", action="store_true")
    e.add_argument("--allow-in-repo", action="store_true")
    ts = sub.choices["train-specs"]
    ts.add_argument("--specs-dir", required=True, help="Directory of JSON strategy specs (versioned in git)")
    ts.add_argument("--instrument", required=True)
    ts.add_argument("--capital", type=float, default=1000.0)
    ts.add_argument("--seed", type=int, default=42)
    cm = sub.choices["compare"]
    cm.add_argument("--instruments", required=True, help="Comma-separated instrument ids, e.g. GOLD,EURUSD,SP500")
    cm.add_argument("--interval", default="1d")
    cm.add_argument("--specs-dir", help="JSON strategy specs to run on every instrument")
    cm.add_argument("--experiment-id", type=int, action="append", default=[], help="Transfer an existing experiment's strategy (repeatable)")
    cm.add_argument("--capital", type=float, default=1000.0)
    t = sub.choices["train"]
    t.add_argument("--capital", type=float, default=1000.0)
    t.add_argument("--experiments", type=int, default=100)
    t.add_argument("--seed", type=int, default=42)
    t.add_argument("--fee-bps", type=float, default=2.0)
    t.add_argument("--spread-bps", type=float, default=3.0)
    t.add_argument("--slippage-bps", type=float, default=2.0)
    t.add_argument("--instrument", help="Instrument id (%s); sets symbol, costs, leverage cap, margin" % ", ".join(sorted(available_instruments())))
    t.add_argument("--max-exposure", type=float, default=None, help="Notional/equity cap (default 1.0 without --instrument, instrument cap with it)")
    t.add_argument("--risk-per-trade-pct", type=float, default=1.0)
    t.add_argument("--daily-loss-limit-pct", type=float, default=3.0)
    t.add_argument("--max-drawdown-halt-pct", type=float, default=20.0)
    t.add_argument("--spread-mult", type=float, default=1.0, help="Stress: spread multiplier")
    t.add_argument("--slippage-mult", type=float, default=1.0, help="Stress: slippage multiplier")
    t.add_argument("--stop-failure-prob", type=float, default=0.0, help="Stress: probability a stop is not honoured")
    t.add_argument("--control-runs", type=int, default=30)
    f = sub.choices["final-test"]
    f.add_argument("--experiment-id", type=int, required=True)
    f.add_argument("--ledger-dir", help="Write trades/decisions/equity CSVs here")
    args = p.parse_args(argv)
    if args.command == "train" and not 1 <= args.experiments <= 10_000:
        p.error("--experiments must be 1..10000")
    archive = MarketArchive(args.root)
    registry = ExperimentRegistry(Path(archive.root) / "experiments.sqlite3")
    try:
        ledger = LedgerStore(archive.root, getattr(args, 'owner_id', 'local'), getattr(args, 'org_id', 'personal'))
        if args.command == "export":
            out = export_all(ledger, args.out or Path(archive.root) / "academy_export",
                             include_abstentions=args.include_abstentions, allow_in_repo=args.allow_in_repo)
            print(json.dumps(out, indent=2))
            return 0
        if args.command in ("resume", "halt"):
            from .academy.orchestrator import Orchestrator
            o = Orchestrator(archive.root, registry, archive, None)
            if args.command == "halt":
                o.halt(args.reason, by="operator")
            else:
                o.resume()
            print(json.dumps({"halted": o.halted()}))
            return 0
        if args.command == "compare":
            plan = []
            if args.specs_dir:
                plan += [{"strategy": "spec", "params": {"spec": sp}, "label": n} for n, sp in load_specs_dir(args.specs_dir).items()]
            for eid in args.experiment_id:
                row = registry.db.execute("SELECT configuration_json FROM experiments WHERE id=?", (eid,)).fetchone()
                if row is None or "params" not in json.loads(row[0]):
                    raise ValueError(f"experiment {eid} is not an Academy training experiment")
                c = json.loads(row[0])
                plan.append({"strategy": c["strategy"], "params": c["params"], "label": f"exp{eid}:{c['strategy']}"})
            if not plan:
                raise ValueError("give --specs-dir and/or --experiment-id")
            out = compare_markets(registry, archive, plan, [load_instrument(i) for i in args.instruments.split(",")],
                                  interval=args.interval, capital=args.capital, ledger=ledger)
            print(json.dumps(out, indent=2, default=str))
            return 0
        spec = load_instrument(args.instrument) if args.command in ("train", "train-specs") and args.instrument else None
        if spec is not None:
            args.symbol = spec.symbol
        bars = archive.read(args.symbol, args.interval)
        if args.command == "train-specs":
            plan = [{"strategy": "spec", "params": {"spec": sp}, "label": n} for n, sp in load_specs_dir(args.specs_dir).items()]
            cfg = SimConfig(args.capital, None, None, instrument=spec)
            out = run_training(bars, registry, symbol=args.symbol, interval=args.interval, seed=args.seed, sim_config=cfg,
                               plan=plan, control_runs=30, ledger=ledger)
        elif args.command == "train":
            explicit_costs = any(a.startswith(("--fee-bps", "--spread-bps", "--slippage-bps")) for a in (argv or []))
            costs = CostModel(args.fee_bps, args.spread_bps, args.slippage_bps) if (spec is None or explicit_costs) else None
            cfg = SimConfig(args.capital, costs, args.max_exposure if args.max_exposure is not None else (None if spec else 1.0),
                            instrument=spec,
                            risk=RiskProfile(args.risk_per_trade_pct, None, 5.0, args.daily_loss_limit_pct, args.max_drawdown_halt_pct),
                            stress=StressModel(args.spread_mult, args.slippage_mult, 0.0, args.stop_failure_prob))
            out = run_training(bars, registry, symbol=args.symbol, interval=args.interval,
                               experiments=args.experiments, seed=args.seed, sim_config=cfg,
                               control_runs=args.control_runs, ledger=ledger)
        else:
            out, result = run_final_test(bars, registry, args.experiment_id, keep_result=True, ledger=ledger)
            if args.ledger_dir:
                import pandas as pd
                d = Path(args.ledger_dir)
                d.mkdir(parents=True, exist_ok=True)
                pd.DataFrame(result.trades).to_csv(d / "trades.csv", index=False)
                pd.DataFrame(result.decisions).to_csv(d / "decisions.csv", index=False)
                pd.DataFrame(result.orders).to_csv(d / "orders.csv", index=False)
                result.equity.to_csv(d / "equity.csv")
        print(json.dumps({k: v for k, v in out.items() if not k.startswith('_')}, indent=2, default=str))
        return 0
    except (ValueError, FileNotFoundError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    finally:
        registry.close()


if __name__ == "__main__":
    raise SystemExit(main())
