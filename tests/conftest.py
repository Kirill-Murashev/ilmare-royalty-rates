"""Small synthetic RFSD parquet fixtures, written solely by DuckDB."""

import csv
import json
from pathlib import Path
from typing import Callable

import duckdb
import pytest

from ilmare_edition.calibration import COLUMNS, calibrate
from ilmare_edition.config import DEFAULT_PATTERN, Config
from ilmare_edition.marts import build_marts


@pytest.fixture
def reference(tmp_path: Path) -> Path:
    items = [
        ("A", "A"), ("01", "A"), ("01.1", "A"), ("01.11", "A"), ("01.11.1", "A"),
        ("01.12", "A"), ("01.2", "A"), ("01.21", "A"),
        ("C", "C"), ("10", "C"), ("10.71", "C"),
        ("H", "H"), ("49", "H"), ("49.4", "H"),
        ("L", "L"), ("68", "L"), ("68.2", "L"), ("68.20", "L"), ("68.20.2", "L"),
    ]
    path = tmp_path / "okved's ref.json"
    path.write_text(json.dumps([{"code": code, "section": sec, "name": f"Название {code}"}
                                for code, sec in items], ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def csv_reference(reference: Path) -> Path:
    records = json.loads(reference.read_text(encoding="utf-8"))
    path = reference.with_suffix(".CSV")
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";", quoting=csv.QUOTE_ALL)
        writer.writerow([])
        for item in records:
            code = " " if item["code"] == item["section"] else item["code"]
            writer.writerow([item["section"], code, item["name"]])
    return path


@pytest.fixture
def company() -> Callable[..., dict]:
    def make(inn: str, **changes: object) -> dict:
        row = dict(inn=inn, okved="01.11", okopf="12300", simplified=0,
                   line_2110=100.0, line_2200=10.0, line_2300=8.0, line_2330=-2.0,
                   line_2400=5.0, line_1300=10.0, line_1530=None)
        row.update(changes)
        return row
    return make


@pytest.fixture
def write_year() -> Callable[[Path, int, list[dict]], Path]:
    def write(root: Path, year: int, rows: list[dict]) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        path = root / DEFAULT_PATTERN.format(year=year)
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = ("inn VARCHAR, okved VARCHAR, okopf VARCHAR, simplified INT, "
                   "line_2110 DOUBLE, line_2200 DOUBLE, line_2300 DOUBLE, line_2330 DOUBLE, "
                   "line_2400 DOUBLE, line_1300 DOUBLE, line_1530 DOUBLE")
        with duckdb.connect() as con:
            con.execute(f"CREATE TABLE source ({columns})")
            if rows:
                con.executemany("INSERT INTO source VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                [tuple(row.values()) for row in rows])
            con.execute("COPY source TO ? (FORMAT parquet)", [str(path)])
        return path
    return write


@pytest.fixture
def synthetic(tmp_path: Path, reference: Path, company: Callable,
              write_year: Callable) -> Config:
    root = tmp_path / "data's root"
    for year in (2011, 2012, 2013):
        rows = []
        sizes = (119999, 120000, 799999, 800000, 1999999, 2000000)
        for i in range(80):
            rev = sizes[i % len(sizes)]
            rows.append(company(f"a{i:03}", line_2110=rev, line_2200=rev * (0.10 + i * .0005),
                                simplified=i % 2))
        for i in range(5):
            rows.append(company(f"b{i}", okved="01.12", line_2200=20 + i))
        for i in range(50):
            rows.append(company(f"l{i:03}", okved="68.20.20", line_2200=40 + i * .01))
        rows.extend([
            company("f1", line_2110=None), company("f2", line_1300=None),
            company("f3", line_1300=-1, line_1530=2), company("f4", line_2400=-1),
            company("f5", okopf="20000"), company("f7a", okved="junk"),
            company("f7b", line_2200=None),
        ])
        write_year(root, year, rows)
    return Config(root, reference, tmp_path / "output", "2026.1", years=(2011, 2012, 2013),
                  draws=1000)


@pytest.fixture
def built_marts(synthetic: Config) -> Path:
    build_marts(synthetic)
    return synthetic.out


@pytest.fixture
def calibration_file(built_marts: Path, tmp_path: Path) -> Path:
    paths = []
    for role in ("ru", "fx", "elb"):
        path = tmp_path / f"{role}.csv"
        rows = []
        for index, tier in enumerate(("low", "normal", "valuable", "core") * 2):
            row = dict.fromkeys(COLUMNS, "")
            row.update(id=f"{role}{index}", source_type="court" if role == "elb" else (
                "franchise" if index % 2 else "literature"
            ), source=f"Дело A40-{index + 1}/20", data_year="2014", okved2="01.11",
                       ip_type="tm_knowhow", license="unknown", value_hint=tier,
                       rate_mid=str(.02 + index * .005), base="revenue", verified="yes")
            rows.append(row)
        if role == "ru":
            rows.append({**rows[0], "source_type": "court"})
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=COLUMNS, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
        paths.append(path)
    out = tmp_path / "calibration.json"
    calibrate(built_marts, *paths, out, code_version="test")
    return out
