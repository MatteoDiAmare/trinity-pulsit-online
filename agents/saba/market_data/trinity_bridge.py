"""JSON tool bridge for Trinity's Saba agent; read-only market data, research-only decisions.

Invoke as a subprocess from Trinity. Input: one JSON object on stdin.
Commands: analyze, decide. Output: one JSON object on stdout.
No brokerage APIs, network calls, shell commands or dynamic execution.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import json
import sys
from pathlib import Path
from .intelligence import analyze_archive
from .decision_cycle import Thesis, evaluate, ExperienceJournal


def handle(request: dict, *, journal_path: str | Path) -> dict:
    if not isinstance(request, dict):
        raise ValueError("JSON object required")
    command = request.get("command")
    if command == "analyze":
        symbol = request.get("symbol", "GC=F")
        if symbol != "GC=F":
            raise ValueError("only archived GC=F proxy is supported in this bridge")
        return analyze_archive(symbol, root=request.get("archive_root"))
    if command != "decide":
        raise ValueError("unsupported command")
    # The evidence must be an actual archived snapshot, not an LLM-supplied
    # claim about the latest candle. Recompute to prevent invented evidence.
    symbol = request.get("symbol", "GC=F")
    if symbol != "GC=F":
        raise ValueError("only archived GC=F proxy is supported")
    analysis = analyze_archive(symbol, root=request.get("archive_root"))
    latest = analysis["timeframes"].get("1m", {})
    if "completed_at_utc" not in latest:
        raise ValueError("no complete 1m analysis")
    completed = latest["completed_at_utc"]
    if request.get("evidence_completed_at_utc") != completed:
        raise ValueError("stale or unverified evidence timestamp")
    if request.get("side") not in ("LONG", "SHORT", "ABSTAIN"):
        raise ValueError("invalid side")
    thesis = Thesis(
        symbol=symbol,
        as_of=datetime.fromisoformat(completed),
        side=request["side"],
        rationale=request["rationale"],
        invalidation_price=request.get("invalidation_price"),
        expected_holding_minutes=request["expected_holding_minutes"],
        confidence=request["confidence"],
        evidence_id=f"{symbol}:1m:{completed}",
    )
    result = evaluate(thesis)
    result["source"] = "archived futures proxy, not XAU/USD CFD"
    result["market_observation"] = latest
    result["journal_sha256"] = ExperienceJournal(journal_path).record(result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Saba Trinity research tool bridge (no orders)")
    parser.add_argument("--journal", default="saba_experiences.sqlite")
    args = parser.parse_args(argv)
    try:
        request = json.load(sys.stdin)
        print(json.dumps(handle(request, journal_path=args.journal), allow_nan=False))
    except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc), "execution": "DISABLED"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
