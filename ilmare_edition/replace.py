"""Replace the share model in a verified mart directory without recalculating rates."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .marts import code_version, sha256, write_json
from .marts_io import verify_marts_digests
from .royalty import parse_model


def _first_tier_difference(old: list, new: list) -> str | None:
    for index in range(max(len(old), len(new))):
        if index >= len(old):
            return str(new[index].get("key", index))
        if index >= len(new):
            return str(old[index].get("key", index))
        if old[index] != new[index]:
            return str(old[index].get("key", new[index].get("key", index)))
    return None


def replace_share_model(marts: Path, share_model: Path, out: Path) -> None:
    """Copy verified marts, replacing only the model, its hashes, and replacement history."""
    source = marts.resolve()
    manifest, digests = verify_marts_digests(source)
    names = set(digests)
    entries = list(source.iterdir())
    for path in entries:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"source marts must contain only regular files: {path.name}")
    actual_names = {path.name for path in entries if path.name != "SHA256SUMS"}
    if actual_names != names:
        raise ValueError("source marts files do not match SHA256SUMS")

    try:
        old_raw = (source / "share_model.json").read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read source share_model.json: {exc}") from exc
    old_hash = hashlib.sha256(old_raw).hexdigest()
    if old_hash != digests["share_model.json"]:
        raise ValueError("source share_model.json does not match SHA256SUMS")
    if manifest.get("share_model_sha256") != old_hash:
        raise ValueError("manifest share_model_sha256 does not match share_model.json")
    try:
        old = json.loads(old_raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid source share_model.json: {exc}") from exc
    if not isinstance(old, dict):
        raise ValueError("source share_model.json must contain an object")
    if not isinstance(old.get("version"), str) or not old["version"].strip():
        raise ValueError("source share model version must be a nonempty string")
    if not isinstance(old.get("tiers"), list):
        raise ValueError("source share model tiers must be a list")
    if any(not isinstance(tier, dict) for tier in old["tiers"]):
        raise ValueError("source share model tiers must contain objects")
    try:
        new_raw = share_model.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read share model: {exc}") from exc
    new = parse_model(new_raw)
    difference = _first_tier_difference(old.get("tiers", []), new["tiers"])
    if difference is not None:
        raise ValueError(f"share model tiers differ at {difference}")
    if old.get("base_combination") != new["base_combination"]:
        raise ValueError("share model base_combination differs")
    new_hash = hashlib.sha256(new_raw).hexdigest()
    if new_hash == manifest["share_model_sha256"]:
        raise ValueError("share model does not change")

    if out.is_symlink():
        raise ValueError("out must not be a symbolic link")
    destination = out.resolve()
    if destination == source:
        raise ValueError("out must differ from source marts")
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("out and source marts must not contain one another")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("out must not exist or must be an empty directory")
    if not destination.parent.is_dir():
        raise ValueError("out parent must be an existing directory")

    temporary: Path | None = None
    try:
        temporary = Path(
            tempfile.mkdtemp(
                prefix=".replace-share-model-",
                dir=destination.parent,
            )
        )
        for name in sorted(names - {"manifest.json", "share_model.json"}):
            shutil.copyfile(source / name, temporary / name)
            if sha256(temporary / name) != digests[name]:
                raise ValueError(f"copied marts file does not match source SHA256SUMS: {name}")
        (temporary / "share_model.json").write_bytes(new_raw)

        updated_manifest = dict(manifest)
        updated_manifest["share_model_sha256"] = new_hash
        replacements = list(manifest.get("share_model_replacements", []))
        replacements.append(
            {
                "from_version": old.get("version"),
                "from_sha256": old_hash,
                "to_version": new["version"],
                "to_sha256": new_hash,
                "replaced_at": datetime.now(timezone.utc).isoformat(),
                "code_version": code_version(),
            }
        )
        updated_manifest["share_model_replacements"] = replacements
        write_json(temporary / "manifest.json", updated_manifest)

        output_files = sorted(path for path in temporary.iterdir() if path.is_file())
        (temporary / "SHA256SUMS").write_text(
            "".join(f"{sha256(path)}  {path.name}\n" for path in output_files),
            encoding="utf-8",
        )
        if destination.exists():
            destination.rmdir()
        current_umask = os.umask(0)
        os.umask(current_umask)
        temporary.chmod(0o777 & ~current_umask)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)

    print(f"share model {old['version']} -> {new['version']}: {destination}")
