"""Share model replacement operates on verified synthetic marts only."""

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from ilmare_edition import marts_io
from ilmare_edition import replace as replace_module
from ilmare_edition.cli import main
from ilmare_edition.config import DEFAULT_MODEL
from ilmare_edition.marts import sha256
from ilmare_edition.marts_io import verify_marts
from ilmare_edition.replace import replace_share_model


def _seal(path: Path) -> None:
    manifest_path = path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["share_model_sha256"] = sha256(path / "share_model.json")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _seal_sums(path)


def _seal_sums(path: Path) -> None:
    files = sorted(file for file in path.iterdir() if file.name != "SHA256SUMS")
    (path / "SHA256SUMS").write_text(
        "".join(f"{sha256(file)}  {file.name}\n" for file in files), encoding="utf-8"
    )


def _v1_copy(source: Path, target: Path) -> Path:
    model = json.loads(DEFAULT_MODEL.read_text(encoding="utf-8"))
    model["version"] = "v1"
    del model["k_ip"]["patent_knowhow"]
    target.write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(target, source / "share_model.json")
    _seal(source)
    return target


def _file_hashes(path: Path) -> dict[str, str]:
    return {item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in path.iterdir()}


def test_verify_marts_returns_manifest_from_verified_bytes(synthetic, monkeypatch) -> None:
    from ilmare_edition.marts import build_marts

    build_marts(synthetic)
    manifest_path = synthetic.out / "manifest.json"
    original_manifest = json.loads(manifest_path.read_bytes())
    original_sha256 = marts_io._sha256

    def tamper_then_hash(path):
        if path.name == "sizes.parquet":
            manifest_path.write_text(
                json.dumps(dict(original_manifest, tampered=True)), encoding="utf-8"
            )
        return original_sha256(path)

    monkeypatch.setattr(marts_io, "_sha256", tamper_then_hash)
    manifest = verify_marts(synthetic.out)
    assert manifest == original_manifest
    assert "tampered" not in manifest
    assert json.loads(manifest_path.read_bytes())["tampered"] is True


def test_verify_marts_rejects_nonobject_manifest(synthetic) -> None:
    from ilmare_edition.marts import build_marts

    build_marts(synthetic)
    (synthetic.out / "manifest.json").write_text("[]", encoding="utf-8")
    _seal_sums(synthetic.out)
    with pytest.raises(ValueError, match="must contain an object"):
        verify_marts(synthetic.out)


def test_old_model_must_match_verified_digest(synthetic, tmp_path: Path, monkeypatch) -> None:
    from ilmare_edition.marts import build_marts

    build_marts(synthetic)
    source = synthetic.out
    model_path = _v1_copy(source, tmp_path / "share-v1.json")
    model = json.loads(model_path.read_bytes())
    model["version"] = "v1-x"
    tampered_raw = json.dumps(model).encode("utf-8")
    original_verify = replace_module.verify_marts_digests

    def verify_then_tamper(path):
        manifest, digests = original_verify(path)
        (path / "share_model.json").write_bytes(tampered_raw)
        manifest = dict(manifest, share_model_sha256=hashlib.sha256(tampered_raw).hexdigest())
        return manifest, digests

    monkeypatch.setattr(replace_module, "verify_marts_digests", verify_then_tamper)
    destination = tmp_path / "tampered-model"
    with pytest.raises(ValueError, match="does not match SHA256SUMS"):
        replace_share_model(source, DEFAULT_MODEL, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".replace-share-model-*"))


