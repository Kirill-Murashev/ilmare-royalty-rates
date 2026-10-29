"""Manual formula references and synthetic benchmark/calibration contracts."""

import csv
import hashlib
import json
import math
from pathlib import Path

import pytest

from ilmare_edition.calibration import (
    COLUMNS,
    COMPUTED_KEYS,
    CellLookup,
    aggregate,
    calibrate,
    compute_values,
    factors,
    norm_case,
    number,
    observations,
    parse_benchmarks,
    read_benchmarks,
    select_rows,
    validate_model,
)
from ilmare_edition.cli import main
from ilmare_edition.config import DEFAULT_MODEL
from ilmare_edition.marts import sha256


def row(**changes):
    result = dict.fromkeys(COLUMNS, "")
    result.update(
        source_type="literature",
        source="A40-123/20",
        verified="yes",
        base="revenue",
        rate_mid="0.05",
        data_year="2026",
        okved2="10.71",
        ip_type="trademark",
        license="unknown",
        value_hint="core",
    )
    return {**result, **changes}


def model(version=2):
    return json.loads((DEFAULT_MODEL.parent / f"share_model_v{version}.json").read_bytes())


def lookup():
    return CellLookup(
        [
            {
                "t": 2026,
                "level": "grp",
                "code": "10.71",
                "prec_q3": 2,
                "q3": 0.20,
                "median": 0.08,
                "wmean": 0.10,
            },
        ],
        [{"code": "10", "level": "cls", "section": "C"}],
    )


def test_manual_formula_reference():
    rows = select_rows(
        [
            row(),
            row(rate_mid="0.03", ip_type="patent_knowhow", license="excl", value_hint="unknown"),
            row(rate_mid="0.04", ip_type="mixed", license="nonexcl", value_hint="valuable"),
        ]
    )
    obs = observations(rows, lookup(), model())
    assert factors(rows[0], model()) == pytest.approx((0.75, 0.30))
    assert obs["q3"] == pytest.approx([0.8333333333, 0.3418803419, 0.6060606061], abs=1e-7)
    assert obs["median"][0] == pytest.approx(2.0833333333, abs=1e-7)
    assert obs["wmean"][0] == pytest.approx(1.6666666667, abs=1e-7)
    assert obs["ratio"][0] == 0.625
    assert obs["share"][0] == pytest.approx(0.3333333333, abs=1e-7)
    assert aggregate(obs["q3"]) == pytest.approx(0.6060606061, abs=1e-7)
    assert aggregate(obs["q3"], spread=True) == pytest.approx(0.3686347, abs=1e-7)
    assert round(aggregate(obs["q3"]), 2) == 0.61
    assert round(aggregate(obs["q3"], spread=True), 3) == 0.369
    assert factors(rows[1], model())[1] == pytest.approx(0.43875)
    assert factors(rows[2], model())[1] == 0.33
    assert factors(rows[1], model(1))[0] == pytest.approx(1.2)
    assert aggregate([1.0, 9.0]) == 5.0  # Median of ratios, not geometric mean.


