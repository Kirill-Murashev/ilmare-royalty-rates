"""Compare an independent CSV with verified edition cells and royalty quantiles."""

import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path

import duckdb

from .marts import CELL_SCHEMA, sha256, write_json_new
from .marts_io import HEX64, read_rows, verify_marts

COLUMNS = ("table", "t", "level", "code", "base", "tier", "field", "value", "tolerance")
LEVELS = ("sec", "cls", "sub", "grp", "all")
CELL_FIELDS = {name for name, kind in CELL_SCHEMA if kind in ("BIGINT", "DOUBLE", "TINYINT")}
ROYALTY_FIELDS = {"p05", "p10", "p25", "p50", "p75", "p90", "p95"}


def finite_number(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def mart_index(marts: Path) -> dict:
    return {
        table: {
            tuple(
                row[key]
                for key in (
                    ("t", "level", "code")
                    if table == "cells"
                    else ("t", "level", "code", "base", "tier")
                )
            ): row
            for row in read_rows(marts, table)
        }
        for table in ("cells", "royalty")
    }


def check_value(
    table: str, key: dict, field: str, independent: float | None, tolerance: float, index: dict
) -> dict:
    if table not in ("cells", "royalty"):
        raise ValueError("unknown table")
    names = ("t", "level", "code") if table == "cells" else ("t", "level", "code", "base", "tier")
    if not isinstance(key, dict) or set(key) != set(names):
        raise ValueError("invalid key fields")
    if type(key["t"]) is not int or key["level"] not in LEVELS or not isinstance(key["code"], str):
        raise ValueError("invalid cell key")
    if table == "royalty" and (
        key["base"] not in ("q3", "median", "wmean")
        or type(key["tier"]) is not int
        or key["tier"] not in range(4)
    ):
        raise ValueError("invalid royalty base or tier")
    if field not in (CELL_FIELDS if table == "cells" else ROYALTY_FIELDS):
        raise ValueError(f"unknown field: {field}")
    if independent is not None and not finite_number(independent):
        raise ValueError("value must be finite or empty")
    if not finite_number(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be finite and nonnegative")
    row = index[table].get(tuple(key[name] for name in names))
    if row is None:
        raise ValueError("key is absent from marts")
    edition = row[field]
    if edition is None or independent is None:
        diff, ok = None, edition is None and independent is None
    else:
        diff = abs(edition - independent)
        ok = diff <= tolerance
    return {
        "table": table,
        "key": key,
        "field": field,
        "edition": edition,
        "independent": independent,
        "abs_diff": diff,
        "tolerance": tolerance,
        "ok": ok,
    }


def check_sort_key(check: dict) -> tuple:
    key = check["key"]
    return (
        check["table"],
        key["t"],
        key["level"],
        key["code"],
        key.get("base", ""),
        key.get("tier", -1),
        check["field"],
    )


def summarize(checks: list[dict]) -> dict:
    cells = {(check["key"]["t"], check["key"]["level"], check["key"]["code"]) for check in checks}
    ok = sum(check["ok"] for check in checks)
    return {
        "checks": len(checks),
        "ok": ok,
        "failed": len(checks) - ok,
        "cells": len(cells),
        "levels": {level: sum(key[1] == level for key in cells) for level in LEVELS},
    }


def crosscheck(marts: Path, independent: Path, out: Path) -> Path:
    return crosscheck_map(marts, independent, out)[0]


def crosscheck_map(marts: Path, independent: Path, out: Path) -> tuple[Path, dict]:
    """Write a deterministic comparison map, including failed checks."""
    try:
        manifest = verify_marts(marts)
        if os.path.lexists(out) or not out.parent.is_dir():
            raise ValueError("--out must not exist and its parent must exist")
        index = mart_index(marts)
        checks, seen = [], set()
        raw = independent.read_bytes()
        with io.StringIO(raw.decode("utf-8"), newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != list(COLUMNS):
                raise ValueError("line 1: expected " + ",".join(COLUMNS))
            for row in reader:
                line = reader.line_num
                try:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError("invalid column count")
                    row = {key: value.strip() for key, value in row.items()}
                    key = {"t": int(row["t"]), "level": row["level"], "code": row["code"]}
                    if row["table"] == "royalty":
                        key.update(base=row["base"], tier=int(row["tier"]))
                    elif row["base"] or row["tier"]:
                        raise ValueError("cells base and tier must be empty")
                    value = float(row["value"]) if row["value"] else None
                    check = check_value(
                        row["table"], key, row["field"], value, float(row["tolerance"]), index
                    )
                    identity = check_sort_key(check)
                    if identity in seen:
                        raise ValueError("duplicate key and field")
                    seen.add(identity)
                    checks.append(check)
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"line {line}: {exc}") from exc
        checks.sort(key=check_sort_key)
        summary = summarize(checks)
        write_json_new(
            out,
            {
                "format": "ilmare-crosscheck/1",
                "edition": manifest["edition"],
                "marts_sha256sums": sha256(marts / "SHA256SUMS"),
                "code_version": manifest["code_version"],
                "independent_sha256": hashlib.sha256(raw).hexdigest(),
                "checks": checks,
                "summary": summary,
            },
        )
    except csv.Error as exc:
        raise ValueError(f"line {reader.line_num}: {exc}") from exc
    except FileExistsError as exc:
        raise ValueError("--out must not exist and its parent must exist") from exc
    except (OSError, UnicodeError, duckdb.Error) as exc:
        raise ValueError(f"cannot crosscheck: {exc}") from exc
    return out, summary


def validate_crosscheck(path: Path, marts: Path) -> dict:
    """Validate a map read once against verified marts."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"invalid crosscheck: {exc}") from exc
    manifest = verify_marts(marts)
    return validate_crosscheck_bytes(raw, marts, manifest)


def validate_crosscheck_bytes(raw: bytes, marts: Path, manifest: dict) -> dict:
    """Re-read mart values and recompute every check before the map is accepted."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("format") != "ilmare-crosscheck/1":
            raise ValueError("invalid crosscheck format")
        if set(data) != {
            "format", "edition", "marts_sha256sums", "code_version", "independent_sha256",
            "checks", "summary",
        }:
            raise ValueError("crosscheck fields mismatch")
        if (data["edition"] != manifest["edition"]
                or data["code_version"] != manifest["code_version"]):
            raise ValueError("crosscheck edition or code_version mismatch")
        if (not isinstance(data["independent_sha256"], str)
                or not HEX64.fullmatch(data["independent_sha256"])):
            raise ValueError("invalid crosscheck independent_sha256")
        if data.get("marts_sha256sums") != sha256(marts / "SHA256SUMS"):
            raise ValueError("crosscheck marts_sha256sums mismatch")
        if not isinstance(data.get("checks"), list):
            raise ValueError("invalid crosscheck checks")
        index = mart_index(marts)
        checked, seen = [], set()
        for item in data["checks"]:
            if not isinstance(item, dict):
                raise ValueError("invalid crosscheck entry")
            actual = check_value(
                item["table"],
                item["key"],
                item["field"],
                item["independent"],
                item["tolerance"],
                index,
            )
            identity = check_sort_key(actual)
            if identity in seen:
                raise ValueError("duplicate crosscheck key and field")
            seen.add(identity)
            if item != actual or type(item["ok"]) is not bool:
                raise ValueError("crosscheck edition or result mismatch")
            checked.append(actual)
        summary = summarize(checked)
        if data.get("summary") != summary:
            raise ValueError("crosscheck summary mismatch")
        if summary["failed"]:
            raise ValueError("crosscheck has failed checks")
        if any(summary["levels"][level] < 1 for level in ("sec", "cls", "sub", "grp")):
            raise ValueError("crosscheck must cover sec, cls, sub, grp")
        return summary
    except (OSError, UnicodeError, KeyError, TypeError, duckdb.Error, RecursionError) as exc:
        raise ValueError(f"invalid crosscheck: {exc}") from exc
