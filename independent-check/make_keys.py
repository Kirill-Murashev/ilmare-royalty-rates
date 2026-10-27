"""Sample of edition cells for the independent recomputation (written 2026-10-24).
Usage: python -I make_keys.py MARTS_DIR > keys.json. Fixed seed; stratified by rate year and level."""
import json
import random
import sys

import duckdb

marts = sys.argv[1]
rows = duckdb.connect().execute(
    "SELECT t, level, code, prec_q3 FROM read_parquet(?) ORDER BY t, level, code",
    [f"{marts}/cells.parquet"]).fetchall()
PLAN = {"all": 1, "sec": 3, "cls": 4, "sub": 4, "grp": 6}
rng = random.Random(20261024)
keys = []
for t in (2012, 2013, 2019, 2026):
    for level, count in PLAN.items():
        pool = [(r[0], r[1], r[2]) for r in rows if r[0] == t and r[1] == level]
        published = [k for k, r in zip(pool, [r for r in rows if r[0] == t and r[1] == level]) if (r[3] or 0) > 0]
        others = sorted(set(pool) - set(published))
        chosen = rng.sample(published, min(count - (1 if level in ("sub", "grp") and others else 0), len(published)))
        if level in ("sub", "grp") and others:
            chosen.append(rng.choice(others))
        keys.extend(chosen)
for extra in ((2026, "sec", "K"), (2026, "sec", "L")):
    if extra not in keys:
        keys.append(extra)
json.dump([list(k) for k in sorted(set(keys))], sys.stdout)
