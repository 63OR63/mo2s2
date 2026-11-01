"""Bounded read-only content probes for validating large local container caches.

Metadata may remain unchanged across in-place Windows writes (creation time is
not change time). These probes catch edits in sampled regions without hashing
multi-gigabyte PAK/UCAS members on every catalog refresh. A probe is *not* a
full content checksum; unsampled modifications still require forced reindexing.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

PROBE_BYTES = 4096


def content_probe(path: Path) -> str:
    """Hash up to three distinct 4-KiB regions of a regular file.

    Fail closed on a short read or an observable metadata change during the
    probe. This function is intentionally independent of Qt and MO2.
    """
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f'Not a regular file: {path}')
        size = before.st_size
        offsets = sorted({0, max(0, (size - PROBE_BYTES) // 2),
                          max(0, size - PROBE_BYTES)})
        for offset in offsets:
            stream.seek(offset)
            expected = min(PROBE_BYTES, size - offset)
            data = stream.read(expected)
            if len(data) != expected:
                raise OSError(f'File changed during bounded content probe: {path}')
            h.update(offset.to_bytes(8, 'big'))
            h.update(len(data).to_bytes(4, 'big'))
            h.update(data)
        after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns):
            raise OSError(f'File changed during bounded content probe: {path}')
    return h.hexdigest()
