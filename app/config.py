"""Runtime configuration.

All paths are resolved once at process start.  The service is intentionally
single instance: one process owns ``DATA_DIR`` via an advisory file lock.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_IMPORT_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB
DEFAULT_MAX_TAR_ENTRIES = 20_000
DEFAULT_STREAM_CHUNK_SIZE = 256 * 1024
DEFAULT_MAX_METADATA_BYTES = 4 * 1024 * 1024  # indexes / manifests / configs


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:  # pragma: no cover - startup misconfiguration
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    seed_initial_image: Path | None = None
    seed_required: bool = True
    demo_dir: Path | None = None
    enable_demo: bool = False
    max_import_bytes: int = DEFAULT_MAX_IMPORT_BYTES
    max_tar_entries: int = DEFAULT_MAX_TAR_ENTRIES
    stream_chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE
    max_metadata_bytes: int = DEFAULT_MAX_METADATA_BYTES
    verify_store_blobs: bool = True
    verify_diff_ids: bool = True
    image_ref_name: str = "offline/image"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("DATA_DIR", "/data")).expanduser().resolve()
        seed_raw = os.environ.get("SEED_INITIAL_IMAGE", "").strip()
        demo_raw = os.environ.get("DEMO_DIR", "").strip()
        return cls(
            data_dir=data_dir,
            seed_initial_image=Path(seed_raw).expanduser().resolve() if seed_raw else None,
            seed_required=_env_bool("SEED_REQUIRED", True),
            demo_dir=Path(demo_raw).expanduser().resolve() if demo_raw else None,
            enable_demo=_env_bool("ENABLE_DEMO", False),
            max_import_bytes=_env_int("MAX_IMPORT_BYTES", DEFAULT_MAX_IMPORT_BYTES),
            max_tar_entries=_env_int("MAX_TAR_ENTRIES", DEFAULT_MAX_TAR_ENTRIES),
            stream_chunk_size=_env_int("STREAM_CHUNK_SIZE", DEFAULT_STREAM_CHUNK_SIZE),
            max_metadata_bytes=_env_int("MAX_METADATA_BYTES", DEFAULT_MAX_METADATA_BYTES),
            verify_store_blobs=_env_bool("VERIFY_STORE_BLOBS", True),
            verify_diff_ids=_env_bool("VERIFY_DIFF_IDS", True),
            image_ref_name=os.environ.get("IMAGE_REF_NAME", "offline/image").strip() or "offline/image",
        )
