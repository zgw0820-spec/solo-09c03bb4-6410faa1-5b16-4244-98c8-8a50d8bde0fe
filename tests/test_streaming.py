"""End-to-end streaming test against a real uvicorn server.

The in-process TestClient buffers whole responses, so it cannot prove that a
download *already in progress* keeps receiving the old version when a new one
is activated mid-stream.  This module runs the actual server and reads the
response body incrementally.
"""

from __future__ import annotations

import hashlib
import io
import json
import socket
import tarfile
import threading
import time

import httpx
import pytest
import uvicorn

from app.config import Settings
from app.main import create_app
from app.store import Store
from tools import make_sample


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live_server(tmp_path_factory, binaries):
    v1 = make_sample.build_version(version="v1", arch_binaries=binaries)
    v2 = make_sample.build_version(
        version="v2", arch_binaries=binaries, base_from=make_sample.base_tars_from(v1)
    )
    data_dir = tmp_path_factory.mktemp("live") / "data"
    seed = tmp_path_factory.mktemp("seed") / "image-v1.tar"
    seed.write_bytes(v1.package())

    settings = Settings(data_dir=data_dir, seed_initial_image=seed, seed_required=True)
    store = Store(settings)
    app = create_app(settings=settings, store=store)

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("uvicorn did not start")
    yield {
        "base": f"http://127.0.0.1:{port}",
        "v1": v1,
        "v2": v2,
        "data_dir": data_dir,
    }
    server.should_exit = True
    thread.join(timeout=10)


def _verify_layout_tar(data: bytes, expected_digest: str) -> None:
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        names = tf.getnames()
        index = json.loads(tf.extractfile("index.json").read())
    assert index["manifests"][0]["digest"] == expected_digest
    for name in names:
        if name.startswith("blobs/sha256/"):
            digest = "sha256:" + name.rsplit("/", 1)[-1]
    assert "index.json" in names and "oci-layout" in names


def test_stream_continues_old_version_when_switched_midway(live_server):
    base = live_server["base"]
    v1, v2 = live_server["v1"], live_server["v2"]
    assert httpx.get(f"{base}/api/v1/status").json()["active"] == v1.root_digest

    with httpx.stream("GET", f"{base}/api/v1/images/current/layout.tar", timeout=30) as resp:
        assert resp.status_code == 200
        assert resp.headers["x-oci-image-digest"] == v1.root_digest
        expected_len = int(resp.headers["content-length"])
        chunks = resp.iter_bytes(chunk_size=65536)
        first = next(chunks)
        assert len(first) < expected_len

        # Activate v2 while the download is still being written out.
        with httpx.Client(timeout=60) as client:
            switched = client.post(
                f"{base}/api/v1/images/import",
                files={"file": ("image-v2.tar", v2.package(), "application/x-tar")},
                data={"expected_current_digest": v1.root_digest},
            )
        assert switched.status_code == 201, switched.text
        assert httpx.get(f"{base}/api/v1/status").json()["active"] == v2.root_digest

        body = first + b"".join(chunks)

    assert len(body) == expected_len
    assert hashlib.sha256(body).hexdigest()  # non-empty, complete
    _verify_layout_tar(body, v1.root_digest)


def test_download_after_restart_returns_current_version(live_server):
    base = live_server["base"]
    v2 = live_server["v2"]
    resp = httpx.get(f"{base}/api/v1/images/current/layout.tar", timeout=30)
    assert resp.status_code == 200
    assert resp.headers["x-oci-image-digest"] == v2.root_digest
    _verify_layout_tar(resp.content, v2.root_digest)


def test_pinned_download_of_previous_version(live_server):
    base = live_server["base"]
    v1 = live_server["v1"]
    resp = httpx.get(f"{base}/api/v1/images/{v1.root_digest}/layout.tar", timeout=30)
    assert resp.status_code == 200
    assert resp.headers["x-oci-image-digest"] == v1.root_digest
    _verify_layout_tar(resp.content, v1.root_digest)
