"""Deterministic, streamable OCI layout tar writer.

Downloads must not be buffered in memory: the response is generated entry by
entry straight from the immutable blob store.  Because the entry list is fixed
before streaming starts, ``Content-Length`` is known up front, which lets the
client detect a truncated transfer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

BLOCK = 512
RECORD = 10 * BLOCK
ZERO_BLOCK = b"\x00" * BLOCK


def _tar_header(name: str, size: int, *, mode: int, mtime: int = 0, typeflag: bytes = b"0") -> bytes:
    encoded = name.encode("utf-8")
    if len(encoded) > 100:
        raise ValueError(f"tar entry name too long: {name}")
    header = bytearray(BLOCK)
    header[0 : len(encoded)] = encoded
    header[100:108] = f"{mode:07o}\x00".encode("ascii")
    header[108:116] = b"0000000\x00"
    header[116:124] = b"0000000\x00"
    header[124:136] = f"{size:011o}\x00".encode("ascii")
    header[136:148] = f"{mtime:011o}\x00".encode("ascii")
    header[148:156] = b" " * 8
    header[156:157] = typeflag
    header[257:265] = b"ustar\x0000"
    header[265:297] = b"root".ljust(32, b"\x00")
    header[297:329] = b"root".ljust(32, b"\x00")
    header[329:337] = b"0000000\x00"
    header[337:345] = b"0000000\x00"
    checksum = sum(header)
    header[148:156] = f"{checksum:06o}\x00 ".encode("ascii")
    return bytes(header)


@dataclass(frozen=True)
class TarEntry:
    name: str
    size: int
    path: Path | None = None
    data: bytes | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.data is None):
            raise ValueError("TarEntry needs exactly one of path/data")
        if self.data is not None and len(self.data) != self.size:
            raise ValueError("TarEntry data length does not match size")

    def padded_size(self) -> int:
        return (self.size + BLOCK - 1) // BLOCK * BLOCK


class TarPlan:
    def __init__(self, entries: list[TarEntry]) -> None:
        self.entries = entries

    def content_length(self) -> int:
        body = sum(BLOCK + entry.padded_size() for entry in self.entries)
        # Two zero blocks plus padding to a full 10 KiB record.
        total = body + 2 * BLOCK
        return (total + RECORD - 1) // RECORD * RECORD

    def chunks(self, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
        written = 0
        for entry in self.entries:
            header = _tar_header(entry.name, entry.size, mode=0o644)
            yield header
            written += len(header)
            if entry.data is not None:
                yield entry.data
                written += entry.size
                pad = entry.padded_size() - entry.size
                if pad:
                    yield ZERO_BLOCK[:pad]
                    written += pad
                continue
            remaining = entry.size
            with open(entry.path, "rb") as handle:  # type: ignore[arg-type]
                while remaining:
                    chunk = handle.read(min(chunk_size, remaining))
                    if not chunk:
                        raise IOError(f"{entry.path} shrank while streaming")
                    remaining -= len(chunk)
                    yield chunk
                    written += len(chunk)
            pad = entry.padded_size() - entry.size
            if pad:
                yield ZERO_BLOCK[:pad]
                written += pad
        tail = 2 * BLOCK
        target = self.content_length()
        pad = target - written - tail
        if pad < 0 or pad % BLOCK:
            raise AssertionError("tar plan length accounting is wrong")
        yield ZERO_BLOCK * 2
        if pad:
            yield ZERO_BLOCK * (pad // BLOCK)
