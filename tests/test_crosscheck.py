"""Independent-value comparison and crosscheck-map validation on synthetic marts."""

import csv
import hashlib
import importlib
import json
from pathlib import Path

import pytest

from ilmare_edition.cli import main
from ilmare_edition.crosscheck import COLUMNS, crosscheck, validate_crosscheck
from ilmare_edition.marts import sha256, write_json
from ilmare_edition.marts_io import read_rows


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def independent_rows(marts):
    cells = read_rows(marts, "cells")
    return [
        {
            "table": "cells",
            "t": cell["t"],
            "level": cell["level"],
            "code": cell["code"],
            "base": "",
            "tier": "",
            "field": "q3",
            "value": cell["q3"],
            "tolerance": 0,
        }
        for level in ("sec", "cls", "sub", "grp")
        for cell in [next(c for c in cells if c["t"] == 2014 and c["level"] == level)]
    ]


def test_summary_boundary_nulls_determinism(built_marts: Path, tmp_path: Path):
    rows = independent_rows(built_marts)
    # Integer count gives an exactly representable boundary difference.
    count = read_rows(built_marts, "cells")[0]
    rows.append(
        {
            **rows[0],
            "t": count["t"],
            "level": count["level"],
            "code": count["code"],
            "field": "n",
            "value": count["n"] + 1,
            "tolerance": 1,
        }
    )
    nullable = next(r for r in read_rows(built_marts, "cells") if r["q3_lo"] is None)
    null_row = {key: nullable[key] for key in ("t", "level", "code")}
    rows.append(
        {
            **null_row,
            "table": "cells",
            "base": "",
            "tier": "",
            "field": "q3_lo",
            "value": "",
            "tolerance": 0,
        }
    )
    rows.append(
        {
            **null_row,
            "table": "cells",
            "base": "",
            "tier": "",
            "field": "median_lo",
            "value": 0.1,
            "tolerance": 0,
        }
    )
    royalty = read_rows(built_marts, "royalty")[0]
    rows.append(
        {
            **{key: royalty[key] for key in ("t", "level", "code", "base", "tier")},
            "table": "royalty",
            "field": "p50",
            "value": royalty["p50"],
            "tolerance": 0,
        }
    )
    rows.append({**rows[0], "field": "median", "value": ""})
    path = write_csv(tmp_path / "independent.csv", rows)
    out = crosscheck(built_marts, path, tmp_path / "map.json")
    data = json.loads(out.read_bytes())
    assert data["summary"]["checks"] == 9
    assert data["summary"]["failed"] == 2
    assert data["summary"]["ok"] == 7
    assert sum(data["summary"]["levels"].values()) == data["summary"]["cells"]
    boundary = next(c for c in data["checks"] if c["field"] == "n")
    assert boundary["abs_diff"] == 1 and boundary["ok"]
    nulls = [c for c in data["checks"] if c["field"] in ("q3_lo", "median_lo")]
    assert next(c for c in nulls if c["field"] == "q3_lo")["ok"]
    assert all(c["abs_diff"] is None for c in nulls)
    assert data["independent_sha256"] == sha256(path)
    assert data["marts_sha256sums"] == sha256(built_marts / "SHA256SUMS")
    second = crosscheck(built_marts, path, tmp_path / "second.json")
    assert second.read_bytes() == out.read_bytes()
    with pytest.raises(ValueError, match="--out"):
        crosscheck(built_marts, path, out)


@pytest.mark.parametrize(
    "change", ["duplicate", "field", "key", "tier", "base", "value", "tolerance"]
)
def test_invalid_csv_has_line_number(built_marts: Path, tmp_path: Path, change):
    rows = independent_rows(built_marts)[:1]
    if change == "duplicate":
        rows.append(dict(rows[0]))
    elif change == "field":
        rows[0]["field"] = "stmt_year"
    elif change == "key":
        rows[0]["code"] = "99.99"
    elif change == "tier":
        rows[0].update(table="royalty", base="q3", tier=4, field="p50")
    elif change == "base":
        rows[0]["base"] = "q3"
    elif change == "value":
        rows[0]["value"] = "nan"
    else:
        rows[0]["tolerance"] = -1
    path = write_csv(tmp_path / "bad.csv", rows)
    with pytest.raises(ValueError, match="line 3" if change == "duplicate" else "line 2"):
        crosscheck(built_marts, path, tmp_path / "bad.json")
    assert not (tmp_path / "bad.json").exists()


def test_cli_records_failure_and_success(built_marts: Path, tmp_path: Path, capsys):
    rows = independent_rows(built_marts)
    rows[0]["value"] += 1
    path = write_csv(tmp_path / "independent.csv", rows)
    out = tmp_path / "map.json"
    args = [
        "crosscheck", "--marts", str(built_marts), "--independent", str(path), "--out", str(out),
    ]
    assert main(args) == 2
    assert out.exists()
    assert "1 crosscheck discrepancies" in capsys.readouterr().err
    write_csv(path, independent_rows(built_marts))
    args[-1] = str(tmp_path / "ok.json")
    assert main(args) == 0


