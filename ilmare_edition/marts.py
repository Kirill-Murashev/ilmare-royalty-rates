"""Year-at-a-time aggregation, typed parquet output and provenance manifest."""

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .config import Config
from .load import load_year
from .okved import LEVELS, Okved
from .royalty import Royalty, read_model
from .stats import margin_statistics

CELL_SCHEMA = (
    ("t", "INT"), ("stmt_year", "INT"), ("level", "VARCHAR"), ("code", "VARCHAR"),
    ("section", "VARCHAR"), ("parent", "VARCHAR"), ("n", "BIGINT"),
    *((key, "DOUBLE") for key in (
        "median", "median_lo", "median_hi", "q1", "q3", "q3_lo", "q3_hi",
        "p05", "p10", "p90", "p95", "wmean", "wmean_x1", "top1_share", "hhi",
        "median_ebit", "share_simplified", "share_negative",
    )),
    ("prec_q3", "TINYINT"), ("prec_median", "TINYINT"), ("indicative", "BOOLEAN"),
    ("limited_years", "BOOLEAN"), ("fallback_code", "VARCHAR"),
)
SCHEMAS = {
    "cells": CELL_SCHEMA,
    "royalty": (
        ("t", "INT"), ("level", "VARCHAR"), ("code", "VARCHAR"), ("base", "VARCHAR"),
        ("tier", "TINYINT"),
        *((key, "DOUBLE") for key in ("p05", "p10", "p25", "p50", "p75", "p90", "p95")),
    ),
    "qgrid": (("t", "INT"), ("level", "VARCHAR"), ("code", "VARCHAR"),
              ("p", "TINYINT"), ("value", "DOUBLE")),
    "sizes": (("t", "INT"), ("level", "VARCHAR"), ("code", "VARCHAR"),
              ("size_class", "VARCHAR"), ("n", "BIGINT"), ("median", "DOUBLE")),
    "funnel": (("t", "INT"), ("section", "VARCHAR"), ("step", "VARCHAR"), ("n", "BIGINT")),
    "okved_names": (("code", "VARCHAR"), ("level", "VARCHAR"),
                    ("section", "VARCHAR"), ("name", "VARCHAR")),
}
SORTS = {
    "cells": "t, level, code", "royalty": "t, level, code, base, tier",
    "qgrid": "t, level, code, p", "sizes": "t, level, code, size_class",
    "funnel": "t, section, step", "okved_names": "level, code",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def insert_rows(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple]) -> None:
    if rows:
        placeholders = ", ".join("?" for _ in SCHEMAS[table])
        con.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)


def year_cells(con: duckdb.DuckDBPyConnection, ref: Okved, year: int) -> list[dict]:
    cells = []
    for level in (*LEVELS, "all"):
        column = "all_code" if level == "all" else level
        section = "NULL::VARCHAR" if level == "all" else "min(sec)"
        # Sort every floating sum explicitly: parallel aggregation must not change bytes.
        query = f"""WITH ranked AS (
            SELECT *, row_number() OVER (
                PARTITION BY {column} ORDER BY rev DESC, op DESC, inn
            ) revenue_rank FROM s WHERE {column} IS NOT NULL
        ) SELECT {column} code, {section} section, count(*) n, list(m ORDER BY m) margins,
            quantile_cont(m, 0.05) p05, quantile_cont(m, 0.10) p10,
            quantile_cont(m, 0.90) p90, quantile_cont(m, 0.95) p95,
            fsum(op ORDER BY rev, op, inn) / fsum(rev ORDER BY rev, op, inn) wmean,
            fsum(op ORDER BY rev, op, inn) FILTER (WHERE revenue_rank > 1)
                / fsum(rev ORDER BY rev, op, inn) FILTER (WHERE revenue_rank > 1) wmean_x1,
            max(rev) / fsum(rev ORDER BY rev, op, inn) top1_share,
            fsum(rev * rev ORDER BY rev, op, inn)
                / pow(fsum(rev ORDER BY rev, op, inn), 2) hhi,
            median(me) median_ebit,
            avg(CASE WHEN simplified = 1 THEN 1.0 ELSE 0.0 END) share_simplified,
            avg(CASE WHEN m < 0 THEN 1.0 ELSE 0.0 END) share_negative
        FROM ranked GROUP BY {column} ORDER BY {column}"""
        result = con.execute(query)
        columns = [item[0] for item in result.description]
        # One aggregate sample at a time; never fetch source-company rows into Python.
        while (row := result.fetchone()) is not None:
            cell = dict(zip(columns, row, strict=True))
            cell.update(margin_statistics(cell.pop("margins")))
            cell.update(t=year + 1, stmt_year=year, level=level,
                        parent=ref.parent(cell["code"], level),
                        indicative=cell["section"] in ("K", "L"),
                        limited_years=year + 1 in (2012, 2013), fallback_code=None)
            cells.append(cell)
    by_code = {cell["code"]: cell for cell in cells}
    for cell in cells:
        if cell["prec_q3"]:
            continue
        ancestor = cell["parent"]
        while ancestor is not None:
            parent = by_code.get(ancestor)
            if parent is None:
                # A reference ancestor normally has a cell if its descendant has one.
                break
            if parent["prec_q3"] > 0:
                cell["fallback_code"] = ancestor
                break
            ancestor = parent["parent"]
    return cells


