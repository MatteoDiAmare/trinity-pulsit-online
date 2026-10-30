"""Run as: python -m market_data.cli fetch GC=F --interval 1d --period 10y"""
import argparse
import json
import sys

from .archive import MarketArchive


def main(argv=None):
    parser = argparse.ArgumentParser(description="Saba historical market-data archive (research only)")
    parser.add_argument("--root", help="Persistent data directory (default SABA_MARKET_DATA_DIR)")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Download and merge Yahoo history")
    fetch.add_argument("symbol")
    fetch.add_argument("--interval", default="1d")
    fetch.add_argument("--period", default="10y")
    info = commands.add_parser("info", help="Show archived coverage")
    info.add_argument("symbol")
    info.add_argument("--interval", default="1d")
    query = commands.add_parser("query", help="Display latest archived bars")
    query.add_argument("symbol")
    query.add_argument("--interval", default="1d")
    query.add_argument("--tail", type=int, default=10)
    query.add_argument("--start")
    query.add_argument("--end")
    args = parser.parse_args(argv)
    archive = MarketArchive(args.root)
    try:
        if args.command == "fetch":
            print(json.dumps(archive.ingest(args.symbol, args.interval, args.period), indent=2))
        elif args.command == "info":
            print(json.dumps(archive.info(args.symbol, args.interval), indent=2))
        else:
            if not 1 <= args.tail <= 1000:
                parser.error("--tail must be between 1 and 1000")
            frame = archive.read(args.symbol, args.interval, args.start, args.end)
            print(frame.tail(args.tail).to_csv(index_label="TimestampUTC"))
    except (ValueError, FileNotFoundError, OSError) as error:
        print(f"Market data error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
