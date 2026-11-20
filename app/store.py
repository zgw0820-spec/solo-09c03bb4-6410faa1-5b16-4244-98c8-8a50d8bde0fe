"""On-disk image store: content-addressed blobs, versions and the active pointer.

Layout of ``DATA_DIR``::

    lock                       advisory single-instance lock (held by the process)
    tmp/                       staging area for uploads / extractions
    blobs/sha256/<hex>         immutable, content-addressed blob store
    versions/<hex>/            a fully validated, staged image version
        meta.json              resolved platforms + blob list + provenance
        index.json             OCI layout index for this version
    state.json                 {"active": "<sha256:...>|null", "generation": N}

Every mutation is write-to-temp + fsync + rename, so a crash before ``enable``
leaves the previous active version untouched.  Blobs and versions are never
deleted while a version references them; that is what lets a download that
started before an upgrade keep streaming the old version.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from . import oci
from .config import Settings


class StoreError(Exception):
    pass


class SingleInstanceViolation(StoreError):
    pass


class StateConflict(StoreError):
    def __init__(
        self,
        message: str,
        *,
        expected: str | None,
        actual: str | None,
        staged_digest: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.expected = expected
        self.actual = actual
        self.staged_digest = staged_digest


class VersionNotFound(StoreError):
    def __init__(self, digest: str) -> None:
        super().__init__(f"version {digest} has not been imported")
        self.digest = digest


class NotReady(StoreError):
    def __init__(self, message: str = "no image version is currently active") -> None:
        super().__init__(message)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{secrets.token_hex(6)}")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_expected(value: str | None) -> str | None:
    """Normalise the client supplied expected-current token.

    ``None`` (omitted) and the literal ``none`` both mean "expect no active
    version".  A digest must be a full ``sha256:`` digest.
    """
    if value is None:
        return None
    value = value.strip()
    if value.lower() in {"", "none", "null"}:
        return None
    return oci.parse_digest(value)


@dataclass(frozen=True)
class VersionRecord:
    digest: str
    size: int
    media_type: str
    platforms: dict[str, dict[str, Any]]
    blobs: tuple[str, ...]
    imported_at: str
    package_sha256: str | None
    expected_current_digest: str | None
    source_name: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "size": self.size,
            "mediaType": self.media_type,
            "platforms": sorted(self.platforms),
            "blobCount": len(self.blobs),
            "importedAt": self.imported_at,
            "packageSha256": self.package_sha256,
            "sourceName": self.source_name,
        }


def _version_meta(record: VersionRecord, layout_index: bytes) -> dict[str, Any]:
    return {
        "formatVersion": 1,
        "root": {"mediaType": record.media_type, "digest": record.digest, "size": record.size},
        "platforms": record.platforms,
        "blobs": list(record.blobs),
        "importedAt": record.imported_at,
        "packageSha256": record.package_sha256,
        "expectedCurrentDigest": record.expected_current_digest,
        "sourceName": record.source_name,
        "layoutIndex": layout_index.decode("utf-8"),
    }


def _record_from_meta(digest: str, meta: dict[str, Any]) -> VersionRecord:
    root = meta["root"]
    return VersionRecord(
        digest=digest,
        size=int(root["size"]),
        media_type=root["mediaType"],
        platforms=meta["platforms"],
        blobs=tuple(meta["blobs"]),
        imported_at=meta["importedAt"],
        package_sha256=meta.get("packageSha256"),
        expected_current_digest=meta.get("expectedCurrentDigest"),
        source_name=meta.get("sourceName"),
    )


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "sha256").mkdir(exist_ok=True)

    def path(self, digest: str) -> Path:
        oci.parse_digest(digest)
        return self.root / "sha256" / digest[len("sha256:"):]

    def has(self, digest: str) -> bool:
        return self.path(digest).is_file()

    def stat_size(self, digest: str) -> int | None:
        try:
            return self.path(digest).stat().st_size
        except FileNotFoundError:
            return None

    def verify(self, digest: str, size: int | None = None) -> None:
        path = self.path(digest)
        if not path.is_file():
            raise oci.BlobMissing(
                f"blob {digest} is not in the package and is missing from the store",
                digest=digest,
                expected_source="store",
            )
        actual_size = path.stat().st_size
        if size is not None and actual_size != size:
            raise oci.BlobMismatch(
                f"stored blob {digest} has size {actual_size} but {size} was expected",
                digest=digest,
                expected_size=size,
                actual_size=actual_size,
                expected_source="store",
            )
        actual_digest = "sha256:" + oci.sha256_file(path)
        if actual_digest != digest:
            raise oci.BlobMismatch(
                f"stored blob content hashes to {actual_digest}, expected {digest}",
                digest=digest,
                actual_digest=actual_digest,
                expected_source="store",
            )

    def read_bytes(self, digest: str, size: int | None = None, *, max_bytes: int) -> bytes:
        path = self.path(digest)
        if not path.is_file():
            raise oci.BlobMissing(
                f"blob {digest} is not in the package and is missing from the store",
                digest=digest,
                expected_source="store",
            )
        actual_size = path.stat().st_size
        if size is not None and actual_size != size:
            raise oci.BlobMismatch(
                f"stored blob {digest} has size {actual_size} but {size} was expected",
                digest=digest,
                expected_size=size,
                actual_size=actual_size,
                expected_source="store",
            )
        if actual_size > max_bytes:
            raise oci.ValidationFailed(
                f"blob {digest} is larger than the metadata limit ({max_bytes} bytes)",
                digest=digest,
            )
        data = path.read_bytes()
        actual = oci.digest_bytes(data)
        if actual != digest:
            raise oci.BlobMismatch(
                f"stored blob content hashes to {actual}, expected {digest}",
                digest=digest,
                actual_digest=actual,
                expected_source="store",
            )
        return data

    def put_file(self, digest: str, source: Path, size: int, *, preverified: bool = False) -> str:
        """Copy ``source`` into the store unless an identical copy already exists.

        ``preverified`` is only set by callers that hashed ``source`` under the
        same exclusive staging lock; it avoids a second read of large layers.
        """
        dest = self.path(digest)
        if dest.is_file():
            try:
                self.verify(digest, size)
                return "store"
            except oci.OciError:
                # The package copy is authoritative.  Repairing is done with an
                # atomic replace below, never by unlinking first: an in-flight
                # download may still hold the old (corrupt) file open.
                pass
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.tmp-{secrets.token_hex(6)}")
        try:
            if preverified:
                shutil.copyfile(source, tmp)
                actual_size = tmp.stat().st_size
                actual_digest = digest
            else:
                with open(source, "rb") as src, open(tmp, "wb") as sink:
                    actual_digest, actual_size = oci.hash_stream(src, sink, chunk_size=1024 * 1024)
            if actual_digest != digest:
                raise oci.BlobMismatch(
                    f"blob content hashes to {actual_digest}, expected {digest}",
                    digest=digest,
                    actual_digest=actual_digest,
                    expected_source="package",
                )
            if actual_size != size:
                raise oci.BlobMismatch(
                    f"blob has size {actual_size}, descriptor says {size}",
                    digest=digest,
                    expected_size=size,
                    actual_size=actual_size,
                    expected_source="package",
                )
            with open(tmp, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(tmp, dest)
            _fsync_dir(dest.parent)
        finally:
            if tmp.exists():
                tmp.unlink()
        return "package"


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.data_dir = settings.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # The lock must be taken before touching tmp/ or versions/, so a second
        # process cannot delete the first process's in-flight staging.
        self._lock = threading.RLock()
        self._lock_file: BinaryIO | None = None
        self.tmp_dir = self.data_dir / "tmp"
        self.versions_dir = self.data_dir / "versions"
        self.blobs = BlobStore(self.data_dir / "blobs")
        self.state_path = self.data_dir / "state.json"

    def acquire_single_instance_lock(self) -> None:
        lock_path = self.data_dir / "lock"
        handle = open(lock_path, "a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise SingleInstanceViolation(
                    f"{lock_path} is locked by another importer process; "
                    "this service must run as a single instance"
                ) from exc
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n{utcnow()}\n".encode())
        handle.flush()
        os.fsync(handle.fileno())
        self._lock_file = handle
        # Now that we own the data dir, prepare it and clear leftovers from a
        # previous crash.
        self.tmp_dir.mkdir(exist_ok=True)
        self.versions_dir.mkdir(exist_ok=True)
        self._cleanup_stale_staging()

    def release_single_instance_lock(self) -> None:
        if self._lock_file is not None:
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_file.close()
                self._lock_file = None

    def _cleanup_stale_staging(self) -> None:
        if not self.tmp_dir.is_dir():
            return
        for stale in self.tmp_dir.iterdir():
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
            else:
                stale.unlink(missing_ok=True)
        for stale in self.versions_dir.glob(".tmp-*"):
            shutil.rmtree(stale, ignore_errors=True)

    # -- logging / state ---------------------------------------------------
    def event(self, message: str, **fields: Any) -> None:
        print(
            json.dumps({"ts": utcnow(), "event": message, **fields}, separators=(",", ":")),
            flush=True,
        )

    def _read_state(self) -> dict[str, Any]:
        try:
            raw = self.state_path.read_bytes()
        except FileNotFoundError:
            return {"active": None, "generation": 0}
        try:
            state = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StoreError(f"{self.state_path} is corrupt: {exc}") from exc
        if not isinstance(state, dict) or "active" not in state:
            raise StoreError(f"{self.state_path} is corrupt")
        return state

    def _write_state(self, active: str | None) -> dict[str, Any]:
        current = self._read_state()
        state = {
            "active": active,
            "generation": int(current.get("generation", 0)) + 1,
            "updatedAt": utcnow(),
        }
        _atomic_write(self.state_path, json.dumps(state, indent=2).encode())
        return state

    def active_digest(self) -> str | None:
        return self._read_state().get("active")

    def state_json(self) -> dict[str, Any]:
        state = self._read_state()
        active = state.get("active")
        return {
            "active": active,
            "generation": state.get("generation", 0),
            "updatedAt": state.get("updatedAt"),
            "version": self.get_version(active).as_json() if active else None,
        }

    # -- versions ----------------------------------------------------------
    def version_dir(self, digest: str) -> Path:
        oci.parse_digest(digest)
        return self.versions_dir / digest[len("sha256:"):]

    def get_version(self, digest: str) -> VersionRecord:
        meta_path = self.version_dir(digest) / "meta.json"
        try:
            meta = json.loads(meta_path.read_bytes())
        except FileNotFoundError as exc:
            raise VersionNotFound(digest) from exc
        return _record_from_meta(digest, meta)

    def list_versions(self) -> list[VersionRecord]:
        records: list[VersionRecord] = []
        for entry in self.versions_dir.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            digest = "sha256:" + entry.name
            try:
                records.append(self.get_version(digest))
            except (VersionNotFound, KeyError, ValueError, TypeError):
                continue
        records.sort(key=lambda r: r.imported_at, reverse=True)
        return records

    def _write_version(self, record: VersionRecord, layout_index: bytes, *, blobs: tuple[str, ...]) -> None:
        final = self.version_dir(record.digest)
        if final.is_dir():
            existing = self.get_version(record.digest)
            missing = [b for b in existing.blobs if not self.blobs.has(b)]
            if missing:
                raise oci.BlobMissing(
                    f"version {record.digest} was staged before but blob {missing[0]} "
                    "is missing; fix the store before re-importing",
                    digest=missing[0],
                    expected_source="store",
                )
            return
        staging = self.versions_dir / f".tmp-{record.digest[len('sha256:'):]}-{secrets.token_hex(4)}"
        staging.mkdir()
        try:
            meta = _version_meta(record, layout_index)
            with open(staging / "meta.json", "wb") as handle:
                handle.write(json.dumps(meta, indent=2).encode())
                handle.flush()
                os.fsync(handle.fileno())
            with open(staging / "index.json", "wb") as handle:
                handle.write(layout_index)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_dir(staging)
            try:
                os.replace(staging, final)
            except OSError as exc:
                # Another importer staged the same version concurrently.
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
                if not final.is_dir():
                    raise
            _fsync_dir(self.versions_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    # -- import ------------------------------------------------------------
    def stage_upload(self, upload: BinaryIO, *, max_bytes: int) -> tuple[Path, str, int]:
        """Spool an uploaded tar to disk with a hard size cap and sha256."""
        tar_path = self.tmp_dir / f"upload-{secrets.token_hex(8)}.tar"
        digest = hashlib.sha256()
        size = 0
        try:
            with open(tar_path, "wb") as out:
                while True:
                    chunk = upload.read(self.settings.stream_chunk_size)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise oci.BadPackage(
                            f"upload exceeds the {max_bytes} byte limit",
                            details={"limit": max_bytes},
                        )
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            tar_path.unlink(missing_ok=True)
            raise
        if size == 0:
            tar_path.unlink(missing_ok=True)
            raise oci.BadPackage("uploaded tar is empty")
        return tar_path, "sha256:" + digest.hexdigest(), size

    def import_package(
        self,
        tar_path: Path,
        *,
        expected_current_digest: str | None,
        source_name: str | None,
    ) -> tuple[VersionRecord, dict[str, str]]:
        """Validate + stage a layout tar; does not activate it."""
        extract_dir = self.tmp_dir / f"extract-{secrets.token_hex(8)}"
        try:
            package = oci.read_layout(
                tar_path,
                extract_dir,
                max_entries=self.settings.max_tar_entries,
                max_total_bytes=self.settings.max_import_bytes,
            )
            return self._stage_package(
                package,
                expected_current_digest=expected_current_digest,
                source_name=source_name,
            )
        finally:
            shutil.rmtree(extract_dir, ignore_errors=True)
            tar_path.unlink(missing_ok=True)

    def _stage_package(
        self,
        package: oci.PackageLayout,
        *,
        expected_current_digest: str | None,
        source_name: str | None,
    ) -> tuple[VersionRecord, dict[str, str]]:
        root = package.root
        root_bytes = self._read_metadata(root, package)
        platforms = oci.validate_version_index(root_bytes, root)

        sources: dict[str, str] = {root.digest: _source_of(root.digest, package)}
        platform_meta: dict[str, dict[str, Any]] = {}
        sizes: dict[str, int] = {root.digest: root.size}
        ordered: list[str] = [root.digest]
        issues: list[oci.OciError] = []

        for entry in platforms:
            try:
                manifest_bytes = self._read_metadata(entry.descriptor, package)
            except oci.OciError as exc:
                issues.append(exc)
                continue
            resolved = oci.validate_manifest(
                manifest_bytes,
                entry.descriptor,
                entry.platform,
                max_metadata_bytes=self.settings.max_metadata_bytes,
            )
            try:
                config_bytes = self._read_metadata(resolved.config, package)
            except oci.OciError as exc:
                issues.append(exc)
                config_bytes = None
            if config_bytes is not None:
                oci.validate_config(config_bytes, resolved.config, entry.platform)
                diff_ids = oci.diff_ids_of(config_bytes)
                if len(diff_ids) != len(resolved.layers):
                    raise oci.ValidationFailed(
                        f"{entry.platform} config lists {len(diff_ids)} diff_ids but the "
                        f"manifest has {len(resolved.layers)} layers",
                        digest=resolved.config.digest,
                    )
                if self.settings.verify_diff_ids:
                    problem = self._diff_id_problem(resolved, diff_ids, package, entry.platform)
                    if problem is not None:
                        issues.append(problem)

            for layer in resolved.layers:
                try:
                    self._check_available(layer, package)
                except oci.OciError as exc:
                    issues.append(exc)

            platform_meta[entry.platform] = {
                "manifest": entry.descriptor.as_json(),
                "config": resolved.config.as_json(),
                "layers": [desc.as_json() for desc in resolved.layers],
            }
            for descriptor in (entry.descriptor, resolved.config, *resolved.layers):
                ordered.append(descriptor.digest)
                sizes[descriptor.digest] = descriptor.size
                sources[descriptor.digest] = _source_of(descriptor.digest, package)

        if issues:
            raise _aggregate(issues)

        # Deduplicate while preserving order (root, then per-platform manifest,
        # config and layers).
        seen: set[str] = set()
        blobs: list[str] = []
        for digest in ordered:
            if digest not in seen:
                seen.add(digest)
                blobs.append(digest)

        with self._lock:
            for digest in blobs:
                size = sizes[digest]
                if sources[digest] == "package":
                    self.blobs.put_file(digest, package.blob_paths[digest], size)
                elif self.settings.verify_store_blobs:
                    self.blobs.verify(digest, size)
                elif not self.blobs.has(digest):
                    raise oci.BlobMissing(
                        f"required blob {digest} is missing from the store",
                        digest=digest,
                        expected_source="store",
                    )

            record = VersionRecord(
                digest=root.digest,
                size=root.size,
                media_type=root.media_type,
                platforms=platform_meta,
                blobs=tuple(blobs),
                imported_at=utcnow(),
                package_sha256=package.package_sha256,
                expected_current_digest=expected_current_digest,
                source_name=source_name,
            )
            self._write_version(record, package.layout_index_bytes, blobs=tuple(blobs))
        self.event(
            "import.staged",
            digest=root.digest,
            blobs=len(blobs),
            platforms=sorted(platform_meta),
        )
        return record, sources

    def _diff_id_problem(
        self,
        resolved: oci.ResolvedManifest,
        diff_ids: list[str],
        package: oci.PackageLayout,
        platform: str,
    ) -> oci.OciError | None:
        """Compare uncompressed layer content with config ``diff_ids``.

        Compressed layers would need decompression (and zstd support) just to be
        verified here; the descriptor digest already covers those bytes, so the
        check is limited to uncompressed layers.
        """
        for layer, diff_id in zip(resolved.layers, diff_ids):
            if not layer.media_type.endswith((".tar", "diff.tar")):
                continue
            path = package.blob_paths.get(layer.digest)
            source = "package"
            if path is None:
                path = self.blobs.path(layer.digest)
                source = "store"
            with open(path, "rb") as handle:
                actual, size = oci.hash_stream(handle)
            if size != layer.size:
                return oci.BlobMismatch(
                    f"{platform} layer {layer.digest} has size {size}, descriptor says {layer.size}",
                    digest=layer.digest,
                    expected_size=layer.size,
                    actual_size=size,
                    expected_source=source,
                )
            if actual != diff_id:
                return oci.BlobMismatch(
                    f"{platform} layer {layer.digest} does not match config diff_id {diff_id}",
                    digest=layer.digest,
                    expected=diff_id,
                    actual=actual,
                    expected_source=source,
                )
        return None

    def _check_available(self, descriptor: oci.Descriptor, package: oci.PackageLayout) -> None:
        local = package.blob_paths.get(descriptor.digest)
        if local is not None:
            actual_size = local.stat().st_size
            if actual_size != descriptor.size:
                raise oci.BlobMismatch(
                    f"blob {descriptor.digest} has size {actual_size} in the package but "
                    f"the descriptor says {descriptor.size}",
                    digest=descriptor.digest,
                    expected_size=descriptor.size,
                    actual_size=actual_size,
                    expected_source="package",
                )
            return
        actual_size = self.blobs.stat_size(descriptor.digest)
        if actual_size is None:
            raise oci.BlobMissing(
                f"required blob {descriptor.digest} is absent from both the package and the store",
                digest=descriptor.digest,
                expected_size=descriptor.size,
                expected_source="package or store",
            )
        if actual_size != descriptor.size:
            raise oci.BlobMismatch(
                f"stored blob {descriptor.digest} has size {actual_size} but the descriptor "
                f"says {descriptor.size}",
                digest=descriptor.digest,
                expected_size=descriptor.size,
                actual_size=actual_size,
                expected_source="store",
            )

    def _read_metadata(self, descriptor: oci.Descriptor, package: oci.PackageLayout) -> bytes:
        local = package.blob_paths.get(descriptor.digest)
        if local is not None:
            if local.stat().st_size != descriptor.size:
                raise oci.BlobMismatch(
                    f"blob {descriptor.digest} has size {local.stat().st_size} in the package "
                    f"but the descriptor says {descriptor.size}",
                    digest=descriptor.digest,
                    expected_size=descriptor.size,
                    actual_size=local.stat().st_size,
                    expected_source="package",
                )
            data = local.read_bytes()
            if oci.digest_bytes(data) != descriptor.digest:
                raise oci.BlobMismatch(
                    f"blob content does not hash to {descriptor.digest}",
                    digest=descriptor.digest,
                    expected_source="package",
                )
            return data
        return self.blobs.read_bytes(
            descriptor.digest, descriptor.size, max_bytes=self.settings.max_metadata_bytes
        )

    # -- activation --------------------------------------------------------
    def stage(
        self,
        tar_path: Path,
        *,
        expected_current_digest: str | None,
        source_name: str | None,
        enable: bool,
    ) -> dict[str, Any]:
        """Validate + stage, and optionally activate atomically.

        ``activate`` re-checks the expected digest under the lock, so a
        concurrent import that won the race turns this into a 409 instead of
        silently overwriting it.
        """
        record, sources = self.import_package(
            tar_path, expected_current_digest=expected_current_digest, source_name=source_name
        )
        result: dict[str, Any] = {
            "digest": record.digest,
            "version": record.as_json(),
            "blobs": sources,
            "staged": True,
            "activated": False,
        }
        if enable:
            activation = self.activate(
                record.digest, expected_current_digest=expected_current_digest
            )
            result.update(activation)
            result["activated"] = True
        return result

    def activate(self, digest: str, *, expected_current_digest: str | None) -> dict[str, Any]:
        with self._lock:
            record = self.get_version(digest)
            active = self.active_digest()
            if expected_current_digest != active:
                raise StateConflict(
                    f"active version is {active or 'none'}, but the import expected "
                    f"{expected_current_digest or 'none'}; re-import against the current version",
                    expected=expected_current_digest,
                    actual=active,
                    staged_digest=digest,
                )
            if active == digest:
                return {
                    "active": digest,
                    "generation": self._read_state().get("generation", 0),
                    "version": record.as_json(),
                    "changed": False,
                }
            for blob in record.blobs:
                if not self.blobs.has(blob):
                    raise oci.BlobMissing(
                        f"cannot activate {digest}: blob {blob} is missing from the store",
                        digest=blob,
                        expected_source="store",
                    )
            state = self._write_state(digest)
        self.event("version.activated", digest=digest, generation=state["generation"])
        return {
            "active": digest,
            "generation": state["generation"],
            "version": record.as_json(),
            "changed": True,
        }

    def seed_if_empty(self, tar_path: Path, *, source_name: str) -> dict[str, Any] | None:
        """Import and activate the initial image when nothing is active yet."""
        if self.active_digest() is not None:
            return None
        # import_package consumes its input; never delete the configured seed.
        staged = self.tmp_dir / f"seed-{secrets.token_hex(8)}.tar"
        shutil.copyfile(tar_path, staged)
        record, _ = self.import_package(
            staged, expected_current_digest=None, source_name=source_name
        )
        with self._lock:
            if self.active_digest() is None:
                self._write_state(record.digest)
                self.event("seed.activated", digest=record.digest, source=source_name)
                return record.as_json()
        return None

    # -- download ----------------------------------------------------------
    def resolve_download(self, digest: str | None) -> tuple[VersionRecord, Path]:
        """Pin a version for one download.

        The caller receives the version directory; all blob files it references
        are immutable and are never removed, so activation changes do not affect
        a download already in flight.
        """
        chosen = digest or self.active_digest()
        if chosen is None:
            raise NotReady()
        record = self.get_version(chosen)
        version_dir = self.version_dir(chosen)
        if not (version_dir / "index.json").is_file():
            raise NotReady(f"version {chosen} is no longer available")
        missing = [blob for blob in record.blobs if not self.blobs.has(blob)]
        if missing:
            raise oci.BlobMissing(
                f"download for {chosen} cannot be served: blob {missing[0]} is missing",
                digest=missing[0],
                expected_source="store",
            )
        return record, version_dir


def _source_of(digest: str, package: oci.PackageLayout) -> str:
    return "package" if digest in package.blob_paths else "store"


def _aggregate(issues: list[oci.OciError]) -> oci.OciError:
    if len(issues) == 1:
        return issues[0]
    missing = [i.as_dict() for i in issues]
    first_digest = issues[0].digest
    exc = oci.ValidationFailed(
        f"{len(issues)} required blob(s) are missing or invalid; first: "
        f"{issues[0].message}",
        digest=first_digest,
    )
    exc.details = {"issues": missing}
    return exc
