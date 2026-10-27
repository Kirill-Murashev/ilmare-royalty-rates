"""Independent values for the crosscheck map (written 2026-10-24, separately from ilmare_edition).

Cells — from xref.jsonl (xref_cells.py on RFSD, an implementation of methodology sections 4–6 that does not use the edition code).
Royalty — exact quantiles of R = max(M*, 0) * S (methodology section 8) by deterministic quadrature over S, not by
simulation: S ~ Beta(a, b) of the tier (base combination invention / nonexclusive, k = 1); M* — split normal around
the base value with scales (value - lo) / 1.96 and (hi - value) / 1.96 from the CI of xref_cells.py; base wmean —
wmean * S. Values come from xref.jsonl, so the royalty check also covers the cell values end to end.

Tolerances, fixed before comparing with the edition:
- n — 0; other cell fields — 1e-12 (same exact definitions, fsum);
- royalty quantile p — max(1e-4, 4 * SE), SE = sqrt(p (1 - p) / N) / f_R(q_p), N = 100 000 draws of the edition
  (its quantiles are Monte Carlo estimates); 1e-4 = 0.01 percentage points.

Usage: python3 -I build_independent.py XREF_JSONL SHARE_MODEL_JSON ROYALTY_KEYS_JSON DRAWS > independent.csv
ROYALTY_KEYS_JSON — [[t, level, code, base, tier], ...] present in royalty.parquet for the sampled cells.
"""

import csv
import json
import math
import sys

import numpy as np
from scipy import special, stats

PROBS = (("p05", 0.05), ("p10", 0.10), ("p25", 0.25), ("p50", 0.50), ("p75", 0.75), ("p90", 0.90), ("p95", 0.95))
CELL_FIELDS = ("n", "median", "q1", "q3", "median_lo", "median_hi", "q3_lo", "q3_hi", "wmean")
K = 20000


def share_grid(tier):
    u = (np.arange(K) + 0.5) / K
    return special.betaincinv(tier["a"], tier["b"], u)


def royalty_quantiles(value, lo, hi, s, draws):
    """Vectorised bisection for all seven probabilities at once; F_R(r) = mean_i P(M <= r / s_i), r >= 0."""
    left, right = (value - lo) / 1.96, (hi - value) / 1.96

    def cdf(r):  # r: shape (m,)
        x = r[:, None] / s[None, :]
        z = np.where(x <= value, (x - value) / left if left > 0 else np.where(x >= value, np.inf, -np.inf),
                     (x - value) / right if right > 0 else np.inf)
        return special.ndtr(z).mean(axis=1)

    probs = np.array([p for _, p in PROBS])
    at_zero = cdf(np.zeros(1))[0]
    a = np.zeros(len(probs))
    b = np.full(len(probs), max(value, hi, 1e-12) * float(s.max()) * 2)
    while np.any(cdf(b) < probs):
        b = np.where(cdf(b) < probs, b * 2, b)
    for _ in range(56):
        mid = (a + b) / 2
        below = cdf(mid) < probs
        a, b = np.where(below, mid, a), np.where(below, b, mid)
    q = np.where(at_zero >= probs, 0.0, (a + b) / 2)
    h = np.maximum(q * 1e-4, 1e-9)
    lower = np.maximum(q - h, 0.0)
    density = (cdf(q + h) - cdf(lower)) / (q + h - lower)
    result = {}
    for (name, p), qi, fi in zip(PROBS, q, density):
        se = math.sqrt(p * (1 - p) / draws) / fi if fi > 0 else math.inf
        result[name] = (float(qi), max(1e-4, 4 * se))
    return result


def wmean_quantiles(value, tier, draws):
    result = {}
    for name, p in PROBS:
        q_s = float(special.betaincinv(tier["a"], tier["b"], p))
        density = stats.beta.pdf(q_s, tier["a"], tier["b"]) / abs(value)
        se = math.sqrt(p * (1 - p) / draws) / density
        result[name] = (value * q_s, max(1e-4, 4 * se))
    return result


def main():
    xref_path, model_path, keys_path, draws = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    cells = {}
    for line in open(xref_path, encoding="utf-8"):
        row = json.loads(line)
        cells[(row["t"], row["level"], row["code"])] = row
    model = json.load(open(model_path, encoding="utf-8"))
    assert model["base_combination"] == {"ip": "invention", "license": "nonexcl"}
    assert model["k_ip"]["invention"] == 1 and model["k_license"]["nonexcl"] == 1
    grids = [share_grid(tier) for tier in model["tiers"]]
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(("table", "t", "level", "code", "base", "tier", "field", "value", "tolerance"))
    for (t, level, code), row in sorted(cells.items()):
        for field in CELL_FIELDS:
            value = row.get(field)
            writer.writerow(("cells", t, level, code, "", "", field, "" if value is None else repr(value),
                             0 if field == "n" else 1e-12))
    for t, level, code, base, tier in sorted(tuple(key) for key in json.load(open(keys_path, encoding="utf-8"))):
        row = cells[(t, level, code)]
        if base == "wmean":
            values = wmean_quantiles(row["wmean"], model["tiers"][tier], draws)
        else:
            values = royalty_quantiles(row[base], row[f"{base}_lo"], row[f"{base}_hi"], grids[tier], draws)
        for name, (q, tolerance) in values.items():
            writer.writerow(("royalty", t, level, code, base, tier, name, repr(float(q)), repr(float(tolerance))))


main()