def test_franchise_variants():
    franchise = select_rows(
        [row(source_type="franchise", ip_type="tm_knowhow", value_hint="normal")]
    )[0]
    assert factors(franchise, model())[1] == 0.1875
    assert observations([franchise], lookup(), model())["q3"] == pytest.approx([1.3333333333])
    assert factors(franchise, model(), "rule")[1] == pytest.approx(0.405)
    assert observations([franchise], lookup(), model(), variant="rule")["q3"] == pytest.approx(
        [0.6172839506], abs=1e-7
    )
    court = {**franchise, "source_type": "court"}
    assert factors(court, model(), "rule") == factors(court, model())
    valuable = {**franchise, "value_hint": "core"}
    assert factors(valuable, model(), "valuable")[1] == pytest.approx(0.2475)
    assert observations([valuable], lookup(), model(), variant="valuable")["q3"] == pytest.approx(
        [1.0101010101], abs=1e-7
    )
    invention = {**valuable, "ip_type": "invention"}
    assert factors(invention, model(), "valuable") == factors(invention, model())
    assert factors({**court, "ip_type": "patent_knowhow", "license": "nonexcl"}, model(1))[0] == 1.0


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"verified": "no"}, None),
        ({"base": "other"}, None),
        ({"base": ""}, 0.05),
        ({"rate_mid": "", "rate_low": "0.02", "rate_high": "0.08"}, 0.04),
        ({"rate_mid": "", "rate_low": "-0.02", "rate_high": "0.08"}, 0.03),
        ({"rate_mid": "", "rate_low": "0", "rate_high": "0.08"}, 0.04),
        ({"rate_mid": "0"}, None),
        ({"rate_mid": "-1"}, None),
        ({"rate_mid": "0,03"}, 0.03),
        ({"source_type": "court", "notes": "спор с ИФНС"}, None),
        ({"source_type": "court", "source": "внутригрупповой налог"}, None),
    ],
)
def test_selection(changes, expected):
    selected = select_rows([row(**changes)])
    if expected is None:
        assert selected == []
    else:
        assert selected[0]["rate"] == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "nan", "inf", "-inf", "not a number"])
def test_non_numbers(text):
    assert number(text) is None


def test_search_year_levels_and_own_values():
    cells = [
        {"t": t, "level": level, "code": code, "prec_q3": 2, "q3": 0.2}
        for t in (2025, 2026)
        for level, code in (("grp", "10.71"), ("sub", "10.7"), ("cls", "10"), ("sec", "C"))
    ]
    search = CellLookup(cells, [{"level": "cls", "code": "10", "section": "C"}])
    assert search.find(row())["level"] == "grp"
    assert search.find(row(data_year="2025"))["level"] == "cls"
    assert search.find(row(data_year=""))["t"] == 2026
    assert search.find(row(data_year="1900"))["t"] == 2026
    assert search.find(row(data_year="junk"))["t"] == 2026
    assert search.code_at("C", "sec") == search.code_at("10.71", "sec") == "C"
    cells[4]["prec_q3"] = 0
    assert search.find(row())["level"] == "sub"
    cells[5]["q3"] = 0
    assert search.find(row())["level"] == "cls"
    cells[6]["q3"] = -1
    assert search.find(row())["level"] == "sec"
    assert search.find(row(), "grp") is None
    cells[0]["prec_q3"] = cells[2]["prec_q3"] = 0
    assert search.find(row(data_year="2025"))["level"] == "sec"
    assert search.code_at("1", "cls") is None
    assert search.find(row(okved2="99.99")) is None


def test_combined_set_deduplicates_before_selection():
    ru = [
        row(source_type=source, value_hint=tier, ip_type="tm_knowhow")
        for source in ("franchise", "literature", "court")
        for tier in ("low", "normal", "valuable", "core")
    ]
    ru.append(row(source_type="court", verified="no"))
    elb = [
        row(source_type="court", source="дело A40-123/20", verified="no"),
        row(source_type="court", source="А40-456/2020"),
    ]
    cells = [
        {
            "t": 2026,
            "level": level,
            "code": code,
            "prec_q3": 2,
            "q3": 0.2,
            "median": 0.08,
            "wmean": 0.1,
        }
        for level, code in (("cls", "10"), ("grp", "10.71"))
    ]
    search = CellLookup(cells, [{"level": "cls", "code": "10", "section": "C"}])
    values = compute_values(ru, [row()], elb, search, model())
    assert norm_case("дело A40-123/20") == norm_case("А40-123/2020")
    assert values["elb_dups_removed"] == 5
    assert values["fin_cls_points"] == values["fin_grp_points"] == 9
    assert values["elb_obs"] == values["elb_obs_cases"] == 1


