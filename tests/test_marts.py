import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Callable

import duckdb
import pytest

from ilmare_edition.cli import main
from ilmare_edition.config import Config
from ilmare_edition.load import load_year
from ilmare_edition.marts import build_marts, sha256
from ilmare_edition.okved import NORMALIZATION_RULES, Okved

EXPECTED_SCHEMAS = {
    "cells": (
        "t:INTEGER stmt_year:INTEGER level:VARCHAR code:VARCHAR section:VARCHAR parent:VARCHAR "
        "n:BIGINT median:DOUBLE median_lo:DOUBLE median_hi:DOUBLE q1:DOUBLE q3:DOUBLE "
        "q3_lo:DOUBLE q3_hi:DOUBLE p05:DOUBLE p10:DOUBLE p90:DOUBLE p95:DOUBLE "
        "wmean:DOUBLE wmean_x1:DOUBLE top1_share:DOUBLE hhi:DOUBLE median_ebit:DOUBLE "
        "share_simplified:DOUBLE share_negative:DOUBLE prec_q3:TINYINT prec_median:TINYINT "
        "indicative:BOOLEAN limited_years:BOOLEAN fallback_code:VARCHAR"
    ),
    "royalty": ("t:INTEGER level:VARCHAR code:VARCHAR base:VARCHAR tier:TINYINT p05:DOUBLE "
                "p10:DOUBLE p25:DOUBLE p50:DOUBLE p75:DOUBLE p90:DOUBLE p95:DOUBLE"),
    "qgrid": "t:INTEGER level:VARCHAR code:VARCHAR p:TINYINT value:DOUBLE",
    "sizes": "t:INTEGER level:VARCHAR code:VARCHAR size_class:VARCHAR n:BIGINT median:DOUBLE",
    "funnel": "t:INTEGER section:VARCHAR step:VARCHAR n:BIGINT",
    "okved_names": "code:VARCHAR level:VARCHAR section:VARCHAR name:VARCHAR",
}


