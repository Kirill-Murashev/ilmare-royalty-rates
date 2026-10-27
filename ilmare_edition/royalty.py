"""Common-random-number Monte Carlo for the base IP/license combination."""

import json
import math
from pathlib import Path

import numpy as np

PROBABILITIES = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)
IP_KEYS = (
    "invention",
    "utility",
    "software",
    "trademark",
    "design",
    "database",
    "knowhow",
    "patent_knowhow",
)
LICENSE_KEYS = ("nonexcl", "excl")


def _positive_finite(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def parse_model(raw: bytes) -> dict:
    try:
        model = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"invalid share model JSON: {exc}") from exc
    if not isinstance(model, dict):
        raise ValueError("share model must contain an object")
    if not isinstance(model.get("version"), str) or not model["version"].strip():
        raise ValueError("share model version must be a nonempty string")
    tiers = model.get("tiers")
    if not isinstance(tiers, list):
        raise ValueError("share model tiers must be a list")
    if len(tiers) != 4:
        raise ValueError("share model must contain four tiers")
    for index, tier in enumerate(tiers):
        if not isinstance(tier, dict):
            raise ValueError(f"share model tier {index} must contain an object")
        for key in ("a", "b"):
            value = tier.get(key)
            if not _positive_finite(value):
                tier_key = tier.get("key", index)
                raise ValueError(f"tier {tier_key}.{key} must be finite and positive")
    for field, expected in (("k_ip", IP_KEYS), ("k_license", LICENSE_KEYS)):
        values = model.get(field)
        if not isinstance(values, dict):
            raise ValueError(f"{field} must be an object")
        if set(values) != set(expected):
            raise ValueError(f"{field} must contain exactly {', '.join(expected)}")
        for key, value in values.items():
            if not _positive_finite(value):
                raise ValueError(f"{field}.{key} must be finite and positive")
    if model.get("base_combination") != {"ip": "invention", "license": "nonexcl"}:
        raise ValueError("base_combination must be invention/nonexcl")
    if model["k_ip"]["invention"] != 1.0 or model["k_license"]["nonexcl"] != 1.0:
        raise ValueError("base combination multipliers must equal 1.0")
    return model


def read_model(path: Path) -> dict:
    return parse_model(path.read_bytes())


class Royalty:
    def __init__(self, model: dict, seed: int, draws: int) -> None:
        rng = np.random.default_rng(seed)
        self.z = rng.standard_normal(draws)
        self.shares = [rng.beta(tier["a"], tier["b"], draws) for tier in model["tiers"]]
        self.share_quantiles = [np.quantile(share, PROBABILITIES) for share in self.shares]

    def quantiles(self, value: float, lo: float, hi: float) -> list[list[float]]:
        margins = np.maximum(
            np.where(
                self.z < 0,
                value + (value - lo) / 1.96 * self.z,
                value + (hi - value) / 1.96 * self.z,
            ),
            0,
        )
        return [np.quantile(margins * share, PROBABILITIES).tolist() for share in self.shares]

    def rows(self, cell: dict) -> list[tuple]:
        rows = []
        for base in ("q3", "median", "wmean"):
            value = cell[base]
            if value is None or value <= 0:
                continue
            if base == "wmean":
                quantiles = [(values * value).tolist() for values in self.share_quantiles]
            else:
                lo, hi = cell[f"{base}_lo"], cell[f"{base}_hi"]
                eligible = cell[f"prec_{base}"] > 0
                if not eligible or lo is None or hi is None:
                    continue
                quantiles = self.quantiles(value, lo, hi)
            rows.extend(
                (cell["t"], cell["level"], cell["code"], base, tier, *values)
                for tier, values in enumerate(quantiles)
            )
        return rows
