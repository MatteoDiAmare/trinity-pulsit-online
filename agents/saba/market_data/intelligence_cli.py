"""Run: python -m market_data.intelligence_cli analyze GC=F"""
import argparse
import sys
from .intelligence import report_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="Saba read-only market intelligence")
    parser.add_argument("command", choices=["analyze"])
    parser.add_argument("symbol", nargs="?", default="GC=F")
    parser.add_argument("--root", default=None)
    args = parser.parse_args(argv)
    try:
        print(report_json(args.symbol, root=args.root))
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"Analysis error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
