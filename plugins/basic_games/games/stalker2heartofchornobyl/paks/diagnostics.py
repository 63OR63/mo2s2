"""Read-only diagnostics and opt-in, transactional MO2-mod repair plans.

No parsing of PAK payloads and no direct modification of the real game folder.
The only write path is the existing stage_new_install() transaction, invoked
separately by the host after a human has approved an unchanged preview.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import hashlib

from .virtual_core import (
    Owner, Item, Link, _case_path, _is_reparse, _scan_base,
    normalization_plan, _assert_mod_subpath_safe,
    _wrapper_unwrap_candidates, _game_root_unwrap_candidates,
    pending_install_dirs, transaction_dirs, package_state,
)


@dataclass(frozen=True)
class Diagnostic:
    """One reported installed or external package problem."""
    origin: str               
    name: str
    state: str
    detail: str
    recommendation: str
    path: Path | None = None
    repairable: bool = False


@dataclass(frozen=True)
class RepairPlan:
    """Prevalidated repair operations and their source metadata."""
    mod_root: Path
    moves: tuple[tuple[Path, Path], ...]
    replacements: tuple[Path, ...]
    fingerprints: tuple[tuple[str, str], ...]


def _fingerprint(source: Path) -> tuple[str, str]:
    """Metadata digest for the whole tree, including nested Zone Kit files.

    This detects changed/added/deleted files after the user saw the preview.
    Contents are never opened; the existing transaction owns actual moves.
    """
    digest = hashlib.sha256()
    paths = [source]
    if source.is_dir():
        for walk_root, dirs, files in os.walk(source, followlinks=False):
            dirs.sort(key=str.casefold)
            files.sort(key=str.casefold)
            for name in dirs + files:
                child = Path(walk_root, name)
                if _is_reparse(child):
                    raise ValueError(f'Linked entry in repair source: {child}')
                paths.append(child)
    for path in paths:
        if _is_reparse(path):
            raise ValueError(f'Linked entry in repair source: {path}')
        st = path.stat(follow_symlinks=False)
        values = (str(path.relative_to(source.parent)), st.st_mode, st.st_size,
                  st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
        digest.update(repr(values).encode('utf-8', 'surrogatepass'))
        digest.update(b'\0')
    return str(source), digest.hexdigest()


def repair_plan(mod_root: Path) -> RepairPlan:
    """Preview ONLY direct, recognized and complete MO2 mod packages.

    Complicated wrapper unwrapping and FOMOD alternatives are deliberately left
    to MO2's normal installer, not guessed from a generic Repair button.
    """
    root = Path(mod_root)
    if not root.is_dir() or _is_reparse(root):
        raise ValueError('Invalid or linked MO2 mod directory')
    if transaction_dirs(root):
        raise ValueError('Unresolved transaction; use Recover interrupted updates first')
    if pending_install_dirs(root):
        raise ValueError('Pending installer; use Finalize pending installs instead')
    if _wrapper_unwrap_candidates(root) or _game_root_unwrap_candidates(root):
        raise ValueError('Wrapped installation: reinstall using MO2 for full normalization')
    if _case_path(root, ('fomod',)) is not None:
        raise ValueError('FOMOD installation: use MO2 installer to preserve user choices')
    plan = normalization_plan(root)
    if not plan.moves:
        raise ValueError('No directly repairable packages; reinstall using MO2 if needed')
    # This is exactly the same candidate/replacement plan that execution
    # later uses; a stale preview is still revalidated before touching disk.
    moves, replaced = plan.moves, plan.replacements
    sources = [src for src, _ in moves] + list(replaced)
    for path in sources:
        _assert_mod_subpath_safe(root, path)
        if _is_reparse(path) or not path.exists():
            raise ValueError(f'Linked or missing source: {path}')
    return RepairPlan(root, tuple(moves), tuple(replaced),
                      tuple(_fingerprint(path) for path in sources))


def assert_unchanged_plan(expected: RepairPlan) -> None:
    """Prevent executing an outdated preview after background MO2 changes."""
    current = repair_plan(expected.mod_root)
    if current != expected:
        raise RuntimeError('Files or planned operations changed since preview. Recheck and retry.')


def external_packages(game_directory: Path) -> list[Diagnostic]:
    """Inspect the PHYSICAL ~mods directory, never MO2 VFS or system folders."""
    root = Path(game_directory)
    current = root
    if _is_reparse(current):
        return [Diagnostic('Game', '~mods', 'UNSAFE', 'Game root is a linked directory',
                           'Inspect the real game location manually', current)]
    for segment in ('Stalker2', 'Content', 'Paks', '~mods'):
        current = current / segment
        if _is_reparse(current):
            return [Diagnostic('Game', '~mods', 'UNSAFE', f'Linked game path: {current}',
                               'Inspect the path manually; mo2s2 will not traverse it', current)]
        if not current.exists():
            return []
        if not current.is_dir():
            return [Diagnostic('Game', '~mods', 'UNSAFE', f'Not a directory: {current}',
                               'Check the installation directory', current)]
    result: list[Diagnostic] = []
    # Report linked objects explicitly rather than silently following them.
    for child in current.iterdir():
        if _is_reparse(child):
            result.append(Diagnostic('Game', child.name, 'UNSAFE',
                                     'Symlink/junction in real game ~mods',
                                     'Review manually; do not import linked files', child))
    owner = Owner('External', current, 0)
    for item in _scan_base(owner, current, False):
        if _is_reparse(item.source) or any(_is_reparse(p) for p in item.companions):
            continue  # Linked files have already been reported as UNSAFE.
        result.append(Diagnostic(
            'Game', item.source.name, 'EXTERNAL',
            (item.warning + '; ' if item.warning else '') +
            'Installed directly in the game folder; outside MO2 priority/profile control',
            'Install the original archive through MO2, then manually remove the external '
            'copy after verifying the MO2 version', item.source,
        ))
    return result


def package_diagnostics(items: list[Item], links: list[Link], warnings: list[str],
                        *, installation_errors=(), pending=(), transactions=(),
                        external=()) -> list[Diagnostic]:
    """Build a stable human-readable inventory without triggering repairs."""
    mapped = {link.item_key for link in links}
    problems: list[Diagnostic] = []
    for item in items:
        state = package_state(item, mapped)
        # An incomplete direct install is *not* repairable merely because the
        # Archives can label unmanaged packages UNPREPARED.
        if item.warning and state == 'UNPREPARED':
            state = 'INCOMPLETE'
        if state == 'MAPPED' and not item.warning:
            continue
        has_fomod = bool(item.mod_root and
                         _case_path(item.mod_root, ('fomod',)) is not None)
        if state == 'UNPREPARED' and has_fomod:
            advice = 'Reinstall through the MO2 FOMOD wizard to retain selected options'
        elif state == 'UNPREPARED':
            advice = 'Review and use Repair selected if a safe plan is available'
        elif state == 'INCOMPLETE':
            advice = 'Reinstall the complete matching PAK/UTOC/UCAS package'
        else:
            advice = 'Inspect virtual mapping report and source paths'
        problems.append(Diagnostic('MO2', item.mod_name, state,
                    item.warning or f'{item.kind} package: {item.source}',
                    advice, item.mod_root, state == 'UNPREPARED' and not has_fomod))
    for name in pending:
        problems.append(Diagnostic('MO2', name, 'PENDING',
                        'Incomplete post-installer finalization',
                        'Use Tools → Finalize pending installs'))
    for entry in transactions:
        problems.append(Diagnostic('MO2', entry, 'TRANSACTION',
                        'An update journal or backup needs attention',
                        'Use Tools → Recover interrupted updates'))
    for problem in installation_errors:
        problems.append(Diagnostic('MO2', str(problem).split(':', 1)[0], 'FAILED',
                        str(problem), 'Inspect the error before retrying installation'))
    for warning in warnings:
        # Warnings are shown even when no Item is associated with them.
        if not any(warning == item.warning for item in items):
            problems.append(Diagnostic('System', 'VFS', 'MAPPING', str(warning),
                                        'Review mapping report in Archives → Tools'))
    problems.extend(external)
    return problems
