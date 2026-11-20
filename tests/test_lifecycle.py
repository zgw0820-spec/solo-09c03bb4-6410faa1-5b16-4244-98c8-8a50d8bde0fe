from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest

from app import oci
from app.store import SingleInstanceViolation, Store
from tests.conftest import extract_tar


def test_download_current_is_complete_layout(make_env, v1):
    env = make_env(seed=v1)
    resp = env.client.get("/api/v1/images/current/layout.tar")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/x-tar"
    assert resp.headers["x-oci-image-digest"] == v1.root_digest
    assert int(resp.headers["content-length"]) == len(resp.content)

    files = extract_tar(resp.content)
    assert files["oci-layout"] == make_canonical({"imageLayoutVersion": "1.0.0"})
    layout_index = json.loads(files["index.json"])
    root = layout_index["manifests"][0]
    assert root["digest"] == v1.root_digest
    # Every blob the version needs (including the version index itself) is present.
    for digest in v1.blobs:
        assert f"blobs/sha256/{digest.split(':', 1)[1]}" in files
    # The downloaded layout is itself importable.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        layout = oci.read_layout(
            _write(td, resp.content), Path(td) / "x", max_entries=1000, max_total_bytes=64 << 20
        )
    assert layout.root.digest == v1.root_digest


def test_download_pinned_version_after_upgrade(make_env, v1, v2):
    env = make_env(seed=v1)
    assert env.import_version(v2, expected=v1.root_digest).status_code == 201
    pinned = env.client.get(f"/api/v1/images/{v1.root_digest}/layout.tar")
    assert pinned.status_code == 200
    assert pinned.headers["x-oci-image-digest"] == v1.root_digest
    current = env.client.get("/api/v1/images/current/layout.tar")
    assert current.headers["x-oci-image-digest"] == v2.root_digest


def test_download_unknown_version_404(make_env, v1):
    env = make_env(seed=v1)
    resp = env.client.get("/api/v1/images/sha256:" + "1" * 64 + "/layout.tar")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "version_not_found"


def test_download_with_no_active_version_503(make_env):
    env = make_env()
    resp = env.client.get("/api/v1/images/current/layout.tar")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_active_version"


def test_inflight_download_survives_version_switch(make_env, v1, v2):
    """A download that started before a switch keeps streaming the old version."""
    env = make_env(seed=v1)
    record, version_dir = env.store.resolve_download(None)
    assert record.digest == v1.root_digest
    # Build the stream plan exactly as the endpoint does, then switch versions.
    plan_blobs = [env.store.blobs.path(d) for d in record.blobs]
    resp = env.import_version(v2, expected=v1.root_digest)
    assert resp.status_code == 201
    assert env.active() == v2.root_digest
    # The pinned files are untouched: same bytes, same digest.
    for digest, path in zip(record.blobs, plan_blobs):
        data = path.read_bytes()
        assert oci.digest_bytes(data) == digest


def test_restart_serves_current_version(make_env, v1, v2, tmp_path):
    env = make_env(seed=v1)
    assert env.import_version(v2, expected=v1.root_digest).status_code == 201
    env.client.__exit__(None, None, None)

    from app.config import Settings
    from app.main import create_app
    from fastapi.testclient import TestClient

    settings = Settings(data_dir=env.data_dir, seed_initial_image=None, seed_required=False)
    with TestClient(create_app(settings=settings, store=Store(settings))) as client:
        resp = client.get("/api/v1/images/current/layout.tar")
        assert resp.status_code == 200
        assert resp.headers["x-oci-image-digest"] == v2.root_digest


def test_single_instance_lock(tmp_path):
    from app.config import Settings

    settings = Settings(data_dir=tmp_path / "data", seed_initial_image=None, seed_required=False)
    first = Store(settings)
    second = Store(settings)
    first.acquire_single_instance_lock()
    try:
        with pytest.raises(SingleInstanceViolation):
            second.acquire_single_instance_lock()
    finally:
        first.release_single_instance_lock()
    second.acquire_single_instance_lock()
    second.release_single_instance_lock()


