"""Persistent content index and priority-aware analysis, independent of Qt/MO2.

Each physical PAK or UTOC owns one cache row. The active profile and conflict
results are deliberately transient: priority changes never invalidate indices.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from collections.abc import Mapping
from itertools import groupby
from functools import lru_cache
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import zlib
from typing import Callable, Iterable

from .readers import IndexErrorUnsupported, read_inventory
from ..paks.virtual_core import Item, _assert_mod_subpath_safe, _is_reparse

SCHEMA_VERSION = 3
CHUNK_RESOURCES = 512
MAX_CHUNK_BYTES = 4 * 1024 * 1024
PARSER_VERSION = 6
ALLOWED = {'.pak', '.utoc'}


@dataclass(frozen=True)
class Source:
    """A physical container and the MO2 mod that owns it."""
    path: Path
    mod: str
    priority: int
    members: tuple[Path, ...]
    mod_root: Path | None = None


@dataclass(frozen=True)
class State:
    """Indexed status and resource paths for one container."""
    path: str
    mod: str
    priority: int
    status: str
    error: str
    resources: Iterable[str]
    # Internal order is used when SQL returns containers in key order rather
    # than in the profile's source order. Direct Snapshot callers need no rank.
    source_order: int | None = None


@dataclass(frozen=True)
class ContainerSummary:
    """Report-only container metadata; never holds the full resource inventory."""
    path: str
    mod: str
    priority: int
    status: str
    error: str
    resource_count: int


@dataclass(frozen=True)
class Collision:
    """Owners and winning mod for a virtual resource path."""
    path: str
    owners: tuple[str, ...]
    winner: str | None


def collision_losers(collision: Collision) -> str:
    """Readable losing mod names, never including the winning mod."""
    if collision.winner is None:
        return 'Undetermined (tie)'
    return ', '.join(owner for owner in collision.owners
                     if owner != collision.winner)


class _LazyCollisionByMod(Mapping):
    """Build overlap lists on demand with a *per-snapshot* bounded cache.

    A function-level @lru_cache on a method would retain past Snapshot objects
    through its bound self argument, leaking large inventories across profiles.
    """
    def __init__(self, collisions: list[Collision], mods):
        self._collisions = collisions
        self._mods = mods
        self._cache: OrderedDict[str, list[Collision]] = OrderedDict()

    def __getitem__(self, mod: str) -> list[Collision]:
        if mod not in self._mods:
            raise KeyError(mod)
        if mod in self._cache:
            self._cache.move_to_end(mod)
            return self._cache[mod]
        result = [c for c in self._collisions if mod in c.owners]
        self._cache[mod] = result
        if len(self._cache) > 4:
            self._cache.popitem(last=False)
        return result

    def __iter__(self):
        return iter(self._mods)

    def __len__(self):
        return len(self._mods)


class CatalogCancelled(Exception):
    """A newer catalog request superseded the current read-only operation."""


class CacheChunkError(ValueError):
    """Only a single container's cached inventory is damaged, not the profile."""

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f'{path}: {reason}')