def test_replace_share_model_and_append_history(synthetic, tmp_path: Path, capsys) -> None:
    from ilmare_edition.marts import build_marts

    build_marts(synthetic)
    source = synthetic.out
    _v1_copy(source, tmp_path / "share-v1.json")
    source_hashes = _file_hashes(source)
    destination = tmp_path / "replaced"
    replacement = tmp_path / "share-v2-other-version.json"
    changed_model = json.loads(DEFAULT_MODEL.read_text(encoding="utf-8"))
    changed_model["version"] = "v2-next"
    replacement.write_text(json.dumps(changed_model), encoding="utf-8")

    replace_share_model(source, DEFAULT_MODEL, destination)
    assert capsys.readouterr().out == f"share model v1 -> v2: {destination.resolve()}\n"
    assert {item.name for item in destination.iterdir()} == {item.name for item in source.iterdir()}
    for name in source_hashes:
        if name.endswith(".parquet"):
            assert (destination / name).read_bytes() == (source / name).read_bytes()
    assert (destination / "share_model.json").read_bytes() == DEFAULT_MODEL.read_bytes()
    output_sums = dict(
        (name, digest)
        for line in (destination / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        for digest, name in [line.split("  ", 1)]
    )
    assert output_sums["share_model.json"] == sha256(destination / "share_model.json")
    source_manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    new_manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    unchanged_source = {
        key: value for key, value in source_manifest.items() if key != "share_model_sha256"
    }
    unchanged_output = {
        key: value
        for key, value in new_manifest.items()
        if key not in {"share_model_sha256", "share_model_replacements"}
    }
    assert unchanged_output == unchanged_source
    record = new_manifest["share_model_replacements"][0]
    assert record["from_version"] == "v1"
    assert record["from_sha256"] == source_manifest["share_model_sha256"]
    assert record["to_version"] == "v2"
    assert record["to_sha256"] == sha256(DEFAULT_MODEL)
    assert record["replaced_at"].endswith("+00:00")
    assert isinstance(record["code_version"], str)
    assert verify_marts(destination) == new_manifest
    assert _file_hashes(source) == source_hashes

    second = tmp_path / "replaced-again"
    replace_share_model(destination, replacement, second)
    second_manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    assert [item["to_version"] for item in second_manifest["share_model_replacements"]] == [
        "v2",
        "v2-next",
    ]


def test_copy_hash_mismatch_removes_temporary_and_does_not_create_destination(
    synthetic, tmp_path: Path, monkeypatch
) -> None:
    from ilmare_edition.marts import build_marts

    build_marts(synthetic)
    source = synthetic.out
    _v1_copy(source, tmp_path / "share-v1.json")
    destination = tmp_path / "tampered-copy"
    original_copyfile = replace_module.shutil.copyfile

    def copy_then_corrupt(src, dst):
        result = original_copyfile(src, dst)
        if Path(src).name == "royalty.parquet":
            copied = bytearray(Path(dst).read_bytes())
            copied[0] ^= 1
            Path(dst).write_bytes(copied)
        return result

    monkeypatch.setattr(replace_module.shutil, "copyfile", copy_then_corrupt)
    with pytest.raises(ValueError, match="royalty.parquet"):
        replace_share_model(source, DEFAULT_MODEL, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".replace-share-model-*"))


def test_destination_mode_matches_process_umask(synthetic, tmp_path: Path) -> None:
    from ilmare_edition.marts import build_marts

    build_marts(synthetic)
    source = synthetic.out
    _v1_copy(source, tmp_path / "share-v1.json")
    destination = tmp_path / "mode-out"
    previous_umask = os.umask(0o027)
    try:
        replace_share_model(source, DEFAULT_MODEL, destination)
    finally:
        os.umask(previous_umask)
    assert destination.stat().st_mode & 0o777 == 0o750


@pytest.mark.parametrize("dangling", [False, True])
def test_destination_symlinks_are_rejected_without_touching_target(
    synthetic, tmp_path: Path, dangling: bool
) -> None:
    from ilmare_edition.marts import build_marts

    build_marts(synthetic)
    source = synthetic.out
    _v1_copy(source, tmp_path / "share-v1.json")
    target = tmp_path / "link-target"
    if not dangling:
        target.mkdir()
    out = tmp_path / "out-link"
    out.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        replace_share_model(source, DEFAULT_MODEL, out)
    assert out.is_symlink()
    assert not target.exists() if dangling else target.is_dir() and not list(target.iterdir())
    assert not list(tmp_path.glob(".replace-share-model-*"))


def test_old_model_requires_version(synthetic, tmp_path: Path) -> None:
    from ilmare_edition.marts import build_marts

    build_marts(synthetic)
    source = synthetic.out
    model_path = source / "share_model.json"
    model = json.loads(model_path.read_text(encoding="utf-8"))
    model.pop("version")
    model_path.write_text(json.dumps(model), encoding="utf-8")
    _seal(source)
    destination = tmp_path / "missing-version"
    with pytest.raises(ValueError, match="version"):
        replace_share_model(source, DEFAULT_MODEL, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".replace-share-model-*"))


