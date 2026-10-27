"""Independent recomputation of edition cells from RFSD (written 2026-10-24, separately from ilmare_edition).

Written from the published methodology (sections 4–6 and appendix D of the reference: filters Ф1–Ф5, Ф7; margin
2200/2110; normalisation rules 1–5; tax-code quartiles of art. 105.8 p. 4; binomial order-statistic CI with the
normal approximation above n = 5000), not from the edition code. Reads RFSD parquet read-only and the Rosstat OKVED2
CSV; prints one JSON line per sampled cell to stdout.

Usage: python -I xref_cells.py DATA_DIR ROSSTAT_CSV KEYS_JSON [FILE_PATTERN]
FILE_PATTERN — path of a year file under DATA_DIR, default RFSD/year={y}/part-0.parquet (Hugging Face layout).
KEYS_JSON — [[t, level, code], ...]; t is the rate year, statements are for t - 1.
"""

import csv
import json
import math
import os
import statistics
import sys
from collections import defaultdict

import duckdb

PATTERN = "RFSD/year={y}/part-0.parquet"
FIRST_YEAR = 2011  # statement years of the edition input: 2011-2025 (manifest "years")


def read_reference(path):
    codes, section_of = set(), {}
    with open(path, encoding="utf-8-sig", newline="") as stream:
        for row in csv.reader(stream, delimiter=";"):
            if not row:
                continue
            section, code, _name = (field.strip() for field in row)
            code = code or section
            codes.add(code)
            section_of[code] = section
    return codes, section_of


def normalise(code, ref):
    """Appendix D, rules 1-5 in order; None — not recognised."""
    if code is None or code == "":
        return None
    if code in ref:
        return code
    if len(code) == 5 and code[-1] == "0" and code[:4] in ref:
        return code[:4]
    if len(code) == 8 and code[-1] == "0" and code[:7] in ref:
        return code[:7]
    if len(code) == 8 and code[:5] in ref:
        return code[:5]
    if len(code) == 8 and code[4] == "0" and code[:4] in ref:
        return code[:4]
    return None


def tax_quantile(ordered, p):
    """Art. 105.8 p. 4 NK: rank n*p rounded up; for an integer n*p — half-sum of ranks n*p and n*p + 1."""
    n = len(ordered)
    if n == 1:
        return ordered[0]
    k = n * p
    if k == int(k):
        k = int(k)
        return (ordered[k - 1] + ordered[k]) / 2
    return ordered[math.ceil(k) - 1]


def ci_ranks(n, p):
    """1-based ranks [l, u]: l = max j with P(B <= j-1) <= 0.025, u = min k with P(B <= k-1) >= 0.975."""
    if n > 5000:
        centre, delta = n * p + 0.5, 1.96 * math.sqrt(n * p * (1 - p))
        return max(1, math.floor(centre - delta)), min(n, math.ceil(centre + delta))
    cdf, acc = [], 0.0
    for k in range(n + 1):
        acc += math.exp(math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
                        + k * math.log(p) + (n - k) * math.log(1 - p))
        cdf.append(acc)
    lows = [j for j in range(1, n + 1) if cdf[j - 1] <= 0.025]
    highs = [k for k in range(1, n + 1) if cdf[k - 1] >= 0.975]
    if not lows or not highs:
        return None
    return lows[-1], highs[0]


def main():
    data, ref_path, keys_path = sys.argv[1:4]
    pattern = sys.argv[4] if len(sys.argv) > 4 else PATTERN
    ref, section_of = read_reference(ref_path)
    keys = [tuple(key) for key in json.load(open(keys_path, encoding="utf-8"))]
    by_year = defaultdict(list)
    for t, level, code in keys:
        by_year[t].append((level, code))
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='3GB'; PRAGMA threads=3")
    for t in sorted(by_year):
        year = t - 1
        path = lambda y: os.path.join(data, pattern.format(y=y))
        con.execute(f"""CREATE OR REPLACE TEMP TABLE b AS SELECT inn, okved, CAST(okopf AS VARCHAR) okopf,
            coalesce(line_2110, 0) rev, line_2200 op, line_2400 np, line_1300 cap, line_1530 ltl
            FROM read_parquet(?)""", [path(year)])
        previous = [path(y) for y in (year - 1, year - 2)]
        if year - 2 >= FIRST_YEAR and all(os.path.exists(p) for p in previous):
            loss = ("np < 0 AND inn IN (SELECT inn FROM read_parquet(?) WHERE line_2400 < 0) "
                    "AND inn IN (SELECT inn FROM read_parquet(?) WHERE line_2400 < 0)")
            loss_params = previous
        else:
            loss, loss_params = "FALSE", []
        okveds = [row[0] for row in con.execute("SELECT DISTINCT okved FROM b").fetchall()]
        mapping = []
        for code in okveds:
            nk = normalise(code, ref)
            sec = section_of.get(nk[:2]) if nk is not None and nk[:2] in ref else None
            mapping.append((code, nk, sec))
        con.execute("CREATE OR REPLACE TEMP TABLE map(okved VARCHAR, nk VARCHAR, sec VARCHAR)")
        con.executemany("INSERT INTO map VALUES (?, ?, ?)", mapping)
        con.execute(f"""CREATE OR REPLACE TEMP TABLE s AS
            SELECT b.rev, b.op, b.op / b.rev m, map.nk, map.sec FROM b JOIN map ON map.okved IS NOT DISTINCT FROM b.okved
            WHERE b.rev > 0 AND b.cap + coalesce(b.ltl, 0) > 0 AND b.cap > 0 AND NOT coalesce(({loss}), FALSE)
              AND left(b.okopf, 1) IN ('1', '6') AND map.sec IS NOT NULL AND b.op IS NOT NULL""", loss_params)
        where = {
            "all": "TRUE", "sec": "sec = ?", "cls": "left(nk, 2) = ?",
            "sub": "length(nk) >= 4 AND left(nk, 4) = ?", "grp": "length(nk) >= 5 AND left(nk, 5) = ?",
        }
        for level, code in by_year[t]:
            params = [] if level == "all" else [code]
            rows = con.execute(f"SELECT m, op, rev FROM s WHERE {where[level]} ORDER BY m", params).fetchall()
            ordered = [row[0] for row in rows]
            n = len(ordered)
            out = {"t": t, "level": level, "code": code, "n": n}
            if n:
                out.update(median=statistics.median(ordered), q1=tax_quantile(ordered, 0.25),
                           q3=tax_quantile(ordered, 0.75),
                           wmean=math.fsum(row[1] for row in rows) / math.fsum(row[2] for row in rows))
                for name, p in (("median", 0.5), ("q3", 0.75)):
                    ranks = ci_ranks(n, p)
                    out[f"{name}_lo"] = ordered[ranks[0] - 1] if ranks else None
                    out[f"{name}_hi"] = ordered[ranks[1] - 1] if ranks else None
            print(json.dumps(out), flush=True)
        sys.stderr.write(f"t={t} done\n")


main()
