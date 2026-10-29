"""MO2 lifecycle helpers. No host imports: they are testable without Windows.

Only the UI-side coordinator may call stage_new_install(). FileMapper and
mod scanning remain read-only. The probe is deliberately metadata-only.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import time

from .virtual_core import (KNOWN_BASES, STORE, _children, _case_path,
                           _has_direct_container, _is_reparse, pending_install_dirs)


VERBOSITY = {'Errors': 0, 'Normal': 1, 'Verbose': 2, 'Debug': 3}


def verbosity_level(value) -> int:
    """Normalize the configured log verbosity to an integer level."""
    if isinstance(value, str):
        return VERBOSITY.get(value.strip().title(), 1)
    try:
        return min(3, max(0, int(value)))
    except (ValueError, TypeError):
        return 1


def is_game_executable(executable: str, game_directory: Path) -> bool:
    """Avoid warnings for unrelated tools launched via MO2 (e.g. UE editors)."""
    name = Path(str(executable).replace('\\', '/')).name.casefold()
    if name not in {'stalker2.exe', 'stalker2-win64-shipping.exe',
                    'stalker2-wingdk-shipping.exe'}:
        return False
    try:
        # The callback can return a bare filename on some MO2 installations.
        path = Path(executable)
        if not path.is_absolute() and not (len(executable) >= 2 and executable[1] == ':'):
            return True  # MO2 resolved configured game executable by name
        # Use ntpath on POSIX for unit tests and real Windows paths.
        import ntpath
        exe = ntpath.normcase(ntpath.normpath(str(executable)))
        base = ntpath.normcase(ntpath.normpath(str(game_directory)))
        return exe.startswith(base.rstrip('\\/') + '\\')
    except (OSError, ValueError, TypeError):
        return False


def _stamp(path: Path):
    try:
        st = path.lstat()
        # st_size contributes for a file that is being copied in place.
        return (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_mode)
    except OSError:
        return None


def _zonekit_shape(folder: Path) -> bool:
    """Quick structural probe, not an expensive recursive PAK inspection."""
    windows = _case_path(folder, ('Windows',))
    if windows is None:
        return False
    return any(_case_path(windows, (branch, 'Windows')) is not None
               for branch in ('NewContent', 'OverrideContent'))


def _pending_roots(root: Path) -> list[Path]:
    """Cheaply find *unmanaged* inputs, without inspecting prepared storage.

    The UI runs this on a timer. Scanning all managed Zone Kit trees every
    2.2 seconds is unnecessary and can stall MO2 on large mod lists.
    The full transactional installer performs the authoritative validation.
    """
    candidates: list[Path] = []
    try:
        if _is_reparse(root) or not root.is_dir():
            return candidates
        # Unfinalized MO2 Merge/Replace inboxes are also candidates, but
        # __mo2s2__/Content/Paks/~mods is deliberately NOT revisited.
        candidates.extend(pending_install_dirs(root))
        known_bases = []
        for relative in KNOWN_BASES:
            base = _case_path(root, relative.parts)
            if base is not None:
                known_bases.append(base)
        for base in [root, *known_bases]:
            if _has_direct_container(base):
                candidates.append(base)
            for child in _children(base):
                if not child.is_dir() or _is_reparse(child):
                    continue
                if base == root and child.name.casefold() in {
                        STORE.casefold(), 'content', 'paks', '~mods',
                        'stalker2', 'binaries', 'engine', 'config', 'logicmods'}:
                    continue
                if _has_direct_container(child) or _zonekit_shape(child):
                    candidates.append(child)
                elif base == root:
                    # Cosmetic Nexus wrapper around a Zone Kit folder. Only
                    # inspect one more shallow level; no general recursive
                    # search for arbitrary UE project/tool PAKs.
                    for nested in _children(child):
                        if nested.is_dir() and not _is_reparse(nested) and _zonekit_shape(nested):
                            candidates.append(child)
                            break
    except (OSError, ValueError):
        # Unreadable candidate path must be checked by the authoritative
        # staging path; do not silently call it a valid empty mod.
        candidates.append(root)
    return list(dict.fromkeys(candidates))


def watch_stamp(root: Path) -> tuple:
    """Watch only *unprepared* inputs and their size/mtime changes.

    The managed store is intentionally excluded: its many installed PAKs
    no longer need to settle, and observing them can make the timer lag on
    large libraries. Incoming data is walked without following symlinks.
    """
    import stat
    roots = _pending_roots(root)
    result = []
    visited = set()
    for path in roots:
        key = str(path).casefold()
        if key in visited:
            continue
        visited.add(key)
        st = _stamp(path)
        result.append((key, st))
        if st is None or not stat.S_ISDIR(st[3]):
            continue
        if _is_reparse(path):
            continue
        # Each shallow source directory is scanned independently. Do not
        # descend from mod root into already managed __mo2s2__ or unrelated
        # game-data directories just because a direct PAK also exists.
        if path == root or path in (root / rel for rel in KNOWN_BASES):
            for child in _children(path):
                if child.is_file() or _is_reparse(child):
                    ekey = str(child).casefold()
                    if ekey not in visited:
                        visited.add(ekey)
                        result.append((ekey, _stamp(child)))
            continue
        for dirpath, dirs, files in os.walk(path, followlinks=False):
            allowed = []
            for name in sorted(dirs):
                child = Path(dirpath, name)
                if not _is_reparse(child):
                    allowed.append(name)
            dirs[:] = allowed
            for name in sorted(dirs + files):
                entry = Path(dirpath, name)
                ekey = str(entry).casefold()
                if ekey not in visited:
                    visited.add(ekey)
                    result.append((ekey, _stamp(entry)))
    return tuple(result)


@dataclass
class _Observed:
    """Track the metadata signature and stability of an installation candidate."""
    stamp: tuple
    changed_at: float
    attempted: bool = False


class PreparationQueue:
    """Edge-triggered, stable-file queue; avoids retry/log storms.

    A mod is prepared at most once per observed metadata version. After a
    failed attempt it is retried only if its metadata changes or the user
    explicitly requests preparation. Read-only observation never opens a PAK.
    """

    def __init__(self, settle_seconds: float = 1.7):
        self.settle_seconds = settle_seconds
        self._observed: dict[str, _Observed] = {}

    def ready(self, mods: list[tuple[str, Path]], now: float | None = None, limit: int = 1) -> list[tuple[str, Path]]:
        """Yield candidates once their on-disk metadata has stopped changing."""
        now = time.monotonic() if now is None else now
        result = []
        live = set()
        for name, root in mods:
            key = os.path.normcase(os.path.abspath(str(root))).casefold()
            stamp = watch_stamp(root)
            if not stamp:
                continue
            live.add(key)
            state = self._observed.get(key)
            if state is None or state.stamp != stamp:
                self._observed[key] = _Observed(stamp, now)
            elif (not state.attempted and now - state.changed_at >= self.settle_seconds
                  and len(result) < limit):
                state.attempted = True
                result.append((name, root))
        for key in self._observed.keys() - live:
            del self._observed[key]
        return result

    def completed(self, root: Path) -> None:
        """Suppress our own filesystem changes from triggering another pass."""
        key = os.path.normcase(os.path.abspath(str(root))).casefold()
        self._observed[key] = _Observed(watch_stamp(root), time.monotonic(), True)

    def reset(self) -> None:
        self._observed.clear()
