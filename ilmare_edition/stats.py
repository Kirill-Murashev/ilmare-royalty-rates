"""Tax-code quantiles, binomial order-statistic intervals and weighted measures."""

import math
from functools import lru_cache
from typing import Sequence

import numpy as np

MIN_N = 35
HALF_WIDTH_RELIABLE = 0.25
HALF_WIDTH_INDICATIVE = 0.5
NORMAL_APPROX_N = 5000
CI_LOWER_TAIL = 0.025
CI_UPPER_TAIL = 0.975


def tax_quantile(values: Sequence[float], p: float, *, presorted: bool = False) -> float | None:
    if not values:
        return None
    ordered = values if presorted else sorted(values)
    n = len(ordered)
    k = n * p
    if n == 1:
        return float(ordered[0])
    if k.is_integer():
        return (float(ordered[int(k) - 1]) + float(ordered[int(k)])) / 2
    return float(ordered[math.ceil(k) - 1])


@lru_cache(maxsize=None)
def ci_ranks(n: int, p: float) -> tuple[int, int] | None:
    """One-based equal-tail 95% binomial interval ranks, per the edition definition."""
    if n < 1:
        return None
    if n > NORMAL_APPROX_N:
        center = n * p + 0.5
        delta = 1.96 * math.sqrt(n * p * (1 - p))
        return max(1, math.floor(center - delta)), min(n, math.ceil(center + delta))
    log_n = math.lgamma(n + 1)
    log_p, log_q = math.log(p), math.log1p(-p)
    probabilities = [math.exp(
        log_n - math.lgamma(k + 1) - math.lgamma(n - k + 1)
        + k * log_p + (n - k) * log_q
    ) for k in range(n)]
    cdf = np.cumsum(probabilities)
    lower = np.flatnonzero(cdf <= CI_LOWER_TAIL)
    upper = np.flatnonzero(cdf >= CI_UPPER_TAIL)
    if not len(lower) or not len(upper):
        return None
    return int(lower[-1]) + 1, int(upper[0]) + 1


def quantile_ci(ordered: Sequence[float], p: float) -> tuple[float | None, float | None]:
    ranks = ci_ranks(len(ordered), p)
    if ranks is None:
        return None, None
    return float(ordered[ranks[0] - 1]), float(ordered[ranks[1] - 1])


def precision(n: int, value: float, lo: float | None, hi: float | None) -> int:
    if n < MIN_N or value <= 0 or lo is None or hi is None:
        return 0
    half_width = (hi - lo) / (2 * value)
    return 2 if half_width <= HALF_WIDTH_RELIABLE else (
        1 if half_width <= HALF_WIDTH_INDICATIVE else 0
    )


def margin_statistics(ordered: Sequence[float]) -> dict:
    """Statistics for one nonempty sorted margin sample."""
    result = {key: tax_quantile(ordered, p, presorted=True)
              for key, p in (("q1", 0.25), ("median", 0.5), ("q3", 0.75))}
    for key, p in (("median", 0.5), ("q3", 0.75)):
        lo, hi = quantile_ci(ordered, p)
        result[f"{key}_lo"], result[f"{key}_hi"] = lo, hi
        result[f"prec_{key}"] = precision(len(ordered), result[key], lo, hi)
    return result


def weighted_measures(revenues: Sequence[float], profits: Sequence[float]) -> dict:
    """Reference helper; pipeline uses ordered DuckDB sums for the same measures."""
    total = math.fsum(revenues)
    profit = math.fsum(profits)
    top = max(range(len(revenues)), key=lambda i: (revenues[i], profits[i]))
    rest = math.fsum(value for i, value in enumerate(revenues) if i != top)
    rest_profit = math.fsum(value for i, value in enumerate(profits) if i != top)
    return {
        "wmean": profit / total,
        "wmean_x1": rest_profit / rest if rest else None,
        "top1_share": revenues[top] / total,
        "hhi": math.fsum((value / total) ** 2 for value in revenues),
    }
