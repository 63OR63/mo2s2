"""mo2s2: STALKER 2 filesystem package discovery and MO2 VFS mapping.

No MO2/PyQt dependencies. The physical file names are NEVER rewritten. Newly
installed containers are staged in __mo2s2__ so FileMapper doesn't expose both
original and renamed copies through USVFS. All in-mod moves are one-time only.

All container types, including nested Zone Kit containers, receive the same
numeric _N_P Unreal patch priority derived from the owning MO2 mod. The full
Zone Kit directory hierarchy and non-container files are preserved virtually.

"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
import re
import stat
import shutil
import tempfile
import sys
import xml.etree.ElementTree as ET

try:
    from .mo2log import error as log_error
except ImportError:
    # A few host-independent tests import virtual_core as a standalone module.
    def log_error(message):
        print('[mo2s2] [ERROR] ' + message, file=sys.stderr)

STORE = '__mo2s2__'
CANONICAL = Path('Content/Paks/~mods')
SHORT_NESTED = Path(STORE) / 'z'  # Short physical source paths for deep Zone Kit packages.
# Keep a margin below Win32 MAX_PATH (260): PyQt / USVFS and Python disagree
# about whether a bare path with 260+ UTF-16 code units can be stat'd.
WINDOWS_SOURCE_PATH_LIMIT = 248
INCOMING = Path(STORE) / 'incoming'  # Unique for each MO2 installer invocation.
EXTENSIONS = {'.pak', '.utoc', '.ucas'}
SIDECAREXT = '.sig'  # Unreal signed PAK sidecar, mapped with its .pak.
CONTAINER_SUFFIXES = EXTENSIONS | {SIDECAREXT}
ROOT_RESERVED = frozenset({'content', 'paks', '~mods', 'stalker2', 'engine',
                           'binaries', 'logicmods', 'mods', 'config', STORE.casefold()})
WRAPPER_DOCUMENTS = frozenset({'readme.txt', 'readme.md', 'description.txt'})
# Shared cosmetic-wrapper decision for both the MO2 archive tree and the
# installed filesystem. The adapters only answer path/existence questions.
WRAPPER_STANDARD_ROOTS = (
    'Content/Paks', 'Content/GameLite', 'Paks/~mods', '~mods',
    'Stalker2/Content/Paks', 'Binaries/Win64',
)
WRAPPER_INNER_DOCUMENTS = WRAPPER_DOCUMENTS


def cosmetic_wrapper_payload(folder_name: str, *, standard_root: bool,
                             children: tuple[tuple[str, bool, bool], ...]) -> bool:
    """Pure layout rule; (name, is_directory, contains_zonekit) per child."""
    if folder_name.casefold() in ROOT_RESERVED:
        return False
    if standard_root:
        return True
    has_zonekit = any(is_directory and zonekit for _, is_directory, zonekit in children)
    return has_zonekit and all(
        (is_directory and zonekit) or
        (not is_directory and name.casefold() in WRAPPER_INNER_DOCUMENTS)
        for name, is_directory, zonekit in children
    )

# A nested package may genuinely be named 'Mods'; reserve it only at the mod
# root, not inside an explicit ~mods package collection.
SCAN_RESERVED = ROOT_RESERVED - {'mods'}
# Allowed incoming install roots, ordered from specific to general.  The last
# two accept *direct* PAK files, not nested assets already covered by ~mods.
KNOWN_BASES = (
    Path('Content/Paks/~mods'), Path('Paks/~mods'), Path('~mods'),
    Path('Stalker2/Content/Paks/~mods'),
    Path('Content/Paks'), Path('Paks'), Path('Stalker2/Content/Paks'),
)
def is_container_filename(name: str) -> bool:
    """One definition for both MO2 IFileTree and installed filesystem scans."""
    return Path(name).suffix.casefold() in CONTAINER_SUFFIXES


def root_package_kind(*, zonekit: bool, direct_container: bool) -> str | None:
    """Only explicitly recognized top-level packages, never arbitrary nested assets."""
    return 'ZONEKIT' if zonekit else 'FLAT_WRAPPER' if direct_container else None


START_ORDER = 10000
MAX_ORDER = 999999


def _is_reparse(path: Path) -> bool:
    """Reject Windows junctions/reparse points as well as ordinary symlinks.

    `Path.is_symlink()` does not cover junctions. Windows exposes the
    FILE_ATTRIBUTE_REPARSE_POINT flag through lstat(). Never follow an
    untrusted mod-owned reparse point when moving or mapping PAK packages.
    Linux/macOS test runs use the symlink check and have no Windows flag.
    """
    if path.is_symlink():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_mod_subpath_safe(mod_root: Path, path: Path) -> None:
    """Prevent reads/writes outside the selected MO2 mod via linked parents.

    It is not enough to check the file (or the final directory): a link in
    any intermediate `incoming/<id>/Content/...` segment can redirect a
    `rename()` to arbitrary files elsewhere on the same volume.
    """
    root = Path(mod_root)
    target = Path(path)
    if _is_reparse(root):
        raise ValueError(f'Mod root is a reparse point: {root}')
    try:
        components = target.relative_to(root).parts
    except ValueError as exc:
        raise ValueError(f'Path escapes MO2 mod directory: {target}') from exc
    current = root
    for component in components:
        if component in ('..', '.'):
            raise ValueError(f'Unsafe relative path: {target}')
        current = current / component
        if _is_reparse(current):
            raise ValueError(f'Symlink or junction in mod path: {current}')


def prune_previous_session_anchors(anchor_root: Path) -> int:
    """Remove empty-skeleton VFS directories left by a *previous* MO2 run.

    One immutable skeleton per virtual load order is needed so running games
    never lose a directory during profile changes. We therefore only prune
    when a NEW plugin instance initializes, before it installs any mappings.
    Never touch other contents of the MO2 plugin-data directory.
    """
    anchor_root = Path(anchor_root)
    if _is_reparse(anchor_root):
        raise ValueError('VFS anchor root must not be a symlink or junction')
    anchor_root.mkdir(parents=True, exist_ok=True)
    removed = 0
    for child in anchor_root.iterdir():
        if (re.fullmatch(r'[0-9a-f]{16}', child.name)
                and child.is_dir() and not _is_reparse(child)):
            shutil.rmtree(child)
            removed += 1
    return removed


@dataclass(frozen=True)
class Owner:
    """MO2 mod identity, path and virtual load-order priority."""
    name: str
    path: Path
    priority: int
    enabled: bool = True


@dataclass(frozen=True)
class Item:
    """Discovered physical package with its source and validation status."""
    key: str
    mod_name: str
    priority: int
    kind: str
    managed: bool
    source: Path
    companions: tuple[Path, ...]
    tree_root: Path
    files: tuple[str, ...]
    warning: str = ''
    mod_root: Path | None = None   # owning MO2 folder, for launch-time path safety


@dataclass(frozen=True)
class Link:
    """One physical-to-virtual FileMapper destination."""
    source: Path
    destination: Path
    directory: bool
    item_key: str
    number: int


def _id(owner: Owner, relative: str) -> str:
    raw = f'{owner.path.absolute()}|{relative}'.casefold().encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:12]


def _children(folder: Path):
    if not folder.is_dir() or _is_reparse(folder):
        return []
    return sorted(folder.iterdir(), key=lambda p: (p.name.casefold(), p.name))


def _case_child(folder: Path, name: str) -> Path | None:
    return next((p for p in _children(folder) if p.name.casefold() == name.casefold()), None)


def _case_path(folder: Path, parts: tuple[str, ...]) -> Path | None:
    cur = folder
    for part in parts:
        cur = _case_child(cur, part)
        if cur is None or not cur.is_dir() or _is_reparse(cur):
            return None
    return cur


def _is_zonekit(folder: Path) -> bool:
    """Recognize Zone Kit roots without treating arbitrary nested PAKs as mods."""
    # NewContent/OverrideContent with nested Windows branches, as in the
    # actual Classic Trilogy Flashlight 1.8 archive.
    for branch in ('NewContent', 'OverrideContent'):
        target = _case_path(folder, ('Windows', branch, 'Windows'))
        if target is None:
            continue
        for walk_root, dirs, files in os.walk(target, followlinks=False):
            dirs[:] = [d for d in dirs if not _is_reparse(Path(walk_root, d))]
            if any(Path(f).suffix.casefold() in EXTENSIONS for f in files):
                return True
    return False


def _has_direct_container(folder: Path) -> bool:
    """A directory with directly contained PAK/IoStore members.

    Do not search recursively: a UE project with a nested incidental PAK is
    not an installable mod package just because a file has this extension.
    Missing anchors are included here so they remain diagnosable.
    """
    return any(p.suffix.casefold() in EXTENSIONS | {SIDECAREXT} and
               (p.is_file() or _is_reparse(p)) for p in _children(folder))


def _container_stem(path: Path) -> tuple[str, int | None]:
    """Match IoStore partition name_sN.ucas to base container name.

    Partition names are not standalone Unreal containers. Numeric _sN only
    applies to UCAS; a PAK actually called MyMod_s1.pak is independent.
    """
    stem = path.stem
    if path.suffix.casefold() == '.ucas':
        match = re.fullmatch(r'(.+)_s([1-9][0-9]*)', stem, re.I)
        if match:
            return match.group(1).casefold(), int(match.group(2))
    return stem.casefold(), None


def _group_files(files: list[Path]) -> dict[str, list[Path]]:
    """Associate PAK and IoStore files with their container companions."""
    groups: dict[str, list[Path]] = {}
    anchors = {p.stem.casefold() for p in files
               if p.suffix.casefold() in {'.pak', '.utoc'}}
    for path in files:
        if path.suffix.casefold() in (EXTENSIONS | {SIDECAREXT}):
            base, partition = _container_stem(path)
            # An actual MyMod_s1.pak/.utoc defines an independent container.
            # Its MyMod_s1.ucas must not be stolen by MyMod's partition group.
            if partition is not None and path.stem.casefold() in anchors:
                base = path.stem.casefold()
            groups.setdefault(base, []).append(path)
    return groups


def _group_warning(members: list[Path]) -> str:
    """Check complete containers and signatures without flattening any tree."""
    regular: dict[str, Path] = {}
    partitions: set[int] = set()
    anchor = next((p.stem.casefold() for p in members if p.suffix.casefold() == '.pak'),
                  next((p.stem.casefold() for p in members if p.suffix.casefold() == '.utoc'), ''))
    for path in members:
        ext = path.suffix.casefold()
        base, index = _container_stem(path)
        if path.stem.casefold() == anchor or base != anchor:
            index = None
        if index is not None:
            if index in partitions:
                return 'Duplicate IoStore partition number'
            partitions.add(index)
        else:
            if ext in regular:
                return 'Windows case-insensitive container filename collision'
            regular[ext] = path
    if '.pak' not in regular:
        return 'Missing PAK anchor for IoStore or signature' if ('.utoc' in regular or '.ucas' in regular or partitions or SIDECAREXT in regular) else ''
    if ('.utoc' in regular) != ('.ucas' in regular):
        return 'Incomplete IoStore .utoc/.ucas pair'
    if partitions and ('.utoc' not in regular or '.ucas' not in regular):
        return 'IoStore partitions without the base .utoc/.ucas'
    if partitions != set(range(1, max(partitions, default=0) + 1)):
        return 'Non-contiguous IoStore partition sequence'
    return ''


# Windows compares paths case-insensitively and ignores trailing spaces/dots.
# Unsafe source folder names must not be passed unchanged to a virtual path.
_WINDOWS_BAD = set('<>:"/\\|?*')
_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL"} | {
    f"COM{i}" for i in range(1, 10)
} | {f"LPT{i}" for i in range(1, 10)}


def windows_component_problem(name: str) -> str:
    """Validate one unchanged source path component for Windows USVFS."""
    if not name or name in ('.', '..'):
        return 'empty or relative component'
    if len(name) > 255:
        return 'component longer than 255 characters'
    if name[-1] in '. ':
        return 'trailing dot or space'
    if any(ord(c) < 32 or c in _WINDOWS_BAD for c in name):
        return 'Windows forbidden character'
    if name.split('.', 1)[0].upper() in _WINDOWS_DEVICES:
        return 'reserved Windows device name'
    return ''


def windows_component_key(name: str) -> str:
    """Compare Windows path components without case or trailing-dot ambiguity."""
    return name.rstrip(' .').casefold()


def _fail_tree_walk(exc: OSError) -> None:
    """Do not silently accept an incomplete mod directory enumeration."""
    raise OSError(f'Cannot enumerate container tree: {exc}') from exc


def _analyze_tree(path: Path) -> tuple[tuple[str, ...], str]:
    """Collect container formats and validate names/bundles in ONE tree walk.

    Record the FIRST problem but keep scanning so invalid packages are still
    reported even when the problematic filename precedes their PAK files.
    Revalidate later when building mappings to detect on-disk changes.
    """
    found: set[str] = set()
    issue = ''

    def mark_walk_error(exc: OSError) -> None:
        nonlocal issue
        if not issue:
            issue = f'Cannot enumerate container tree: {exc}'

    for root, dirs, files in os.walk(path, followlinks=False,
                                     onerror=mark_walk_error):
        if not issue and any(_is_reparse(Path(root, name)) for name in dirs + files):
            issue = 'Symlink or junction in container tree (cannot safely virtualize)'
        # Never traverse linked directory trees, including Windows junctions.
        dirs[:] = [d for d in dirs if not _is_reparse(Path(root, d))]
        seen_names: set[str] = set()
        for name in dirs + files:
            problem = windows_component_problem(name)
            if problem and not issue:
                issue = f'Unsupported Windows path component {name!r}: {problem}'
            folded = windows_component_key(name)
            if folded in seen_names and not issue:
                issue = f'Windows case-insensitive name collision: {name}'
            seen_names.add(folded)
        for name in files:
            suffix = Path(name).suffix.casefold()
            if suffix in EXTENSIONS | {SIDECAREXT} and not _is_reparse(Path(root, name)):
                found.add(suffix)
        if not issue:
            for members in _group_files([Path(root) / n for n in files]).values():
                warning = _group_warning(members)
                if warning:
                    issue = f'{warning}: {members[0].stem}'
                    break
    return tuple(sorted(found)), issue


def _tree_warning(path: Path) -> str:
    return _analyze_tree(path)[1]


def _scan_base(owner: Owner, base: Path, managed: bool, *,
               include_trees: bool = True, zonekit_only: bool = False) -> list[Item]:
    """Scan only immediate contents of a ~mods-like directory.

    Nested *packages* are treated as whole trees. Unknown game content in
    subfolders isn't repackaged or flattened.
    """
    if not base.is_dir() or _is_reparse(base):
        return []
    result: list[Item] = []
    for folder in _children(base):
        if _is_reparse(folder) or not folder.is_dir():
            continue
        # Never inspect standard game data roots as nested mod packages.
        # These can contain huge loose-file trees, script engines or DLLs.
        if folder.name.casefold() in SCAN_RESERVED:
            continue
        # Content/Paks (without ~mods) supports directly placed container
        # files only. In particular DO NOT move Paks/LogicMods or other
        # subtrees just because they happen to contain a PAK.
        if not include_trees:
            continue
        is_zonekit = _is_zonekit(folder)
        # At the MO2 mod root, arbitrary application/tool directories are NOT
        # assumed to be STALKER 2 packages merely because they contain a PAK
        # somewhere below them. Only explicit Zone Kit trees qualify there.
        is_flat_wrapper = _has_direct_container(folder)
        if zonekit_only and root_package_kind(zonekit=is_zonekit, direct_container=is_flat_wrapper) is None:
            continue
        kind = root_package_kind(zonekit=is_zonekit, direct_container=is_flat_wrapper) or 'NESTED'
        types, path_warning = _analyze_tree(folder)
        # Report unreadable/unsafe nested packages even if enumeration failed
        # before the first PAK/IoStore member was discovered.
        if not types and not path_warning:
            continue
        name_problem = windows_component_problem(folder.name)
        warning = (f'Unsupported package directory {folder.name!r}: {name_problem}'
                   if name_problem else path_warning)
        result.append(Item(_id(owner, str(folder.relative_to(owner.path))),
                           owner.name, owner.priority, kind, managed, folder,
                           (), folder, types, warning, owner.path))

    children = _children(base)
    # Group valid files together with suspicious linklike entries so a
    # linked `_s1.ucas` joins the correct container (including the corner
    # case where `A_s1.pak` is itself an independent PAK anchor).
    entries = [p for p in children
               if p.suffix.casefold() in EXTENSIONS | {SIDECAREXT}
               and (p.is_file() or _is_reparse(p))]
    for stem, members in sorted(_group_files(entries).items()):
        warning = ('Symlink or junction among container files'
                   if any(_is_reparse(p) for p in members)
                   else _group_warning(members))
        if not warning:
            invalid = next(((m.name, windows_component_problem(m.name)) for m in members
                            if windows_component_problem(m.name)), None)
            if invalid:
                warning = f'Unsupported Windows filename {invalid[0]!r}: {invalid[1]}'
        anchor = next((p for p in members if p.suffix.casefold() == '.pak'), members[0])
        result.append(Item(_id(owner, str(anchor.relative_to(owner.path))),
                           owner.name, owner.priority, 'PAK', managed, anchor,
                           tuple(sorted(members, key=lambda p: p.name.casefold())),
                           base, tuple(sorted({p.suffix.casefold() for p in members})), warning, owner.path))
    return result


def scan(owners: list[Owner]) -> list[Item]:
    """Discover complete package containers and report unmanaged or unsafe inputs."""
    results = []
    for owner in owners:
        if not owner.enabled or not owner.path.is_dir() or _is_reparse(owner.path):
            continue
        private = owner.path / STORE / CANONICAL
        try:
            _assert_mod_subpath_safe(owner.path, private)
        except ValueError as exc:
            # Keep unsafe installed packages visible as a diagnostic instead
            # of silently treating their external contents as valid mod data.
            results.append(Item(_id(owner, str(private)), owner.name,
                                owner.priority, 'NESTED', True, private,
                                (), private, (), str(exc), owner.path))
        else:
            results.extend(_scan_base(owner, private, True))
        # Short source storage is used only for Zone Kit trees whose original
        # canonical path would cross the Win32 path-length boundary.
        short_base = owner.path / SHORT_NESTED
        try:
            _assert_mod_subpath_safe(owner.path, short_base)
        except ValueError as exc:
            results.append(Item(_id(owner, str(short_base)), owner.name,
                                owner.priority, 'NESTED', True, short_base,
                                (), short_base, (), str(exc), owner.path))
        else:
            results.extend(_scan_base(owner, short_base, True))
        # Unmanaged direct PAK files are shown for diagnostics, never mapped
        # a second time; USVFS can't hide the originals using FileMapper.
        for relative in KNOWN_BASES:
            found_base = _case_path(owner.path, relative.parts)
            if found_base is not None:
                results.extend(_scan_base(owner, found_base, False,
                                          include_trees=relative.name.casefold() == '~mods'))
        # Entire unpacked mod directly at MO2 mod root, including old-style
        # package root with Classic/Windows/..., is supported by the installer.
        results.extend(_scan_base(owner, owner.path, False, zonekit_only=True))
    # Avoid duplicate packages caused by overlapping alias roots or root scan.
    seen = set()
    unique = []
    for item in results:
        sig = (str(item.source).casefold(), item.kind)
        if sig not in seen:
            seen.add(sig)
            unique.append(item)
    return unique


def _neutral_stem(stem: str) -> str:
    return re.sub(r'(?:_\d+)?_P$', '', stem, flags=re.I).strip(' ._') or 'Mod'


def _safe_name(raw: str, limit: int = 48) -> str:
    return re.sub(r'[^\w.\-]+', '_', raw, flags=re.UNICODE).strip(' ._')[:limit] or 'Mod'


def priority_number(rank: int, first: int = START_ORDER) -> int:
    """Translate a MO2 priority to the virtual Unreal numeric load order."""
    value = first + rank
    if value < 1 or value > MAX_ORDER:
        raise ValueError('Numeric Unreal PakOrder outside supported range')
    return value


def ue_numeric_bonus(filename: str) -> int:
    """FPakPlatformFile style numeric _N_P patch bonus (not full PakOrder)."""
    name = Path(filename).name
    if not name.casefold().endswith('_p.pak'):
        return 0
    stripped = name[:-6]
    token = stripped.rsplit('_', 1)[-1]
    n = int(token) if token.isascii() and token.isdecimal() else 0
    return 100 * (n + 1 if n >= 1 else 1)


def nested_patch_classes(tree: Path) -> tuple[int, ...]:
    """All Unreal numeric PAK-order bonuses present inside one folder tree."""
    classes = set()
    for folder, dirs, files in os.walk(tree, followlinks=False,
                                       onerror=_fail_tree_walk):
        dirs[:] = [d for d in dirs if not _is_reparse(Path(folder, d))]
        for filename in files:
            if filename.lower().endswith('.pak'):
                classes.add(ue_numeric_bonus(filename))
    return tuple(sorted(classes))


def lexical_tree_name(rank: int, original: str, identity: str = '') -> str:
    """Lexically rank top-level folders, preserving the full contents.

    The stable identity suffix prevents case-insensitive Windows filename
    collisions between two different packages with the same sanitized name.
    The rank comes FIRST; the suffix does not affect cross-owner precedence.
    """
    suffix = f'_{identity[:12]}' if identity else ''
    return f'~mo2s2_{rank:08d}_{_safe_name(original, 38)}{suffix}'



def _nested_sources(item: Item, dest_root: Path, number: int) -> list[tuple[Path, Path, bool]]:
    """Virtually rename *every* nested container without flattening its tree.

    Never map a whole source directory followed by renamed container files:
    that would expose both original and new pak names. Instead map each file,
    including unchanged manifests, into a virtual hierarchy anchored by the
    caller to an empty on-disk directory skeleton outside the game tree.
    """
    out: list[tuple[Path, Path, bool]] = []
    for root, dirs, files in os.walk(item.source, followlinks=False,
                                     onerror=_fail_tree_walk):
        dirs[:] = sorted((d for d in dirs if not _is_reparse(Path(root, d))), key=str.casefold)
        paths = [Path(root) / filename for filename in sorted(files, key=str.casefold)]
        grouped = _group_files([x for x in paths if x.suffix.casefold() in EXTENSIONS | {SIDECAREXT}])
        named = set()
        for _key, members in sorted(grouped.items()):
            anchor = next((m for m in members if m.suffix.casefold() == '.pak'), None)
            if anchor is None:
                # scan() already rejects an invalid group: no .pak anchor.
                raise ValueError(f'Incomplete nested container: {members[0]}')
            relative = anchor.relative_to(item.source)
            part = relative.parts
            branch = ('OverrideContent' if any(c.casefold() == 'overridecontent' for c in part)
                      else 'NewContent' if any(c.casefold() == 'newcontent' for c in part)
                      else 'Container')
            identifier = hashlib.sha256((item.key + '|' + str(relative).casefold()).encode('utf-8')).hexdigest()[:10]
            # Keep an explicit branch tag; the full relative folder tree and
            # container contents are unchanged. Only the VFS *filename* moves.
            stem = f'mo2s2_{identifier}_{branch}_{number}_P'
            for member in members:
                if member in named:
                    raise ValueError(f'Duplicate nested container member: {member}')
                named.add(member)
                original_base, partition = _container_stem(member)
                if member.stem.casefold() == anchor.stem.casefold() or original_base != anchor.stem.casefold():
                    partition = None
                suffix = f'_s{partition}' if partition is not None else ''
                target = dest_root / member.relative_to(item.source).parent / (stem + suffix + member.suffix.casefold())
                out.append((member, target, False))
        # Manifests, text files and other non-container assets keep the same
        # relative path inside the destination tree.
        for path in paths:
            if path not in named:
                out.append((path, dest_root / path.relative_to(item.source), False))
    return out


def build_links(items: list[Item], game_dir: Path, first: int = START_ORDER) -> tuple[list[Link], list[str]]:
    """Construct deterministic VFS links from left-pane priorities.

    * All packages belonging to the same MO2 mod receive one priority number.
    * Flat PAK/IoStore companions receive an identical virtual basename with
      _N_P: this maps to FPakPlatformFile numeric PakOrder.
    * Nested packages retain the entire relative tree, and every file is
      individually mapped. Embedded PAK/IoStore trios also receive _N_P.
    """
    warnings: list[str] = []
    mappings: list[Link] = []
    targets: set[str] = set()
    # Snapshot each flat package directory just once per plan. Apart from
    # avoiding O(packages * siblings) work, this catches new IoStore partitions
    # and sidecars created after scan() but before MO2 requests mappings().
    # A stale group must never be mapped as if it were the current one.
    flat_groups_by_parent: dict[Path, dict[str, list[Path]]] = {}
    eligible = sorted((i for i in items if i.managed and not i.warning),
                      key=lambda i: (i.priority, i.mod_name.casefold(), i.key))
    for item in items:
        if not item.managed:
            warnings.append(f'{item.mod_name}: unmanaged {item.kind}; install using MO2')
        elif item.warning:
            warnings.append(f'{item.mod_name}: {item.warning}')

    # MO2 priority is per mod, not per archive. Preserve this association.
    owner_order = sorted({(i.priority, i.mod_name.casefold()) for i in eligible})
    if len(owner_order) + first > MAX_ORDER:
        raise ValueError('Numeric PakOrder range exhausted')
    ranks = {owner: rank for rank, owner in enumerate(owner_order, 1)}
    base = game_dir / 'Stalker2' / CANONICAL

    for item in eligible:
        number = priority_number(ranks[(item.priority, item.mod_name.casefold())], first)
        try:
            if item.mod_root is None:
                raise ValueError('Missing owning MO2 mod directory')
            _assert_mod_subpath_safe(item.mod_root, item.source)
            if item.kind == 'PAK':
                parent = item.source.parent
                if _is_reparse(parent) or not parent.is_dir():
                    raise ValueError(f'Flat package directory is unavailable or unsafe: {parent}')
                if parent not in flat_groups_by_parent:
                    flat_groups_by_parent[parent] = _group_files([
                        p for p in _children(parent)
                        if p.is_file() and not _is_reparse(p)
                    ])
                current = flat_groups_by_parent[parent].get(item.source.stem.casefold())
                if current is None:
                    raise ValueError('Flat package disappeared after scan')
                if set(current) != set(item.companions):
                    raise ValueError('Flat package members changed after scan; refresh the mapping plan')
                problem = _group_warning(current)
                if problem:
                    raise ValueError(f'Flat package changed: {problem}')
                stem = (f'mo2s2_{item.key}_'
                        f'{_safe_name(_neutral_stem(item.source.stem), 24)}_{number}_P')
                sources = []
                for p in item.companions:
                    base_stem, partition = _container_stem(p)
                    if p.stem.casefold() == item.source.stem.casefold() or base_stem != item.source.stem.casefold():
                        partition = None
                    suffix = f'_s{partition}' if partition is not None else ''
                    sources.append((p, base / (stem + suffix + p.suffix.casefold()), False))
            else:
                # A removed or substituted directory is not an empty valid
                # package. The tree scan otherwise treats os.walk() producing
                # no rows as a clean result, hiding the reason for a skip.
                if not item.source.is_dir() or _is_reparse(item.source):
                    raise ValueError(f'Nested package directory is missing or unsafe: {item.source}')
                # Revalidate at mapping time as files may have changed since
                # the UI scanned the mod. Never map a half-updated IoStore tree.
                problem = _tree_warning(item.source)
                if problem:
                    raise ValueError(f'Nested package changed: {problem}')
                folder = lexical_tree_name(number, item.tree_root.name, item.key)
                sources = _nested_sources(item, base / folder, number)
                if item.kind == 'FLAT_WRAPPER':
                    # A cosmetic download folder containing direct PAK/IoStore
                    # files is *not* an Unreal mount point. Put its direct
                    # container members at ~mods root (where UE looks for
                    # conventional PAKs), preserving its physical structure.
                    # Any loose/ancillary files and deeper package trees keep
                    # the namespaced directory to avoid cross-mod collisions.
                    sources = [(src, base / dst.name, isdir)
                               if (src.parent == item.source and
                                   src.suffix.casefold() in EXTENSIONS | {SIDECAREXT})
                               else (src, dst, isdir)
                               for src, dst, isdir in sources]
                if not any(src.suffix.casefold() == '.pak' for src, _, _ in sources):
                    raise ValueError('Nested package has no PAK anchor after scan')

            pending: set[str] = set()
            for src, dst, is_directory in sources:
                _assert_mod_subpath_safe(item.mod_root, src)
                if not src.is_file() or _is_reparse(src):
                    units = _windows_path_units(src)
                    if units >= 260:
                        raise ValueError(
                            f'Windows source path has {units} UTF-16 characters '
                            f'(MAX_PATH is 260); reinstall this mod with mo2s2 '
                            f'to stage it in short storage: {src}')
                    raise ValueError(f'Source missing, unsafe or not a file: {src}')
                key = str(dst).casefold()
                if key in targets or key in pending:
                    raise ValueError(f'Virtual target collision: {dst}')
                # The target path is entirely constructed by this plugin, but
                # preserves nested relative directories supplied by the mod.
                relative = dst.relative_to(base)
                for component in relative.parts:
                    issue = windows_component_problem(component)
                    if issue:
                        raise ValueError(f'Invalid VFS component {component!r}: {issue}')
                pending.add(key)
        except (OSError, ValueError) as exc:
            warnings.append(f'{item.mod_name}: cannot map {item.source}: {exc}')
            continue

        targets.update(pending)
        mappings.extend(Link(src, dst, is_directory, item.key, number)
                        for src, dst, is_directory in sources)
    # Every managed container, flat or nested, now shares the owning mod's
    # numeric Unreal _N_P priority. The lexical folder name is a stable tie-breaker.
    return mappings, warnings


def package_state(item: Item, mapped_keys: set[str]) -> str:
    """State shared by MO2 UI and diagnostic exports.

    State depends on the *actual* output of build_links rather than only the
    installed file layout; a staged package may be skipped at launch.
    """
    if not item.managed:
        return 'UNPREPARED'
    if item.warning:
        return 'INCOMPLETE'
    if item.key not in mapped_keys:
        return 'SKIPPED'
    return 'MAPPED'


def format_mapping_report(items: list[Item], links: list[Link],
                          warnings: list[str]) -> str:
    """Deterministic read-only report of the exact MO2 FileMapper plan."""
    lines = [
        'mo2s2 virtual mapping plan',
        f'packages={len(items)} mappings={len(links)}',
        'priority: one Unreal numeric _N_P class per active MO2 mod',
        '',
    ]
    link_index: dict[str, list[Link]] = {}
    for link in links:
        link_index.setdefault(link.item_key, []).append(link)
    mapped_keys = set(link_index)
    for item in sorted(items, key=lambda x: (x.priority, x.mod_name.casefold(), x.key)):
        active = link_index.get(item.key, [])
        status = package_state(item, mapped_keys).lower()
        lines.extend([
            f'[{item.priority}] {item.mod_name} / {item.kind} / {status}',
            f'  source: {item.source}',
            f'  content: {", ".join(item.files) or "unknown"}',
        ])
        if item.warning:
            lines.append(f'  problem: {item.warning}')
        if item.managed and not item.warning and not active:
            # Preserve exact underlying issue from the mapping planner,
            # rather than incorrectly claiming the container was mapped.
            issues = [w for w in warnings
                      if w.startswith(f'{item.mod_name}:') and
                      str(item.source) in w]
            lines.extend('  mapping error: ' + issue for issue in issues)
        if item.kind != 'PAK' and not item.warning:
            try:
                classes = nested_patch_classes(item.source)
            except OSError as exc:
                lines.append(f'  original embedded PAK bonuses: unavailable ({exc})')
            else:
                lines.append(f'  original embedded PAK bonuses: {classes} (virtualized to owner rank)')
        for link in active:
            lines.append(f'  {link.source} => {link.destination}'
                         + (' [directory]' if link.directory else ''))
        lines.append('')
    if warnings:
        lines.append('WARNINGS')
        lines.extend(f'- {warning}' for warning in warnings)
    return '\n'.join(lines).rstrip() + '\n'


def pending_install_dirs(mod_root: Path) -> list[Path]:
    """Find only mo2s2-created inboxes; do not follow links or other folders."""
    private_store = Path(mod_root) / STORE
    inbox = Path(mod_root) / INCOMING
    if _is_reparse(private_store) or not inbox.is_dir() or _is_reparse(inbox):
        return []
    return [p for p in _children(inbox)
            if re.fullmatch(r'[0-9a-f]{32}', p.name)
            and p.is_dir() and not _is_reparse(p)]


def _prune_empty_inboxes(mod_root: Path) -> None:
    """Remove only EMPTY installer-created directories after a committed update."""
    for folder in pending_install_dirs(mod_root):
        # Track only regular directories; a junction in this inbox is not a
        # disposable empty folder, regardless of the host os.walk semantics.
        regular_dirs = []
        for walk_root, dirs, _ in os.walk(folder, topdown=True, followlinks=False):
            dirs[:] = [d for d in dirs if not _is_reparse(Path(walk_root, d))]
            regular_dirs.append((Path(walk_root), tuple(dirs)))
        for current, dirs in reversed(regular_dirs):
            for name in dirs:
                child = current / name
                if not _is_reparse(child):
                    try:
                        child.rmdir()
                    except OSError:
                        pass
            try:
                current.rmdir()
            except OSError:
                pass
    try:
        (Path(mod_root) / INCOMING).rmdir()
    except OSError:
        pass


def _windows_path_units(path: Path) -> int:
    """Number of Windows UTF-16 code units (excluding any implicit NUL)."""
    return len(str(path).encode('utf-16-le')) // 2


def _longest_staged_path(source_dir: Path, destination_dir: Path) -> int:
    """Find the longest projected destination of a nested package on disk."""
    longest = _windows_path_units(destination_dir)
    for folder, dirs, files in os.walk(source_dir, followlinks=False,
                                       onerror=_fail_tree_walk):
        dirs[:] = [d for d in dirs if not _is_reparse(Path(folder, d))]
        for name in dirs + files:
            candidate = destination_dir / Path(folder).relative_to(source_dir) / name
            longest = max(longest, _windows_path_units(candidate))
    return longest


def _short_package_name(name: str) -> str:
    """Stable case-insensitive identifier; retains a short human-readable hint."""
    digest = hashlib.sha256(name.casefold().encode('utf-8')).hexdigest()[:12]
    return 'z_' + digest


def _package_storage_target(root: Path, source: Path, normal_target: Path) -> Path:
    """Only shorten a nested tree if its full on-disk source path is unsafe.

    Preserve all inner filenames and directories *unchanged*; virtual mapping
    still reconstructs the full tree from those files. Ordinary packages keep
    the original canonical storage path, including existing simple mods.
    """
    longest = _longest_staged_path(source, normal_target)
    if longest < WINDOWS_SOURCE_PATH_LIMIT:
        return normal_target
    short = root / SHORT_NESTED / _short_package_name(source.name)
    shorter = _longest_staged_path(source, short)
    if shorter >= WINDOWS_SOURCE_PATH_LIMIT:
        raise ValueError(
            f'Zone Kit package is too deeply nested for a safe Windows VFS source '
            f'(original {longest}, short {shorter}, limit '
            f'{WINDOWS_SOURCE_PATH_LIMIT - 1} UTF-16 characters); '
            f'move MO2 to a shorter directory or shorten the MO2 mod name: {source.name}')
    return short


def _staging_candidates(root: Path) -> list[tuple[Path, Path]]:
    """Build non-overlapping rename operations for ONE newly installed mod.

    The paths below __mo2s2__ never contain duplicates and all operations
    are calculated and checked before touching disk.
    """
    moves: list[tuple[Path, Path]] = []
    canonical_target = root / STORE / CANONICAL
    owners = [Owner(root.name, root, 0)]
    seen_sources = set()

    # Each stock MO2 installer invocation receives a separate inbox. During
    # Merge, MO2 can overwrite paths already in the archive's target mod; the
    # inbox keeps old canonical packages untouched until this transaction.
    # Legacy direct-path sources are still supported for manual/BAIN installs.
    incoming_bases = [p / CANONICAL for p in pending_install_dirs(root)]
    for base in incoming_bases + [p for rel in KNOWN_BASES
                                  if (p := _case_path(root, rel.parts)) is not None]:
        # Check before scanning: a linked intermediate directory can lead
        # to an entirely unrelated filesystem tree outside the mod.
        _assert_mod_subpath_safe(root, base)
        if not base.is_dir() or _is_reparse(base):
            continue
        for item in _scan_base(owners[0], base, False,
                               include_trees=base.name.casefold() == '~mods'):
            if item.warning:
                raise ValueError(f'Invalid installed package: {base}: {item.warning}')
            for source in ((item.source,) if item.kind != 'PAK' else item.companions):
                if source in seen_sources:
                    continue
                if any(parent in seen_sources for parent in source.parents):
                    continue
                seen_sources.add(source)
                # Maintain any nested folders relative to the selected base.
                target = canonical_target / source.relative_to(base)
                if item.kind != 'PAK':
                    target = _package_storage_target(root, source, target)
                moves.append((source, target))

    # Mod root may contain direct containers or top-level Zone Kit folders;
    # only process those not already captured by a more specific path.
    for item in _scan_base(owners[0], root, False, zonekit_only=True):
        if item.warning:
            raise ValueError(f'Invalid installed package at mod root: {item.warning}')
        for source in ((item.source,) if item.kind != 'PAK' else item.companions):
            if source in seen_sources or any(p in seen_sources for p in source.parents):
                continue
            if source.name in {STORE, 'Content', 'Paks', 'Stalker2', '~mods'}:
                continue
            seen_sources.add(source)
            target = canonical_target / source.name
            if item.kind != 'PAK':
                target = _package_storage_target(root, source, target)
            moves.append((source, target))

    # Identical source files in different accepted roots might otherwise
    # land on the same Windows destination. Refuse the whole transaction.
    origin_strings = [str(a).casefold() for a, _ in moves]
    target_strings = [str(b).casefold() for _, b in moves]
    if len(set(origin_strings)) != len(origin_strings):
        raise ValueError('Duplicate source in installer staging')
    if len(set(target_strings)) != len(target_strings):
        raise ValueError('Duplicate virtual-storage destination in installer staging')
    for src, dst in moves:
        _assert_mod_subpath_safe(root, src)
        _assert_mod_subpath_safe(root, dst)
        if not src.exists() or _is_reparse(src):
            raise ValueError(f'Cannot stage {src} -> {dst}')
        if src == dst or src in dst.parents:
            raise ValueError('Refusing to move source into itself')
    return moves



def _simple_fomod_required_plan(
    root: Path, ordinary_moves: list[tuple[Path, Path]], txn: Path | None,
) -> tuple[list[tuple[Path, Path]], list[Path]] | None:
    """Honor deterministic required-file-only FOMOD in the Manual installer.

    MO2's FOMOD installer applies requiredInstallFiles and destination names;
    Manual extracts the raw archive instead. Only replay a manifest when ALL
    package files are accounted for and no choices/conditions/extra files are
    present. Never guess at optional groups, rename game containers or parse
    their contents. The regular nested-package planner handles other archives.
    """
    fomod = _case_child(root, 'fomod')
    if fomod is None or not fomod.is_dir() or _is_reparse(fomod):
        return None
    config = _case_child(fomod, 'ModuleConfig.xml')
    if config is None or not config.is_file() or _is_reparse(config):
        return None
    if config.stat().st_size > 1024 * 1024:
        return None
    if any(p.name.casefold() not in {'moduleconfig.xml', 'info.xml'} or
           not p.is_file() or _is_reparse(p) for p in _children(fomod)):
        return None
    # Treat all unexpected root material as significant: it might be an
    # optional variant, a script, or normal game data not described by FOMOD.
    allowed = {'fomod', 'content', 'stalker2', 'readme.txt', 'meta.ini', STORE.casefold()}
    if any((p.name.casefold() not in allowed and p != txn) or _is_reparse(p)
           for p in _children(root)):
        return None
    try:
        document = ET.fromstring(config.read_bytes())
    except (ET.ParseError, OSError, ValueError):
        return None
    if document.tag != 'config' or any(child.tag not in
            {'moduleName', 'requiredInstallFiles'} for child in document):
        return None
    required = document.find('requiredInstallFiles')
    if required is None or len(required) == 0 or len(required) > 2048:
        return None
    if any(child.tag != 'file' or list(child) for child in required):
        return None
    destination_base = root / STORE / CANONICAL
    planned: list[tuple[Path, Path]] = []
    sources: set[str] = set()
    targets: set[str] = set()
    for entry in required:
        raw = entry.get('source', '').replace('\\', '/')
        destination = entry.get('destination', '').replace('\\', '/')
        components = raw.split('/')
        if (len(components) < 6 or components[:4] != ['Stalker2', 'Content', 'Paks', '~mods']
                or any(windows_component_problem(part) for part in components)
                or '/' in destination or windows_component_problem(destination)
                or Path(destination).suffix.casefold() not in EXTENSIONS | {SIDECAREXT}
                or components[-1].casefold() != destination.casefold()):
            return None
        source = root.joinpath(*components[1:])  # game-root stripped earlier
        if not source.is_file() or _is_reparse(source):
            return None
        folded_source, folded_target = str(source).casefold(), destination.casefold()
        if folded_source in sources or folded_target in targets:
            return None
        sources.add(folded_source)
        targets.add(folded_target)
        planned.append((source, destination_base / destination))

    # Never discard unlisted payload or install only one alternative out of a
    # mixed package. Ordinary staging already validated IoStore completeness.
    ordinary_files = set()
    for src, _ in ordinary_moves:
        if src.is_dir():
            ordinary_files.update(str(p).casefold() for p in src.rglob('*') if p.is_file())
            if any(_is_reparse(p) for p in src.rglob('*')):
                return None
        else:
            ordinary_files.add(str(src).casefold())
    if ordinary_files != sources:
        return None

    leftovers = [fomod]
    readme = _case_child(root, 'README.txt')
    if readme is not None and readme.is_file() and not _is_reparse(readme):
        leftovers.append(readme)
    return planned, leftovers


def _prune_empty_source_parents(root: Path, sources: list[Path]) -> None:
    """Remove only empty directories left by successfully staged packages."""
    candidates = set()
    for source in sources:
        parent = source.parent
        while parent != root and root in parent.parents:
            candidates.add(parent)
            parent = parent.parent
    for directory in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        if _is_reparse(directory):
            continue
        try:
            directory.rmdir()  # never delete contents, even unknown files
        except OSError:
            pass


def _wrapper_unwrap_candidates(root: Path) -> list[tuple[Path, Path]]:
    """Recognise a cosmetic single top-level wrapper in a newly installed mod.

    A Zone Kit package named <ModName>/Windows/... is NOT cosmetic and must
    remain untouched. Unwrap only a directory directly containing standard
    game data roots such as Content/Paks or ~mods.
    """
    if not root.is_dir():
        return []
    children = _children(root)
    allowed_docs = WRAPPER_DOCUMENTS
    dirs = [p for p in children if p.is_dir() and not _is_reparse(p)]
    if len(dirs) != 1 or any(_is_reparse(p) or
                                (p.is_file() and p.name.casefold() not in allowed_docs)
                                for p in children):
        return []
    folder = dirs[0]
    if folder.name.casefold() in {'content', 'paks', '~mods', 'binaries',
                                 'engine', 'logicmods', STORE.casefold()}:
        return []
    standard_root = any(_case_path(folder, tuple(path.split('/')))
                        for path in WRAPPER_STANDARD_ROOTS)
    # The archive tree adapter below uses the exact same payload rule.
    children = tuple((p.name, p.is_dir(), bool(p.is_dir() and _is_zonekit(p)))
                     for p in _children(folder))
    if not cosmetic_wrapper_payload(folder.name, standard_root=standard_root,
                                    children=children):
        return []
    changes = [(p, root / p.name) for p in _children(folder)]
    if any(destination.exists() for _, destination in changes):
        raise ValueError('Unwrapping archive would overwrite existing mod files')
    return changes


def _game_root_unwrap_candidates(root: Path) -> list[tuple[Path, Path]]:
    """Remove the redundant ``Stalker2/`` game directory from MO2 installs.

    ``BasicGame.GameDataPath`` is already ``Stalker2``. A downloaded archive
    can include that folder, but leaving it inside a MO2 mod adds an extra
    game-directory layer. Unlike a cosmetic wrapper, this may coexist with
    README files at the installation root. We therefore only move the game
    content inside it and never touch unrelated root entries.
    """
    game_root = _case_child(root, 'Stalker2')
    if game_root is None or not game_root.is_dir() or _is_reparse(game_root):
        return []
    # This must be an actual game-data root, not a folder coincidentally
    # named Stalker2. Do not recognize arbitrary .pak files nested below it.
    if not any(_case_child(game_root, name) is not None for name in
               ('Content', 'Binaries', 'Engine')):
        return []
    changes = [(child, root / child.name) for child in _children(game_root)]
    existing = {p.name.casefold() for p in _children(root) if p != game_root}
    if any(target.name.casefold() in existing for _, target in changes):
        raise ValueError('Redundant Stalker2 game root conflicts with installed root files')
    if any(_is_reparse(src) or target.exists() for src, target in changes):
        raise ValueError('Unsafe or occupied Stalker2 game-root destination')
    return changes


def _windows_existing(root: Path, target: Path) -> Path | None:
    """Resolve a target using Windows case-insensitive comparison.

    Reject symlinked path components instead of following them outside the
    installed MO2 mod. This is also used by the preflight of updates.
    """
    relative = target.relative_to(root)
    path = root
    for component in relative.parts:
        if not path.is_dir() or _is_reparse(path):
            raise ValueError(f'Unsafe destination parent: {path}')
        siblings = [p for p in path.iterdir()
                    if p.name.casefold() == component.casefold()]
        if len(siblings) > 1:
            raise ValueError(f'Ambiguous Windows destination: {target}')
        if not siblings:
            return None
        path = siblings[0]
        if _is_reparse(path):
            raise ValueError(f'Symlink or junction in installation destination: {path}')
    return path


def _update_backups(mod_root: Path, moves: list[tuple[Path, Path]]) -> list[Path]:
    """Find old versions to replace, including obsolete IoStore partitions.

    A directory package is replaced as a whole. A flat container is replaced
    as an entire *same-name* bundle, so old _sN.ucas partitions cannot remain
    attached to a newly installed .utoc. Updates never modify other packages.
    """
    backups: dict[str, Path] = {}
    new_by_parent: dict[Path, list[Path]] = {}
    for source, target in moves:
        current = _windows_existing(mod_root, target)
        if current is not None:
            if source.is_dir() != current.is_dir():
                raise ValueError(f'Update changes file/directory type: {target}')
            backups[str(current).casefold()] = current
        # A reinstalled long Zone Kit tree can change physical storage from
        # canonical to short (or back if MO2 itself was moved). Back up the
        # previous location as part of the SAME transaction: never leave a
        # duplicate staged copy that would be scanned and mapped twice.
        if source.is_dir():
            canonical = mod_root / STORE / CANONICAL / source.name
            short = mod_root / SHORT_NESTED / _short_package_name(source.name)
            if target in (canonical, short):
                other = short if target == canonical else canonical
                previous = _windows_existing(mod_root, other)
                if previous is not None:
                    if not previous.is_dir():
                        raise ValueError(f'Installed nested package is not a directory: {previous}')
                    backups[str(previous).casefold()] = previous
        if source.is_file():
            new_by_parent.setdefault(target.parent, []).append(source)

    for parent, incoming in new_by_parent.items():
        installed_parent = _windows_existing(mod_root, parent)
        if installed_parent is None:
            continue
        if not installed_parent.is_dir():
            raise ValueError(f'Occupied installation parent: {parent}')
        old_groups = _group_files([p for p in installed_parent.iterdir()
                                  if p.is_file() and not _is_reparse(p)])
        new_groups = _group_files(incoming)
        for name, sources in new_groups.items():
            old = old_groups.get(name)
            if not old:
                continue
            # Partial PAK-only updates of IoStore packages are unsafe: the
            # old UTOC/UCAS would be mismatched with the new PAK.
            old_ext = {p.suffix.casefold() for p in old}
            new_ext = {p.suffix.casefold() for p in sources}
            if '.utoc' in old_ext and '.utoc' not in new_ext:
                raise ValueError(f'Incomplete IoStore replacement: {name}')
            if '.ucas' in old_ext and '.ucas' not in new_ext:
                raise ValueError(f'Incomplete IoStore replacement: {name}')
            for old_file in old:
                backups[str(old_file).casefold()] = old_file

    selected = list(backups.values())
    selected.sort(key=lambda x: (len(x.parts), str(x).casefold()))
    if any(path in other.parents for idx, path in enumerate(selected)
           for other in selected[idx + 1:]):
        raise ValueError('Overlapping installed package replacements')
    return selected


# A durable journal is created before the first filesystem rename. Every path
# is stored relative to the MO2 mod directory; a corrupt/foreign journal is
# never allowed to operate outside it. Unjournaled transaction directories
# are never inferred or migrated.
TXN_JOURNAL = 'journal.json'
TXN_VERSION = 1


def transaction_dirs(mod_root: Path) -> list[Path]:
    root = Path(mod_root)
    if not root.is_dir() or _is_reparse(root):
        return []
    return sorted((p for p in root.iterdir() if p.name.startswith('__mo2s2_txn_')),
                  key=lambda p: p.name)


def _tx_relative(root: Path, path: Path) -> str:
    _assert_mod_subpath_safe(root, path)
    relative = path.relative_to(root)
    if not relative.parts or any(part in ('', '.', '..') for part in relative.parts):
        raise ValueError(f'Unsafe journal path: {path}')
    return relative.as_posix()


def _tx_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise ValueError('Invalid transaction journal path')
    pure = Path(relative)
    if pure.is_absolute() or any(p in ('', '.', '..') for p in relative.split('/')):
        raise ValueError(f'Unsafe transaction journal path: {relative!r}')
    path = root.joinpath(*relative.split('/'))
    _assert_mod_subpath_safe(root, path)
    return path


def _tx_signature(path: Path) -> dict:
    """Cheap identity check; never read the contents of a PAK or IoStore file."""
    st = path.lstat()
    return {'dir': stat.S_ISDIR(st.st_mode), 'size': st.st_size,
            'mtime': st.st_mtime_ns, 'ino': st.st_ino, 'dev': st.st_dev}


def _tx_matches(path: Path, expected: dict) -> bool:
    if _is_reparse(path) or not path.exists():
        return False
    current = _tx_signature(path)
    if current['dir'] != expected['dir']:
        return False
    # inode numbers are stable for a rename on the same volume. Some Windows
    # filesystems report 0, so size + mtime are our fallback there.
    if expected['ino'] and current['ino'] and expected['dev'] == current['dev']:
        if expected['ino'] != current['ino']:
            return False
    return (current['size'], current['mtime']) == (expected['size'], expected['mtime'])


def _write_txn(txn: Path, data: dict) -> None:
    temporary = txn / (TXN_JOURNAL + '.tmp')
    with temporary.open('w', encoding='utf-8') as f:
        json.dump(data, f, sort_keys=True, separators=(',', ':'))
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, txn / TXN_JOURNAL)


def _read_txn(mod_root: Path, txn: Path) -> dict:
    """Read and validate a recovery journal before using its recorded paths."""
    root = Path(mod_root)
    _assert_mod_subpath_safe(root, txn)
    if (not txn.name.startswith('__mo2s2_txn_') or not txn.is_dir()
            or _is_reparse(txn)):
        raise ValueError('Unsafe transaction directory')
    journal = txn / TXN_JOURNAL
    if not journal.is_file() or _is_reparse(journal):
        raise ValueError(f'Legacy/unknown transaction with no {TXN_JOURNAL}: {txn.name}; manual inspection required')
    with journal.open('r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get('version') != TXN_VERSION or data.get('state') not in ('prepared', 'committed', 'rolled_back'):
        raise ValueError(f'Unknown transaction journal format in {txn.name}')
    for field in ('wrappers', 'moves', 'backups'):
        if not isinstance(data.get(field), list):
            raise ValueError(f'Invalid {field} in transaction journal')
        for entry in data[field]:
            if not isinstance(entry, dict) or not isinstance(entry.get('from'), str) or not isinstance(entry.get('to'), str):
                raise ValueError(f'Invalid {field} journal entry')
            _tx_path(root, entry['from'])
            _tx_path(root, entry['to'])
            if field == 'backups':
                if _tx_path(root, entry['to']).parent != txn or not _tx_path(root, entry['to']).name.isdecimal():
                    raise ValueError('Backup path does not belong to transaction')
            if field != 'wrappers':
                signature = entry.get('signature')
                if not isinstance(signature, dict) or any(type(signature.get(k)) is not t for k, t in
                   (('dir', bool), ('size', int), ('mtime', int), ('ino', int), ('dev', int))):
                    raise ValueError('Invalid transaction identity metadata')
    return data


def _tx_ops(root: Path, entries: list[dict]) -> list[tuple[Path, Path, dict]]:
    return [(_tx_path(root, e['from']), _tx_path(root, e['to']), e.get('signature', {}))
            for e in entries]


def inspect_transaction(mod_root: Path, txn: Path) -> tuple[str, str]:
    """Read-only overview. No legacy transaction is automatically deleted."""
    try:
        data = _read_txn(Path(mod_root), txn)
        status = data['state']
        if status == 'prepared':
            return status, (f'rollback {len(data["moves"])} new paths, '
                            f'{len(data["backups"])} saved old paths, '
                            f'{len(data["wrappers"])} archive wrappers')
        if status == 'committed':
            return status, 'update completed; remove retained old-version backup'
        return status, 'rollback completed; remove empty transaction metadata'
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return 'manual', str(exc)


def recover_transaction(mod_root: Path, txn: Path) -> str:
    """Explicit all-or-nothing *preflight*, then resumable rollback/cleanup.

    Never overwrites a path, follows a symlink/junction, or guesses metadata
    for legacy backups. Refuses to act when users have changed affected paths.
    A crash in the middle of recovery is resumable from the same journal.
    """
    root = Path(mod_root)
    data = _read_txn(root, Path(txn))
    status = data['state']
    if status in ('committed', 'rolled_back'):
        # Do not follow an attacker-created link inside a backup. The journal
        # and backup entries were validated by _read_txn, but other children
        # may have appeared since. Never delete unrecognized extra files.
        allowed = {TXN_JOURNAL, TXN_JOURNAL + '.tmp'}
        if status == 'committed':
            allowed.update(Path(e['to']).name for e in data['backups'])
        leftovers = [p.name for p in Path(txn).iterdir() if p.name not in allowed or _is_reparse(p)]
        if leftovers:
            raise ValueError('Transaction directory contains unknown files: ' + ', '.join(leftovers))
        if status == 'committed':
            for source, backup, signature in _tx_ops(root, data['backups']):
                if backup.exists() and not _tx_matches(backup, signature):
                    raise ValueError(f'Backup changed after commit: {backup}')
        # The directory and all recorded old-version backups are discarded
        # together, as in previous releases. A partial cleanup is resumable.
        shutil.rmtree(txn)
        return 'cleaned' if status == 'committed' else 'cleaned_rollback'

    moves = _tx_ops(root, data['moves'])
    backups = _tx_ops(root, data['backups'])
    wrappers = _tx_ops(root, data['wrappers'])
    backup_origins = {str(old).casefold(): (backup, signature)
                      for old, backup, signature in backups}
    to_return: list[tuple[Path, Path]] = []
    to_restore: list[tuple[Path, Path]] = []
    to_unwrap: list[tuple[Path, Path]] = []
    # Validate complete plan before making any filesystem changes. Backups
    # distinguish an old target (before update) from a newly moved target.
    for source, target, signature in reversed(moves):
        actual_source = _windows_existing(root, source)
        actual_target = _windows_existing(root, target)
        if actual_source is not None and actual_target is None:
            continue  # not moved yet, or already restored
        if actual_source is None and actual_target is not None:
            if not _tx_matches(actual_target, signature):
                raise ValueError(f'Changed/new file at update destination: {actual_target}')
            to_return.append((actual_target, source))
            continue
        if actual_source is not None and actual_target is not None:
            saved = backup_origins.get(str(actual_target).casefold())
            if saved and _windows_existing(root, saved[0]) is None and _tx_matches(actual_target, saved[1]):
                continue  # both files untouched; backup has not started
            raise ValueError(f'Both incoming and installed paths occupied: {source}, {actual_target}')
        raise ValueError(f'Missing incoming and installed paths: {source}, {target}')

    returning = {str(src).casefold() for src, _ in to_return}
    for original, backup, signature in reversed(backups):
        actual_original = _windows_existing(root, original)
        actual_backup = _windows_existing(root, backup)
        if actual_backup is not None:
            if not _tx_matches(actual_backup, signature):
                raise ValueError(f'Old-version backup changed: {actual_backup}')
            if actual_original is not None and str(actual_original).casefold() not in returning:
                raise ValueError(f'Original path occupied before recovery: {actual_original}')
            to_restore.append((actual_backup, original))
        elif actual_original is not None and _tx_matches(actual_original, signature):
            continue  # old file was never backed up, or already restored
        else:
            raise ValueError(f'Old version is missing or changed: {original}')

    # Wrapper movements were recorded before the original renames. Their
    # target may contain a moved-in file until to_return has run. The wrapper
    # source itself must not have been replaced by unrelated user content.
    for source, target, _ in reversed(wrappers):
        if _windows_existing(root, source) is not None:
            if _windows_existing(root, target) is not None:
                raise ValueError(f'Both wrapper locations exist: {source}, {target}')
            continue
        if _windows_existing(root, target) is None:
            raise ValueError(f'Missing archive wrapper content: {target}')
        to_unwrap.append((target, source))

    # Changes are reversible independently; a failure leaves the journal for
    # another explicit attempt rather than deleting the sole old copy.
    for current, original in to_return:
        _assert_mod_subpath_safe(root, current)
        _assert_mod_subpath_safe(root, original)
        original.parent.mkdir(parents=True, exist_ok=True)
        if _windows_existing(root, original) is not None:
            raise ValueError(f'Recovery would overwrite incoming file: {original}')
        current.rename(original)
    for current, original in to_restore:
        _assert_mod_subpath_safe(root, current)
        _assert_mod_subpath_safe(root, original)
        original.parent.mkdir(parents=True, exist_ok=True)
        if _windows_existing(root, original) is not None:
            raise ValueError(f'Recovery would overwrite existing file: {original}')
        current.rename(original)
    for current, original in to_unwrap:
        _assert_mod_subpath_safe(root, current)
        _assert_mod_subpath_safe(root, original)
        original.parent.mkdir(parents=True, exist_ok=True)
        if _windows_existing(root, original) is not None:
            raise ValueError(f'Recovery would overwrite wrapper file: {original}')
        current.rename(original)
    data['state'] = 'rolled_back'
    _write_txn(Path(txn), data)
    # Empty journal storage may fail to delete (antivirus, crash, etc). A
    # later explicit recovery cleans its rolled_back marker without undoing.
    try:
        recover_transaction(root, Path(txn))
    except (OSError, ValueError):
        # For example, a user placed unknown content in the transaction dir.
        # The rollback is complete; keep the journal and unknown files safe.
        pass
    return 'rolled_back'


@dataclass(frozen=True)
class NormalizationPlan:
    """Immutable preflight for post-extraction staging, updates and Repair.

    The planner NEVER writes anything and never opens a .pak payload. Cosmetic
    wrapper removal is a separate journaled phase: compute this plan AFTER
    unwrapping, or reject wrappers in read-only Repair previews.
    """
    mod_root: Path
    moves: tuple[tuple[Path, Path], ...]
    replacements: tuple[Path, ...]
    auxiliaries: tuple[Path, ...]
    deterministic_fomod: bool


def normalization_plan(mod_root: Path, *, transaction: Path | None = None) -> NormalizationPlan:
    """Common candidate, FOMOD, replacement and path-safety analysis.

    Used unchanged by normal Install/Auto/Manual/Merge/Existing and the
    diagnostics Repair preview. Execution remains a single journaled rename
    path. The transaction path is only used to ignore our own journal folder
    while testing a deterministic Manual FOMOD manifest.
    """
    root = Path(mod_root)
    if not root.is_dir() or _is_reparse(root):
        raise ValueError(f'Mod installation directory is invalid: {root}')
    moves = _staging_candidates(root)
    simple_fomod = _simple_fomod_required_plan(root, moves, transaction)
    auxiliaries: list[Path] = []
    if simple_fomod is not None:
        moves, auxiliaries = simple_fomod
    replacements = _update_backups(root, moves)
    if any(str(old).casefold() == str(extra).casefold()
           for old in replacements for extra in auxiliaries):
        raise ValueError('FOMOD ancillary cleanup collides with an old package')
    for source in [src for src, _ in moves] + replacements + auxiliaries:
        _assert_mod_subpath_safe(root, source)
        if not source.exists() or _is_reparse(source):
            raise ValueError(f'Unsafe or missing normalization input: {source}')
    for _, target in moves:
        _assert_mod_subpath_safe(root, target)
    return NormalizationPlan(root, tuple(moves), tuple(replacements),
                             tuple(auxiliaries), simple_fomod is not None)


def stage_new_install(mod_root: Path) -> int:
    """Install and update containers using a crash-recoverable transaction.

    On success the journal is committed before old backups are removed. On
    failure the same conservative rollback used by the manual UI is applied.
    """
    mod_root = Path(mod_root)
    if not mod_root.is_dir() or _is_reparse(mod_root):
        raise ValueError(f'Mod installation directory is invalid: {mod_root}')
    if transaction_dirs(mod_root):
        raise RuntimeError('Unresolved __mo2s2_txn_* transaction; use Recover interrupted updates first')

    txn: Path | None = None
    count = 0
    try:
        wrapper_moves = _wrapper_unwrap_candidates(mod_root)
        game_root_moves = _game_root_unwrap_candidates(mod_root)
        # For the common already-extracted case, validate the COMPLETE plan
        # before creating even a temporary transaction journal. A second
        # pass after unwrapping is necessary only for cosmetic wrappers.
        direct_plan = None
        if not wrapper_moves and not game_root_moves:
            direct_plan = normalization_plan(mod_root)
            if not direct_plan.moves:
                return 0
        # Game-root moves are computed again after cosmetic unwrapping below.
        for source, target in wrapper_moves:
            _assert_mod_subpath_safe(mod_root, source)
            _assert_mod_subpath_safe(mod_root, target)
            if _is_reparse(source) or _windows_existing(mod_root, target) is not None:
                raise ValueError(f'Unsafe wrapper unwrap: {source} -> {target}')
        # A wrapper's nested game-root candidates can only be discovered once
        # the wrapper has been removed; retain the old behavior for this path.
        # The first journal is written before ANY physical rename.
        txn = Path(tempfile.mkdtemp(prefix='__mo2s2_txn_', dir=mod_root))
        state = {'version': TXN_VERSION, 'state': 'prepared',
                 'wrappers': [], 'moves': [], 'backups': []}
        _write_txn(txn, state)
        for source, target in wrapper_moves:
            state['wrappers'].append({'from': _tx_relative(mod_root, source),
                                      'to': _tx_relative(mod_root, target)})
        _write_txn(txn, state)
        for source, target in wrapper_moves:
            source.rename(target)

        game_moves = _game_root_unwrap_candidates(mod_root)
        for source, target in game_moves:
            _assert_mod_subpath_safe(mod_root, source)
            _assert_mod_subpath_safe(mod_root, target)
            if _is_reparse(source) or _windows_existing(mod_root, target) is not None:
                raise ValueError(f'Unsafe game-root unwrap: {source} -> {target}')
        for source, target in game_moves:
            state['wrappers'].append({'from': _tx_relative(mod_root, source),
                                      'to': _tx_relative(mod_root, target)})
        _write_txn(txn, state)
        for source, target in game_moves:
            source.rename(target)

        # The same plan builder powers read-only Repair and every mutation
        # entrypoint. Do not let execution invent separate candidate rules.
        plan = (direct_plan if direct_plan is not None
                else normalization_plan(mod_root, transaction=txn))
        moves = plan.moves
        old_files = [*plan.replacements, *plan.auxiliaries]
        state['moves'] = [{'from': _tx_relative(mod_root, src),
                           'to': _tx_relative(mod_root, dst),
                           'signature': _tx_signature(src)} for src, dst in moves]
        state['backups'] = [{'from': _tx_relative(mod_root, old),
                             'to': _tx_relative(mod_root, txn / str(i)),
                             'signature': _tx_signature(old)}
                            for i, old in enumerate(old_files)]
        _write_txn(txn, state)
        for i, old in enumerate(old_files):
            _assert_mod_subpath_safe(mod_root, old)
            old.rename(txn / str(i))
        for source, target in moves:
            _assert_mod_subpath_safe(mod_root, source)
            _assert_mod_subpath_safe(mod_root, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if _windows_existing(mod_root, target) is not None:
                raise ValueError(f'Unexpected installed package at {target}')
            source.rename(target)
            count += 1
        state['state'] = 'committed'
        _write_txn(txn, state)
    except Exception as error:
        if txn is not None:
            try:
                recover_transaction(mod_root, txn)
            except Exception as recover_error:
                raise RuntimeError('Installation failed and rollback needs manual recovery: '
                                   f'{recover_error}') from error
        raise

    # Commit was durable before the first cleanup. Failure to remove an old
    # backup leaves a committed journal, not an ambiguous rollback request.
    try:
        _prune_empty_inboxes(mod_root)
        if plan.deterministic_fomod:
            _prune_empty_source_parents(mod_root, [src for src, _ in moves] +
                                        [mod_root / 'Stalker2/Content', mod_root / 'Stalker2'])
        recover_transaction(mod_root, txn)
    except (OSError, ValueError) as exc:
        log_error(f'Committed update cleanup pending in {txn}: {exc}')
    return count
