"""Staged-only import then explicit activation (the "interrupted before enable" path)."""

from __future__ import annotations

def test_stage_only_does_not_change_active(make_env, v1, v2):
    env = make_env(seed=v1)
    resp = env.client.post(
        "/api/v1/images/import",
        files={"file": ("image-v2.tar", v2.package(), "application/x-tar")},
        data={"expected_current_digest": v1.root_digest, "enable": "false"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "staged"
    assert body["changed"] is None
    assert body["digest"] == v2.root_digest
    assert env.active() == v1.root_digest
    # A staged version is downloadable in full.
    download = env.client.get(f"/api/v1/images/{v2.root_digest}/layout.tar")
    assert download.status_code == 200


def test_enable_staged_version(make_env, v1, v2):
    env = make_env(seed=v1)
    staged = env.client.post(
        "/api/v1/images/import",
        files={"file": ("image-v2.tar", v2.package(), "application/x-tar")},
        data={"expected_current_digest": v1.root_digest, "enable": "false"},
    )
    assert staged.status_code == 201
    enabled = env.client.post(
        f"/api/v1/images/{v2.root_digest}/enable",
        data={"expected_current_digest": v1.root_digest},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["digest"] == v2.root_digest
    assert env.active() == v2.root_digest


def test_enable_rejects_stale_expected(make_env, v1, v2):
    env = make_env(seed=v1)
    env.client.post(
        "/api/v1/images/import",
        files={"file": ("image-v2.tar", v2.package(), "application/x-tar")},
        data={"expected_current_digest": v1.root_digest, "enable": "false"},
    )
    resp = env.client.post(
        f"/api/v1/images/{v2.root_digest}/enable",
        data={"expected_current_digest": "sha256:" + "9" * 64},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["actual"] == v1.root_digest
    assert env.active() == v1.root_digest


def test_enable_unknown_version_404(make_env, v1):
    env = make_env(seed=v1)
    resp = env.client.post(
        "/api/v1/images/sha256:" + "2" * 64 + "/enable",
        data={"expected_current_digest": v1.root_digest},
    )
    assert resp.status_code == 404


def test_enable_refuses_when_blob_removed(make_env, v1, v2):
    env = make_env(seed=v1)
    env.client.post(
        "/api/v1/images/import",
        files={"file": ("image-v2.tar", v2.package(), "application/x-tar")},
        data={"expected_current_digest": v1.root_digest, "enable": "false"},
    )
    env.store.blobs.path(v2.base_layers["linux/arm64"]).unlink()
    resp = env.client.post(
        f"/api/v1/images/{v2.root_digest}/enable",
        data={"expected_current_digest": v1.root_digest},
    )
    assert resp.status_code == 422
    assert v2.base_layers["linux/arm64"] in resp.text
    assert env.active() == v1.root_digest