@pytest.mark.parametrize(
    "tier_update",
    [
        {"a": True},
        {"a": "1"},
        {"b": None},
        {"a": 10**400},
    ],
)
def test_cli_invalid_tier_parameter_returns_two(
    synthetic, tmp_path: Path, capsys, tier_update: dict
) -> None:
    from ilmare_edition.marts import build_marts

    build_marts(synthetic)
    source = synthetic.out
    _v1_copy(source, tmp_path / "share-v1.json")
    model = json.loads(DEFAULT_MODEL.read_text(encoding="utf-8"))
    model["tiers"][0].update(tier_update)
    candidate = tmp_path / "invalid-tier.json"
    candidate.write_text(json.dumps(model), encoding="utf-8")
    result = main(
        [
            "replace-share-model",
            "--marts",
            str(source),
            "--share-model",
            str(candidate),
            "--out",
            str(tmp_path / "cli-invalid"),
        ]
    )
    assert result == 2
    captured = capsys.readouterr()
    assert "tier " in captured.err
    assert "ilmare-edition:" in captured.err
    assert not (tmp_path / "cli-invalid").exists()


@pytest.mark.parametrize("difference", ["a", "target", "order", "base"])
def test_replacement_rejects_incompatible_model(synthetic, tmp_path: Path, difference: str) -> None:
    from ilmare_edition.marts import build_marts

    build_marts(synthetic)
    model = json.loads(DEFAULT_MODEL.read_text(encoding="utf-8"))
    if difference == "a":
        model["tiers"][0]["a"] += 1
    elif difference == "target":
        model["tiers"][0]["target"][0] += 0.01
    elif difference == "order":
        model["tiers"].reverse()
    else:
        model["base_combination"]["ip"] = "utility"
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(model), encoding="utf-8")
    destination = tmp_path / "out"
    with pytest.raises(ValueError):
        replace_share_model(synthetic.out, candidate, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".replace-share-model-*"))


@pytest.mark.parametrize(
    "failure", ["unchanged", "nonempty", "inside", "corrupt", "extra", "manifest"]
)
def test_replacement_rejects_invalid_source_or_destination(
    synthetic, tmp_path: Path, failure: str
) -> None:
    from ilmare_edition.marts import build_marts

    build_marts(synthetic)
    source = synthetic.out
    candidate = DEFAULT_MODEL
    destination = tmp_path / "out"
    if failure == "nonempty":
        destination.mkdir()
        (destination / "keep").write_text("keep")
    elif failure == "inside":
        destination = source / "inside"
    elif failure == "corrupt":
        path = source / "royalty.parquet"
        value = bytearray(path.read_bytes())
        value[0] ^= 1
        path.write_bytes(value)
    elif failure == "extra":
        (source / "extra.txt").write_text("extra")
    elif failure == "manifest":
        manifest_path = source / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["share_model_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        _seal_sums(source)
    else:
        candidate = source / "share_model.json"

    with pytest.raises(ValueError):
        replace_share_model(source, candidate, destination)
    assert not list(tmp_path.glob(".replace-share-model-*"))
    if failure == "nonempty":
        assert (destination / "keep").read_text() == "keep"
    else:
        assert not destination.exists()


def test_cli_replace_success_and_error(synthetic, tmp_path: Path, capsys) -> None:
    from ilmare_edition.marts import build_marts

    build_marts(synthetic)
    source = synthetic.out
    _v1_copy(source, tmp_path / "share-v1.json")
    destination = tmp_path / "cli-out"
    assert (
        main(
            [
                "replace-share-model",
                "--marts",
                str(source),
                "--share-model",
                str(DEFAULT_MODEL),
                "--out",
                str(destination),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.startswith("share model v1 -> v2:")
    assert (
        main(
            [
                "replace-share-model",
                "--marts",
                str(destination),
                "--share-model",
                str(DEFAULT_MODEL),
                "--out",
                str(tmp_path / "failed"),
            ]
        )
        == 2
    )
    assert "ilmare-edition:" in capsys.readouterr().err


@pytest.mark.parametrize("value", [5, "abc", None, {"a": 1}, [1]])
def test_replacement_history_must_be_list_of_objects(synthetic, tmp_path, value):
    from ilmare_edition.marts import build_marts

    build_marts(synthetic)
    source = synthetic.out
    manifest = json.loads((source / "manifest.json").read_bytes())
    manifest["share_model_replacements"] = value
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _seal_sums(source)
    out = tmp_path / "replacement"
    with pytest.raises(ValueError, match="share_model_replacements"):
        replace_share_model(source, DEFAULT_MODEL, out)
    assert not out.exists()
    assert not list(tmp_path.glob(".replace-share-model-*"))
    assert main([
        "replace-share-model", "--marts", str(source), "--share-model", str(DEFAULT_MODEL),
        "--out", str(out),
    ]) == 2
    assert not out.exists()
    assert not list(tmp_path.glob(".replace-share-model-*"))
