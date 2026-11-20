"""OCI image layout primitives.

The service speaks the OCI Image Layout format (``oci-layout``, ``index.json``,
``blobs/sha256/<hex>``).  Both OCI and Docker v2 media types are accepted for
blobs, because real-world image tars often mix them; the layout itself is always
served back as an OCI layout.

Nothing in this module touches the network or the service state.  It only
parses bytes and describes errors with the offending digest attached.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_CONFIG = "application/vnd.docker.container.image.v1+json"

INDEX_TYPES = frozenset({OCI_INDEX, DOCKER_LIST})
MANIFEST_TYPES = frozenset({OCI_MANIFEST, DOCKER_MANIFEST})
CONFIG_TYPES = frozenset({OCI_CONFIG, DOCKER_CONFIG})
LAYER_TYPES = frozenset(
    {
        "application/vnd.oci.image.layer.v1.tar",
        "application/vnd.oci.image.layer.v1.tar+gzip",
        "application/vnd.oci.image.layer.v1.tar+zstd",
        "application/vnd.docker.image.rootfs.diff.tar.gzip",
        "application/vnd.docker.image.rootfs.diff.tar",
    }
)

REQUIRED_PLATFORMS = ("linux/amd64", "linux/arm64")

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
BLOB_NAME_RE = re.compile(r"^[0-9a-f]{64}$")


class OciError(Exception):
    """Base class for package problems; ``code`` is machine readable."""

    code = "oci_error"
    http_status = 422

    def __init__(self, message: str, *, digest: str | None = None, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.digest = digest
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.digest is not None:
            payload["digest"] = self.digest
        if self.details:
            payload["details"] = self.details
        return payload


class BadPackage(OciError):
    code = "bad_package"
    http_status = 400


class ValidationFailed(OciError):
    code = "validation_failed"
    http_status = 422


class BlobMissing(ValidationFailed):
    code = "blob_missing"


class BlobMismatch(ValidationFailed):
    code = "blob_mismatch"


class UnsupportedPlatform(ValidationFailed):
    code = "unsupported_platform"


def is_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(DIGEST_RE.match(value))


def parse_digest(value: str) -> str:
    if not is_digest(value):
        raise BadPackage(
            "digest must be 'sha256:' followed by 64 lowercase hex characters",
            digest=value if isinstance(value, str) else None,
        )
    return value


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_path(digest: str) -> str:
    return f"blobs/sha256/{digest[len('sha256:'):]}"


@dataclass(frozen=True)
class Descriptor:
    media_type: str
    digest: str
    size: int
    platform: dict[str, str] | None = None
    annotations: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any, *, where: str) -> "Descriptor":
        if not isinstance(raw, dict):
            raise BadPackage(f"{where}: descriptor must be an object")
        media_type = raw.get("mediaType")
        digest = raw.get("digest")
        size = raw.get("size")
        if not isinstance(media_type, str) or not media_type:
            raise BadPackage(f"{where}: descriptor is missing mediaType")
        if not isinstance(digest, str) or not is_digest(digest):
            raise BadPackage(
                f"{where}: descriptor digest is not a sha256 digest",
                digest=digest if isinstance(digest, str) else None,
            )
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BadPackage(f"{where}: descriptor {digest} has an invalid size")
        platform = raw.get("platform")
        if platform is not None:
            if not isinstance(platform, dict):
                raise BadPackage(f"{where}: descriptor {digest} has an invalid platform")
            platform = {
                key: platform.get(key)
                for key in ("os", "architecture", "variant")
                if platform.get(key) is not None
            }
        annotations = raw.get("annotations")
        if annotations is not None and not isinstance(annotations, dict):
            raise BadPackage(f"{where}: descriptor {digest} has invalid annotations")
        return cls(media_type=media_type, digest=digest, size=size, platform=platform, annotations=annotations or {})

    @property
    def platform_key(self) -> str | None:
        if not self.platform:
            return None
        os_name = self.platform.get("os")
        arch = self.platform.get("architecture")
        if not os_name or not arch:
            return None
        return f"{os_name}/{arch}"

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mediaType": self.media_type,
            "digest": self.digest,
            "size": self.size,
        }
        if self.platform:
            out["platform"] = dict(self.platform)
        if self.annotations:
            out["annotations"] = dict(self.annotations)
        return out


def load_json(raw: bytes, *, where: str, digest: str | None = None) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadPackage(f"{where}: not valid UTF-8 JSON ({exc})", digest=digest) from exc
    if not isinstance(value, dict):
        raise BadPackage(f"{where}: JSON document must be an object", digest=digest)
    return value


@dataclass(frozen=True)
class PackageLayout:
    """What a validated layout tar claims about the image version."""

    root: Descriptor
    layout_index_bytes: bytes
    package_sha256: str
    blob_paths: dict[str, Path]  # digest -> extracted file (package-local only)


ALLOWED_TOP_FILES = {"oci-layout", "index.json"}


def _safe_member_name(name: str) -> str:
    if "\x00" in name or "\\" in name:
        raise BadPackage(f"tar entry has an unsafe name: {name!r}")
    # ``tar -C dir .`` prefixes every entry with "./"; accept and normalise it
    # rather than rejecting otherwise valid exports.
    while name.startswith("./"):
        name = name[2:]
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in {"..", ""} for part in pure.parts):
        raise BadPackage(f"tar entry has an unsafe path: {name!r}")
    return str(pure)


def extract_layout_tar(
    tar_path: Path,
    dest: Path,
    *,
    max_entries: int,
    max_total_bytes: int,
) -> dict[str, Path]:
    """Safely extract an OCI layout tar.

    Only regular files and directories are accepted; names are restricted to
    ``oci-layout``, ``index.json`` and ``blobs/sha256/<64 hex>``.  Symlinks,
    hardlinks, devices and duplicate entries are rejected outright.

    Every blob is hashed while it is copied, so a blob whose content does not
    match its file name is rejected here, before anything reaches the store.
    """
    dest.mkdir(parents=True, exist_ok=False)
    files: dict[str, Path] = {}
    seen: set[str] = set()
    total = 0
    try:
        archive = tarfile.open(tar_path, "r:*")
    except (tarfile.TarError, OSError) as exc:
        raise BadPackage(f"upload is not a readable tar archive: {exc}") from exc
    with archive:
        count = 0
        for member in archive:
            count += 1
            if count > max_entries:
                raise BadPackage(f"tar has more than {max_entries} entries")
            name = _safe_member_name(member.name)
            if name in {"", "."}:
                # ``tar -C dir .`` emits a root entry; it carries no data.
                if not member.isdir():
                    raise BadPackage("tar root entry is not a directory")
                continue
            if name in seen:
                raise BadPackage(f"tar contains duplicate entry: {name}")
            seen.add(name)
            if member.isdir():
                if name not in {"blobs", "blobs/sha256"}:
                    raise BadPackage(f"unexpected directory in OCI layout: {name}")
                (dest / name).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise BadPackage(f"tar entry is not a regular file: {name}")
            is_blob = name.startswith("blobs/sha256/") and BLOB_NAME_RE.match(
                name.rsplit("/", 1)[-1]
            )
            if not is_blob and name not in ALLOWED_TOP_FILES:
                raise BadPackage(
                    f"unexpected file in OCI image layout: {name} "
                    "(expected oci-layout, index.json and blobs/sha256/<digest>)"
                )
            total += member.size
            if total > max_total_bytes:
                raise BadPackage(f"uncompressed layout exceeds {max_total_bytes} bytes")
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:  # pragma: no cover - guarded by isfile()
                raise BadPackage(f"cannot read tar entry: {name}")
            hasher = hashlib.sha256()
            written = 0
            with source, open(target, "wb") as out:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > member.size:
                        raise BadPackage(f"tar entry {name} grew while extracting")
                    hasher.update(chunk)
                    out.write(chunk)
            if written != member.size:
                raise BadPackage(
                    f"tar entry {name} is truncated ({written} of {member.size} bytes)"
                )
            if is_blob:
                expected_name = name.rsplit("/", 1)[-1]
                if hasher.hexdigest() != expected_name:
                    raise BlobMismatch(
                        f"blob in package does not match its digest: sha256:{expected_name}",
                        digest="sha256:" + expected_name,
                        actual_digest="sha256:" + hasher.hexdigest(),
                        expected_size=member.size,
                        expected_source="package",
                    )
            files[name] = target
    return files


def read_layout(tar_path: Path, dest: Path, *, max_entries: int, max_total_bytes: int) -> PackageLayout:
    files = extract_layout_tar(
        tar_path, dest, max_entries=max_entries, max_total_bytes=max_total_bytes
    )
    if "oci-layout" not in files:
        raise BadPackage("layout tar is missing oci-layout")
    if "index.json" not in files:
        raise BadPackage("layout tar is missing index.json")
    layout_doc = load_json(files["oci-layout"].read_bytes(), where="oci-layout")
    if str(layout_doc.get("imageLayoutVersion", "")) != "1.0.0":
        raise BadPackage(
            "oci-layout must declare imageLayoutVersion 1.0.0",
            details={"found": layout_doc.get("imageLayoutVersion")},
        )

    index_bytes = files["index.json"].read_bytes()
    top = load_json(index_bytes, where="index.json")
    manifests = top.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        raise BadPackage("index.json has no manifests")
    if top.get("schemaVersion") != 2:
        raise BadPackage("index.json schemaVersion must be 2")

    blob_paths: dict[str, Path] = {}
    for name, path in files.items():
        if name.startswith("blobs/sha256/"):
            blob_paths["sha256:" + name.rsplit("/", 1)[-1]] = path

    def package_blob(digest: str) -> Path:
        try:
            return blob_paths[digest]
        except KeyError as exc:
            raise BlobMissing(
                f"blob {digest} is referenced by the package but not included in it",
                digest=digest,
                expected_source="package",
            ) from exc

    # Two accepted package shapes:
    #  * Form A: index.json is a layout index wrapping one version-index blob.
    #  * Form B: index.json *is* the multi-arch version index (as some tooling writes).
    single = Descriptor.parse(manifests[0], where="index.json manifests[0]") if len(manifests) == 1 else None
    form_a = (
        single is not None
        and single.platform is None
        and (single.media_type in INDEX_TYPES or single.media_type in MANIFEST_TYPES)
    )
    if form_a:
        root = single
        root_bytes = package_blob(root.digest).read_bytes()
        if len(root_bytes) != root.size:
            raise BlobMismatch(
                f"version index {root.digest} has size {len(root_bytes)} but descriptor says {root.size}",
                digest=root.digest,
                expected_size=root.size,
                actual_size=len(root_bytes),
                expected_source="package",
            )
        if digest_bytes(root_bytes) != root.digest:
            raise BlobMismatch(
                f"version index content does not match digest {root.digest}",
                digest=root.digest,
                expected_source="package",
            )
        layout_index_bytes = index_bytes
    else:
        # Form B: the raw index.json is the version index.  Reject extra entries
        # that are not platform manifests so the version stays a single image.
        descriptors = [
            Descriptor.parse(entry, where=f"index.json manifests[{i}]")
            for i, entry in enumerate(manifests)
        ]
        if any(desc.platform is None for desc in descriptors):
            raise BadPackage(
                "index.json mixes descriptors with and without platforms; "
                "expected either a layout index wrapping one version index or a multi-platform version index"
            )
        root = Descriptor(
            media_type=top.get("mediaType") or OCI_INDEX,
            digest=digest_bytes(index_bytes),
            size=len(index_bytes),
        )
        # ``index.json`` is not stored under blobs/ in the package, but the
        # version index must become a content-addressed blob in the store.
        # Materialise it in the staging extraction dir so the import path is
        # identical for both package shapes.
        synthetic = dest / digest_path(root.digest)
        synthetic.parent.mkdir(parents=True, exist_ok=True)
        synthetic.write_bytes(index_bytes)
        blob_paths[root.digest] = synthetic
        if root.media_type not in INDEX_TYPES:
            raise UnsupportedPlatform(
                "version index has an unsupported mediaType",
                digest=root.digest,
                details={"media_type": root.media_type, "supported": sorted(INDEX_TYPES)},
            )
        layout_index_bytes = _layout_index_for(root).encode()

    if root.media_type not in INDEX_TYPES:
        raise UnsupportedPlatform(
            "the version index must be an OCI image index (multi-platform); "
            f"got {root.media_type}",
            digest=root.digest,
            details={"supported": sorted(INDEX_TYPES)},
        )
    return PackageLayout(
        root=root,
        layout_index_bytes=layout_index_bytes,
        package_sha256=sha256_file(tar_path),
        blob_paths=blob_paths,
    )


def _layout_index_for(root: Descriptor) -> str:
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [
                {
                    "mediaType": root.media_type,
                    "digest": root.digest,
                    "size": root.size,
                    "annotations": {"org.opencontainers.image.ref.name": "current"},
                }
            ],
        },
        separators=(",", ":"),
    )


def layout_index_bytes(root: Descriptor) -> bytes:
    return _layout_index_for(root).encode()


def layout_document(root: Descriptor) -> bytes:
    """The ``oci-layout`` marker file for a layout serving ``root``."""
    return json.dumps({"imageLayoutVersion": "1.0.0"}, separators=(",", ":")).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_stream(source: BinaryIO, sink: BinaryIO | None = None, *, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = source.read(chunk_size)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
        if sink is not None:
            sink.write(chunk)
    if sink is not None:
        sink.flush()
    return "sha256:" + digest.hexdigest(), size


@dataclass(frozen=True)
class PlatformManifest:
    platform: str
    descriptor: Descriptor


@dataclass(frozen=True)
class ResolvedManifest:
    platform: str
    manifest: Descriptor
    config: Descriptor
    layers: tuple[Descriptor, ...]


def validate_version_index(raw: bytes, root: Descriptor) -> list[PlatformManifest]:
    doc = load_json(raw, where="version index", digest=root.digest)
    if doc.get("schemaVersion") != 2:
        raise ValidationFailed("version index schemaVersion must be 2", digest=root.digest)
    entries = doc.get("manifests")
    if not isinstance(entries, list) or not entries:
        raise ValidationFailed("version index has no manifests", digest=root.digest)
    found: dict[str, PlatformManifest] = {}
    for i, entry in enumerate(entries):
        desc = Descriptor.parse(entry, where=f"version index manifests[{i}]")
        key = desc.platform_key
        if key is None:
            raise UnsupportedPlatform(
                f"version index entry {desc.digest} has no os/architecture platform",
                digest=desc.digest,
            )
        if key not in REQUIRED_PLATFORMS:
            raise UnsupportedPlatform(
                f"version index contains unsupported platform {key} ({desc.digest}); "
                f"only {', '.join(REQUIRED_PLATFORMS)} are accepted",
                digest=desc.digest,
                details={"platform": key, "accepted": list(REQUIRED_PLATFORMS)},
            )
        if key in found:
            raise ValidationFailed(
                f"version index has more than one manifest for {key}",
                digest=desc.digest,
            )
        if desc.media_type not in MANIFEST_TYPES:
            raise UnsupportedPlatform(
                f"manifest for {key} has unsupported mediaType {desc.media_type}",
                digest=desc.digest,
            )
        found[key] = PlatformManifest(platform=key, descriptor=desc)
    missing = [p for p in REQUIRED_PLATFORMS if p not in found]
    if missing:
        raise ValidationFailed(
            f"version index is missing required platform(s): {', '.join(missing)}",
            details={"missing": missing, "found": sorted(found)},
        )
    return [found[p] for p in REQUIRED_PLATFORMS]


def validate_manifest(
    raw: bytes,
    manifest: Descriptor,
    platform: str,
    *,
    max_metadata_bytes: int,
) -> ResolvedManifest:
    doc = load_json(raw, where=f"{platform} manifest", digest=manifest.digest)
    if doc.get("schemaVersion") != 2:
        raise ValidationFailed(f"{platform} manifest schemaVersion must be 2", digest=manifest.digest)
    config = Descriptor.parse(doc.get("config"), where=f"{platform} manifest config")
    if config.media_type not in CONFIG_TYPES:
        raise UnsupportedPlatform(
            f"{platform} config has unsupported mediaType {config.media_type}",
            digest=config.digest,
        )
    layers_raw = doc.get("layers")
    if not isinstance(layers_raw, list):
        raise ValidationFailed(f"{platform} manifest has no layers array", digest=manifest.digest)
    layers = tuple(
        Descriptor.parse(entry, where=f"{platform} manifest layers[{i}]")
        for i, entry in enumerate(layers_raw)
    )
    for layer in layers:
        if layer.media_type not in LAYER_TYPES:
            raise UnsupportedPlatform(
                f"{platform} layer has unsupported mediaType {layer.media_type}",
                digest=layer.digest,
            )
        if layer.size > max_metadata_bytes * 1024:
            raise ValidationFailed(
                f"{platform} layer {layer.digest} is unreasonably large ({layer.size} bytes)",
                digest=layer.digest,
            )
    return ResolvedManifest(platform=platform, manifest=manifest, config=config, layers=layers)


def validate_config(raw: bytes, config: Descriptor, platform: str) -> None:
    doc = load_json(raw, where=f"{platform} config", digest=config.digest)
    os_name = doc.get("os")
    arch = doc.get("architecture")
    actual = f"{os_name}/{arch}"
    if actual != platform:
        raise ValidationFailed(
            f"{platform} config declares {actual}; image would not run on {platform}",
            digest=config.digest,
            details={"expected_platform": platform, "config_platform": actual},
        )
    rootfs = doc.get("rootfs")
    if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
        raise ValidationFailed(f"{platform} config rootfs.type must be 'layers'", digest=config.digest)
    diff_ids = rootfs.get("diff_ids")
    if not isinstance(diff_ids, list) or any(not is_digest(d) for d in diff_ids):
        raise ValidationFailed(f"{platform} config rootfs.diff_ids is invalid", digest=config.digest)
    return None


def diff_ids_of(raw: bytes) -> list[str]:
    doc = json.loads(raw.decode("utf-8"))
    return list(doc["rootfs"]["diff_ids"])
