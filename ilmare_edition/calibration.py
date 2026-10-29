"""Reproducible external-rate calibration against verified edition marts."""

import csv
import hashlib
import io
import json
import math
import os
import re
import statistics
from collections import defaultdict
from pathlib import Path

import duckdb

from .marts import code_version as current_code_version
from .marts import write_json_new
from .marts_io import SAFE_NAME, read_rows, require_marts_unchanged, verify_marts_snapshot

TIERS = ("low", "normal", "valuable", "core")
COLUMNS = (
    "id",
    "source_type",
    "source",
    "notes",
    "data_year",
    "okved2",
    "ip_type",
    "license",
    "value_hint",
    "rate_low",
    "rate_mid",
    "rate_high",
    "base",
    "verified",
)
COMPUTED_KEYS = (
    "calib_mult_median_ru",
    "calib_mult_q3_foreign",
    "calib_mult_q3_ru",
    "calib_mult_q3_ru_rule",
    "calib_points_foreign",
    "calib_points_ru",
    "elb_dups_removed",
    "elb_obs",
    "elb_obs_cases",
    "elb_obs_median",
    "ext_ratio_to_median_ru",
    "fin_cls_median",
    "fin_cls_points",
    "fin_cls_q3",
    "fin_cls_q3_rule",
    "fin_cls_sd_median",
    "fin_cls_sd_q3",
    "fin_cls_sd_w",
    "fin_elb_median",
    "fin_elb_points",
    "fin_elb_q3",
    "fin_grp_median",
    "fin_grp_points",
    "fin_grp_q3",
    "fin_valuable_cls_q3",
    "fin_valuable_elb_q3",
    "fin_valuable_grp_q3",
    "k_fx_median",
    "k_med_franchise",
    "k_med_literature",
    "k_q3_franchise",
    "k_q3_literature",
    "n_franchise",
    "n_literature",
    "r_med_courts",
    "r_med_franchise",
    "r_med_fx",
    "r_med_literature",
    "ru_share_franchise",
    "s_q3_core",
    "s_q3_fx",
    "s_q3_low",
    "s_q3_normal",
    "s_q3_valuable",
    "sec_q3_k",
)

COUNT_KEYS = (
    "calib_points_foreign", "calib_points_ru", "elb_dups_removed", "elb_obs", "elb_obs_cases",
    "fin_cls_points", "fin_elb_points", "fin_grp_points", "n_franchise", "n_literature",
)
SPREAD_KEYS = ("fin_cls_sd_median", "fin_cls_sd_q3", "fin_cls_sd_w")


def number(text: str) -> float | None:
    """Parse a finite benchmark number, accepting decimal commas."""
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def read_benchmarks(path: Path) -> list[dict]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read benchmarks {path.name}: {exc}") from exc
    return parse_benchmarks(raw, path.name)


def parse_benchmarks(raw: bytes, name: str) -> list[dict]:
    try:
        with io.StringIO(raw.decode("utf-8"), newline="") as stream:
            reader = csv.DictReader(stream, delimiter=";")
            for column in COLUMNS:
                if column not in (reader.fieldnames or []):
                    raise ValueError(f"missing benchmark column: {column}")
            return [{key: (row.get(key) or "").strip() for key in COLUMNS} for row in reader]
    except (UnicodeError, csv.Error) as exc:
        raise ValueError(f"cannot read benchmarks {name}: {exc}") from exc


def norm_case(s):
    s = s.replace("A", "А").replace(" ", "")
    m = re.search(r"([АA]\d{2,3})-(\d+)/(\d{2,4})", s)
    if not m:
        return None
    y = m.group(3)
    y = ("20" + y) if len(y) == 2 else y
    return f"{m.group(1)}-{m.group(2)}/{y}"


def select_rows(rows: list[dict]) -> list[dict]:
    selected = []
    for row in rows:
        if row["verified"] != "yes" or row["base"] not in (
            "revenue",
            "net_revenue",
            "net_sales",
            "",
        ):
            continue
        if row["source_type"] == "court" and re.search(
            r"налог|тцо|105\.|ифнс|инспекц|взаимозавис|внутригруп",
            (row["notes"] + " " + row["source"]).lower(),
        ):
            continue
        rate = number(row["rate_mid"])
        low, high = number(row["rate_low"]), number(row["rate_high"])
        if rate is None and low is not None and high is not None:
            if low > 0:
                rate = math.sqrt(low * high)
            else:
                rate = (low + high) / 2
        if rate is not None and math.isfinite(rate) and rate > 0:
            selected.append({**row, "rate": rate})
    return selected


def validate_model(model: dict) -> dict:
    try:
        if not isinstance(model, dict) or not model["version"]:
            raise ValueError("missing share model version")
        if len(model["tiers"]) != 4:
            raise ValueError("share model must have four tiers")
        values = [tier["target"][1] for tier in model["tiers"]]
        for field, required in (
            ("k_ip", {"invention", "trademark"}),
            ("k_license", {"excl", "nonexcl"}),
        ):
            factors = model[field]
            if not isinstance(factors, dict) or not required <= factors.keys():
                raise ValueError(f"invalid share model {field}")
            values.extend(factors.values())
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("share model factors must be finite positive numbers")
    except (KeyError, TypeError, IndexError, OverflowError) as exc:
        raise ValueError(f"invalid share model: {exc}") from exc
    return model


