"""Shared disk transaction primitives for candle downloads and quality repairs.

Beginner note:
Atomic rename prevents partial files, but alone cannot prevent lost updates.
Every writer must hold the same stable lock while reading and publishing its
decision. Vendor requests belong outside that critical section.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from weakref import WeakValueDictionary

import pandas as pd

_THREAD_LOCKS: WeakValueDictionary[str, threading.Lock] = WeakValueDictionary()
_REGISTRY_LOCK = threading.Lock()
# Windows refuses to replace a file another handle has open without
# FILE_SHARE_DELETE, which is how pyarrow opens parquet for reading. Readers
# take no lock, so a publish that races a read gets a short bounded retry there.
# On POSIX a PermissionError is a real, persistent permission problem: no retry.
_REPLACE_ATTEMPTS = 5 if sys.platform == "win32" else 1
_REPLACE_BACKOFF_SECONDS = 0.05
# Temp files are named ``.<stem>.<random>.tmp``; stale cleanup matches this.
TEMP_FILE_GLOB = ".*.tmp"


@contextmanager
def cache_write_lock(path: Path) -> Iterator[None]:
    """Serialize a symbol's disk transaction across threads and processes.

    Args:
        path: Canonical parquet destination, shared by downloader and repair.

    Yields:
        Control with exclusive ownership of this file's write transaction.

    Beginner note:
    Locking the parquet itself would lock an obsolete inode after ``replace``.
    A permanent sibling ``.lock`` file gives all writers one stable identity;
    never unlink it during cleanup. The Python lock also serializes threads on
    platforms whose OS locks belong to a process. Closing the OS descriptor
    releases a dead worker's lock automatically. No vendor I/O is allowed here.
    """
    path = path.resolve()
    key = os.path.normcase(str(path))
    with _REGISTRY_LOCK:
        thread_lock = _THREAD_LOCKS.get(key)
        if thread_lock is None:
            thread_lock = threading.Lock()
            _THREAD_LOCKS[key] = thread_lock
    with thread_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a+b") as handle:
            if sys.platform == "win32":
                import msvcrt

                # Windows byte-range locks require a byte to lock. Appending a
                # byte during simultaneous first creation is harmless: all
                # contenders always lock byte zero, never the current EOF.
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                # LK_LOCK waits for short local transactions. If the OS gives
                # up, propagate the error; proceeding unlocked would lose data.
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def cache_revision(path: Path) -> bytes | None:
    """Fingerprint the current bytes while the caller holds ``cache_write_lock``.

    Args:
        path: Cache whose exact input revision a repair was validated against.

    Returns:
        SHA-256 content digest, or ``None`` when the file has been removed.

    Beginner note:
    A repair may release its lock to ask the vendor a slow question. Rechecking
    this digest under the lock prevents publishing calculations based on an
    older file. Size and modification time alone can miss an equal-size rewrite.
    """
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").digest()
    except FileNotFoundError:
        return None


def atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
    """Publish a complete parquet using a unique, same-directory temporary file.

    Args:
        frame: Already merged or validated candidate to publish.
        path: Destination protected by the caller's ``cache_write_lock``.

    Beginner note:
    The temporary file shares a filesystem with its destination, so ``replace``
    is atomic. Closing it before pandas opens it also works on Windows. Unique
    names prevent collisions; the ``finally`` block removes incomplete output
    if serialization or replacement raises. A process killed outright may leave
    an inert ``.tmp`` sibling, never a partially serialized live parquet;
    ``cleanup_stale_cache_files`` removes old ones. On Windows a replace that
    races an unlocked reader is retried briefly (doubling backoff from 50 ms).
    """
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        frame.to_parquet(temp_path, index=False)
        _replace_with_retry(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _replace_with_retry(source: Path, destination: Path) -> None:
    """Rename ``source`` over ``destination``, retrying transient sharing violations."""
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt >= _REPLACE_ATTEMPTS:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * 2 ** (attempt - 1))
