# ilmare-royalty-rates

[Русская версия → README.md](README.md)

Calculation scripts behind the **Royalty Rate Reference by OKVED2 Industry (licensee profit-split
method)**, edition 2026.1 ("Ilmarë"). This repository contains only the code that turns open
financial statements of Russian companies into the profitability statistics and royalty rates of the
reference, plus scripts that re-derive those numbers with a separately written implementation. The
purpose is simple: anyone can check that the numbers of the reference are computed, not invented.

The reference: <https://app.digitalpm.info>. Authors: N. R. Kirshina, K. A. Murashev. The reference
text is in Russian.

## Method in one paragraph

Royalty rate `R = M × S`: `M` is the operating margin on sales (line 2200 / line 2110 of the income
statement) of the companies in an industry, `S` is the share of that margin the licensee pays the
licensor. For every year and for every OKVED2 section, class, subclass and group, companies pass six
cumulative filters (Ф1–Ф5, Ф7; the number Ф6 is reserved) — positive revenue, net assets and equity;
exclusion of persistently loss-making firms; commercial organisations only; a recognised OKVED2 code. The quartiles follow
art. 105.8 p. 4 of the Russian Tax Code, 95% confidence intervals come from binomial order
statistics, and the interval width assigns an accuracy class to each cell. The base is the upper
quartile `Q3` ("successful licensee"); the share `S` is modelled by four beta distributions (value
tiers of the intellectual property), and the distribution of the rate is obtained by Monte Carlo
(100,000 draws, fixed seed, the same draws for all cells and years). The full description is in
sections 3–8 of the reference's methodology.

## Contents

```
ilmare_edition/           calculation package
  load.py                 reads one RFSD year and applies filters Ф1–Ф7 (DuckDB)
  okved.py                OKVED2 reference and normalisation of RFSD codes
  stats.py                tax-code quartiles, confidence intervals, accuracy classes
  marts.py                builds the marts (parquet) and a manifest with checksums
  royalty.py              share model and Monte Carlo
  replace.py              replaces the share model in finished marts without recomputing
  calibration.py          comparison with external rates (command calibrate)
  crosscheck.py           crosscheck map against the independent recomputation (command crosscheck)
  marts_io.py             verification and reading of a marts directory
  data/share_model_v2.json   share model of edition 2026.1
  data/share_model_v1.json   earlier model; history only, rejected for new builds
independent-check/        independent recomputation (see below)
tests/                    tests on synthetic data
```

## Installation

Python 3.12 or newer.

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'                 # duckdb 1.5.6, numpy 2.5.4; pytest, ruff
pip install -e '.[independent-check]'    # scipy 1.16.3 — only for the independent recomputation
```

Dependency versions are pinned exactly; byte-for-byte reproducibility of parquet output depends on
the DuckDB version.

## Input data

### 1. RFSD (financial statements)

The open **Russian Financial Statements Database**, version 3.1.0 of 2026-09-02, on Hugging Face:
<https://huggingface.co/datasets/irlspbru/RFSD>. The loader reads a yearly file **without** any extra
columns: it uses only `inn`, `okved`, `okopf`, `simplified` and the lines `line_1300`, `line_1530`,
`line_2110`, `line_2200`, `line_2300`, `line_2330`, `line_2400`.

Download statements for 2011–2025 (about 6 GB on disk):

```sh
pip install 'huggingface_hub[cli]'
hf download irlspbru/RFSD --repo-type dataset \
  --revision ca3665997c977ba8ad01cf93c3e2a74ce7fe9c05 \
  --include 'RFSD/year=*/part-0.parquet' --local-dir rfsd
```

Revision `ca3665997c…` is the commit "Update to 3.1.0" (2026-09-02); the next commit only fixes the
README. You get `rfsd/RFSD/year=2011/part-0.parquet` … `year=2025/part-0.parquet`. The file name
template is the parameter `--file-pattern` (default `RFSD/year={year}/part-0.parquet`, relative to
`--data-root`); a different file layout is expressed with the same parameter.

For filter Ф4 (persistent losses) the loader also reads the two previous years by the same
template; for statement years 2011 and 2012 they do not exist, and the filter is not applied there
(see methodology, section 5).

### 2. OKVED2 reference

Open data of Rosstat, dataset **7708234640-okvedva** (All-Russian Classifier of Economic Activities,
OKVED2), version of 2026-10-01: <https://rosstat.gov.ru/opendata/7708234640-okvedva>, file
`data-20261001T1110-structure-20180402T1704.csv` (464,582 bytes, SHA-256
`8e2bfae03a0f75b5cc71b2bb6e58063df4a415e64819059541fef05b2618d5be`).

```sh
curl -O https://rosstat.gov.ru/opendata/7708234640-okvedva/data-20261001T1110-structure-20180402T1704.csv
sha256sum data-20261001T1110-structure-20180402T1704.csv
```

The Rosstat site uses a certificate issued by a Russian certification authority; if your system does
not trust it, add the Russian Ministry of Digital Development root certificate
(<https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt>) and verify the chain.
File format: UTF-8, no header, delimiter `;`, three fields — section, code, name; the code of a
section row is blank.

**The file itself is not included.** Rosstat publishes the dataset under the "typical terms of use of
publicly available information in the form of open data" (<https://data.gov.ru/information-usage>);
the dataset has no licence of its own. We could not read the full terms automatically, so we do not
redistribute the file: download it from Rosstat and check the hash.

## Reproduce the marts of edition 2026.1

```sh
python -m ilmare_edition build-marts \
  --data-root rfsd \
  --okved-ref data-20261001T1110-structure-20180402T1704.csv \
  --out marts-2026.1 --edition 2026.1 \
  --years 2011-2025 --seed 20261017 --draws 100000 \
  --threads 3 --memory-limit 3GB