def test_full_build(synthetic: Config) -> None:
    config = synthetic
    assert main(["build-marts", "--data-root", str(config.data_root),
                 "--okved-ref", str(config.okved_ref), "--out", str(config.out),
                 "--edition", config.edition, "--years", "2011-2013", "--draws", "1000"]) == 0
    assert {path.name for path in config.out.iterdir()} == {
        *(name + ".parquet" for name in EXPECTED_SCHEMAS),
        "share_model.json", "manifest.json", "SHA256SUMS",
    }
    manifest = json.loads((config.out / "manifest.json").read_text())
    assert manifest["edition"] == "2026.1"
    assert manifest["years"] == [2011, 2012, 2013]
    assert manifest["seed"] == 20261017
    assert manifest["draws"] == 1000
    assert manifest["code_version"] != "unknown"
    assert manifest["created_at"].endswith("+00:00")
    assert len(manifest["inputs"]) == 4
    assert manifest["okved_ref"] == config.okved_ref.name
    normalization = manifest["okved_normalization"]
    assert normalization["scope"] == "rows passing f1_rev..f5_opf, before f7_okved"
    assert normalization["rules"] == {
        "exact": "code in reference", "sub_pad": "XX.X0 -> XX.X",
        "subgroup_pad": "XX.XX.X0 -> XX.XX.X", "group_prefix": "XX.XX.XX -> XX.XX",
        "sub_zero": "XX.X0.XX -> XX.X", "empty": "NULL or empty code",
        "unmatched": "no rule applies",
    }
    expected_normalization = []
    for t in (2012, 2013, 2014):
        values = {"exact": (86 if t == 2014 else 87, 2),
                  "subgroup_pad": (50, 1), "unmatched": (1, 1)}
        for rule in NORMALIZATION_RULES:
            rows, codes = values.get(rule, (0, 0))
            expected_normalization.append({"t": t, "rule": rule, "rows": rows, "codes": codes})
    assert normalization["counts"] == expected_normalization
    assert (config.out / "share_model.json").read_bytes() == config.share_model.read_bytes()
    assert manifest["share_model_sha256"] == sha256(config.share_model)
    for line in (config.out / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        assert digest == hashlib.sha256((config.out / name).read_bytes()).hexdigest()
    with duckdb.connect() as con:
        for name, expected in EXPECTED_SCHEMAS.items():
            path = str(config.out / f"{name}.parquet")
            schema = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [path]).fetchall()
            assert " ".join(f"{row[0]}:{row[1]}" for row in schema) == expected
            count = con.execute("SELECT count(*) FROM read_parquet(?)", [path]).fetchone()[0]
            assert count == manifest["counts"][f"{name}.parquet"]
        con.execute("CREATE TABLE cells AS SELECT * FROM read_parquet(?)",
                    [str(config.out / "cells.parquet")])
        rows = con.execute("SELECT * FROM cells ORDER BY t, level, code").fetchall()
        stored = con.execute("SELECT * FROM cells").fetchall()
        assert stored == rows
        fallback = con.execute("""SELECT c.code, c.fallback_code, p.prec_q3
            FROM cells c JOIN cells p ON p.t=c.t AND p.code=c.fallback_code""").fetchall()
        assert fallback and all(code == "01.12" and parent == "01.1" and prec > 0
                                for code, parent, prec in fallback)
        indicative = con.execute(
            "SELECT DISTINCT indicative FROM cells WHERE section='L'"
        ).fetchall()
        assert indicative == [(True,)]
        assert con.execute("SELECT DISTINCT limited_years FROM cells WHERE t=2014").fetchall() == [
            (False,)]
        assert con.execute("SELECT DISTINCT limited_years FROM cells WHERE t=2012").fetchall() == [
            (True,)]
        assert con.execute("SELECT n FROM cells WHERE t=2014 AND code='A'").fetchone()[0] == 85
        funnel = con.execute("SELECT * FROM read_parquet(?)",
                             [str(config.out / "funnel.parquet")]).fetchall()
        all_cells = con.execute("""SELECT t, n, section, parent, indicative, fallback_code
            FROM cells WHERE level='all' AND code='ALL' ORDER BY t""").fetchall()
        totals = [(t, n, None, None, False, None) for t, sec, step, n in funnel
                  if sec == 'total' and step == 'f7_okved']
        assert all_cells == sorted(totals)
        assert len(all_cells) == 3
        for table in ("qgrid", "sizes", "royalty"):
            years = con.execute("""SELECT DISTINCT t FROM read_parquet(?)
                WHERE level='all' AND code='ALL' ORDER BY t""",
                                [str(config.out / f"{table}.parquet")]).fetchall()
            assert years == [(2012,), (2013,), (2014,)]
        assert con.execute("""SELECT code, level, section, name FROM read_parquet(?)
            WHERE level='all'""", [str(config.out / "okved_names.parquet")]).fetchall() == [
                ("ALL", "all", None, "Все отрасли")]
        assert con.execute("""SELECT DISTINCT base FROM read_parquet(?)
            WHERE level='all' ORDER BY base""",
                           [str(config.out / "royalty.parquet")]).fetchall() == [
                               ("median",), ("q3",), ("wmean",)]
        assert con.execute("""SELECT count(*) FROM read_parquet(?) r
            JOIN cells c USING (t, level, code)
            WHERE r.base='q3' AND c.prec_q3=0""",
                           [str(config.out / "royalty.parquet")]).fetchone()[0] == 0
        steps = ("all", "f1_rev", "f2_na", "f3_cap", "f4_loss3", "f5_opf", "f7_okved")
        for year in (2012, 2013, 2014):
            normalization_total = sum(row["rows"] for row in normalization["counts"]
                                      if row["t"] == year)
            assert normalization_total == next(
                n for t, sec, step, n in funnel
                if t == year and sec == "total" and step == "f5_opf"
            ) == (137 if year == 2014 else 138)
            for section in ("A", "L", "?", "total"):
                counts = {step: n for t, sec, step, n in funnel if t == year and sec == section}
                assert list(counts)  # zero-count steps must also be retained
                ns = [counts[step] for step in steps]
                assert ns == sorted(ns, reverse=True)
                assert counts["f3_cap"] - counts["f4_loss3"] == (
                    1 if year == 2014 and section in ("A", "total") else 0)
            for step in steps:
                assert sum(n for t, sec, st, n in funnel
                           if t == year and sec != "total" and st == step) == next(
                    n for t, sec, st, n in funnel if t == year and sec == "total" and st == step)
        sizes = con.execute("""SELECT size_class, n FROM read_parquet(?)
            WHERE t=2014 AND level='cls' AND code='01' ORDER BY size_class""",
                            [str(config.out / "sizes.parquet")]).fetchall()
        assert dict(sizes) == {"micro": 19, "small": 27, "medium": 26, "large": 13}
        assert con.execute("SELECT min(p), max(p), count(*) FROM read_parquet(?)",
                           [str(config.out / "qgrid.parquet")]).fetchone() == (1, 99, 1485)
    repeated = replace(config, out=config.out.parent / "second")
    build_marts(repeated)
    for name in (*EXPECTED_SCHEMAS, "share_model"):
        suffix = ".json" if name == "share_model" else ".parquet"
        assert sha256(config.out / (name + suffix)) == sha256(repeated.out / (name + suffix))
    manifest2 = json.loads((repeated.out / "manifest.json").read_text())
    manifest.pop("created_at")
    manifest2.pop("created_at")
    assert manifest == manifest2


