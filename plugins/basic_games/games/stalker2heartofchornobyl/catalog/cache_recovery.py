"""Fail-closed, one-shot recovery for a *disposable* SQLite inventory cache.

Only SQLite corruption (not locked, busy, read-only, or disk I/O failures)
permits quarantine. Original database and WAL companions are kept next to the
cache for diagnosis; game files, MO2 profiles, and mod containers are untouched.
"""
from __future__ import annotations

from pathlib import Path
import os
import sqlite3
from threading import Lock
from uuid import uuid4


class InvalidCacheSchema(sqlite3.DatabaseError):
    """Current-version cache tables exist with an incompatible shape."""


class CacheRecoveryBlocked(RuntimeError):
    """Cannot safely isolate a cache while another live catalog uses it."""


_guard = Lock()
_active: dict[Path, int] = {}


def is_corrupt_database(exc: BaseException) -> bool:
    """Distinguish fatal SQLite corruption from transient operational errors."""
    if isinstance(exc, InvalidCacheSchema):
        return True
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    code = getattr(exc, 'sqlite_errorcode', None)
    if code is not None:
        return (code & 0xff) in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)
    message = str(exc).lower()
    return any(phrase in message for phrase in (
        'database disk image is malformed', 'file is not a database',
        'malformed database schema', 'database schema is corrupt'))


def register_open(path: Path) -> None:
    with _guard:
        _active[path] = _active.get(path, 0) + 1


def unregister_open(path: Path) -> None:
    with _guard:
        if _active.get(path, 0) <= 1:
            _active.pop(path, None)
        else:
            _active[path] -= 1


def quarantine_corrupt_database(path: Path) -> Path | None:
    """Atomically rename the database and WAL companions, or restore on failure.

    Fail closed for symlinks, live local Catalogs, permissions and filesystem
    errors. A unique stem avoids overwriting an earlier damaged database.
    """
    path = Path(path).absolute()
    with _guard:
        if _active.get(path, 0):
            raise CacheRecoveryBlocked('Database recovery blocked by another open catalog')
        sources = [Path(str(path) + suffix) for suffix in ('', '-wal', '-shm', '-journal')]
        present = [source for source in sources if source.exists() or source.is_symlink()]
        if not present:
            return None
        if any(source.is_symlink() or not source.is_file() for source in present):
            raise CacheRecoveryBlocked('Refusing to move a non-file or symlinked cache')
        label = uuid4().hex[:12]
        moved: list[tuple[Path, Path]] = []
        try:
            for source in present:
                target = Path(str(path) + '.corrupt-' + label + str(source)[len(str(path)):])
                # A UUID gives uniqueness; no overwrite even in a collision.
                if target.exists() or target.is_symlink():
                    raise CacheRecoveryBlocked('Cache quarantine destination already exists')
                os.rename(source, target)
                moved.append((source, target))
        except Exception:
            for source, target in reversed(moved):
                os.rename(target, source)
            raise
        return Path(str(path) + '.corrupt-' + label)
