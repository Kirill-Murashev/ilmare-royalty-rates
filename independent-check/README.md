# Independent check / Независимый пересчёт

These four scripts recompute a sample of edition cells and royalty quantiles **without importing
`ilmare_edition`**. They were written separately from the edition code, from the published
methodology only (filters Ф1–Ф5 and Ф7, margin 2200/2110, OKVED2 normalisation rules 1–5, tax-code
quartiles of art. 105.8 p. 4, binomial order-statistic confidence intervals, `R = max(M*, 0) × S`).
Their values are fed to `python -m ilmare_edition crosscheck`; agreement of the two implementations is
the evidence that the edition code does what the methodology says.

Четыре сценария пересчитывают выборку ячеек и квантилей ставки, не импортируя `ilmare_edition`.
Они написаны отдельно от кода издания — по тексту методологии. Их значения подаются команде
`crosscheck`; совпадение двух реализаций и есть проверка.

| Script | What it does |
|---|---|
| `make_keys.py` | fixed-seed stratified sample of cells (rate years 2012, 2013, 2019, 2026 × levels) from `cells.parquet` |
| `royalty_keys.py` | royalty keys (`base`, `tier`) present in `royalty.parquet` for the sampled cells |
| `xref_cells.py` | recomputes the sampled cells from raw RFSD: `n`, median, Q1, Q3, confidence limits, weighted mean |
| `build_independent.py` | independent royalty quantiles (numerical integration over the share distribution, not simulation) and the CSV for `crosscheck` |

The independence is deliberate: these scripts use only DuckDB (to read parquet), the standard
library and, for `build_independent.py`, NumPy and SciPy. Tolerances are fixed in the script before
any comparison: `n` — 0; other cell fields — `1e-12`; royalty quantile — `max(1e-4, 4 × SE)`, where SE
is the standard error of the Monte Carlo quantile of the edition (`--draws` of the edition).

## Run

Needs `pip install -e '.[independent-check]'` (SciPy). `MARTS` is the output of `build-marts`,
`RFSD` its `--data-root`, `OKVED` the Rosstat CSV.

```sh
python -I make_keys.py MARTS > keys.json
python -I royalty_keys.py MARTS keys.json > royalty_keys.json
python -I xref_cells.py RFSD OKVED keys.json > xref.jsonl          # optional 4th argument: file pattern
python -I build_independent.py xref.jsonl MARTS/share_model.json royalty_keys.json 100000 > independent.csv
python -m ilmare_edition crosscheck --marts MARTS --independent independent.csv --out crosscheck.json
```

`xref_cells.py` reads the statement year of each sampled rate year plus the two previous years
(for filter Ф4). Its optional fourth argument is the same file pattern as `--file-pattern` of `build-marts`
(default `RFSD/year={y}/part-0.parquet`; note `{y}`, not `{year}`).

Result: `crosscheck.json`; the command exits with code 2 and prints the number of discrepancies if
there are any. For edition 2026.1 the map has 6 350 checks over 74 cells and 0 discrepancies.

## Differences from the original working copies

Headers were rewritten; `xref_cells.py` gained the optional file-pattern
argument (the original had the pattern of the authors' own file naming hard-coded); `royalty_keys.py`
got standard import formatting. The only other change: file paths are passed to `read_parquet` as
query parameters instead of being pasted into the SQL text. The logic is unchanged.
