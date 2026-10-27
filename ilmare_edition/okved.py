"""OKVED2 reference, RFSD padding normalization and publication hierarchy."""

import csv
import json
from pathlib import Path

import duckdb

LEVELS = ("sec", "cls", "sub", "grp")


class Okved:
    def __init__(self, path: Path) -> None:
        if path.suffix.lower() == ".csv":
            self.records = {}
            with path.open(encoding="utf-8-sig", newline="") as stream:
                for n, row in enumerate(csv.reader(stream, delimiter=";"), 1):
                    if not row:
                        continue
                    if len(row) != 3:
                        raise ValueError(f"invalid OKVED2 CSV line {n}: expected 3 fields")
                    section, code, name = (value.strip() for value in row)
                    if not (len(section) == 1 and "A" <= section <= "Z"):
                        raise ValueError(f"invalid OKVED2 CSV line {n}: section")
                    if not name:
                        raise ValueError(f"invalid OKVED2 CSV line {n}: name")
                    code = code or section
                    if code in self.records:
                        raise ValueError(f"duplicate OKVED2 code {code} on line {n}")
                    self.records[code] = {"code": code, "section": section, "name": name}
        else:
            records = json.loads(path.read_text(encoding="utf-8"))
            self.records = {item["code"].strip(): item for item in records}
        if not self.records:
            raise ValueError("OKVED2 reference is empty")

    def normalize(self, code: str | None) -> str | None:
        if code in self.records:
            return code
        if code is None:
            return None
        if len(code) == 5 and code.endswith("0") and code[:4] in self.records:
            return code[:4]
        if len(code) == 8:
            if code.endswith("0") and code[:7] in self.records:
                return code[:7]
            if code[:5] in self.records:
                return code[:5]
            if code[4] == "0" and code[:4] in self.records:
                return code[:4]
        return None

    def section(self, code: str) -> str | None:
        return self.records.get(code[:2], {}).get("section")

    def parent(self, code: str, level: str) -> str | None:
        if level == "grp":
            return code[:4] if code[:4] in self.records else code[:2]
        if level == "sub":
            return code[:2]
        if level == "cls":
            return self.section(code)
        return None

    def names(self) -> list[tuple[str, str, str | None, str]]:
        lengths = {1: "sec", 2: "cls", 4: "sub", 5: "grp"}
        return [
            (code, lengths[len(code)], item.get("section"), item["name"])
            for code, item in sorted(self.records.items()) if len(code) in lengths
        ] + [("ALL", "all", None, "Все отрасли")]

    def install(self, con: duckdb.DuckDBPyConnection) -> None:
        con.execute("CREATE TABLE ref(code VARCHAR PRIMARY KEY, section VARCHAR, name VARCHAR)")
        if self.records:
            con.executemany("INSERT INTO ref VALUES (?, ?, ?)", [
                (code, item.get("section"), item["name"])
                for code, item in sorted(self.records.items())
            ])


# Only fixed SQL identifiers and constants appear here. File paths are bound parameters.
NORMALIZATION_RULES = (
    "exact", "sub_pad", "subgroup_pad", "group_prefix", "sub_zero", "empty", "unmatched",
)
NORMALIZE_RULE_SQL = """CASE
 WHEN okved IN (SELECT code FROM ref) THEN 'exact'
 WHEN length(okved)=5 AND right(okved,1)='0'
   AND left(okved,4) IN (SELECT code FROM ref) THEN 'sub_pad'
 WHEN length(okved)=8 AND right(okved,1)='0'
   AND left(okved,7) IN (SELECT code FROM ref) THEN 'subgroup_pad'
 WHEN length(okved)=8 AND left(okved,5) IN (SELECT code FROM ref) THEN 'group_prefix'
 WHEN length(okved)=8 AND substr(okved,5,1)='0'
   AND left(okved,4) IN (SELECT code FROM ref) THEN 'sub_zero'
 WHEN okved IS NULL OR okved = '' THEN 'empty'
 ELSE 'unmatched' END"""
NORMALIZE_SQL = """CASE
 WHEN okved IN (SELECT code FROM ref) THEN okved
 WHEN length(okved)=5 AND right(okved,1)='0'
   AND left(okved,4) IN (SELECT code FROM ref) THEN left(okved,4)
 WHEN length(okved)=8 AND right(okved,1)='0'
   AND left(okved,7) IN (SELECT code FROM ref) THEN left(okved,7)
 WHEN length(okved)=8 AND left(okved,5) IN (SELECT code FROM ref) THEN left(okved,5)
 WHEN length(okved)=8 AND substr(okved,5,1)='0'
   AND left(okved,4) IN (SELECT code FROM ref) THEN left(okved,4)
 ELSE NULL END"""
