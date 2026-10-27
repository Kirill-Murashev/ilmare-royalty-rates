"""End to end on synthetic data: build marts, recompute independently, compare."""

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from ilmare_edition.cli import main
from ilmare_edition.config import Config
from ilmare_edition.marts import build_marts

pytest.importorskip("scipy")
CHECK = Path(__file__).parent.parent / "independent-check"
DRAWS = 100000


def run(script: str, *args: object, stdout: Path | None = None) -> None:
    command = [sys.executable, "-I", str(CHECK / script), *map(str, args)]
    if stdout is None:
        subprocess.run(command, check=True, capture_output=True, text=True)
    else:
        with stdout.open("w", encoding="utf-8") as stream:
            subprocess.run(command, check=True, stdout=stream, stderr=subprocess.PIPE, text=True)


def test_map_has_no_discrepancies(synthetic: Config, csv_reference: Path, tmp_path: Path) -> None:
    # Fixture paths contain an apostrophe; the scripts must pass paths as query parameters.
    data_root, reference = synthetic.data_root, csv_reference
    config = Config(data_root, reference, tmp_path / "marts", "2026.1",
                    years=synthetic.years, draws=DRAWS, code_version="test")
    build_marts(config)
    marts = config.out
    with duckdb.connect() as con:
        existing = {tuple(row) for row in con.execute(
            "SELECT t, level, code FROM read_parquet(?)", [str(marts / "cells.parquet")]
        ).fetchall()}
    sample = tmp_path / "keys.json"
    run("make_keys.py", marts, stdout=sample)
    keys = [key for key in json.loads(sample.read_text()) if tuple(key) in existing]
    keys += [list(key) for key in sorted(existing) if key[1] in ("all", "sec")]
    keys = [list(key) for key in sorted({tuple(key) for key in keys})]
    assert keys
    keys_json = tmp_path / "keys_filtered.json"
    keys_json.write_text(json.dumps(keys))
    royalty_keys = tmp_path / "royalty_keys.json"
    run("royalty_keys.py", marts, keys_json, stdout=royalty_keys)
    xref = tmp_path / "xref.jsonl"
    run("xref_cells.py", data_root, reference, keys_json, stdout=xref)
    independent = tmp_path / "independent.csv"
    run("build_independent.py", xref, marts / "share_model.json", royalty_keys, DRAWS,
        stdout=independent)
    out = tmp_path / "crosscheck.json"
    code = main(["crosscheck", "--marts", str(marts), "--independent", str(independent),
                 "--out", str(out)])
    summary = json.loads(out.read_bytes())["summary"]
    assert code == 0
    assert summary["failed"] == 0
    assert summary["checks"] > 100
    assert summary["levels"]["sec"] > 0
