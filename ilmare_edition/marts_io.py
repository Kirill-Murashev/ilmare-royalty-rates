"""Verified reading of mart directories: checksum file, manifest and parquet tables."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

import duckdb

TABLES = ("cells", "royalty", "qgrid", "sizes", "funnel", "okved_names")
REQUIRED_MARTS = {
    "cells.parquet", "royalty.parquet", "qgrid.parquet", "sizes.parquet",
    "funnel.parquet", "okved_names.parquet", "share_model.json", "manifest.json",
}
SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
HEX64 = re.compile(r"[0-9a-f]{64}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_rows(marts: Path, table: str) -> list[dict[str, Any]]:
    """Read a parquet table without pandas and preserve its declared columns."""
    if table not in TABLES:
        raise ValueError(f"unsupported marts table: {table}")
    path = marts / f"{table}.parquet"
    with duckdb.connect() as con:
        result = con.execute("SELECT * FROM read_parquet(?)", [str(path)])
        names = [column[0] for column in result.description]
        return [dict(zip(names, row, strict=True)) for row in result.fetchall()]


def verify_marts(marts: Path) -> dict[str, Any]:
    """Verify every entry in the checksum file before reading any mart."""
    return verify_marts_digests(marts)[0]


def verify_marts_digests(marts: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """Return the verified manifest and digests from a single checksum file read."""
    marts = marts.resolve()
    manifest_path = marts / "manifest.json"
    sums_path = marts / "SHA256SUMS"
    if not manifest_path.is_file() or not sums_path.is_file():
        raise ValueError("--marts must contain manifest.json and SHA256SUMS")
    entries: dict[str, str] = {}
    for line_number, line in enumerate(sums_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        if "  " not in line:
            raise ValueError(f"invalid SHA256SUMS line {line_number}")
        digest, name = line.split("  ", 1)
        relative = PurePosixPath(name)
        valid_digest = len(digest) == 64 and all(
            character in "0123456789abcdefABCDEF" for character in digest
        )
        if not valid_digest:
            raise ValueError(f"invalid SHA-256 digest on line {line_number}")
        if relative.is_absolute() or len(relative.parts) != 1 or relative.name in (".", ".."):
            raise ValueError(f"invalid marts checksum path on line {line_number}: {name}")
        if name in entries:
            raise ValueError(f"duplicate entry in SHA256SUMS: {name}")
        entries[name] = digest.lower()
    missing = REQUIRED_MARTS.difference(entries)
    if missing:
        raise ValueError(f"SHA256SUMS omits required marts files: {', '.join(sorted(missing))}")
    manifest_raw = manifest_path.read_bytes()
    for name, expected in entries.items():
        path = marts / name
        if not path.is_file():
            raise ValueError(f"marts checksum target is missing: {name}")
        actual = (
            hashlib.sha256(manifest_raw).hexdigest() if name == "manifest.json" else _sha256(path)
        )
        if actual != expected:
            raise ValueError(f"SHA-256 mismatch for marts file: {name}")
    try:
        manifest = json.loads(manifest_raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise ValueError(f"invalid marts manifest.json: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("marts manifest.json must contain an object")
    return manifest, entries
