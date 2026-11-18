"""Read a zstd frame as the plain bytes it holds.

The CLI is stdlib-only, and ``compression.zstd`` arrives in Python 3.14.
Until then a frame is decoded by the ``zstd`` command, which ships with
the machine. A file that is not a frame is returned as stored, so a live
JSONL is unchanged.
"""

from __future__ import annotations

import shutil
import subprocess

_MAGIC = b"\x28\xb5\x2f\xfd"


def is_frame(head: bytes) -> bool:
    return head.startswith(_MAGIC)


def read_bytes(path: str) -> bytes:
    """The plain bytes of ``path``. A zstd frame is decoded. Anything
    else is the file itself."""
    with open(path, "rb") as handle:
        head = handle.read(4)
        if not is_frame(head):
            return head + handle.read()
    return decode(path)


def decode(path: str) -> bytes:
    """Decode one zstd frame. Raises ``OSError`` when ``zstd`` is missing
    or the frame does not decode, so a bad copy is reported, never read
    as text."""
    tool = shutil.which("zstd")
    if tool is None:
        raise OSError("zstd is not installed, so a compressed transcript copy cannot be read")
    try:
        done = subprocess.run([tool, "-d", "-c", "-q", path],
                              check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        raise OSError(f"could not decode {path}") from exc
    return done.stdout


def plain(path: str) -> bytes | None:
    """The decoded bytes of a zstd frame, or None when ``path`` is not one."""
    with open(path, "rb") as handle:
        if not is_frame(handle.read(4)):
            return None
    return decode(path)


class PlainFile:
    """A file-like over plain bytes, so a seek and a fingerprint see the
    transcript rather than a zstd frame. ``size`` is the plain length."""

    def __init__(self, data: bytes):
        self._data = data
        self.size = len(data)
        self._pos = 0

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self.size + offset
        else:
            raise OSError(f"unknown seek whence {whence}")
        self._pos = max(0, min(self._pos, self.size))
        return self._pos

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            n = self.size - self._pos
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def readline(self, n: int = -1) -> bytes:
        end = self._data.find(b"\n", self._pos)
        if end < 0:
            end = self.size
        else:
            end += 1
        if n >= 0:
            end = min(end, self._pos + n)
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk

    def close(self) -> None:
        return None

    def __enter__(self) -> "PlainFile":
        return self

    def __exit__(self, *exc) -> None:
        return None


def open_plain(path: str):
    """A readable file of the transcript's bytes. A zstd frame is decoded
    first. Anything else is the file itself. The caller closes it."""
    decoded = plain(path)
    if decoded is None:
        return open(path, "rb")
    return PlainFile(decoded)
