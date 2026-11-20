"""Build OCI image layout tars for demos and tests.

Images built here are real and runnable: each layer contains a static busybox
binary, exported by the config's Entrypoint, so a downloaded layout can be
``docker load``-ed and run offline.

Two layer kinds are produced per architecture:

* a **base** layer that is identical across versions (busybox + /etc/hostname),
  so a later version can be shipped without it and completed from the store;
* an **app** layer that carries ``/etc/version`` and changes every version.

The image index therefore changes with every version, while the base layers
stay byte-identical, which is exactly what an incremental update package on a
USB stick looks like.

Usage::

    python -m tools.make_sample --out samples --version v1
    python -m tools.make_sample --out samples --version v2 --reuse-base-from samples/image-v1.tar.pkg
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"

BUSYBOX = {
    "linux/amd64": ("busybox-static_1.37.0-6+b9_amd64.deb", "usr/bin/busybox"),
    "linux/arm64": ("busybox-static_1.37.0-6+b9_arm64.deb", "usr/bin/busybox"),
}
BUSYBOX_URL = "https://deb.debian.org/debian/pool/main/b/busybox/{}"


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def gzip_bytes(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=6) as gz:
        gz.write(data)
    return buf.getvalue()


def tar_bytes(entries: list[tuple[str, bytes, int]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def deb_extract(deb_path: Path, member: str) -> bytes:
    """Extract one file from a .deb (ar + tar.xz) without dpkg."""
    deb_path = deb_path.resolve()
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["ar", "x", str(deb_path)], cwd=td, check=True, capture_output=True)
        data_tar = next(Path(td).glob("data.tar.*"))
        with tarfile.open(data_tar) as tf:
            for info in tf.getmembers():
                if info.name.lstrip("./") == member:
                    handle = tf.extractfile(info)
                    assert handle is not None
                    return handle.read()
    raise KeyError(f"{member} not in {deb_path}")


def fetch_busybox(cache: Path, platform: str) -> bytes:
    deb_name, member = BUSYBOX[platform]
    cache.mkdir(parents=True, exist_ok=True)
    deb = cache / deb_name
    if not deb.is_file():
        print(f"downloading {deb_name}")
        subprocess.run(
            ["curl", "-fsSL", "--retry", "3", "-o", str(deb), BUSYBOX_URL.format(deb_name)],
            check=True,
        )
    return deb_extract(deb, member)


@dataclass
class ImageVersion:
    version: str
    root_digest: str
    root_size: int
    index_bytes: bytes
    blobs: dict[str, bytes]  # every blob of this version, digest -> bytes
    base_layers: dict[str, str]  # platform -> layer digest that repeats across versions
    app_layers: dict[str, str]
    layer_bytes: dict[str, bytes]  # layer digest -> compressed bytes

    @property
    def platform_manifests(self) -> dict[str, str]:
        doc = json.loads(self.index_bytes)
        return {
            f"{e['platform']['os']}/{e['platform']['architecture']}": e["digest"]
            for e in doc["manifests"]
        }

    def package(self, *, include: set[str] | None = None) -> bytes:
        """Build a layout tar; ``include`` restricts which blobs travel.

        The version index and all its metadata blobs are always included.
        """
        selected = set(self.blobs) if include is None else set(include)
        # Metadata is mandatory for validation.
        for digest in self.metadata_digests():
            selected.add(digest)
        entries: list[tuple[str, bytes, int]] = [
            ("oci-layout", canonical({"imageLayoutVersion": "1.0.0"}), 0o644),
            ("index.json", self.layout_index_bytes(), 0o644),
        ]
        for digest in sorted(selected):
            entries.append(
                (f"blobs/sha256/{digest[len('sha256:'):]}", self.blobs[digest], 0o644)
            )
        return tar_bytes(entries)

    def metadata_digests(self) -> set[str]:
        out = {self.root_digest}
        for manifest_digest in self.platform_manifests.values():
            out.add(manifest_digest)
            manifest = json.loads(self.blobs[manifest_digest])
            out.add(manifest["config"]["digest"])
        return out

    def layout_index_bytes(self) -> bytes:
        return canonical(
            {
                "schemaVersion": 2,
                "mediaType": OCI_INDEX,
                "manifests": [
                    {
                        "mediaType": OCI_INDEX,
                        "digest": self.root_digest,
                        "size": self.root_size,
                        "annotations": {"org.opencontainers.image.ref.name": "offline/image"},
                    }
                ],
            }
        )


def _base_layer(binary: bytes) -> bytes:
    return tar_bytes(
        [
            ("bin/", b"", 0o755),
            ("bin/busybox", binary, 0o755),
            ("etc/", b"", 0o755),
            ("etc/hostname", b"offline-importer\n", 0o644),
        ]
    )


def _app_layer(version: str, platform: str, ref_name: str) -> bytes:
    return tar_bytes(
        [
            ("etc/version", f"{ref_name}:{version} ({platform})\n".encode(), 0o644),
            ("etc/app.conf", f"release={version}\n".encode(), 0o644),
        ]
    )


def build_version(
    *,
    version: str,
    arch_binaries: dict[str, bytes],
    ref_name: str = "offline/image",
    base_from: dict[str, bytes] | None = None,
) -> ImageVersion:
    base_from = base_from or {}
    blobs: dict[str, bytes] = {}
    base_layers: dict[str, str] = {}
    app_layers: dict[str, str] = {}
    layer_bytes: dict[str, bytes] = {}
    index_entries = []

    for platform, binary in arch_binaries.items():
        os_name, arch = platform.split("/")
        base_tar = base_from.get(platform) or _base_layer(binary)
        app_tar = _app_layer(version, platform, ref_name)
        diff_ids = [sha256(base_tar), sha256(app_tar)]
        layers = []
        for raw in (base_tar, app_tar):
            gz = gzip_bytes(raw)
            digest = sha256(gz)
            blobs[digest] = gz
            layer_bytes[digest] = gz
            layers.append({"mediaType": OCI_LAYER, "digest": digest, "size": len(gz)})
        base_layers[platform] = layers[0]["digest"]
        app_layers[platform] = layers[1]["digest"]

        config = {
            "architecture": arch,
            "os": os_name,
            "created": "1970-01-01T00:00:00Z",
            "config": {
                "Env": [f"IMAGE_VERSION={version}"],
                "Entrypoint": ["/bin/busybox"],
                "Cmd": ["sh", "-c", f"echo {ref_name} {version}; exec sh"],
                "Labels": {"org.opencontainers.image.version": version},
            },
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
            "history": [
                {"created": "1970-01-01T00:00:00Z", "created_by": f"base ({platform})"},
                {"created": "1970-01-01T00:00:00Z", "created_by": f"app {version} ({platform})"},
            ],
        }
        config_bytes = canonical(config)
        config_digest = sha256(config_bytes)
        blobs[config_digest] = config_bytes

        manifest = {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {"mediaType": OCI_CONFIG, "digest": config_digest, "size": len(config_bytes)},
            "layers": layers,
        }
        manifest_bytes = canonical(manifest)
        manifest_digest = sha256(manifest_bytes)
        blobs[manifest_digest] = manifest_bytes
        index_entries.append(
            {
                "mediaType": OCI_MANIFEST,
                "digest": manifest_digest,
                "size": len(manifest_bytes),
                "platform": {"os": os_name, "architecture": arch},
            }
        )

    index_bytes = canonical({"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": index_entries})
    root_digest = sha256(index_bytes)
    blobs[root_digest] = index_bytes
    return ImageVersion(
        version=version,
        root_digest=root_digest,
        root_size=len(index_bytes),
        index_bytes=index_bytes,
        blobs=blobs,
        base_layers=base_layers,
        app_layers=app_layers,
        layer_bytes=layer_bytes,
    )


def base_tars_from(previous: ImageVersion) -> dict[str, bytes]:
    """Recover the uncompressed base layer tar per platform from a prior build."""
    out: dict[str, bytes] = {}
    for platform, digest in previous.base_layers.items():
        out[platform] = gzip.decompress(previous.layer_bytes[digest])
    return out


def load_base_tars(pkg_json: Path) -> dict[str, bytes]:
    """Recover the uncompressed base-layer tars from a generated ":class:`ImageVersion`"."""
    meta = json.loads(pkg_json.read_text())
    out: dict[str, bytes] = {}
    for platform, digest in meta["baseLayers"].items():
        blob = pkg_json.parent / "blobs" / digest.split(":", 1)[1]
        out[platform] = gzip.decompress(blob.read_bytes())
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("samples"))
    parser.add_argument("--cache", type=Path, default=Path("tools/.cache"))
    parser.add_argument("--version", default="v1")
    parser.add_argument("--ref", default="offline/image")
    parser.add_argument(
        "--base-from",
        type=Path,
        help="reuse base layers from a previously exported image.json + blobs/ dir",
    )
    args = parser.parse_args()

    binaries = {platform: fetch_busybox(args.cache, platform) for platform in BUSYBOX}
    base_from = load_base_tars(args.base_from) if args.base_from else None
    image = build_version(
        version=args.version, arch_binaries=binaries, ref_name=args.ref, base_from=base_from
    )
    args.out.mkdir(parents=True, exist_ok=True)
    dest = args.out / f"image-{args.version}.tar"
    dest.write_bytes(image.package())
    print(f"wrote {dest} ({dest.stat().st_size} bytes)")
    print(f"index digest: {image.root_digest}")
    (args.out / f"image-{args.version}.digest").write_text(image.root_digest + "\n")


if __name__ == "__main__":
    main()
