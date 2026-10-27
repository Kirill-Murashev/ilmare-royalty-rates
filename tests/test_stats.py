import math

import pytest

from ilmare_edition.stats import ci_ranks, precision, tax_quantile, weighted_measures


@pytest.mark.parametrize(("values", "expected"), [
    ([9], (9, 9, 9)), ([1, 7], (1, 4, 7)), ([1, 2, 3, 4], (1.5, 2.5, 3.5)),
    ([1, 2, 3, 4, 5], (2, 3, 4)), ([1, 2, 3, 4, 5, 6, 7, 8], (2.5, 4.5, 6.5)),
])
def test_tax_quantiles(values: list[float], expected: tuple) -> None:
    assert tuple(tax_quantile(values[::-1], p) for p in (.25, .5, .75)) == expected


def independent_ranks(n: int, p: float) -> tuple[int, int] | None:
    cdf = [sum(math.comb(n, k) * p**k * (1 - p)**(n - k) for k in range(j + 1))
           for j in range(n)]
    lower = [j for j in range(1, n + 1) if cdf[j - 1] <= .025]
    upper = [k for k in range(1, n + 1) if 1 - cdf[k - 1] <= .025]
    return (max(lower), min(upper)) if lower and upper else None


@pytest.mark.parametrize(("n", "p", "expected"), [
    (35, .5, (12, 24)), (100, .5, (40, 61)),
    (35, .75, (21, 32)), (100, .75, (66, 84)),
])
def test_exact_ranks(n: int, p: float, expected: tuple[int, int]) -> None:
    assert ci_ranks(n, p) == independent_ranks(n, p) == expected


@pytest.mark.parametrize("p", [.5, .75])
def test_rank_transition(p: float) -> None:
    assert max(abs(a - b) for a, b in zip(ci_ranks(5000, p), ci_ranks(5001, p))) <= 1


@pytest.mark.parametrize("n", range(1, 12))
@pytest.mark.parametrize("p", [.5, .75])
def test_small_sample_ranks(n: int, p: float) -> None:
    assert ci_ranks(n, p) == independent_ranks(n, p)


def test_weighted_measures() -> None:
    result = weighted_measures([10, 20, 70], [1, 4, 21])
    assert result == pytest.approx(dict(wmean=.26, wmean_x1=5 / 30, top1_share=.7, hhi=.54))
    assert weighted_measures([10], [2])["wmean_x1"] is None


@pytest.mark.parametrize(("n", "value", "lo", "hi", "expected"), [
    (34, .2, .2, .2, 0), (35, 0, 0, 0, 0), (35, .2, None, .3, 0),
    (35, .2, .15, .25, 2), (35, .2, .1, .3, 1), (35, .2, .0, .4, 0),
])
def test_precision(n: int, value: float, lo: float | None,
                   hi: float | None, expected: int) -> None:
    assert precision(n, value, lo, hi) == expected
