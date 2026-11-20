from __future__ import annotations

import hashlib
import io
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import Store
from tools import make_sample

OCI_INDEX = make_sample.OCI_INDEX


@pytest.fixture(scope="session")
def binaries() -> dict[str, bytes]:
    """Deterministic per-architecture payloads (no network, no external files).

    Tests only need blobs with stable, distinct content; layers do not have to
    be runnable here.  The shipped ``samples/`` are the runnable artifacts.
    """
    return {
        platform: _pseudo_payload(platform, 512 * 1024)
        for platform in make_sample.BUSYBOX
    }


def _pseudo_payload(seed: str, size: int) -> bytes:
    out = bytearray()
    block = hashlib.sha256(seed.encode()).digest()
    while len(out) < size:
        block = hashlib.sha256(block).digest()
        out.extend(block)
    return bytes(out[:size])


@pytest.fixture(scope="session")
def v1(binaries) -> make_sample.ImageVersion:
    return make_sample.build_version(version="v1", arch_binaries=binaries)


@pytest.fixture(scope="session")
def v2(binaries, v1) -> make_sample.ImageVersion:
    return make_sample.build_version(
        version="v2", arch_binaries=binaries, base_from=make_sample.base_tars_from(v1)
    )


@dataclass
class Env:
    client: TestClient
    data_dir: Path
    store: Store

    def status(self) -> dict:
        return self.client.get("/api/v1/status").json()

    def active(self) -> str | None:
        return self.status()["active"]

    def import_tar(self, data: bytes, *, expected: str | None, name: str = "pkg.tar") -> "object":
        form = {"expected_current_digest": expected if expected is not None else "none"}
        return self.client.post(
            "/api/v1/images/import",
            files={"file": (name, data, "application/x-tar")},
            data=form,
        )

    def import_version(self, version: make_sample.ImageVersion, *, expected: str | None):
        return self.import_tar(version.package(), expected=expected, name=f"image-{version.version}.tar")


@pytest.fixture()
def make_env(tmp_path: Path):
    def _make(seed: make_sample.ImageVersion | None = None, **overrides) -> Env:
        data_dir = tmp_path / "data"
        settings = Settings(
            data_dir=data_dir,
            seed_initial_image=None,
            seed_required=False,
            max_import_bytes=64 * 1024 * 1024,
            **overrides,
        )
        store = Store(settings)
        app = create_app(settings=settings, store=store)
        client = TestClient(app)
        client.__enter__()
        if seed is not None:
            seed_path = tmp_path / f"seed-{seed.version}.tar"
            seed_path.write_bytes(seed.package())
            store.seed_if_empty(seed_path, source_name=seed_path.name)
        return Env(client=client, data_dir=data_dir, store=store)

    created: list[Env] = []

    def factory(*args, **kwargs) -> Env:
        env = _make(*args, **kwargs)
        created.append(env)
        return env

    yield factory
    for env in created:
        env.client.__exit__(None, None, None)


def extract_tar(data: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        for member in tf:
            handle = tf.extractfile(member)
            assert handle is not None
            out[member.name] = handle.read()
    return out


def package_blobs(data: bytes) -> dict[str, bytes]:
    files = extract_tar(data)
    return {
        "sha256:" + name.rsplit("/", 1)[-1]: content
        for name, content in files.items()
        if name.startswith("blobs/sha256/")
    }
