import json
from pathlib import Path

import numpy as np
import pytest

from ilmare_edition.config import DEFAULT_MODEL
from ilmare_edition.royalty import IP_KEYS, LICENSE_KEYS, PROBABILITIES, Royalty, read_model


def test_packaged_model_v2() -> None:
    model = read_model(DEFAULT_MODEL)
    assert model["version"] == "v2"
    assert tuple(model["k_ip"]) == IP_KEYS
    assert tuple(model["k_license"]) == LICENSE_KEYS


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda model: model["k_ip"].update(extra=1.0), "k_ip"),
        (lambda model: model["k_ip"].pop("utility"), "k_ip"),
        (lambda model: model["k_license"].update(extra=1.0), "k_license"),
        (lambda model: model["k_license"].pop("excl"), "k_license"),
        (lambda model: model.update(k_ip=list(model["k_ip"])), "k_ip must be an object"),
        (lambda model: model.update(k_license=None), "k_license must be an object"),
        (lambda model: model.pop("k_ip"), "k_ip must be an object"),
        (lambda model: model.pop("base_combination"), "base_combination"),
        (lambda model: model["k_ip"].update(utility=10**400), r"k_ip\.utility"),
        (lambda model: model["tiers"][0].update(b=10**400), r"tier .+\.b"),
        (lambda model: model["k_ip"].update(utility=True), "k_ip.utility"),
        (lambda model: model["k_ip"].update(utility=float("nan")), "k_ip.utility"),
        (lambda model: model["k_ip"].update(utility=0), "k_ip.utility"),
        (lambda model: model["tiers"][0].update(a=True), "tier .+\\.a"),
        (lambda model: model["tiers"][0].update(a="1"), "tier .+\\.a"),
        (lambda model: model["tiers"][0].update(b=None), "tier .+\\.b"),
        (
            lambda model: model.update(base_combination={"ip": "utility", "license": "nonexcl"}),
            "base_combination",
        ),
        (lambda model: model["k_ip"].update(invention=0.9), "base combination multipliers"),
        (lambda model: model["k_license"].update(nonexcl=0.9), "base combination multipliers"),
        (lambda model: model.update(version="  "), "version"),
    ],
)
def test_invalid_share_model(mutation, error: str, tmp_path: Path) -> None:
    model = json.loads(DEFAULT_MODEL.read_text(encoding="utf-8"))
    mutation(model)
    path = tmp_path / "share_model.json"
    path.write_text(json.dumps(model), encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        read_model(path)


def test_v1_model_is_rejected() -> None:
    old_model = DEFAULT_MODEL.with_name("share_model_v1.json")
    with pytest.raises(ValueError, match="k_ip"):
        read_model(old_model)


def test_seed_and_scaling() -> None:
    model = read_model(DEFAULT_MODEL)
    first, second = Royalty(model, 42, 10000), Royalty(model, 42, 10000)
    q = np.array(first.quantiles(0.2, 0.15, 0.3))
    assert np.array_equal(q, second.quantiles(0.2, 0.15, 0.3))
    assert np.array_equal(2 * q, first.quantiles(0.4, 0.3, 0.6))
    assert not np.array_equal(q, Royalty(model, 43, 10000).quantiles(0.2, 0.15, 0.3))


def test_narrow_interval() -> None:
    sampler = Royalty(read_model(DEFAULT_MODEL), 20261017, 20000)
    for index, quantiles in enumerate(sampler.quantiles(0.2, 0.2 - 1e-8, 0.2 + 1e-8)):
        assert quantiles[3] == pytest.approx(0.2 * np.median(sampler.shares[index]), rel=0.005)


def test_draw_order() -> None:
    model = read_model(DEFAULT_MODEL)
    sampler = Royalty(model, 42, 100)
    rng = np.random.default_rng(42)
    assert np.array_equal(sampler.z, rng.standard_normal(100))
    for tier, actual in zip(model["tiers"], sampler.shares, strict=True):
        assert np.array_equal(actual, rng.beta(tier["a"], tier["b"], 100))


@pytest.mark.parametrize("level", ["sec", "all"])
@pytest.mark.parametrize("precision", [0, 1, 2])
def test_base_eligibility_and_wmean(level: str, precision: int) -> None:
    sampler = Royalty(read_model(DEFAULT_MODEL), 42, 1000)
    cell = dict(
        t=2026,
        level=level,
        code="A" if level == "sec" else "ALL",
        q3=0.3,
        q3_lo=0.2,
        q3_hi=0.4,
        prec_q3=precision,
        median=0.1,
        median_lo=0.05,
        median_hi=0.15,
        prec_median=0,
        wmean=0.2,
    )
    rows = sampler.rows(cell)
    assert {row[3] for row in rows} == ({"q3", "wmean"} if precision else {"wmean"})
    if precision:
        # Captured before the eligibility change: seed 42, 1000 draws.
        expected = np.array(
            [
                [
                    0.014387049823909292,
                    0.016904866493728916,
                    0.02236634626467303,
                    0.02931564005898283,
                    0.03806837321026965,
                    0.04645013859804549,
                    0.05069677911850576,
                ],
                [
                    0.04458837640169012,
                    0.051105647262039804,
                    0.061457485000792754,
                    0.07387796043860029,
                    0.08821977697717756,
                    0.10179455765449451,
                    0.10901925019318777,
                ],
                [
                    0.06438464470010219,
                    0.07004941087937509,
                    0.08305247137464582,
                    0.09656785059506562,
                    0.11313564160954398,
                    0.1296099989120715,
                    0.13818640605226531,
                ],
                [
                    0.07902323053139608,
                    0.08613318126005469,
                    0.09915790999917841,
                    0.11649017655941978,
                    0.13380674311258203,
                    0.15040768337703264,
                    0.1610799172348399,
                ],
            ]
        )
        q3 = [row for row in rows if row[3] == "q3"]
        assert [row[4] for row in q3] == [0, 1, 2, 3]
        assert np.array([row[5:] for row in q3]).tobytes() == expected.tobytes()
    wmean = [row for row in rows if row[3] == "wmean"]
    assert wmean[0][5:] == tuple(0.2 * np.quantile(sampler.shares[0], PROBABILITIES))
    cell["q3_lo"] = None
    assert {row[3] for row in sampler.rows(cell)} == {"wmean"}
    cell["q3_lo"] = 0.2
    cell.update(level="cls", prec_q3=0)
    assert {row[3] for row in sampler.rows(cell)} == {"wmean"}
    cell.update(prec_q3=2, prec_median=1)
    assert {row[3] for row in sampler.rows(cell)} == {"q3", "median", "wmean"}
    cell.update(q3=-0.1, median=0, wmean=-0.1)
    assert sampler.rows(cell) == []