```

`--years` are statement years; rates are for the next year (statements 2025 → rates 2026). The
output directory must not exist or must be empty. The run processes one year at a time and is
designed for a 3 GB memory limit. Output: six parquet files (`cells`, `royalty`, `qgrid`, `sizes`,
`funnel`, `okved_names`), `share_model.json`, `manifest.json` (version, parameters, size and SHA-256
of every file read) and `SHA256SUMS`. See `python -m ilmare_edition build-marts -h`.

The marts of edition 2026.1 were built with the same calculation code before it was moved into
this repository. The checksums below are the expected ones; confirmation of byte-for-byte equality
by a run of the public code will be published separately (a repository tag). Equality is expected
with the same input files and dependency versions:

```
0df9ea51925801687e3672da2f21bd4e17a71982f80335f5718f12a047b7dfa1  cells.parquet
540252f34a844c6992acf2731aa883426e4cca4b750414fc370d416e78a476b5  funnel.parquet
d874fd5d658d54abfff7ef9acb8586e24c92eb9f5b21a7740de6659629b68cea  okved_names.parquet
f9b04075096bb6d094c1421c64ea889e76cae5430f9c56ba20d1592202a2e09f  qgrid.parquet
bc5cc84ff1f82a5ab059e44f7b37e7b4ac7ca0f2fcecbe57736174045b645056  royalty.parquet
ec356d805d7422a2c9fde71087dde8d1f0c14568dddd5dd6201b927e775f0831  sizes.parquet
24ed09975a122285383b971689604ca27237428342079eeeb84410e15e3ef0c9  share_model.json
```

`manifest.json` and its line in `SHA256SUMS` differ: the manifest records build time, code version
and input file names.

## What the crosscheck map verifies

The `crosscheck` command compares values of an independent recomputation (CSV) with finished marts
and writes a map `crosscheck.json`: for every check the edition value, the independent value, the
tolerance and the outcome. For edition 2026.1 the map holds **6,350 checks over 74 cells, 0
discrepancies**:

- 666 checks of cell values (number of companies `n`, median, `Q1`, `Q3`, confidence limits of the
  median and `Q3`, weighted margin) for rates of 2012, 2013, 2019 and 2026 — tolerance 0 for `n`,
  `1e-12` for the rest;
- 5,684 checks of royalty-rate quantiles (P5, P10, P25, P50, P75, P90, P95 for each tier and base);
  tolerance is at least 0.01 percentage points and at least four standard errors of the Monte Carlo
  estimate.

The cells were drawn with a fixed seed across all levels (all industries, sections, classes,
subclasses, groups) and include sections K and L and cells with no published rate.

## Independent recomputation

`independent-check/` holds four scripts **written separately from the edition code**, from the
methodology text rather than from `ilmare_edition`. They do not import the package, reimplement the
filters, OKVED normalisation, tax-code quartiles and confidence intervals (plain Python and SQL), and
find rate quantiles by numerical integration over the distribution function, not by simulation.
Agreement between the two implementations shows that the code does exactly what the methodology says.
See [independent-check/README.md](independent-check/README.md) for the run order.

## Comparison with external rates

The `calibrate` command matches rates from court rulings, franchise agreements, literature and
foreign statistics against the marts and derives control multipliers. It reads three
semicolon-separated CSV files (format: the constant `COLUMNS` in `ilmare_edition/calibration.py`).
**The data sets themselves are not included** (see below), so the command is demonstrated on
synthetic data in `tests/test_calibration.py`.

## What is not in this repository, and why

- **The Elbereth case export.** The results of the court-ruling analysis come from a separate
  database whose rights are not covered by the licence of the reference.
- **BVR and Markables data.** They belong to third-party providers; the authors have not obtained the
  right to redistribute them.
- **Sets of external rates (Russian and foreign).** Their rows come from franchise catalogues and
  court rulings; rights to the rows have not been checked.
- **The OKVED2 reference** — see above: not redistributed, downloaded from Rosstat.
- **RFSD data and finished marts.** RFSD is downloaded from Hugging Face under CC BY 4.0; the marts
  are reproduced by the command above.
- **The book generator** (LaTeX typesetting, text templates, figures, XLSX) and **the calculator** —
  presentation and service, not calculation.

## Tests

```sh
python -m pytest -q
python -m ruff check .
```

Tests use synthetic data only (up to a hundred rows per year). `tests/test_independent_check.py`
runs the whole path "marts → independent recomputation → crosscheck map" on synthetic data; it is
skipped when scipy is not installed.

## Licences

- Code — [MIT](LICENSE).
- RFSD — [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Authors of the database:
  Bondarkov S., Ledenev V., Skougarevskiy D. Russian Financial Statements Database. *Scientific
  Data* 12(1), 2025, [doi:10.1038/s41597-025-05150-1](https://doi.org/10.1038/s41597-025-05150-1)
  (Institute for the Rule of Law, European University at St Petersburg). The dataset page also shows
  a CC BY-NC-SA 4.0 badge; its Licence section and metadata state CC BY 4.0.
- OKVED2 reference — Rosstat open data; terms at the link above.
- The text of the reference (the book) is distributed separately under CC BY-SA 4.0.