def factors(row: dict, model: dict, variant: str = "data") -> tuple[float, float]:
    """Return the IP/licence factor K and the median share S."""
    ip = "trademark" if row["ip_type"] == "tm_knowhow" else row["ip_type"]
    if ip not in model["k_ip"]:
        ip = "invention"
    license_key = "excl" if row["license"] == "excl" else "nonexcl"
    tier = row["value_hint"] if row["value_hint"] in TIERS else "normal"
    if variant == "rule" and row["source_type"] == "franchise":
        ip, license_key, tier = "trademark", "excl", "core"
    if variant == "valuable" and row["ip_type"] == "tm_knowhow" and tier == "core":
        tier = "valuable"
    k = model["k_ip"][ip] * model["k_license"][license_key]
    return k, model["tiers"][TIERS.index(tier)]["target"][1] * k


class CellLookup:
    """The two specified searches share code/year resolution and own-value rules."""

    def __init__(self, cells: list[dict], names: list[dict]):
        self.cells = {(r["t"], r["level"], r["code"]): r for r in cells}
        self.years = {r["t"] for r in cells}
        if not self.years:
            raise ValueError("cells.parquet has no rate years")
        self.last = max(self.years)
        self.sections = {r["code"]: r["section"] for r in names if r["level"] == "cls"}

    def code_at(self, code: str, level: str) -> str | None:
        if level == "sec":
            return code[0] if code and code[0].isalpha() else self.sections.get(code[:2])
        if len(code[:2]) != 2 or not code[:2].isdigit():
            return None
        if level == "cls":
            return code[:2]
        length = 4 if level == "sub" else 5
        return code[:length] if len(code) >= length and code[2] == "." else None

    def find(self, row: dict, level: str | None = None) -> dict | None:
        year = number(row["data_year"])
        t = int(year) if year is not None and int(year) in self.years else self.last
        levels = (
            (level,)
            if level
            else (("grp", "sub", "cls", "sec") if t == self.last else ("cls", "sec"))
        )
        for candidate in levels:
            code = self.code_at(row["okved2"], candidate)
            cell = self.cells.get((t, candidate, code))
            if cell and (cell["prec_q3"] or 0) > 0 and (cell["q3"] or 0) > 0:
                return cell
        return None


def observations(
    rows: list[dict],
    lookup: CellLookup,
    model: dict,
    level: str | None = None,
    variant: str = "data",
) -> dict[str, list]:
    result = defaultdict(list)
    for row in rows:
        cell = lookup.find(row, level)
        if cell is None:
            continue
        k, share = factors(row, model, variant)
        result["points"].append(row)
        for base in ("median", "q3", "wmean"):
            if cell[base] is not None and cell[base] > 0:
                result[base].append(row["rate"] / (cell[base] * share))
        if cell["median"] is not None and cell["median"] > 0:
            result["ratio"].append(row["rate"] / cell["median"])
        result["share"].append(row["rate"] / (cell["q3"] * k))
    return result


def aggregate(values: list[float], spread: bool = False) -> float | None:
    if not values:
        return None
    return statistics.pstdev([math.log(v) for v in values]) if spread else statistics.median(values)