@pytest.mark.parametrize("change", [
    "failed", "marts", "edition", "summary", "sub", "extra", "code_version", "independent", "deep",
    "huge_independent", "huge_tolerance",
])
def test_validate_refuses_invalid_map(built_marts: Path, tmp_path: Path, change):
    rows = independent_rows(built_marts)
    if change == "failed":
        rows[0]["value"] += 1
    if change == "sub":
        rows = [row for row in rows if row["level"] != "sub"]
    path = write_csv(tmp_path / "independent.csv", rows)
    out = crosscheck(built_marts, path, tmp_path / "map.json")
    data = json.loads(out.read_bytes())
    if change == "marts":
        data["marts_sha256sums"] = "wrong"
    elif change == "edition":
        data["checks"][0]["edition"] += 1
    elif change == "summary":
        data["summary"]["checks"] += 1
    elif change == "extra":
        data["extra"] = 1
    elif change == "code_version":
        data["code_version"] = "other"
    elif change == "independent":
        data["independent_sha256"] = "x"
    elif change == "huge_independent":
        data["checks"][0]["independent"] = 10**400
    elif change == "huge_tolerance":
        data["checks"][0]["tolerance"] = 10**400
    if change == "deep":
        out.write_text("[" * 100000 + "]" * 100000)
    else:
        write_json(out, data)
    with pytest.raises(ValueError):
        validate_crosscheck(out, built_marts)


def test_validate_accepts_map(built_marts: Path, tmp_path: Path):
    path = write_csv(tmp_path / "independent.csv", independent_rows(built_marts))
    check = crosscheck(built_marts, path, tmp_path / "map.json")
    summary = validate_crosscheck(check, built_marts)
    assert summary == {
        "checks": 4,
        "ok": 4,
        "failed": 0,
        "cells": 4,
        "levels": {"sec": 1, "cls": 1, "sub": 1, "grp": 1, "all": 0},
    }


def test_crosscheck_hashes_parsed_bytes(built_marts, tmp_path, monkeypatch):
    module = importlib.import_module("ilmare_edition.crosscheck")
    rows = independent_rows(built_marts)
    path = write_csv(tmp_path / "independent.csv", rows)
    raw = path.read_bytes()
    original = module.check_value
    changed = False

    def check(*args):
        nonlocal changed
        if not changed:
            changed = True
            write_csv(path, [{**row, "value": 999} for row in rows])
        return original(*args)

    monkeypatch.setattr(module, "check_value", check)
    out = crosscheck(built_marts, path, tmp_path / "map.json")
    data = json.loads(out.read_bytes())
    assert data["independent_sha256"] == hashlib.sha256(raw).hexdigest()
    assert data["summary"]["failed"] == 0


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_crosscheck_refuses_existing_output(built_marts, tmp_path, kind):
    path = write_csv(tmp_path / "independent.csv", independent_rows(built_marts))
    target = tmp_path / "target.json"
    out = tmp_path / "map.json"
    if kind == "symlink":
        out.symlink_to(target)
    else:
        out.write_bytes(b"existing")
    with pytest.raises(ValueError, match="--out"):
        crosscheck(built_marts, path, out)
    assert main([
        "crosscheck", "--marts", str(built_marts), "--independent", str(path), "--out", str(out),
    ]) == 2
    assert not target.exists()
    if kind == "file":
        assert out.read_bytes() == b"existing"


def test_cli_crosscheck_does_not_reread_output(built_marts, tmp_path, monkeypatch, capsys):
    rows = independent_rows(built_marts)
    rows[0]["value"] += 1
    path = write_csv(tmp_path / "independent.csv", rows)
    out = tmp_path / "map.json"
    original = Path.read_bytes

    def read_bytes(self):
        if self == out:
            raise AssertionError("CLI must not reread output")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert main([
        "crosscheck", "--marts", str(built_marts), "--independent", str(path), "--out", str(out),
    ]) == 2
    assert "1 crosscheck discrepancies" in capsys.readouterr().err
    assert out.exists()


def test_crosscheck_refuses_marts_changed_after_read(built_marts, tmp_path, monkeypatch):
    module = importlib.import_module("ilmare_edition.crosscheck")
    path = write_csv(tmp_path / "independent.csv", independent_rows(built_marts))
    original = module.summarize

    def summarize(checks):
        result = original(checks)
        with (built_marts / "royalty.parquet").open("ab") as stream:
            stream.write(b"x")
        (built_marts / "SHA256SUMS").write_text("".join(
            f"{sha256(file)}  {file.name}\n"
            for file in sorted(built_marts.iterdir()) if file.name != "SHA256SUMS"
        ), encoding="utf-8")
        return result

    monkeypatch.setattr(module, "summarize", summarize)
    out = tmp_path / "changed.json"
    with pytest.raises(ValueError, match="marts changed"):
        crosscheck(built_marts, path, out)
    assert not out.exists()
