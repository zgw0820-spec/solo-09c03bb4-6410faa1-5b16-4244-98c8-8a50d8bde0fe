from __future__ import annotations

import io
import json
import tarfile

from tools import make_sample


def test_import_requires_expected_digest_error_shape(make_env, v1):
    env = make_env(seed=v1)
    resp = env.import_tar(v1.package(), expected="not-a-digest")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_expected_digest"


def test_import_v2_activates_and_reports_previous(make_env, v1, v2):
    env = make_env(seed=v1)
    assert env.active() == v1.root_digest
    resp = env.import_version(v2, expected=v1.root_digest)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "activated"
    assert body["digest"] == v2.root_digest
    assert body["previous"] == v1.root_digest
    assert body["changed"] is True
    assert body["generation"] == 2
    status = env.status()
    assert status["active"] == v2.root_digest
    assert status["version"]["platforms"] == ["linux/amd64", "linux/arm64"]


def test_incremental_package_completes_blobs_from_store(make_env, v1, v2):
    env = make_env(seed=v1)
    incremental = set(v2.metadata_digests()) | set(v2.app_layers.values())
    package = v2.package(include=incremental)
    # The base layers are absent from the package on disk.
    assert all(digest not in package_blob_names(package) for digest in v2.base_layers.values())
    resp = env.import_tar(package, expected=v1.root_digest)
    assert resp.status_code == 201, resp.text
    sources = resp.json()["blobs"]
    for digest in v2.base_layers.values():
        assert sources[digest] == "store"
    for digest in v2.app_layers.values():
        assert sources[digest] == "package"


def test_missing_blob_is_rejected_with_digest(make_env, v1, v2):
    env = make_env(seed=v1)
    arm64_layer = v2.app_layers["linux/arm64"]
    package = v2.package(include=set(v2.blobs) - {arm64_layer})
    resp = env.import_tar(package, expected=v1.root_digest)
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] in {"blob_missing", "validation_failed"}
    assert arm64_layer in json.dumps(error)
    # Nothing changed.
    assert env.active() == v1.root_digest


def test_corrupted_blob_is_rejected_with_digest(make_env, v1, v2):
    env = make_env(seed=v1)
    layer = v2.app_layers["linux/arm64"]
    blobs = dict(v2.blobs)
    data = bytearray(blobs[layer])
    data[100] ^= 0xFF
    blobs[layer] = bytes(data)
    package = v2.package_from(blobs) if hasattr(v2, "package_from") else _raw_package(v2, blobs)
    resp = env.import_tar(package, expected=v1.root_digest)
    assert resp.status_code == 422, resp.text
    assert layer in json.dumps(resp.json()["error"])
    assert env.active() == v1.root_digest


def test_wrong_descriptor_size_is_rejected(make_env, v1, v2):
    env = make_env(seed=v1)
    layer = v2.base_layers["linux/amd64"]
    blobs = dict(v2.blobs)
    manifest_digest = v2.platform_manifests["linux/amd64"]
    manifest = json.loads(blobs[manifest_digest])
    for entry in manifest["layers"]:
        if entry["digest"] == layer:
            entry["size"] += 1
    blobs[manifest_digest] = make_sample.canonical(manifest)
    # Re-point the index at the new manifest so the package is otherwise valid.
    new_manifest_digest = make_sample.sha256(blobs[manifest_digest])
    blobs.pop(manifest_digest)
    blobs[new_manifest_digest] = blobs.pop(new_manifest_digest, make_sample.canonical(manifest))
    index = json.loads(v2.index_bytes)
    for entry in index["manifests"]:
        if entry["digest"] == manifest_digest:
            entry["digest"] = new_manifest_digest
            entry["size"] = len(blobs[new_manifest_digest])
    new_root = make_sample.sha256(make_sample.canonical(index))
    blobs[new_root] = make_sample.canonical(index)
    layout_index = make_sample.canonical(
        {
            "schemaVersion": 2,
            "mediaType": make_sample.OCI_INDEX,
            "manifests": [
                {
                    "mediaType": make_sample.OCI_INDEX,
                    "digest": new_root,
                    "size": len(blobs[new_root]),
                    "annotations": {"org.opencontainers.image.ref.name": "offline/image"},
                }
            ],
        }
    )
    package = _tar_with(layout_index, blobs)
    resp = env.import_tar(package, expected=v1.root_digest)
    assert resp.status_code == 422, resp.text
    assert layer in json.dumps(resp.json()["error"])
    assert env.active() == v1.root_digest


