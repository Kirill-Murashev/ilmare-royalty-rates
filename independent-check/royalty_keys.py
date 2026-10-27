"""Royalty keys of royalty.parquet for the sampled cells. Usage: python -I royalty_keys.py MARTS KEYS_JSON"""
import json
import sys

import duckdb

marts, keys = sys.argv[1], {tuple(k) for k in json.load(open(sys.argv[2], encoding="utf-8"))}
rows = duckdb.connect().execute(
    "SELECT t, level, code, base, tier FROM read_parquet(?) ORDER BY 1, 2, 3, 4, 5",
    [f"{marts}/royalty.parquet"]).fetchall()
json.dump([list(r) for r in rows if (r[0], r[1], r[2]) in keys], sys.stdout)