def compute_values(
    ru_raw: list[dict], fx_raw: list[dict], elb_raw: list[dict], lookup: CellLookup, model: dict
) -> dict:
    ru, fx, elb = (select_rows(rows) for rows in (ru_raw, fx_raw, elb_raw))
    cases = {case for row in elb_raw if (case := norm_case(row["source"])) is not None}

    def duplicate(row):
        return (
            row["source_type"] == "court" and norm_case(row["source"] + " " + row["notes"]) in cases
        )

    combined = [row for row in ru if not duplicate(row)] + elb
    values = {}

    def put(key, series, spread=False, digits=2):
        value = aggregate(series, spread)
        values[key] = round(value, 3 if spread else digits) if value is not None else None

    r, f = (observations(rows, lookup, model) for rows in (ru, fx))
    values.update(calib_points_ru=len(r["points"]), calib_points_foreign=len(f["points"]))
    for key, series in (
        ("calib_mult_median_ru", r["median"]),
        ("calib_mult_q3_ru", r["q3"]),
        ("k_fx_median", f["median"]),
        ("calib_mult_q3_foreign", f["q3"]),
        ("ext_ratio_to_median_ru", r["ratio"]),
        ("r_med_fx", f["ratio"]),
        ("s_q3_fx", f["share"]),
        ("calib_mult_q3_ru_rule", observations(ru, lookup, model, variant="rule")["q3"]),
    ):
        put(key, series)
    for source in ("franchise", "literature", "court"):
        obs = observations([row for row in ru if row["source_type"] == source], lookup, model)
        put("r_med_" + ("courts" if source == "court" else source), obs["ratio"])
        if source != "court":
            values["n_" + source] = len(obs["points"])
            put("k_med_" + source, obs["median"])
            put("k_q3_" + source, obs["q3"])
    values["ru_share_franchise"] = (
        round(values["n_franchise"] / values["calib_points_ru"], 2) if r["points"] else None
    )
    for tier in TIERS:
        put(
            "s_q3_" + tier,
            observations([row for row in ru if row["value_hint"] == tier], lookup, model)["share"],
        )
    for tag, rows, level in (
        ("cls", combined, "cls"),
        ("grp", combined, "grp"),
        ("elb", elb, "cls"),
    ):
        obs = observations(rows, lookup, model, level)
        values[f"fin_{tag}_points"] = len(obs["q3"])
        for base in ("median", "q3"):
            put(f"fin_{tag}_{base}", obs[base])
        put(f"fin_valuable_{tag}_q3", observations(rows, lookup, model, level, "valuable")["q3"])
        if tag == "cls":
            for base, suffix in (("median", "median"), ("q3", "q3"), ("wmean", "w")):
                put("fin_cls_sd_" + suffix, obs[base], spread=True)
            put("fin_cls_q3_rule", observations(rows, lookup, model, level, "rule")["q3"])
    values.update(
        elb_obs=len(elb),
        elb_obs_cases=len({case for row in elb if (case := norm_case(row["source"])) is not None}),
        elb_dups_removed=sum(duplicate(row) for row in ru_raw),
    )
    put("elb_obs_median", [row["rate"] for row in elb], digits=3)
    sections = defaultdict(list)
    for row in ru + fx:
        cell = lookup.find(row)
        if cell is not None:
            _, share = factors(row, model)
            sec = lookup.code_at(row["okved2"], "sec")
            if sec is not None:
                sections[sec].append(row["rate"] / (cell["q3"] * share))
    values["sec_q3_k"] = {
        sec: {"n": len(series), "q3": round(statistics.median(series), 2)}
        for sec, series in sorted(sections.items())
        if len(series) >= 5
    }
    empty = [
        key
        for key, value in values.items()
        if key not in ("elb_dups_removed", "sec_q3_k")
        and (
            value is None
            or (
                key.endswith("points")
                or key
                in (
                    "calib_points_ru",
                    "calib_points_foreign",
                    "n_franchise",
                    "n_literature",
                    "elb_obs",
                    "elb_obs_cases",
                )
            )
            and value == 0
        )
    ]
    if empty:
        raise ValueError("calibration values have no observations: " + ", ".join(sorted(empty)))
    return values


def calibrate(
    marts: Path,
    benchmarks_ru: Path,
    benchmarks_fx: Path,
    elbereth_cases: Path,
    out: Path,
    code_version: str | None = None,
) -> Path:
    """Write all 45 rounded values, or fail without creating an output file."""
    try:
        manifest, digests, sums_raw = verify_marts_snapshot(marts)
        if os.path.lexists(out) or not out.parent.is_dir():
            raise ValueError("--out must not exist and its parent must exist")
        lookup = CellLookup(read_rows(marts, "cells"), read_rows(marts, "okved_names"))
        model_raw = (marts / "share_model.json").read_bytes()
        if hashlib.sha256(model_raw).hexdigest() != digests["share_model.json"]:
            raise ValueError("share_model.json does not match SHA256SUMS")
        model = validate_model(json.loads(model_raw))
        paths = (benchmarks_ru, benchmarks_fx, elbereth_cases)
        for path in paths:
            if not isinstance(path.name, str) or not SAFE_NAME.fullmatch(path.name):
                raise ValueError(f"unsafe input file name: {path.name!r}")
        raw_inputs = [path.read_bytes() for path in paths]
        rows = [
            parse_benchmarks(raw, path.name)
            for raw, path in zip(raw_inputs, paths, strict=True)
        ]
        values = compute_values(*rows, lookup, model)
        require_marts_unchanged(marts, manifest, digests, sums_raw)
        write_json_new(
            out,
            {
                "format": "ilmare-calibration/1",
                "edition": manifest["edition"],
                "marts_sha256sums": hashlib.sha256(sums_raw).hexdigest(),
                "share_model": {
                    "version": model["version"],
                    "sha256": manifest["share_model_sha256"],
                },
                "code_version": code_version
                if code_version is not None
                else current_code_version(),
                "inputs": [
                    {"role": role, "name": path.name,
                     "sha256": hashlib.sha256(raw).hexdigest(), "rows": len(data)}
                    for role, path, data, raw in zip(
                        ("ru", "fx", "elbereth_cases"), paths, rows, raw_inputs, strict=True
                    )
                ],
                "values": values,
            },
        )
    except FileExistsError as exc:
        raise ValueError("--out must not exist and its parent must exist") from exc
    except (OSError, UnicodeError, duckdb.Error, RecursionError) as exc:
        raise ValueError(f"cannot calibrate: {exc}") from exc
    return out