def test_low_precision_q3_is_not_published(tmp_path: Path, reference: Path,
                                           company: Callable, write_year: Callable) -> None:
    root = tmp_path / "data"
    write_year(root, 2025, [company(str(i), line_2200=10 + i) for i in range(20)])
    config = Config(root, reference, tmp_path / "out", "test", years=(2025,), draws=1000)
    build_marts(config)
    with duckdb.connect() as con:
        con.execute("CREATE TABLE cells AS SELECT * FROM read_parquet(?)",
                    [str(config.out / "cells.parquet")])
        cells = con.execute("""SELECT level, prec_q3, q3 > 0,
            q3_lo IS NOT NULL AND q3_hi IS NOT NULL, fallback_code FROM cells""").fetchall()
        assert {row[0] for row in cells} == {"all", "sec", "cls", "sub", "grp"}
        assert all(row[1:] == (0, True, True, None) for row in cells)
        bases = con.execute("SELECT DISTINCT base FROM read_parquet(?)",
                            [str(config.out / "royalty.parquet")]).fetchall()
        assert bases == [("wmean",)]


@pytest.mark.parametrize("code", ["01.11", "ALL"])
def test_independent_cell_statistics(synthetic: Config, code: str) -> None:
    build_marts(synthetic)
    with duckdb.connect() as con:
        cell = con.execute("""SELECT median, q1, q3, p05, p10, p90, p95,
            wmean, wmean_x1, top1_share, hhi, median_ebit, share_negative
            FROM read_parquet(?) WHERE t=2014 AND code=?""",
                           [str(synthetic.out / "cells.parquet"), code]).fetchone()
        companies = con.execute("""SELECT line_2110, line_2200 FROM read_parquet(?)
            WHERE left(inn, 1)='a' OR (? = 'ALL' AND left(inn, 1) IN ('b', 'l'))
            ORDER BY line_2200 / line_2110""",
                                [str(synthetic.input_path(2013)), code]).fetchall()
        margins = [profit / revenue for revenue, profit in companies]
        total = sum(rev for rev, _ in companies)
        top = max(companies, key=lambda row: (row[0], row[1]))
        if code == "ALL":
            assert len(margins) == 135
            expected = [margins[67], margins[33], margins[101]]
        else:
            assert len(margins) == 80
            expected = [(margins[39] + margins[40]) / 2, (margins[19] + margins[20]) / 2,
                        (margins[59] + margins[60]) / 2]
        for p in (.05, .1, .9, .95):
            position = (len(margins) - 1) * p
            index = int(position)
            expected.append(margins[index] + (position - index)
                            * (margins[index + 1] - margins[index]))
        expected.extend([sum(op for _, op in companies) / total,
                         (sum(op for _, op in companies) - top[1]) / (total - top[0]),
                         top[0] / total, sum((rev / total)**2 for rev, _ in companies),
                         0, 0])
        # EBIT is 10 for every fixture company, so its median can be checked separately.
        ebits = sorted(10 / rev for rev, _ in companies)
        expected[-2] = ebits[67] if code == "ALL" else (ebits[39] + ebits[40]) / 2
        assert cell == pytest.approx(expected, abs=1e-12)