class Snapshot:
    """Bounded, priority-aware view of cached container resources."""
    def __init__(self, states: Iterable[State], discovery_errors: Iterable[str] = (),
                 known_mods: Iterable[str] = (),
                 is_cancelled: Callable[[], bool] | None = None):
        summaries: list[tuple[int, ContainerSummary]] = []
        self.discovery_errors = tuple(discovery_errors)
        self.mods: dict[str, list[str]] = defaultdict(list)
        self.errors: dict[str, list[str]] = defaultdict(list)
        self.discovery_by_mod: dict[str, list[str]] = defaultdict(list)
        # Intern (mod, priority) pairs once per snapshot. Each unique path
        # stores a *single* token rather than a dict or a repeated tuple;
        # shared paths only allocate a map when a second mod owns them.
        # Priority is tied to the exact resource's source container, never
        # to the last processed container from the same mod.
        owner_pairs: list[tuple[str, int]] = []
        owner_ids: dict[tuple[str, int], int] = {}
        by_resource: dict[str, int | dict[str, int]] = {}
        names: dict[str, str] = {}
        for position, state in enumerate(states):
            if is_cancelled and is_cancelled():
                raise CatalogCancelled()
            self.mods[state.mod]
            resource_count = 0
            if state.status != 'indexed':
                self.errors[state.mod].append(f'{Path(state.path).name}: {state.error or state.status}')
            else:
                pair = (state.mod, state.priority)
                token = owner_ids.get(pair)
                if token is None:
                    token = len(owner_pairs) + 1  # Zero is reserved for absent.
                    owner_ids[pair] = token
                    owner_pairs.append(pair)
                paths = self.mods[state.mod]
                for resource in state.resources:
                    resource_count += 1
                    if is_cancelled and resource_count % 1024 == 0 and is_cancelled():
                        raise CatalogCancelled()
                    norm = resource.replace('\\', '/').casefold()
                    previous = by_resource.get(norm)
                    if previous is None:
                        names[norm] = resource
                        by_resource[norm] = token
                        paths.append(norm)
                    elif isinstance(previous, int):
                        previous_mod, previous_priority = owner_pairs[previous - 1]
                        if previous_mod == state.mod:
                            # For duplicate paths within one mod, keep the highest source rank.
                            if state.priority > previous_priority:
                                by_resource[norm] = token
                        else:
                            by_resource[norm] = {previous_mod: previous,
                                                 state.mod: token}
                            paths.append(norm)
                    else:
                        old = previous.get(state.mod)
                        if old is None:
                            previous[state.mod] = token
                            paths.append(norm)
                        elif state.priority > owner_pairs[old - 1][1]:
                            previous[state.mod] = token
            order = position if state.source_order is None else state.source_order
            summaries.append((order, ContainerSummary(
                state.path, state.mod, state.priority, state.status,
                state.error, resource_count)))
        # SQLite streams grouped by container key to avoid buffering all names.
        # Restore caller/source order for diagnostics and tests with only small
        # fixed-size per-container summaries, not heavyweight resource lists.
        summaries.sort(key=lambda entry: entry[0])
        self.states = tuple(entry[1] for entry in summaries)
        known_mods = set(known_mods) | {state.mod for state in self.states}
        for mod in known_mods:
            self.mods[mod]
        longest_first = sorted(known_mods, key=len, reverse=True)
        for error in self.discovery_errors:
            mod = next((name for name in longest_first if error.startswith(name + ':')), None)
            if mod is None and ': ' in error:
                mod = error.split(': ', 1)[0]
            if mod is not None:
                self.mods[mod]
                self.discovery_by_mod[mod].append(error)
        self.paths = names
        # Only Contents needs alphabetical global paths; Conflicts and the
        # diagnostics page should not pay for sorting the entire catalog.
        self._sorted_paths: tuple[str, ...] | None = None
        # Sorting every per-mod index duplicates many thousands of pointers
        # even when the user never selects those mods in Contents.
        self._sorted_by_mod: dict[str, tuple[str, ...]] = {}
        self.ownership = by_resource
        self._owner_pairs = owner_pairs
        self.collisions: list[Collision] = []
        self._overlap_counts: dict[str, int] = defaultdict(int)
        self._overwritten_counts: dict[str, int] = defaultdict(int)

        # Interned owner tokens identify collision shapes without copying mod
        # names and priorities for every overlapping resource.
        @lru_cache(maxsize=256)
        def collision_shape(tokens: tuple[int, ...]):
            pairs = [owner_pairs[token - 1] for token in tokens]
            highest = max(priority for _, priority in pairs)
            leaders = [mod for mod, priority in pairs if priority == highest]
            winner = leaders[0] if len(leaders) == 1 else None
            sorted_owners = tuple(mod for mod, _ in sorted(
                pairs, key=lambda pair: (pair[1], pair[0].casefold())))
            losers = tuple(mod for mod, priority in pairs if priority < highest)
            return sorted_owners, winner, losers

        for index, (key, owners) in enumerate(by_resource.items()):
            if is_cancelled and index % 1024 == 0 and is_cancelled():
                raise CatalogCancelled()
            if isinstance(owners, int):
                continue
            sorted_owners, winner, losers = collision_shape(tuple(owners.values()))
            self.collisions.append(Collision(names[key], sorted_owners, winner))
            for mod in sorted_owners:
                self._overlap_counts[mod] += 1
            for mod in losers:
                self._overwritten_counts[mod] += 1
        self.collisions.sort(key=lambda c: c.path.casefold())
        # Populate a selected mod's conflict list only when requested.
        # The main view needs none of the per-mod arrays at startup.
        self.by_mod = _LazyCollisionByMod(self.collisions, self.mods)

    @property
    def sorted_paths(self) -> tuple[str, ...]:
        if self._sorted_paths is None:
            self._sorted_paths = tuple(sorted(self.paths))
        return self._sorted_paths

    def owner_names(self, resource: str) -> tuple[str, ...]:
        """Owner names without allocating a dict for a uniquely owned file."""
        owner = self.ownership[resource.replace('\\', '/').casefold()]
        if isinstance(owner, int):
            return (self._owner_pairs[owner - 1][0],)
        return tuple(sorted(owner, key=str.casefold))

    def diagnostic_report(self) -> str:
        """Compact, copyable report. Never reads archive payloads or the VFS."""
        indexed = sum(state.status == 'indexed' for state in self.states)
        lines = [
            'mo2s2 catalog diagnostics',
            f'Containers: {len(self.states)} (indexed: {indexed}, '
            f'incomplete: {len(self.states) - indexed})',
            f'Known mods: {len(self.mods)}',
            f'Unique resource paths: {len(self.paths)}',
            f'Overlapping resource paths: {len(self.collisions)}',
            f'Discovery errors: {len(self.discovery_errors)}',
            '', 'Containers:',
        ]
        for state in sorted(self.states, key=lambda entry: (entry.mod.casefold(), entry.path.casefold())):
            lines.append(f'  [{state.status.upper()}] {state.mod} :: {state.path} :: '
                         f'{state.resource_count} resources')
            if state.error:
                lines.append(f'    {state.error}')
        if self.discovery_errors:
            lines.extend(['', 'Discovery errors:'])
            lines.extend('  ' + error for error in self.discovery_errors)
        lines.extend(['', 'Resource overlaps by mod:'])
        for mod in sorted(self.mods, key=str.casefold):
            covered = self.fully_overwritten(mod)
            note = ('unknown' if covered is None else
                    'yes' if covered else 'no')
            lines.append(f'  {mod}: {len(self.mods[mod])} resources, '
                         f'{self._overlap_counts.get(mod, 0)} overlapping, '
                         f'fully overwritten: {note}')
        return '\n'.join(lines) + '\n'

    def fully_overwritten(self, mod: str) -> bool | None:
        """None means incomplete/unknown. Strictly higher priority required."""
        if self.errors.get(mod) or self.discovery_by_mod.get(mod):
            return None
        paths = self.mods.get(mod, ())
        if not paths:
            return None
        # Computed once while building collisions. This query is O(1) even
        # for a mod with hundreds of thousands of resources.
        return self._overwritten_counts.get(mod, 0) == len(paths)

    def collision_results(self, mod: str | None, query: str = '', limit: int = 600):
        """Bounded page plus total, without allocating an entire filtered copy."""
        needle = query.replace('\\', '/').casefold()
        candidates = self.collisions if mod is None else self.by_mod.get(mod, ())
        if not needle:
            return list(candidates[:max(0, limit)]), len(candidates)
        page = []
        total = 0
        for collision in candidates:
            if needle and needle not in collision.path.casefold() and not any(
                    needle in owner.casefold() for owner in collision.owners):
                continue
            total += 1
            if len(page) < limit:
                page.append(collision)
        return page, total

    def results(self, mod: str | None, query: str = '', limit: int = 1000):
        """Return a bounded page of matching resources and the full match count."""
        needle = query.replace('\\', '/').casefold()
        if mod is None:
            keys = self.sorted_paths
        else:
            keys = self._sorted_by_mod.get(mod)
            if keys is None:
                # A mod is sorted only when selected, not at startup. Keep a
                # small cache for switching between recently selected mods.
                keys = tuple(sorted(self.mods.get(mod, ())))
                if len(self._sorted_by_mod) >= 4:
                    self._sorted_by_mod.pop(next(iter(self._sorted_by_mod)))
                self._sorted_by_mod[mod] = keys
        # The common unfiltered view already knows the exact count. Avoid
        # walking hundreds of thousands of paths just to render 800 rows.
        if not needle:
            return [self.paths[key] for key in keys[:max(0, limit)]], len(keys)
        # Never allocate a second list of hundreds of thousands of strings
        # merely to display the first page in a bounded Qt tree.
        matches = []
        total = 0
        for key in keys:
            if needle in key:
                total += 1
                if len(matches) < limit:
                    matches.append(self.paths[key])
        return matches, total


