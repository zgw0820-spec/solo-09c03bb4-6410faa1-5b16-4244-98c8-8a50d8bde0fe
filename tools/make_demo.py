"""Generate the shipped demo images and the incremental update packages.

Outputs (all under ``samples/``):

    image-v1.tar              initial image, complete (amd64 + arm64)
    image-v2.tar              full v2 image
    update-v2.tar             v2 index + new blobs only (base layers omitted)
    broken-missing-layer.tar  v3 metadata minus an arm64 layer  -> 422, digest named
    broken-bad-digest.tar     v3 with a corrupted layer          -> 422, digest named
    broken-mismatch-size.tar  v3 with a wrong descriptor size     -> 422, digest named
    demo.json                 digests, filenames, expected outcomes

Broken packages are built from a *v3* version that is never imported, so the
layers they omit or corrupt are genuinely unknown to the store at demo time.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools import make_sample
from tools.make_sample import build_version, canonical, sha256, tar_bytes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("samples"))
    parser.add_argument("--cache", type=Path, default=Path("tools/.cache"))
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    for stale in list(out.glob("*.tar")) + list(out.glob("*.digest")) + list(out.glob("*.meta.json")):
        stale.unlink()

    binaries = {
        platform: make_sample.fetch_busybox(args.cache, platform)
        for platform in make_sample.BUSYBOX
    }

    v1 = build_version(version="v1", arch_binaries=binaries)
    base_tars = make_sample.base_tars_from(v1)
    v2 = build_version(version="v2", arch_binaries=binaries, base_from=base_tars)
    v3 = build_version(version="v3", arch_binaries=binaries, base_from=base_tars)

    if set(v1.base_layers.values()) != set(v2.base_layers.values()):
        raise SystemExit("base layers must be identical across versions")
    if set(v1.app_layers.values()) & set(v2.app_layers.values()):
        raise SystemExit("app layers must differ across versions")

    (out / "image-v1.tar").write_bytes(v1.package())
    (out / "image-v2.tar").write_bytes(v2.package())

    # Incremental package: only v2 metadata + new app layers.  The base layers
    # must already be on the storage volume for this to import.
    incremental = set(v2.metadata_digests()) | set(v2.app_layers.values())
    (out / "update-v2.tar").write_bytes(v2.package(include=incremental))

    arm64 = "linux/arm64"
    amd64 = "linux/amd64"

    # 1. Missing layer: v3 metadata, arm64 app layer omitted (not in store).
    missing_digest = v3.app_layers[arm64]
    (out / "broken-missing-layer.tar").write_bytes(
        v3.package(include=set(v3.blobs) - {missing_digest})
    )

    # 2. Corrupt blob: same file name, different bytes.
    corrupted = dict(v3.blobs)
    layer = corrupted[v3.app_layers[arm64]]
    damaged = bytearray(layer)
    damaged[len(damaged) // 2] ^= 0xFF
    corrupted[v3.app_layers[arm64]] = bytes(damaged)
    (out / "broken-bad-digest.tar").write_bytes(_package_raw(v3, corrupted))

    # 3. Descriptor size mismatch on the amd64 app layer, manifest rehashed so
    #    the package is otherwise self-consistent.
    mismatch = _with_wrong_layer_size(v3, amd64, v3.app_layers[amd64])
    (out / "broken-mismatch-size.tar").write_bytes(mismatch["package"])

    demo = {
        "ref": "offline/image",
        "v1": {
            "file": "image-v1.tar",
            "indexDigest": v1.root_digest,
            "platforms": sorted(v1.base_layers),
        },
        "v2": {
            "file": "image-v2.tar",
            "updateFile": "update-v2.tar",
            "indexDigest": v2.root_digest,
            "fullSize": (out / "image-v2.tar").stat().st_size,
            "updateSize": (out / "update-v2.tar").stat().st_size,
            "newBlobs": sorted(set(v2.app_layers.values())),
            "reusedBlobs": sorted(set(v2.base_layers.values())),
        },
        "broken": {
            "missingLayer": {
                "file": "broken-missing-layer.tar",
                "missingDigest": missing_digest,
                "platform": arm64,
                "expect": "blob_missing",
            },
            "badDigest": {
                "file": "broken-bad-digest.tar",
                "corruptedDigest": v3.app_layers[arm64],
                "platform": arm64,
                "expect": "blob_mismatch",
            },
            "mismatchSize": {
                "file": "broken-mismatch-size.tar",
                "layerDigest": v3.app_layers[amd64],
                "platform": amd64,
                "expect": "blob_mismatch",
                **mismatch["meta"],
            },
        },
    }
    (out / "demo.json").write_text(json.dumps(demo, indent=2, sort_keys=True) + "\n")

    for name in sorted(out.glob("*.tar")):
        print(f"{name.name}: {name.stat().st_size} bytes")
    print(f"v1 index: {v1.root_digest}")
    print(f"v2 index: {v2.root_digest}")


def _package_raw(version, blobs: dict[str, bytes]) -> bytes:
    entries = [
        ("oci-layout", canonical({"imageLayoutVersion": "1.0.0"}), 0o644),
        ("index.json", version.layout_index_bytes(), 0o644),
    ]
    for digest in sorted(blobs):
        entries.append((f"blobs/sha256/{digest[len('sha256:'):]}", blobs[digest], 0o644))
    return tar_bytes(entries)


def _with_wrong_layer_size(version, platform: str, layer_digest: str) -> dict:
    blobs = dict(version.blobs)
    manifest_digest = version.platform_manifests[platform]
    manifest = json.loads(blobs[manifest_digest])
    wrong_size = None
    for entry in manifest["layers"]:
        if entry["digest"] == layer_digest:
            entry["size"] += 4096
            wrong_size = entry["size"]
    assert wrong_size is not None
    manifest_bytes = canonical(manifest)
    new_manifest_digest = sha256(manifest_bytes)
    blobs.pop(manifest_digest, None)
    blobs[new_manifest_digest] = manifest_bytes

    index = json.loads(version.index_bytes)
    for entry in index["manifests"]:
        if entry["digest"] == manifest_digest:
            entry["digest"] = new_manifest_digest
            entry["size"] = len(manifest_bytes)
    index_bytes = canonical(index)
    new_root = sha256(index_bytes)
    blobs[new_root] = index_bytes
    layout_index = canonical(
        {
            "schemaVersion": 2,
            "mediaType": make_sample.OCI_INDEX,
            "manifests": [
                {
                    "mediaType": make_sample.OCI_INDEX,
                    "digest": new_root,
                    "size": len(index_bytes),
                    "annotations": {"org.opencontainers.image.ref.name": "offline/image"},
                }
            ],
        }
    )
    entries = [
        ("oci-layout", canonical({"imageLayoutVersion": "1.0.0"}), 0o644),
        ("index.json", layout_index, 0o644),
    ]
    for digest in sorted(blobs):
        entries.append((f"blobs/sha256/{digest[len('sha256:'):]}", blobs[digest], 0o644))
    return {
        "package": tar_bytes(entries),
        "meta": {
            "descriptorSize": wrong_size,
            "actualSize": len(blobs[layer_digest]),
            "indexDigest": new_root,
        },
    }


if __name__ == "__main__":
    main()