def test_refuse_out_inside_data_root(synthetic: Config) -> None:
    bad = synthetic.data_root / "new-output"
    assert main(["build-marts", "--data-root", str(synthetic.data_root),
                 "--okved-ref", str(synthetic.okved_ref), "--out", str(bad),
                 "--edition", "test", "--years", "2011-2013"]) == 2
    assert not bad.exists()
    link = synthetic.out.parent / "link"
    link.symlink_to(synthetic.data_root, target_is_directory=True)
    with pytest.raises(ValueError, match="inside data-root"):
        build_marts(replace(synthetic, out=link / "also-bad"))


def test_nonempty_output_is_preserved(synthetic: Config) -> None:
    synthetic.out.mkdir()
    sentinel = synthetic.out / "sentinel"
    sentinel.write_text("keep")
    with pytest.raises(ValueError, match="empty"):
        build_marts(synthetic)
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize("rows", [[], ["excluded"]])
def test_empty_years(tmp_path: Path, reference: Path, company: Callable,
                     write_year: Callable, rows: list) -> None:
    root = tmp_path / "data"
    write_year(root, 2025, [company("x", line_2110=0)] if rows else [])
    config = Config(root, reference, tmp_path / "out", "test", years=(2025,), draws=10)
    build_marts(config)
    manifest = json.loads((config.out / "manifest.json").read_text())
    assert manifest["okved_normalization"]["counts"] == [
        {"t": 2026, "rule": rule, "rows": 0, "codes": 0} for rule in NORMALIZATION_RULES
    ]
    for table in ("cells", "royalty", "qgrid", "sizes"):
        assert manifest["counts"][table + ".parquet"] == 0


def test_history_provenance(synthetic: Config) -> None:
    config = replace(synthetic, years=(2013,))
    build_marts(config)
    manifest = json.loads((config.out / "manifest.json").read_text())
    assert len(manifest["inputs"]) == 4  # current + two historical + reference