def insert_graph_marts(con: duckdb.DuckDBPyConnection, year: int) -> None:
    probabilities = ",".join(str(p / 100) for p in range(1, 100))
    for level in ("sec", "cls", "all"):
        column = "all_code" if level == "all" else level
        con.execute(f"""INSERT INTO qgrid
            SELECT ?, ?, code, unnest(range(1, 100)), unnest(qs)
            FROM (SELECT {column} code, quantile_cont(m, [{probabilities}]) qs
                  FROM s WHERE {column} IS NOT NULL GROUP BY {column})""", [year + 1, level])
        con.execute(f"""INSERT INTO sizes
            SELECT ?, ?, {column}, size_class, count(*), median(m)
            FROM s WHERE {column} IS NOT NULL GROUP BY {column}, size_class""", [year + 1, level])


def code_version() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                               allow_nan=False) + "\n", encoding="utf-8")


def write_json_new(path: Path, value: dict) -> None:
    """Write JSON like write_json, atomically and only into a path that does not exist yet."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        current_umask = os.umask(0)
        os.umask(current_umask)
        temporary.chmod(0o666 & ~current_umask)
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_outputs(con: duckdb.DuckDBPyConnection, config: Config) -> dict[str, int]:
    counts = {}
    for table in SCHEMAS:
        target = config.out / f"{table}.parquet"
        con.execute(f"COPY (SELECT * FROM {table} ORDER BY {SORTS[table]}) "
                    "TO ? (FORMAT parquet)", [str(target)])
        counts[target.name] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return counts


def build_marts(config: Config) -> None:
    config.validate()
    normalization = []
    ref = Okved(config.okved_ref)
    model = read_model(config.share_model)
    sampler = Royalty(model, config.seed, config.draws)
    # Include every historical file actually consulted, even outside --years.
    files = {config.okved_ref.resolve()}
    for year in config.years:
        for candidate in (year, year - 1, year - 2):
            path = config.input_path(candidate)
            if path.is_file():
                files.add(path)
    inputs = [{"name": path.name, "size": path.stat().st_size, "sha256": sha256(path)}
              for path in sorted(files)]
    config.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".duckdb-", dir=config.out) as temporary:
        with duckdb.connect() as con:
            con.execute("SET memory_limit = ?", [config.memory_limit])
            con.execute("SET threads = ?", [config.threads])
            con.execute("SET temp_directory = ?", [temporary])
            ref.install(con)
            for table, columns in SCHEMAS.items():
                declarations = ", ".join(f'"{name}" {kind}' for name, kind in columns)
                con.execute(f"CREATE TABLE {table} ({declarations})")
            insert_rows(con, "okved_names", ref.names())
            for year in sorted(config.years):
                insert_rows(con, "funnel", load_year(con, config, year, normalization))
                cells = year_cells(con, ref, year)
                insert_rows(con, "cells", [tuple(cell[key] for key, _ in CELL_SCHEMA)
                                           for cell in cells])
                for cell in cells:
                    insert_rows(con, "royalty", sampler.rows(cell))
                insert_graph_marts(con, year)
                con.execute("DROP TABLE s")
            counts = write_outputs(con, config)
    (config.out / "share_model.json").write_bytes(config.share_model.read_bytes())
    filters = {
        "all": "All rows in the statement year",
        "f1_rev": "line_2110 > 0; NULL excluded",
        "f2_na": "line_1300 + coalesce(line_1530, 0) > 0; NULL line_1300 excluded",
        "f3_cap": "line_1300 > 0",
        "f4_loss3": (
            "Exclude current line_2400 < 0 only if INN has line_2400 < 0 in both "
            "Y-1 and Y-2 files; either file absent means no exclusion; "
            "absent INN or NULL profit is not a loss"
        ),
        "f5_opf": "First digit of okopf is 1 or 6; NULL excluded",
        "f7_okved": "Valid normalized OKVED2, known class section, line_2200 IS NOT NULL",
    }
    write_json(config.out / "manifest.json", {
        "edition": config.edition, "created_at": datetime.now(timezone.utc).isoformat(),
        "code_version": config.code_version if config.code_version is not None else code_version(),
        "inputs": inputs,
        "okved_ref": config.okved_ref.name,
        "okved_normalization": {
            "scope": "rows passing f1_rev..f5_opf, before f7_okved",
            "rules": {
                "exact": "code in reference", "sub_pad": "XX.X0 -> XX.X",
                "subgroup_pad": "XX.XX.X0 -> XX.XX.X", "group_prefix": "XX.XX.XX -> XX.XX",
                "sub_zero": "XX.X0.XX -> XX.X", "empty": "NULL or empty code",
                "unmatched": "no rule applies",
            },
            "counts": [
                {"t": t, "rule": rule, "rows": rows, "codes": codes}
                for t, rule, rows, codes in normalization
            ],
        },
        "share_model_sha256": sha256(config.out / "share_model.json"),
        "seed": config.seed, "draws": config.draws, "years": sorted(config.years),
        "filters": filters, "counts": counts,
    })
    outputs = sorted(path for path in config.out.iterdir() if path.is_file())
    (config.out / "SHA256SUMS").write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in outputs), encoding="utf-8",
    )
