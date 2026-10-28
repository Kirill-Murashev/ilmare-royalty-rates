from pathlib import Path

import duckdb
import pytest

from ilmare_edition.okved import NORMALIZE_RULE_SQL, NORMALIZE_SQL, Okved


@pytest.mark.parametrize(("code", "expected"), [
    ("49.40", "49.4"), ("68.20.20", "68.20.2"), ("01.11.10", "01.11.1"),
    ("10.71", "10.71"), ("01.11.99", "01.11"), ("49.40.99", "49.4"),
    ("junk", None), (None, None), ("99.99", None),
])
def test_normalization(reference: Path, code: str | None, expected: str | None) -> None:
    ref = Okved(reference)
    assert ref.normalize(code) == expected
    with duckdb.connect() as con:
        ref.install(con)
        actual = con.execute(f"SELECT {NORMALIZE_SQL} FROM (SELECT ?::VARCHAR okved)",
                             [code]).fetchone()[0]
        assert actual == expected


def test_reference_code_wins(reference: Path) -> None:
    ref = Okved(reference)
    ref.records["49.40"] = {"section": "H", "name": "existing"}
    assert ref.normalize("49.40") == "49.40"


def test_parents(reference: Path) -> None:
    ref = Okved(reference)
    assert ref.parent("10.71", "grp") == "10"
    assert ref.parent("10", "cls") == "C"
    assert ref.parent("C", "sec") is None
    assert ref.parent("01.11", "grp") == "01.1"
    assert ref.parent("01.1", "sub") == "01"


def test_rosstat_csv_matches_json(reference: Path, csv_reference: Path) -> None:
    assert csv_reference.read_bytes().startswith(b"\xef\xbb\xbf\r\n")
    assert Okved(csv_reference).records == Okved(reference).records


def test_rosstat_csv_strips_fields(tmp_path: Path) -> None:
    path = tmp_path / "reference.csv"
    path.write_text('" A ";" 01 ";" Name "\r\n', encoding="utf-8")
    assert Okved(path).records == {"01": {"code": "01", "section": "A", "name": "Name"}}


@pytest.mark.parametrize(("row", "message"), [
    ('"A";"01"', "invalid OKVED2 CSV record 3: expected 3 fields"),
    ('"a";"01";"name"', "invalid OKVED2 CSV record 3: section"),
    ('"AB";"01";"name"', "invalid OKVED2 CSV record 3: section"),
    ('"A";"01";" "', "invalid OKVED2 CSV record 3: name"),
    ('"A";"01";"name"', "duplicate OKVED2 code 01 on record 3"),
    ('"A";" ";"name"', "duplicate OKVED2 code A on record 3"),
])
def test_invalid_rosstat_csv(tmp_path: Path, row: str, message: str) -> None:
    path = tmp_path / "reference.csv"
    path.write_text('"A";" ";"section"\n"A";"01";"name"\n' + row, encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        Okved(path)
    assert str(exc.value) == message


@pytest.mark.parametrize(("suffix", "content"), [("csv", ""), ("csv", "\n"), ("json", "[]")])
def test_empty_reference(tmp_path: Path, suffix: str, content: str) -> None:
    path = tmp_path / f"reference.{suffix}"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="^OKVED2 reference is empty$"):
        Okved(path)


@pytest.mark.parametrize(("code", "expected"), [
    ("49.40", "sub_pad"), ("68.20.20", "subgroup_pad"), ("01.11.10", "subgroup_pad"),
    ("10.71", "exact"), ("01.11.99", "group_prefix"), ("49.40.99", "sub_zero"),
    ("junk", "unmatched"), ("99.99", "unmatched"), (None, "empty"), ("", "empty"),
])
def test_normalization_rule(reference: Path, code: str | None, expected: str) -> None:
    with duckdb.connect() as con:
        Okved(reference).install(con)
        rule, normalized = con.execute(
            f"SELECT {NORMALIZE_RULE_SQL}, {NORMALIZE_SQL} FROM (SELECT ?::VARCHAR okved)",
            [code],
        ).fetchone()
    assert rule == expected
    assert (rule not in {"empty", "unmatched"}) == (normalized is not None)


def test_csv_field_limit_is_value_error(tmp_path: Path) -> None:
    path = tmp_path / "reference.csv"
    path.write_text('"A";" ";"section"\n"A";"01";"' + "x" * 200000 + '"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="^invalid OKVED2 CSV record 2"):
        Okved(path)


def test_csv_error_reports_record_not_line(tmp_path: Path) -> None:
    path = tmp_path / "reference.csv"
    path.write_text('"A";" ";"section"\n"A";"01";"multi\nline"\n"A";"02"', encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        Okved(path)
    assert str(exc.value) == "invalid OKVED2 CSV record 3: expected 3 fields"


@pytest.mark.parametrize("content", [
    "{}", "[1]", '[{"name": "x"}]', '[{"code": "01"}]', '[{"code": 1, "name": "x"}]',
    "[" * 100000 + "]" * 100000,
])
def test_invalid_json_reference(tmp_path: Path, content: str) -> None:
    path = tmp_path / "reference.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        Okved(path)
