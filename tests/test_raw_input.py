"""Raw RFSD files (Hugging Face layout) are read by named columns only."""

import hashlib
from pathlib import Path

import duckdb

from ilmare_edition.config import DEFAULT_PATTERN, Config
from ilmare_edition.marts import build_marts

RAW_COLUMNS = (
    "inn VARCHAR, ogrn VARCHAR, okved VARCHAR, okopf VARCHAR, region VARCHAR, year INT, "
    "filed DOUBLE, simplified DOUBLE, line_2110 DOUBLE, line_2200 DOUBLE, line_2300 DOUBLE, "
    "line_2330 DOUBLE, line_2400 DOUBLE, line_1300 DOUBLE, line_1530 DOUBLE, line_1600 DOUBLE"
)
AL_COLUMNS = "al_flag VARCHAR, al_score DOUBLE, al_note VARCHAR"
MART_FILES = ("cells", "royalty", "qgrid", "sizes", "funnel", "okved_names")


def write_raw_year(root: Path, year: int, with_al: bool) -> None:
    path = root / DEFAULT_PATTERN.format(year=year)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = RAW_COLUMNS + (", " + AL_COLUMNS if with_al else "")
    with duckdb.connect() as con:
        con.execute(f"CREATE TABLE source ({columns})")
        for i in range(60):
            rev = 500000.0 + i * 1000
            values = [f"{i:04}", "1", "01.11", "12300", "77", year, 1.0, float(i % 2), rev,
                      rev * (0.08 + i * 0.001), rev * 0.07, -1.0, 5.0, 10.0, None, 99.0]
            if with_al:
                values += ["x", 1.5, "not a number"]
            con.execute(f"INSERT INTO source VALUES ({', '.join('?' for _ in values)})", values)
        con.execute("COPY source TO ? (FORMAT parquet)", [str(path)])


def build(tmp_path: Path, reference: Path, name: str, with_al: bool) -> Path:
    root = tmp_path / name / "data"
    for year in (2011, 2012, 2013):
        write_raw_year(root, year, with_al)
    out = tmp_path / name / "marts"
    build_marts(Config(root, reference, out, "2026.1", years=(2012, 2013), draws=1000,
                       code_version="test"))
    return out


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_raw_rfsd_layout_is_read(tmp_path: Path, reference: Path) -> None:
    out = build(tmp_path, reference, "raw", with_al=False)
    with duckdb.connect() as con:
        n = con.execute(
            "SELECT n FROM read_parquet(?) WHERE t = 2013 AND level = 'all'",
            [str(out / "cells.parquet")],
        ).fetchone()[0]
    assert n == 60


def test_al_columns_are_ignored(tmp_path: Path, reference: Path) -> None:
    plain = build(tmp_path, reference, "plain", with_al=False)
    extra = build(tmp_path, reference, "extra", with_al=True)
    for table in MART_FILES:
        assert digest(plain / f"{table}.parquet") == digest(extra / f"{table}.parquet")


def test_source_reads_no_al_column() -> None:
    from ilmare_edition import load

    assert "al_" not in Path(load.__file__).read_text(encoding="utf-8")