def test_stale_expected_digest_is_conflict(make_env, v1, v2, tmp_path):
    env = make_env(seed=v1)
    resp = env.import_version(v2, expected="sha256:" + "0" * 64)
    assert resp.status_code == 409, resp.text
    body = resp.json()["error"]
    assert body["code"] == "version_conflict"
    assert body["expected"] == "sha256:" + "0" * 64
    assert body["actual"] == v1.root_digest
    assert body["stagedDigest"] == v2.root_digest
    # The running version is unchanged.
    assert env.active() == v1.root_digest


def test_import_twice_is_idempotent(make_env, v1):
    env = make_env(seed=v1)
    resp = env.import_version(v1, expected=v1.root_digest)
    assert resp.status_code == 201
    assert resp.json()["changed"] is False
    assert env.status()["generation"] == 1


def test_missing_both_platforms_rejected(make_env, binaries):
    env = make_env()
    single = make_sample.build_version(
        version="amd-only",
        arch_binaries={"linux/amd64": binaries["linux/amd64"]},
    )
    resp = env.import_tar(single.package(), expected=None)
    assert resp.status_code == 422, resp.text
    assert "linux/arm64" in resp.text
    assert env.active() is None


def test_unsupported_platform_rejected(make_env, binaries):
    env = make_env()
    image = make_sample.build_version(
        version="three",
        arch_binaries={
            "linux/amd64": binaries["linux/amd64"],
            "linux/arm64": binaries["linux/arm64"],
            "linux/riscv64": binaries["linux/arm64"],
        },
    )
    resp = env.import_tar(image.package(), expected=None)
    assert resp.status_code == 422, resp.text
    assert "riscv64" in resp.text


def test_flat_layout_form_b_is_accepted(make_env, v2):
    env = make_env()
    blobs = {d: b for d, b in v2.blobs.items() if d != v2.root_digest}
    package = _tar_with(v2.index_bytes, blobs)
    resp = env.import_tar(package, expected=None)
    assert resp.status_code == 201, resp.text
    assert resp.json()["digest"] == v2.root_digest


def test_tar_path_traversal_rejected(make_env, v1):
    env = make_env(seed=v1)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("../../etc/passwd")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    resp = env.import_tar(buf.getvalue(), expected=v1.root_digest)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_package"


def test_garbage_upload_rejected(make_env, v1):
    env = make_env(seed=v1)
    resp = env.import_tar(b"this is not a tar", expected=v1.root_digest)
    assert resp.status_code == 400
    assert env.active() == v1.root_digest


def _raw_package(version, blobs: dict[str, bytes]) -> bytes:
    return _tar_with(version.layout_index_bytes(), blobs)


def _tar_with(layout_index: bytes, blobs: dict[str, bytes]) -> bytes:
    entries = [
        ("oci-layout", make_sample.canonical({"imageLayoutVersion": "1.0.0"}), 0o644),
        ("index.json", layout_index, 0o644),
    ]
    for digest in sorted(blobs):
        entries.append((f"blobs/sha256/{digest.split(':', 1)[1]}", blobs[digest], 0o644))
    return make_sample.tar_bytes(entries)


def package_blob_names(data: bytes) -> dict[str, bytes]:
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        for member in tf:
            if member.name.startswith("blobs/sha256/"):
                out["sha256:" + member.name.rsplit("/", 1)[-1]] = b""
    return out


def test_tar_with_dot_prefix_and_gzip_accepted(make_env, v1):
    """Packages produced by ``tar -C layout -czf pkg.tgz .`` must import."""
    import subprocess
    import tempfile
    from pathlib import Path

    env = make_env()
    with tempfile.TemporaryDirectory() as td:
        layout_dir = Path(td) / "layout"
        layout_dir.mkdir()
        with tarfile.open(fileobj=io.BytesIO(v1.package())) as tf:
            tf.extractall(layout_dir)
        for args in (["-cf"], ["-czf"]):
            out = Path(td) / f"pkg{args[0]}.tgz"
            subprocess.run(["tar", "-C", str(layout_dir), *args, str(out), "."], check=True)
            expected = env.active()
            resp = env.client.post(
                "/api/v1/images/import",
                files={"file": (out.name, out.read_bytes(), "application/octet-stream")},
                data={"expected_current_digest": expected or "none"},
            )
            assert resp.status_code == 201, resp.text
            assert resp.json()["digest"] == v1.root_digest
