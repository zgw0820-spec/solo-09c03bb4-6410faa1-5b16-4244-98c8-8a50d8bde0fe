"""Blob sourcing rules: package wins, store completes, anything else is refused."""

from __future__ import annotations

import json

import pytest

from app import oci


def test_package_only_import_works_with_empty_store(make_env, v1):
    env = make_env()
    resp = env.import_version(v1, expected=None)
    assert resp.status_code == 201, resp.text
    assert all(source == "package" for source in resp.json()["blobs"].values())


def test_incremental_against_empty_store_names_missing_digest(make_env, v2):
    env = make_env()
    incremental = set(v2.metadata_digests()) | set(v2.app_layers.values())
    resp = env.import_tar(v2.package(include=incremental), expected=None)
    assert resp.status_code == 422, resp.text
    body = resp.json()["error"]
    issues = body.get("details", {}).get("issues") or [body]
    reported = {issue.get("digest") for issue in issues}
    assert set(v2.base_layers.values()) <= reported
    assert env.active() is None


def test_blob_with_correct_size_but_wrong_content_in_store(make_env, v1, v2):
    env = make_env(seed=v1)
    base = v2.base_layers["linux/arm64"]
    path = env.store.blobs.path(base)
    original = path.read_bytes()
    # Same length, different bytes: only a content hash catches this.
    tampered = bytes([original[0] ^ 0xFF]) + original[1:]
    path.write_bytes(tampered)
    incremental = set(v2.metadata_digests()) | set(v2.app_layers.values())
    resp = env.import_tar(v2.package(include=incremental), expected=v1.root_digest)
    assert resp.status_code == 422, resp.text
    assert base in json.dumps(resp.json()["error"])
    assert env.active() == v1.root_digest


def test_package_blob_wins_over_broken_store_copy(make_env, v1, v2):
    """A full package repairs a corrupted store copy instead of failing."""
    env = make_env(seed=v1)
    base = v2.base_layers["linux/amd64"]
    path = env.store.blobs.path(base)
    path.write_bytes(b"broken")
    resp = env.import_version(v2, expected=v1.root_digest)
    assert resp.status_code == 201, resp.text
    assert oci.digest_bytes(env.store.blobs.path(base).read_bytes()) == base


def test_activation_refuses_version_with_vanished_blob(make_env, v1, v2):
    env = make_env(seed=v1)
    tar_path, _, _ = env.store.stage_upload(
        __import__("io").BytesIO(v2.package()), max_bytes=64 << 20
    )
    record, _ = env.store.import_package(
        tar_path, expected_current_digest=v1.root_digest, source_name=None
    )
    env.store.blobs.path(v2.app_layers["linux/amd64"]).unlink()
    with pytest.raises(oci.BlobMissing):
        env.store.activate(record.digest, expected_current_digest=v1.root_digest)
    assert env.active() == v1.root_digest


def test_metadata_blob_from_store_is_used(make_env, v1, v2):
    """Even the version index may be completed from the store (already present)."""
    env = make_env(seed=v1)
    incremental = set(v2.metadata_digests()) | set(v2.app_layers.values())
    resp = env.import_tar(v2.package(include=incremental), expected=v1.root_digest)
    assert resp.status_code == 201
    sources = resp.json()["blobs"]
    assert sources[v2.root_digest] == "package"
    assert all(sources[d] == "store" for d in v2.base_layers.values())


def test_blobstore_has_no_temp_files_after_import(make_env, v1):
    env = make_env()
    assert env.import_version(v1, expected=None).status_code == 201
    leftovers = [p.name for p in (env.data_dir / "blobs" / "sha256").iterdir() if p.name.startswith(".")]
    assert leftovers == []
    leftovers = list((env.data_dir / "versions").glob(".tmp-*"))
    assert leftovers == []