@pytest.mark.parametrize("mutation", ["version", "tiers", "k_ip", "k_license", "nan", "zero"])
def test_invalid_model(mutation):
    data = model()
    if mutation in ("version", "tiers", "k_ip", "k_license"):
        data.pop(mutation)
    else:
        data["k_ip"]["invention"] = math.nan if mutation == "nan" else 0
    with pytest.raises(ValueError):
        validate_model(data)


def test_file_contract_and_cli(built_marts: Path, calibration_file: Path, tmp_path: Path):
    data = json.loads(calibration_file.read_bytes())
    assert len(COMPUTED_KEYS) == 45 and list(COMPUTED_KEYS) == sorted(COMPUTED_KEYS)
    assert set(data["values"]) == set(COMPUTED_KEYS)
    assert data["values"]["sec_q3_k"]["A"]["n"] >= 5
    assert data["values"]["calib_mult_q3_ru"] != data["values"]["calib_mult_q3_ru_rule"]
    assert data["values"]["fin_cls_q3"] != data["values"]["fin_cls_q3_rule"]
    paths = [calibration_file.parent / item["name"] for item in data["inputs"]]
    assert [item["role"] for item in data["inputs"]] == ["ru", "fx", "elbereth_cases"]
    for path, item in zip(paths, data["inputs"], strict=True):
        assert item["sha256"] == sha256(path)
        assert item["rows"] == len(read_benchmarks(path))
    second = tmp_path / "second.json"
    calibrate(built_marts, *paths, second, code_version="test")
    assert second.read_bytes() == calibration_file.read_bytes()
    with pytest.raises(ValueError, match="--out"):
        calibrate(built_marts, *paths, second)
    args = [
        "calibrate",
        "--marts",
        str(built_marts),
        "--benchmarks-ru",
        str(paths[0]),
        "--benchmarks-fx",
        str(paths[1]),
        "--elbereth-cases",
        str(paths[2]),
        "--out",
        str(tmp_path / "cli.json"),
        "--code-version",
        "test",
    ]
    assert main(args) == 0
    assert main(args) == 2
    ru = [r for r in read_benchmarks(paths[0]) if r["source_type"] != "literature"]
    with paths[0].open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, delimiter=";")
        writer.writeheader()
        writer.writerows(ru)
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError) as error:
        calibrate(built_marts, *paths, missing)
    assert "k_med_literature" in str(error.value) and "n_literature" in str(error.value)
    assert not missing.exists()


def test_missing_column_and_whitespace(tmp_path: Path):
    path = tmp_path / "rows.csv"
    path.write_text("id;source\nx;y\n")
    with pytest.raises(ValueError, match="source_type"):
        read_benchmarks(path)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, delimiter=";")
        writer.writeheader()
        writer.writerow(row(source=" source ", rate_mid=" 0,03 "))
    assert read_benchmarks(path)[0]["source"] == "source"
    assert select_rows(read_benchmarks(path))[0]["rate"] == 0.03


def test_calibrate_hashes_parsed_bytes(built_marts, calibration_file, tmp_path, monkeypatch):
    import ilmare_edition.calibration as module

    data = json.loads(calibration_file.read_bytes())
    paths = [calibration_file.parent / item["name"] for item in data["inputs"]]
    raw = paths[0].read_bytes()
    original = module.compute_values

    def compute(*args):
        result = original(*args)
        paths[0].write_bytes(raw + raw.splitlines(keepends=True)[1])
        return result

    monkeypatch.setattr(module, "compute_values", compute)
    out = calibrate(built_marts, *paths, tmp_path / "new.json", code_version="test")
    item = json.loads(out.read_bytes())["inputs"][0]
    assert item["sha256"] == hashlib.sha256(raw).hexdigest()
    assert item["rows"] == data["inputs"][0]["rows"]


def test_unsafe_input_name(built_marts, calibration_file, tmp_path):
    data = json.loads(calibration_file.read_bytes())
    paths = [calibration_file.parent / item["name"] for item in data["inputs"]]
    unsafe = tmp_path / "ru data}.csv"
    unsafe.write_bytes(paths[0].read_bytes())
    out = tmp_path / "unsafe.json"
    with pytest.raises(ValueError, match="unsafe input file name"):
        calibrate(built_marts, unsafe, *paths[1:], out)
    assert not out.exists()


