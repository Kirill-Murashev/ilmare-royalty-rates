"""Read one statement year and apply the fixed cumulative selection funnel."""

import duckdb

from .config import Config
from .okved import NORMALIZATION_RULES, NORMALIZE_RULE_SQL, NORMALIZE_SQL

FILTERS = (
    ("all", "TRUE"),
    ("f1_rev", "rev > 0"),
    ("f2_na", "na > 0"),
    ("f3_cap", "cap > 0"),
    ("f4_loss3", "NOT loss3"),
    ("f5_opf", "left(okopf, 1) IN ('1', '6')"),
    ("f7_okved", "nk IS NOT NULL AND sec IS NOT NULL AND op IS NOT NULL"),
)


def load_year(con: duckdb.DuckDBPyConnection, config: Config, year: int,
              normalization: list[tuple] | None = None) -> list[tuple]:
    """Materialize selected rows in s; return section and total counts at every step."""
    con.execute("""CREATE OR REPLACE TEMP TABLE b AS
        SELECT cast(inn AS VARCHAR) inn, cast(okved AS VARCHAR) okved,
            cast(okopf AS VARCHAR) okopf, simplified,
            cast(line_2110 AS DOUBLE) rev, cast(line_2200 AS DOUBLE) op,
            cast(line_2300 AS DOUBLE) + abs(coalesce(cast(line_2330 AS DOUBLE), 0)) ebit,
            line_2400 np, line_1300 cap,
            cast(line_1300 AS DOUBLE) + coalesce(cast(line_1530 AS DOUBLE), 0) na
        FROM read_parquet(?)""", [str(config.input_path(year))])
    loss = "b.np < 0"
    for index, previous in enumerate((year - 1, year - 2)):
        path = config.input_path(previous)
        if not path.is_file():
            loss = "FALSE"
            break
        # EXISTS preserves source multiplicity even when the historical file repeats an INN.
        con.execute(f"""CREATE OR REPLACE TEMP TABLE loss{index} AS
            SELECT DISTINCT cast(inn AS VARCHAR) inn
            FROM read_parquet(?) WHERE line_2400 < 0""", [str(path)])
        loss += f" AND EXISTS (SELECT 1 FROM loss{index} p WHERE p.inn = b.inn)"
    con.execute(f"""CREATE OR REPLACE TEMP TABLE normalized AS
        SELECT b.*, coalesce(({loss}), FALSE) loss3, {NORMALIZE_SQL} nk FROM b""")
    if normalization is not None:
        conditions = []
        for step, condition in FILTERS:
            conditions.append(condition)
            if step == "f5_opf":
                break
        where = " AND ".join(f"({item})" for item in conditions)
        rule_counts = {
            rule: (rows, codes) for rule, rows, codes in con.execute(f"""
                SELECT {NORMALIZE_RULE_SQL}, count(*), count(DISTINCT okved)
                FROM normalized WHERE {where} GROUP BY 1""").fetchall()
        }
        normalization.extend(
            (year + 1, rule, *rule_counts.get(rule, (0, 0))) for rule in NORMALIZATION_RULES
        )
    con.execute("""CREATE OR REPLACE TEMP TABLE selected AS
        SELECT b.*, r.section sec FROM normalized b
        LEFT JOIN ref r ON r.code = left(b.nk, 2)""")
    sections = [row[0] for row in con.execute(
        "SELECT DISTINCT coalesce(sec, '?') FROM selected ORDER BY 1"
    ).fetchall()]
    funnel, conditions = [], []
    for step, condition in FILTERS:
        conditions.append(condition)
        where = " AND ".join(f"({item})" for item in conditions)
        counts = dict(con.execute(f"""SELECT coalesce(sec, '?'), count(*)
            FROM selected WHERE {where} GROUP BY 1""").fetchall())
        funnel.extend((year + 1, sec, step, counts.get(sec, 0)) for sec in sections)
        funnel.append((year + 1, 'total', step, sum(counts.values())))
    con.execute(f"""CREATE OR REPLACE TEMP TABLE s AS
        SELECT inn, rev, op, ebit, simplified, op / rev m, ebit / rev me,
            sec, left(nk, 2) cls, 'ALL' all_code,
            CASE WHEN length(nk) >= 4 THEN left(nk, 4) END sub,
            CASE WHEN length(nk) >= 5 THEN left(nk, 5) END grp,
            CASE WHEN rev < 120000 THEN 'micro' WHEN rev < 800000 THEN 'small'
                 WHEN rev < 2000000 THEN 'medium' ELSE 'large' END size_class
        FROM selected WHERE {where}""")
    for name in ("b", "normalized", "selected", "loss0", "loss1"):
        con.execute(f"DROP TABLE IF EXISTS {name}")
    return funnel