def test_cli_code_version(synthetic: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_git_version() -> str:
        raise AssertionError("git version must not be queried with --code-version")

    monkeypatch.setattr("ilmare_edition.marts.code_version", unexpected_git_version)
    version = "release/v1+'quoted'"
    assert main(["build-marts", "--data-root", str(synthetic.data_root),
                 "--okved-ref", str(synthetic.okved_ref), "--out", str(synthetic.out),
                 "--edition", "2026.1", "--years", "2013", "--draws", "1000",
                 "--code-version", version]) == 0
    manifest = json.loads((synthetic.out / "manifest.json").read_text())
    assert manifest["code_version"] == version


def test_module_entrypoint() -> None:
    result = subprocess.run([sys.executable, "-m", "ilmare_edition", "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "build-marts" in result.stdout


def test_csv_and_json_marts_are_identical(synthetic: Config, csv_reference: Path) -> None:
    build_marts(synthetic)
    csv_config = replace(synthetic, okved_ref=csv_reference, out=synthetic.out.parent / "csv")
    build_marts(csv_config)
    for name in (*EXPECTED_SCHEMAS, "share_model"):
        suffix = ".json" if name == "share_model" else ".parquet"
        assert (synthetic.out / (name + suffix)).read_bytes() == (
            csv_config.out / (name + suffix)
        ).read_bytes()
    manifests = [json.loads((out / "manifest.json").read_text())
                 for out in (synthetic.out, csv_config.out)]
    assert manifests[1]["okved_ref"] == csv_reference.name
    for manifest in manifests:
        for key in ("created_at", "inputs", "okved_ref"):
            manifest.pop(key)
    assert manifests[0] == manifests[1]


def test_normalization_counts_source_codes_before_f7(
    tmp_path: Path, reference: Path, company: Callable, write_year: Callable,
) -> None:
    root = tmp_path / "data"
    codes = ["10.71", "10.71", "49.40", "68.20.20", "01.11.10", "01.11.99", "49.40.99",
             None, None, "", "junk", "99.99"]
    rows = [company(str(i), okved=code) for i, code in enumerate(codes)]
    rows.extend([
        company("missing-profit", okved="10.71", line_2200=None),
        company("f1", okved="junk", line_2110=0),
        company("f2", okved="junk", line_1300=None),
        company("f3", okved="junk", line_1300=-1, line_1530=2),
        company("f5", okved="junk", okopf="20000"),
    ])
    write_year(root, 2025, rows)
    config = Config(root, reference, tmp_path / "out", "test", years=(2025,))
    normalization = []
    with duckdb.connect() as con:
        Okved(reference).install(con)
        funnel = load_year(con, config, 2025, normalization)
    assert normalization == [
        (2026, "exact", 3, 1), (2026, "sub_pad", 1, 1), (2026, "subgroup_pad", 2, 2),
        (2026, "group_prefix", 1, 1), (2026, "sub_zero", 1, 1),
        (2026, "empty", 3, 1), (2026, "unmatched", 2, 2),
    ]
    assert sum(row[2] for row in normalization) == next(
        n for _, sec, step, n in funnel if sec == "total" and step == "f5_opf"
    ) == 13


def test_write_json_new_is_atomic(tmp_path: Path, monkeypatch) -> None:
    from ilmare_edition.marts import write_json_new

    def fail_fsync(descriptor):
        raise OSError("fsync failed")

    monkeypatch.setattr("ilmare_edition.marts.os.fsync", fail_fsync)
    out = tmp_path / "x.json"
    with pytest.raises(OSError, match="fsync failed"):
        write_json_new(out, {"value": 1})
    assert not out.exists()
    assert not list(tmp_path.glob(".x.json.*.tmp"))


def test_write_json_new_keeps_foreign_file(tmp_path: Path, monkeypatch) -> None:
    import os

    from ilmare_edition.marts import write_json_new

    original = os.link
    out = tmp_path / "x.json"

    def link(source, destination):
        out.write_bytes(b"other")
        original(source, destination)

    monkeypatch.setattr("ilmare_edition.marts.os.link", link)
    with pytest.raises(FileExistsError):
        write_json_new(out, {"value": 1})
    assert out.read_bytes() == b"other"
    assert not list(tmp_path.glob(".x.json.*.tmp"))


def test_write_json_new_bytes_match_write_json(tmp_path: Path) -> None:
    from ilmare_edition.marts import write_json, write_json_new

    value = {"text": "Название", "values": [None, True, 0, 1.25], "nested": {"b": 2, "a": 1}}
    ordinary, atomic = tmp_path / "ordinary.json", tmp_path / "atomic.json"
    write_json(ordinary, value)
    write_json_new(atomic, value)
    assert atomic.read_bytes() == ordinary.read_bytes()
    assert atomic.stat().st_mode == ordinary.stat().st_mode
    assert not list(tmp_path.glob(".atomic.json.*.tmp"))


def test_write_json_new_syncs_directory(tmp_path: Path, monkeypatch) -> None:
    import os
    import stat

    from ilmare_edition.marts import write_json_new

    original = os.fsync
    calls = []

    def fsync(descriptor):
        calls.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        original(descriptor)

    monkeypatch.setattr("ilmare_edition.marts.os.fsync", fsync)
    write_json_new(tmp_path / "x.json", {"value": 1})
    assert calls == [False, True]