def test_calibrate_refuses_dangling_output(built_marts, calibration_file, tmp_path):
    data = json.loads(calibration_file.read_bytes())
    paths = [calibration_file.parent / item["name"] for item in data["inputs"]]
    target = tmp_path / "target.json"
    out = tmp_path / "link.json"
    out.symlink_to(target)
    with pytest.raises(ValueError, match="--out"):
        calibrate(built_marts, *paths, out)
    assert not target.exists()
    assert main([
        "calibrate", "--marts", str(built_marts), "--benchmarks-ru", str(paths[0]),
        "--benchmarks-fx", str(paths[1]), "--elbereth-cases", str(paths[2]), "--out", str(out),
    ]) == 2
    assert not target.exists()


def test_parse_benchmarks_matches_file(calibration_file):
    data = json.loads(calibration_file.read_bytes())
    for item in data["inputs"]:
        path = calibration_file.parent / item["name"]
        raw = path.read_bytes()
        expected = read_benchmarks(path)
        assert parse_benchmarks(raw, path.name) == expected
        lf = raw.replace(b"\r\n", b"\n")
        assert parse_benchmarks(lf, path.name) == expected
        assert parse_benchmarks(lf.replace(b"\n", b"\r\n"), path.name) == expected


def test_calibrate_reads_model_once(built_marts, calibration_file, tmp_path, monkeypatch):
    import ilmare_edition.calibration as module

    data = json.loads(calibration_file.read_bytes())
    paths = [calibration_file.parent / item["name"] for item in data["inputs"]]
    original = module.verify_marts_snapshot

    def verify(marts):
        result = original(marts)
        changed = json.loads((marts / "share_model.json").read_bytes())
        changed["version"] = "changed"
        (marts / "share_model.json").write_text(json.dumps(changed), encoding="utf-8")
        return result

    monkeypatch.setattr(module, "verify_marts_snapshot", verify)
    out = tmp_path / "changed.json"
    with pytest.raises(ValueError, match="share_model.json"):
        calibrate(built_marts, *paths, out)
    assert not out.exists()


def test_validate_model_huge_integer():
    data = model()
    data["k_ip"]["trademark"] = 10**400
    with pytest.raises(ValueError):
        validate_model(data)


def test_calibrate_refuses_marts_changed_after_read(
    built_marts, calibration_file, tmp_path, monkeypatch,
):
    import ilmare_edition.calibration as module

    data = json.loads(calibration_file.read_bytes())
    paths = [calibration_file.parent / item["name"] for item in data["inputs"]]
    original = module.compute_values

    def compute(*args):
        result = original(*args)
        with (built_marts / "royalty.parquet").open("ab") as stream:
            stream.write(b"x")
        (built_marts / "SHA256SUMS").write_text("".join(
            f"{sha256(path)}  {path.name}\n"
            for path in sorted(built_marts.iterdir()) if path.name != "SHA256SUMS"
        ), encoding="utf-8")
        return result

    monkeypatch.setattr(module, "compute_values", compute)
    out = tmp_path / "changed.json"
    with pytest.raises(ValueError, match="marts changed"):
        calibrate(built_marts, *paths, out)
    assert not out.exists()


def test_calibrate_sums_digest_from_verified_bytes(built_marts, calibration_file, tmp_path):
    from ilmare_edition.marts_io import verify_marts_snapshot

    data = json.loads(calibration_file.read_bytes())
    paths = [calibration_file.parent / item["name"] for item in data["inputs"]]
    expected = hashlib.sha256(verify_marts_snapshot(built_marts)[2]).hexdigest()
    out = calibrate(built_marts, *paths, tmp_path / "new.json", code_version="test")
    assert json.loads(out.read_bytes())["marts_sha256sums"] == expected