def bounded_partner_preview(overlaps: Iterable[Collision], selected_mod: str,
                            *, max_groups: int = 128, max_rows: int = 1000):
    """Bound Qt conflict rows even when one path overlaps hundreds of mods.

    Count all partners in the already-bounded page, but return no more than
    max_groups headings and max_rows children. Allocation is balanced so one
    partner cannot starve other partners of visible examples. This function
    intentionally has no Qt imports and is covered by unit tests.
    """
    overlaps = tuple(overlaps)
    counts: dict[str, int] = defaultdict(int)
    for collision in overlaps:
        for partner in collision.owners:
            if partner != selected_mod:
                counts[partner] += 1
    names = sorted(counts, key=str.casefold)
    shown_names = names[:max(0, min(max_groups, max_rows))]
    if not shown_names:
        return [], len(names), bool(names)
    # One slot for each displayed partner, then spread the remainder.
    cap = max(1, max_rows // len(shown_names))
    groups = {name: [] for name in shown_names}
    for collision in overlaps:
        for partner in collision.owners:
            if partner in groups and len(groups[partner]) < cap:
                groups[partner].append(collision)
    result = [(name, groups[name], counts[name]) for name in shown_names]
    truncated = (len(shown_names) < len(names) or
                 any(len(rows) < count for name, rows, count in result))
    return result, len(names), truncated


def discover(items: Iterable[Item], *,
             is_cancelled: Callable[[], bool] | None = None) -> tuple[list[Source], list[str]]:
    """Enumerate real files only, never following linked mod-owned directories."""
    found: dict[str, Source] = {}
    errors: list[str] = []
    for item in items:
        if is_cancelled and is_cancelled():
            raise CatalogCancelled()
        if not item.managed:
            # A visible active mod must not look 'empty' merely because its
            # packages are outside the managed virtual load order.
            errors.append(f'{item.mod_name}: {item.source.name}: not managed by mo2s2')
            continue
        if item.warning:
            errors.append(f'{item.mod_name}: {item.source.name}: {item.warning}')
            continue
        if item.mod_root is None:
            errors.append(f'{item.mod_name}: no trusted mod root')
            continue
        # Even a nested package root must be checked before os.walk starts.
        try:
            _assert_mod_subpath_safe(item.mod_root, item.source)
        except ValueError as exc:
            errors.append(f'{item.mod_name}: {item.source.name}: {exc}')
            continue
        paths = []
        if item.kind == 'PAK':
            paths = list(item.companions)
        else:
            def walk_error(exc):
                # os.walk normally swallows scandir failures. A truncated
                # discovery is *not* a complete resource inventory.
                errors.append(f'{item.mod_name}: cannot enumerate {exc.filename}: {exc}')

            for folder, dirs, files in os.walk(item.source, followlinks=False,
                                               onerror=walk_error):
                if is_cancelled and is_cancelled():
                    raise CatalogCancelled()
                safe_dirs = []
                for name in dirs:
                    candidate = Path(folder, name)
                    try:
                        _assert_mod_subpath_safe(item.mod_root, candidate)
                        if _is_reparse(candidate):
                            raise ValueError('linked/reparse directory')
                    except (OSError, ValueError) as exc:
                        errors.append(f'{item.mod_name}: {candidate}: {exc}')
                    else:
                        safe_dirs.append(name)
                dirs[:] = safe_dirs
                paths.extend(Path(folder, name) for name in files)
        # Pre-group companions by normalized stems. Searching every file in a
        # directory for each UTOC would be quadratic in large Zone Kit mods.
        parent_groups: dict[Path, tuple[dict[str, list[Path]], dict[str, list[Path]]]] = {}
        def companions_by_group(parent: Path):
            """Index same-directory container companions without repeated scans."""
            if parent not in parent_groups:
                candidates = (item.companions if item.kind == 'PAK' else
                              tuple(parent.iterdir()))
                exact: dict[str, list[Path]] = defaultdict(list)
                partitions: dict[str, list[Path]] = defaultdict(list)
                for candidate in candidates:
                    ext = candidate.suffix.casefold()
                    if ext not in {'.utoc', '.ucas', '.sig'}:
                        continue
                    stem = candidate.stem.casefold()
                    exact[stem].append(candidate)
                    if ext == '.ucas':
                        base, separator, number = stem.rpartition('_s')
                        if separator and number.isdigit():
                            partitions[base].append(candidate)
                parent_groups[parent] = exact, partitions
            return parent_groups[parent]

        for file in paths:
            if is_cancelled and is_cancelled():
                raise CatalogCancelled()
            if file.suffix.casefold() not in ALLOWED:
                continue
            try:
                _assert_mod_subpath_safe(item.mod_root, file)
                if not file.is_file() or _is_reparse(file):
                    raise ValueError('not a regular local file')
                # UTOC validity depends on matching UCAS and optional segments.
                group = file.stem.casefold()
                members = [file]
                if file.suffix.casefold() == '.utoc':
                    exact, partitions = companions_by_group(file.parent)
                    members = exact.get(group, []) + partitions.get(group, [])
                    if not any(p.suffix.casefold() == '.ucas' and p.stem.casefold() == group for p in members):
                        raise ValueError('missing .ucas companion')
                for member in members:
                    _assert_mod_subpath_safe(item.mod_root, member)
                    if not member.is_file() or _is_reparse(member):
                        raise ValueError(f'unsafe or missing companion: {member.name}')
            except (OSError, ValueError) as exc:
                errors.append(f'{item.mod_name}: {file.name}: {exc}')
                continue
            k = str(file.absolute()).casefold()
            found[k] = Source(file, item.mod_name, item.priority,
                              tuple(sorted(set(members), key=str)), item.mod_root)
    return list(found.values()), errors


def signature(source: Source) -> str:
    """Cheap, conservative metadata fingerprint for every physical member."""
    records = []
    for member in source.members:
        if source.mod_root is not None:
            _assert_mod_subpath_safe(source.mod_root, member)
        if _is_reparse(member):
            raise ValueError(f'Unsafe linked container: {member}')
        st = member.stat()
        if not member.is_file():
            raise ValueError(f'Not a file: {member}')
        records.append((str(member.absolute()).casefold(), st.st_size, st.st_mtime_ns,
                        st.st_ctime_ns))
    return hashlib.sha256(json.dumps(records, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()


def encode_resource_chunks(paths: Iterable[str]):
    """Emit bounded zlib blocks with SHA-256 of the exact UTF-8 contents.

    zlib integrity alone cannot detect a valid but modified compressed stream.
    A digest for each independent chunk detects count-preserving changes to
    resource paths without hashing the whole PAK/UCAS payload.
    """
    pending: list[bytes] = []
    used = 0
    chunk = 0

    def encode(rows):
        raw = b'\x00'.join(rows)
        return sqlite3.Binary(zlib.compress(raw, 3)), len(rows), hashlib.sha256(raw).digest()

    for path in paths:
        value = path.encode('utf-8', 'strict')
        if not value or b'\x00' in value or len(value) > MAX_CHUNK_BYTES:
            raise ValueError('Invalid resource path in catalog inventory')
        if pending and (len(pending) >= CHUNK_RESOURCES or
                        used + len(value) + 1 > MAX_CHUNK_BYTES):
            payload, count, digest = encode(pending)
            yield chunk, payload, count, digest
            chunk += 1
            pending.clear()
            used = 0
        pending.append(value)
        used += len(value) + 1
    if pending:
        payload, count, digest = encode(pending)
        yield chunk, payload, count, digest


def decode_resource_chunks(records):
    """Validate each block before yielding any resource from that block."""
    expected_chunk = 0
    for _, chunk, data, count, digest in records:
        if chunk != expected_chunk or not (1 <= count <= CHUNK_RESOURCES):
            raise ValueError('Invalid catalog resource chunk order or count')
        expected_chunk += 1
        if not isinstance(digest, bytes) or len(digest) != hashlib.sha256().digest_size:
            raise ValueError('Missing or invalid catalog block digest')
        inflater = zlib.decompressobj()
        raw = inflater.decompress(data, MAX_CHUNK_BYTES + 1)
        if (len(raw) > MAX_CHUNK_BYTES or not inflater.eof or
                inflater.unconsumed_tail or inflater.unused_data):
            raise ValueError('Invalid or oversized catalog resource chunk')
        if hashlib.sha256(raw).digest() != digest:
            raise ValueError('Catalog resource chunk SHA-256 mismatch')
        values = raw.split(b'\x00')
        if len(values) != count:
            raise ValueError('Catalog chunk count mismatch')
        for value in values:
            if not value:
                raise ValueError('Empty resource name in catalog chunk')
            yield value.decode('utf-8', 'strict')


def checked_inventory(records, expected_count: int):
    total = 0
    for path in decode_resource_chunks(records):
        total += 1
        yield path
    if total != expected_count:
        raise ValueError('Cached resource count mismatch')


class Catalog:
    """SQLite-backed inventory cache with isolated recovery of corrupt entries."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=15)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        # Development cache schemas are disposable; avoid maintaining
        # migrations for incompatible indexes created by earlier builds.
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version != SCHEMA_VERSION:
            with self.db:
                self.db.execute('DROP TABLE IF EXISTS resources')
                self.db.execute('DROP TABLE IF EXISTS containers')
                self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        self.db.execute('''CREATE TABLE IF NOT EXISTS containers (
            path TEXT PRIMARY KEY COLLATE NOCASE,
            signature TEXT NOT NULL, parser_version INTEGER NOT NULL,
            index_sha256 TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL,
            resource_count INTEGER NOT NULL DEFAULT 0)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS resources (
            container_path TEXT NOT NULL REFERENCES containers(path) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL, payload BLOB NOT NULL,
            resource_count INTEGER NOT NULL, digest BLOB NOT NULL,
            PRIMARY KEY(container_path, chunk_index))''')
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def sync(self, sources: Iterable[Source], *, force=False, on_progress: Callable | None = None,
             is_cancelled: Callable[[], bool] | None = None):
        """Atomic per-container updates; failures are explicit, never cached as zero files."""
        sources = list(sources)
        indexed = reused = errors = 0
        # Most launches are cache hits. Prefetch signatures in SQLite-sized
        # batches instead of issuing one SELECT per container on every launch.
        cached_rows: dict[str, tuple[str, int]] = {}
        if not force:
            for start in range(0, len(sources), 300):
                batch = sources[start:start + 300]
                paths = [str(source.path.absolute()) for source in batch]
                placeholders = ','.join('?' for _ in paths)
                for path, digest, version, status in self.db.execute(
                    f'SELECT path,signature,parser_version,status FROM containers '
                    f'WHERE path IN ({placeholders})', paths):
                    # Unsupported formats are deterministic for a given parser
                    # version. I/O and race errors are transient and must be
                    # retried even if the filesystem metadata did not change.
                    if status in ('indexed', 'unsupported'):
                        cached_rows[path.casefold()] = (digest, version)
        for number, source in enumerate(sources, 1):
            if is_cancelled and is_cancelled():
                break
            before = ''
            path = str(source.path.absolute())
            try:
                before = signature(source)
                cached = cached_rows.get(path.casefold())
                if not force and cached == (before, PARSER_VERSION):
                    reused += 1
                else:
                    inv = read_inventory(source.path)
                    after = signature(source)
                    if before != after:
                        raise IndexErrorUnsupported('Container changed while indexing')
                    with self.db:
                        old = self.db.execute('SELECT index_sha256,status,parser_version,resource_count FROM containers WHERE path=?', (path,)).fetchone()
                        self.db.execute('''INSERT INTO containers VALUES (?,?,?,?,?,?,?)
                            ON CONFLICT(path) DO UPDATE SET signature=excluded.signature,
                            parser_version=excluded.parser_version,index_sha256=excluded.index_sha256,
                            status=excluded.status,error=excluded.error,
                            resource_count=excluded.resource_count''',
                            (path, after, PARSER_VERSION, inv.index_hash, 'indexed', '', len(inv.paths)))
                        if force or old != (inv.index_hash, 'indexed', PARSER_VERSION, len(inv.paths)):
                            self.db.execute('DELETE FROM resources WHERE container_path=?', (path,))
                            self.db.executemany('INSERT INTO resources VALUES (?,?,?,?,?)',
                                ((path, chunk, payload, count, digest)
                                 for chunk, payload, count, digest in encode_resource_chunks(inv.paths)))
                    cached_rows[path.casefold()] = (after, PARSER_VERSION)
                    indexed += 1
            except (OSError, ValueError, sqlite3.DatabaseError) as exc:
                errors += 1
                state = ('stale' if str(exc) == 'Container changed while indexing' else
                         'unsupported' if isinstance(exc, IndexErrorUnsupported) else 'error')
                # Refuse to keep stale paths after a parsing failure.
                try:
                    with self.db:
                        self.db.execute('DELETE FROM containers WHERE path=?', (path,))
                        self.db.execute('INSERT INTO containers VALUES (?,?,?,?,?,?,?)',
                                        (path, before, PARSER_VERSION, '', state, str(exc)[:500], 0))
                    cached_rows[path.casefold()] = (before, PARSER_VERSION)
                except sqlite3.DatabaseError:
                    raise
            if on_progress:
                on_progress(number, len(sources), source.path.name)
        return indexed, reused, errors

    def snapshot(self, sources: Iterable[Source], discovery_errors=(), *,
                 known_mods=(), is_cancelled: Callable[[], bool] | None = None,
                 repair: bool = False) -> Snapshot:
        """Load active containers with bounded, fault-isolated cache recovery.

        The common path remains one pass / two queries per batch. A damaged
        resource stream aborts the unfinished Snapshot, then a *single* bounded
        validation pass identifies all bad containers. Repair may reread only
        those physical archives; never discard good cached inventories.

        The cache is shared across profiles. Fetching only active sources also
        avoids pulling unused cached mods into memory; batching avoids SQLite's
        host-parameter limit. Keep the input order for stable diagnostics.
        """
        sources = list(sources)
        batch_size = 300
        damaged: dict[str, str] = {}
        self.last_repaired = 0
        self.last_cache_corrupt = 0

        def checked_container(path: str, records, expected: int):
            """Validate and read one source container before caching it."""
            try:
                yield from checked_inventory(records, expected)
            except (ValueError, TypeError, UnicodeError, zlib.error) as exc:
                raise CacheChunkError(path, str(exc)) from exc

        def states_from_db():
            """Stream cached inventories while preserving source order."""
            for start in range(0, len(sources), batch_size):
                if is_cancelled and is_cancelled():
                    raise CatalogCancelled()
                batch = sources[start:start + batch_size]
                paths = [str(source.path.absolute()) for source in batch]
                placeholders = ','.join('?' for _ in paths)
                found = {
                    path.casefold(): (path, status, error, resource_count)
                    for path, status, error, resource_count in self.db.execute(
                        f'SELECT path,status,error,resource_count FROM containers '
                        f'WHERE path IN ({placeholders})', paths)
                }
                # Fetch in index order and consume *one container at a time*,
                # avoiding the previous all-containers defaultdict(list) and
                # a second copy of every name retained by Snapshot.states.
                indexed_paths = [row[0] for row in found.values()
                                 if row[1] == 'indexed' and row[0].casefold() not in damaged]
                by_key: dict[str, list[tuple[int, Source, str]]] = defaultdict(list)
                for number, (source, path) in enumerate(zip(batch, paths), start):
                    by_key[path.casefold()].append((number, source, path))
                emitted: set[str] = set()
                if indexed_paths:
                    resource_placeholders = ','.join('?' for _ in indexed_paths)
                    cursor = self.db.execute(
                        f'SELECT container_path,chunk_index,payload,resource_count,digest FROM resources '
                        f'WHERE container_path IN ({resource_placeholders}) '
                        'ORDER BY container_path,chunk_index', indexed_paths)
                    for key, records in groupby(cursor, key=lambda row: row[0].casefold()):
                        attached = by_key.get(key, ())
                        if not attached:
                            continue
                        emitted.add(key)
                        # Normally there is exactly one MO2 owner of a physical
                        # container. Duplicate input sources need repeatable
                        # resources, so materialize only that unusual case.
                        if len(attached) > 1:
                            names = tuple(checked_container(found[key][0], records, found[key][3]))
                            for number, source, path in attached:
                                yield State(path, source.mod, source.priority,
                                            'indexed', '', names, number)
                        else:
                            number, source, path = attached[0]
                            yield State(path, source.mod, source.priority,
                                        'indexed', '', checked_container(path, records, found[key][3]), number)
                for key, attached in by_key.items():
                    if key in emitted:
                        continue
                    row = found.get(key)
                    for number, source, path in attached:
                        if key in damaged:
                            yield State(path, source.mod, source.priority,
                                        'error', f'Damaged cached inventory: {damaged[key]}',
                                        (), number)
                        elif row is None:
                            yield State(path, source.mod, source.priority,
                                        'pending', 'Not indexed yet', (), number)
                        elif row[1] != 'indexed':
                            yield State(path, source.mod, source.priority,
                                        row[1], row[2], (), number)
                        elif row[3]:
                            # No resource rows for an inventory that was nonempty.
                            # Retry through the same isolated repair path.
                            raise CacheChunkError(path, 'Cached resource chunks missing')
                        else:
                            yield State(path, source.mod, source.priority,
                                        'indexed', '', (), number)

        try:
            return Snapshot(states_from_db(), discovery_errors,
                            known_mods=known_mods, is_cancelled=is_cancelled)
        except CacheChunkError as first_error:
            if is_cancelled and is_cancelled():
                raise CatalogCancelled()
            # The first broken entry could be followed by several others.
            # Collect all failures in one pass rather than retrying the full
            # Snapshot once for each bad archive (quadratic worst case).
            damaged = self._check_cached_inventories(sources, is_cancelled)
            damaged.setdefault(first_error.path.casefold(), first_error.reason)
            self.last_cache_corrupt = len(damaged)
            # Cache keys are casefolded for ownership comparisons, but never
            # use those folded strings as SQL paths: SQLite's NOCASE collation
            # only folds ASCII. Cyrillic and other Unicode mod names must use
            # their original absolute path for an exact cache-row update.
            physical_paths = {
                str(source.path.absolute()).casefold(): str(source.path.absolute())
                for source in sources
            }
            with self.db:
                for key, reason in damaged.items():
                    if is_cancelled and is_cancelled():
                        raise CatalogCancelled()
                    self.db.execute("UPDATE containers SET parser_version=0, "
                                    "status='error',error=?,index_sha256='' "
                                    "WHERE path=? COLLATE NOCASE AND status='indexed'",
                        (f'Damaged cached inventory: {reason}'[:500], physical_paths[key]))
                    # Deleting only this container's chunks frees bad data and
                    # guarantees the next sync cannot treat it as a cache hit.
                    self.db.execute('DELETE FROM resources WHERE container_path=?', (physical_paths[key],))
            if repair:
                targets = []
                seen = set()
                for source in sources:
                    key = str(source.path.absolute()).casefold()
                    if key in damaged and key not in seen:
                        targets.append(source)
                        seen.add(key)
                if is_cancelled and is_cancelled():
                    raise CatalogCancelled()
                # Forced reindexing replaces only the damaged rows. Parsing
                # failure remains an explicit per-mod error in the Snapshot.
                scanned, _, _ = self.sync(targets, force=True,
                                          is_cancelled=is_cancelled)
                self.last_repaired = scanned
                # Rebuild from the post-repair database. Failed repairs have
                # status='error' and are still clearly incomplete; successful
                # repairs must not be masked by the first pass's bad-key set.
                damaged = {}
            # Do not suppress a further unexpected failure. If an archive is
            # rewritten concurrently, it is safer to report it as incomplete.
            return Snapshot(states_from_db(), discovery_errors,
                            known_mods=known_mods, is_cancelled=is_cancelled)

    def _check_cached_inventories(self, sources: list[Source], is_cancelled=None) -> dict[str, str]:
        """Full validation only on the exceptional path, in O(cache size) time."""
        damaged: dict[str, str] = {}
        paths = list(dict.fromkeys(str(source.path.absolute()) for source in sources))
        for start in range(0, len(paths), 300):
            if is_cancelled and is_cancelled():
                raise CatalogCancelled()
            batch = paths[start:start + 300]
            marks = ','.join('?' for _ in batch)
            expected = {path.casefold(): count for path, count in self.db.execute(
                f"SELECT path,resource_count FROM containers WHERE status='indexed' "
                f'AND path IN ({marks})', batch)}
            if not expected:
                continue
            indexed_paths = [path for path in batch if path.casefold() in expected]
            if not indexed_paths:
                continue
            matches = ','.join('?' for _ in indexed_paths)
            cursor = self.db.execute(
                f'SELECT container_path,chunk_index,payload,resource_count,digest FROM resources '
                f'WHERE container_path IN ({matches}) ORDER BY container_path,chunk_index',
                indexed_paths)
            present = set()
            for key, records in groupby(cursor, key=lambda r: r[0].casefold()):
                present.add(key)
                try:
                    count = 0
                    for _ in checked_inventory(records, expected[key]):
                        count += 1
                        if is_cancelled and count % 1024 == 0 and is_cancelled():
                            raise CatalogCancelled()
                except (ValueError, TypeError, UnicodeError, zlib.error) as exc:
                    damaged[key] = str(exc)[:350]
            for key, count in expected.items():
                if count and key not in present:
                    damaged[key] = 'Cached resource chunks missing'
        return damaged
