from pathlib import Path
from typing import Callable

import duckdb
import pytest

from ilmare_edition.config import Config
from ilmare_edition.load import load_year
from ilmare_edition.okved import Okved


def selected(config: Config) -> tuple[list[str], list[tuple]]:
    with duckdb.connect() as con:
        Okved(config.okved_ref).install(con)
        funnel = load_year(con, config, config.years[0])
        inns = [row[0] for row in con.execute("SELECT inn FROM s ORDER BY inn").fetchall()]
    return inns, funnel


def test_each_filter(tmp_path: Path, reference: Path, company: Callable,
                     write_year: Callable) -> None:
    root = tmp_path / "data"
    rows = [company("good"), company("f1", line_2110=None),
            company("f2", line_1300=None), company("f3", line_1300=-1, line_1530=2),
            company("f4", line_2400=-1), company("f5", okopf="20000"),
            company("f7", line_2200=None)]
    write_year(root, 2025, rows)
    for year in (2024, 2023):
        write_year(root, year, [company("f4", line_2400=-1)])
    config = Config(root, reference, tmp_path / "out", "test", years=(2025,))
    inns, funnel = selected(config)
    assert inns == ["good"]
    assert [n for _, sec, _, n in funnel if sec == "total"] == [7, 6, 5, 4, 3, 2, 1]


def test_rows_without_inn_are_dropped(tmp_path: Path, reference: Path, company: Callable,
                                      write_year: Callable) -> None:
    rows = [company("good"), company("f1", line_2110=None)]
    results = []
    for name, extra in (("plain", []), ("with_blank", [company(None), company("")])):
        root = tmp_path / name
        write_year(root, 2025, rows + extra)
        config = Config(root, reference, tmp_path / f"out-{name}", "test", years=(2025,))
        results.append(selected(config))
    assert results[0] == results[1]
    assert results[1][0] == ["good"]
    assert [n for _, sec, _, n in results[1][1] if sec == "total"] == [2, 1, 1, 1, 1, 1, 1]


@pytest.mark.parametrize(("changes", "excluded"), [
    ({"line_2110": 0}, True), ({"line_2110": None}, True),
    ({"line_2110": -1}, True), ({"line_1300": 0}, True),
    ({"line_1300": None, "line_1530": 100}, True),
    ({"okopf": None}, True), ({"okved": "junk"}, True), ({"line_2200": None}, True),
    ({"line_2200": -5}, False), ({"line_2400": None}, False),
    ({"line_1530": None}, False), ({"okopf": "60000"}, False),
])
def test_null_and_sign_semantics(tmp_path: Path, reference: Path, company: Callable,
                                 write_year: Callable, changes: dict, excluded: bool) -> None:
    root = tmp_path / "data"
    write_year(root, 2025, [company("x", **changes)])
    config = Config(root, reference, tmp_path / "out", "test", years=(2025,))
    assert selected(config)[0] == ([] if excluded else ["x"])


@pytest.mark.parametrize("missing_year", [2023, 2024])
def test_missing_file_vs_missing_company(tmp_path: Path, reference: Path,
                                       company: Callable, write_year: Callable,
                                       missing_year: int) -> None:
    root = tmp_path / "data"
    write_year(root, 2025, [company("loss", line_2400=-1), company("absent", line_2400=-1),
                            company("not_loss", line_2400=-1)])
    history = [company("loss", line_2400=-1), company("not_loss", line_2400=None)]
    write_year(root, 2023 if missing_year == 2024 else 2024, history)
    config = Config(root, reference, tmp_path / "out", "test", years=(2025,))
    assert selected(config)[0] == ["absent", "loss", "not_loss"]
    # All three reports must contain a loss for the same INN.
    write_year(root, missing_year, history)
    assert selected(config)[0] == ["absent", "not_loss"]
    # A present Y-2 file with no matching INN prevents exclusion.
    write_year(root, 2023, [company("other", line_2400=-1)])
    assert selected(config)[0] == ["absent", "loss", "not_loss"]


def test_historical_duplicates_do_not_multiply_rows(tmp_path: Path, reference: Path,
                                                   company: Callable, write_year: Callable) -> None:
    root = tmp_path / "data"
    write_year(root, 2025, [company("x")])
    write_year(root, 2024, [company("x", line_2400=-1), company("x", line_2400=-1)])
    config = Config(root, reference, tmp_path / "out", "test", years=(2025,))
    assert selected(config)[1][0][3] == 1
