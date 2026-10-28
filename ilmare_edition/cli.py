"""Command-line entry point."""

import argparse
import sys
from pathlib import Path

import duckdb

from .calibration import calibrate
from .config import DEFAULT_MODEL, DEFAULT_PATTERN, Config, parse_years
from .crosscheck import crosscheck_map
from .marts import build_marts
from .replace import replace_share_model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Ilmarë edition marts from RFSD")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-marts")
    for argument in ("data-root", "okved-ref", "out"):
        build.add_argument(f"--{argument}", type=Path, required=True)
    build.add_argument("--edition", required=True)
    build.add_argument("--code-version")
    build.add_argument("--years", default="2011-2025")
    build.add_argument("--file-pattern", default=DEFAULT_PATTERN)
    build.add_argument("--share-model", type=Path, default=DEFAULT_MODEL)
    build.add_argument("--seed", type=int, default=20261017)
    build.add_argument("--draws", type=int, default=100000)
    build.add_argument("--threads", type=int, default=3)
    build.add_argument("--memory-limit", default="3GB")
    calibration = commands.add_parser("calibrate", help="Calibrate external rates against marts")
    for argument in ("marts", "benchmarks-ru", "benchmarks-fx", "elbereth-cases", "out"):
        calibration.add_argument(f"--{argument}", type=Path, required=True)
    calibration.add_argument("--code-version")
    check = commands.add_parser("crosscheck", help="Compare independent values with marts")
    for argument in ("marts", "independent", "out"):
        check.add_argument(f"--{argument}", type=Path, required=True)
    replace = commands.add_parser(
        "replace-share-model", help="Replace share model in verified marts"
    )
    replace.add_argument("--marts", type=Path, required=True)
    replace.add_argument("--share-model", type=Path, required=True)
    replace.add_argument("--out", type=Path, required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    try:
        if command == "build-marts":
            args["years"] = parse_years(args["years"])
            build_marts(Config(**args))
        elif command == "calibrate":
            calibrate(**args)
        elif command == "crosscheck":
            _, summary = crosscheck_map(**args)
            failed = summary["failed"]
            if failed:
                print(f"ilmare-edition: {failed} crosscheck discrepancies", file=sys.stderr)
                return 2
        else:
            replace_share_model(args["marts"], args["share_model"], args["out"])
    except (ValueError, OSError, KeyError, RuntimeError, duckdb.Error) as exc:
        print(f"ilmare-edition: {exc}", file=sys.stderr)
        return 2
    return 0
