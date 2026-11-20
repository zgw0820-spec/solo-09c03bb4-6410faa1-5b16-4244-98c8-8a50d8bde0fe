#!/usr/bin/env python3
"""Scripted demo of the whole lifecycle against a running instance.

    python scripts/demo.py --base http://127.0.0.1:8080

Steps: status -> download v1 -> ingest incremental update v2 -> verify store
reuse -> reject the three broken packages (digest named) -> conflict on a stale
expected digest -> download v2 -> restart-safe check.

Uses only the standard library so it runs inside the container image as-is.
"""

from __future__ import annotations

import argparse
import io
import json
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


def request(method: str, url: str, *, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def get_json(url: str):
    status, _, body = request("GET", url)
    return status, json.loads(body) if body else None


def multipart(fields: dict[str, str], file_field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----demo" + uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f"--{boundary}\r\n".encode())
        out.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.write(str(value).encode() + b"\r\n")
    out.write(f"--{boundary}\r\n".encode())
    out.write(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    )
    out.write(b"Content-Type: application/x-tar\r\n\r\n")
    out.write(content)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def import_package(base: str, filename: str, content: bytes, expected: str | None):
    fields = {"expected_current_digest": expected or "none"}
    body, content_type = multipart(fields, "file", filename, content)
    status, _, payload = request(
        "POST", f"{base}/api/v1/images/import", data=body, headers={"Content-Type": content_type}
    )
    return status, json.loads(payload)


def import_package_staged(base: str, filename: str, content: bytes, expected: str | None):
    fields = {"expected_current_digest": expected or "none", "enable": "false"}
    body, content_type = multipart(fields, "file", filename, content)
    status, _, payload = request(
        "POST", f"{base}/api/v1/images/import", data=body, headers={"Content-Type": content_type}
    )
    return status, json.loads(payload)


def request_json(method: str, url: str, *, form: dict[str, str] | None = None):
    data = None
    headers = {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    status, _, payload = request(method, url, data=data, headers=headers)
    return status, json.loads(payload) if payload else None


def download(base: str, digest: str | None):
    url = f"{base}/api/v1/images/{'current' if digest is None else digest}/layout.tar"
    status, headers, body = request("GET", url)
    if status != 200:
        raise RuntimeError(f"download failed: {status} {body[:200]}")
    length = headers.get("Content-Length") or headers.get("content-length")
    assert length is not None and int(length) == len(body), "truncated download"
    digest = headers.get("X-OCI-Image-Digest") or headers.get("x-oci-image-digest")
    return digest, body


def summarize_layout(body: bytes) -> dict:
    with tarfile.open(fileobj=io.BytesIO(body)) as tf:
        names = tf.getnames()
        layout_index = json.loads(tf.extractfile("index.json").read())
        index_digest = layout_index["manifests"][0]["digest"]
        root_name = "blobs/sha256/" + index_digest.split(":", 1)[1]
        version_index = json.loads(tf.extractfile(root_name).read())
    return {
        "indexDigest": index_digest,
        "platforms": sorted(
            f"{e['platform']['os']}/{e['platform']['architecture']}" for e in version_index["manifests"]
        ),
        "blobs": len([n for n in names if n.startswith("blobs/sha256/")]),
        "bytes": len(body),
    }


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    step("Status before demo")
    status, state = get_json(f"{base}/api/v1/status")
    check("status reachable", status == 200)
    catalog_status, catalog = get_json(f"{base}/api/v1/demo/samples")
    check("sample catalog reachable", catalog_status == 200)
    meta = catalog["demo"]
    v1_digest = meta["v1"]["indexDigest"]
    v2_digest = meta["v2"]["indexDigest"]
    active = state["active"]
    check(
        "v1 is the active version",
        active == v1_digest,
        f"active={active}",
    )

    step("Download current (v1) and verify it is a complete OCI layout")
    digest, body = download(base, None)
    info = summarize_layout(body)
    check("current download is v1", digest == v1_digest)
    check("layout has both platforms", info["platforms"] == ["linux/amd64", "linux/arm64"])
    print(json.dumps(info, indent=2))

    def sample(name: str) -> bytes:
        status, _, content = request("GET", f"{base}/api/v1/demo/samples/{name}")
        if status != 200:
            raise SystemExit(f"cannot fetch {name}: {status}")
        return content

    step("Import the incremental v2 package (index + new blobs only)")
    update = sample(meta["v2"]["updateFile"])
    print(f"update package: {len(update)} bytes; full image: {meta['v2'].get('fullSize')} bytes")
    status, result = import_package(base, meta["v2"]["updateFile"], update, v1_digest)
    check("incremental import activated", status == 201, f"status={status}")
    check("new index digest", result.get("digest") == v2_digest)
    reused = [d for d, src in result["blobs"].items() if src == "store"]
    check("base layers completed from the storage volume", bool(reused), f"{len(reused)} blobs from store")
    print(f"store-reused blobs: {reused}")

    step("Reject the three broken packages (each names the offending digest)")
    broken = meta["broken"]
    cases = [
        ("missing layer", broken["missingLayer"], broken["missingLayer"]["missingDigest"], "blob_missing"),
        ("corrupted blob", broken["badDigest"], broken["badDigest"]["corruptedDigest"], "blob_mismatch"),
        ("descriptor size mismatch", broken["mismatchSize"], broken["mismatchSize"]["layerDigest"], "blob_mismatch"),
    ]
    for label, info_broken, expected_digest, expect_code in cases:
        content = sample(info_broken["file"])
        status, result = import_package(base, info_broken["file"], content, v2_digest)
        err = result.get("error", {})
        named = expected_digest in json.dumps(result)
        check(
            f"{label} rejected with 422 and digest named",
            status == 422 and named and err.get("code") == expect_code,
            f"status={status} code={err.get('code')} digest={err.get('digest')}",
        )

    step("Stale expected digest -> 409 conflict, active version unchanged")
    v2_full = sample(meta["v2"]["file"])
    status, result = import_package(base, meta["v2"]["file"], v2_full, v1_digest)
    check("conflict returned", status == 409, f"status={status}")
    check(
        "conflict reports expected/actual",
        result["error"].get("expected") == v1_digest and result["error"].get("actual") == v2_digest,
    )
    _, state = get_json(f"{base}/api/v1/status")
    check("active version still v2", state["active"] == v2_digest)

    step("Re-import v2 with the correct expected digest is idempotent")
    status, result = import_package(base, meta["v2"]["file"], v2_full, v2_digest)
    check("200-created with changed=false", status == 201 and result.get("changed") is False)

    step("Stage-only import leaves the active version untouched, then explicit enable")
    staged = import_package_staged(base, meta["v2"]["file"], v2_full, v2_digest)
    check("package validated and staged", staged[0] == 201 and staged[1].get("status") == "staged")
    _, state = get_json(f"{base}/api/v1/status")
    check("active version unchanged after staging", state["active"] == v2_digest)
    enable_status, enable_body = request_json(
        "POST", f"{base}/api/v1/images/{v2_digest}/enable",
        form={"expected_current_digest": v2_digest},
    )
    check("enable of the already active version is a no-op", enable_status == 200 and enable_body.get("changed") is False)

    step("Download current (now v2) and the pinned v1 layout")
    digest, body = download(base, None)
    check("current download is v2", digest == v2_digest)
    pinned_digest, pinned = download(base, v1_digest)
    check("pinned v1 download still available", pinned_digest == v1_digest)
    check(
        "pinned v1 layout verifies",
        summarize_layout(pinned)["indexDigest"] == v1_digest,
    )

    print("\nDemo completed successfully.")


if __name__ == "__main__":
    main()