def test_failed_import_keeps_previous_and_cleans_tmp(make_env, v1, v2):
    env = make_env(seed=v1)
    arm64_layer = v2.app_layers["linux/arm64"]
    broken = v2.package(include=set(v2.blobs) - {arm64_layer})
    for _ in range(2):
        resp = env.import_tar(broken, expected=v1.root_digest)
        assert resp.status_code == 422
    assert env.active() == v1.root_digest
    assert list((env.data_dir / "tmp").iterdir()) == []


def test_interrupted_import_before_enable_keeps_previous(make_env, v1, v2):
    """Simulate a crash between staging and activation."""
    env = make_env(seed=v1)
    with env.store._lock:  # type: ignore[attr-defined]
        tar_path, _, _ = env.store.stage_upload(
            io.BytesIO(v2.package()), max_bytes=64 << 20
        )
        record, _ = env.store.import_package(
            tar_path, expected_current_digest=v1.root_digest, source_name=None
        )
        assert record.digest == v2.root_digest
    # No activation happened: the old version is still served.
    assert env.active() == v1.root_digest
    resp = env.client.get("/api/v1/images/current/layout.tar")
    assert resp.headers["x-oci-image-digest"] == v1.root_digest
    # The staged version is listed but not active.
    versions = env.client.get("/api/v1/versions").json()
    by_digest = {v["digest"]: v for v in versions["versions"]}
    assert by_digest[v2.root_digest]["active"] is False


def test_concurrent_imports_one_wins_one_conflicts(make_env, v1, v2):
    """Two imports racing on the same expected digest: one 201, one 409."""
    env = make_env(seed=v1)
    package = v2.package()
    barrier = threading.Barrier(2)
    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        resp = env.client.post(
            "/api/v1/images/import",
            files={"file": ("pkg.tar", package, "application/x-tar")},
            data={"expected_current_digest": v1.root_digest},
        )
        with lock:
            results.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = sorted(code for code, _ in results)
    assert codes == [201, 409], results
    conflict = [body for code, body in results if code == 409][0]
    assert conflict["error"]["code"] == "version_conflict"
    assert env.active() == v2.root_digest


def test_download_during_concurrent_imports_stays_consistent(make_env, v1, v2):
    env = make_env(seed=v1)
    package = v2.package()
    stop = threading.Event()
    seen: list[str] = []

    def downloader():
        while not stop.is_set():
            resp = env.client.get("/api/v1/images/current/layout.tar")
            assert resp.status_code == 200
            assert int(resp.headers["content-length"]) == len(resp.content)
            seen.append(resp.headers["x-oci-image-digest"])

    thread = threading.Thread(target=downloader)
    thread.start()
    try:
        for _ in range(3):
            env.import_version(v2, expected=env.active())
    finally:
        stop.set()
        thread.join()
    assert seen
    assert set(seen) <= {v1.root_digest, v2.root_digest}
    assert env.active() == v2.root_digest


def test_store_rejects_corrupted_store_blob(make_env, v1, v2):
    """If a blob on the storage volume rots, import refuses it and names it."""
    env = make_env(seed=v1)
    base = v2.base_layers["linux/amd64"]
    path = env.store.blobs.path(base)
    data = bytearray(path.read_bytes())
    data[10] ^= 0xFF
    path.write_bytes(bytes(data))
    incremental = set(v2.metadata_digests()) | set(v2.app_layers.values())
    resp = env.import_tar(v2.package(include=incremental), expected=v1.root_digest)
    assert resp.status_code == 422, resp.text
    assert base in json.dumps(resp.json()["error"])
    assert env.active() == v1.root_digest


def _write(directory: str, data: bytes) -> Path:
    path = Path(directory) / "layout.tar"
    path.write_bytes(data)
    return path


def make_canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
