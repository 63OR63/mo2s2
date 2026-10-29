"""Bounded, read-only Unreal PAK and IoStore directory-index readers.

Only paths are decoded. No asset payloads, Oodle decompression, AES keys, or
extractors are used. Unsupported index layouts fail closed (never 'no files').
"""
from __future__ import annotations

from dataclasses import dataclass
from array import array
from bisect import bisect_left
import hashlib
from pathlib import Path
import struct

MAX_INDEX = 256 * 1024 * 1024
MAX_ENTRIES = 3_000_000
MAX_TEXT = 32_768
MAX_DEPTH = 256
PAK_MAGIC = 0x5A6F12E1
UTOC_MAGIC = b'-==--==--==--==-'
NONE = 0xFFFFFFFF


class IndexErrorUnsupported(ValueError):
    """An index cannot be listed reliably; do not report an empty archive."""


@dataclass(frozen=True)
class Inventory:
    """Validated resource paths and metadata extracted from one index."""
    paths: tuple[str, ...]
    index_hash: str
    format: str


class Reader:
    """Bounds-checked cursor for Unreal binary directory-index structures."""
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise IndexErrorUnsupported('Truncated or invalid directory index')
        out = self.data[self.pos:self.pos + size]
        self.pos += size
        return out

    def num(self, fmt: str) -> int:
        # The index contains millions of small fixed-width fields. Avoid
        # allocating a fresh bytes object for every scalar read.
        spec = '<' + fmt
        size = struct.calcsize(spec)
        if self.pos + size > len(self.data):
            raise IndexErrorUnsupported('Truncated or invalid directory index')
        value = struct.unpack_from(spec, self.data, self.pos)[0]
        self.pos += size
        return value

    def count(self) -> int:
        n = self.num('i')
        if n < 0 or n > MAX_ENTRIES:
            raise IndexErrorUnsupported('Invalid index item count')
        return n

    def fstring(self) -> str:
        """Read a bounded Unreal FString in ANSI or UTF-16 encoding."""
        n = self.num('i')
        if not n or abs(n) > MAX_TEXT:
            if not n:
                return ''
            raise IndexErrorUnsupported('Unreasonable Unreal string length')
        raw = self.take(-n * 2 if n < 0 else n)
        try:
            if n < 0:
                if raw[-2:] != b'\x00\x00':
                    raise ValueError('missing terminator')
                return raw[:-2].decode('utf-16-le', 'strict')
            if raw[-1:] != b'\x00':
                raise ValueError('missing terminator')
            return raw[:-1].decode('utf-8', 'strict')
        except (ValueError, UnicodeError) as exc:
            raise IndexErrorUnsupported('Invalid Unreal string') from exc


def resource_name(mount: str, relative: str) -> str:
    """Canonical logical path with mount point, never a host filesystem path."""
    mount = mount.replace('\\', '/')
    relative = relative.replace('\\', '/')
    # Unreal mountpoints conventionally begin with '../../../'. Remove only
    # their leading parent components, never allow traversal in the resource.
    while mount.startswith('../'):
        mount = mount[3:]
    combined = '/'.join((mount.strip('/'), relative.strip('/'))).strip('/')
    parts = combined.split('/')
    if not combined or any(p in ('', '.', '..') for p in parts) or '\x00' in combined:
        raise IndexErrorUnsupported('Unsafe or invalid resource path')
    return combined


def _checked_slice(data: bytes, start: int, length: int) -> bytes:
    if start < 0 or length < 0 or length > MAX_INDEX or start + length > len(data):
        raise IndexErrorUnsupported('Directory section outside index')
    return data[start:start + length]



def _encoded_entry_offsets(data: bytes, count: int) -> array:
    """Validate packed FPakEntry *record boundaries*, not just blob bounds.

    Directory entry references are byte offsets into this table. A reference
    in the middle of a record is not a valid resource even if it is < size.
    This reads only metadata; no compression or archive payload is touched.
    """
    reader = Reader(data)
    # Encoded blobs are capped at 256 MiB, so unsigned 32-bit offsets suffice.
    # An array uses 4 bytes/entry instead of a large Python set of ints.
    starts = array('I')
    for _ in range(count):
        starts.append(reader.pos)
        flags = reader.num('I')
        method = (flags >> 23) & 0x3f
        encrypted = bool(flags & (1 << 22))
        blocks = (flags >> 6) & 0xffff
        if method > 5:
            raise IndexErrorUnsupported('Unknown encoded PAK compression method')
        # Escape code: block-size field is serialized BEFORE file offset.
        if flags & 0x3f == 0x3f:
            reader.num('I')
        reader.num('I' if flags & (1 << 31) else 'Q')
        reader.num('I' if flags & (1 << 30) else 'Q')
        if method:
            reader.num('I' if flags & (1 << 29) else 'Q')
        if blocks and (blocks > 1 or encrypted):
            reader.take(blocks * 4)
    if reader.pos != len(data):
        raise IndexErrorUnsupported('Encoded PAK entry table size/count mismatch')
    return starts


def _pak_footer(stream, size: int):
    """Locate an exact FPakInfo versioned trailer, including legacy PAKs.

    Some version-8 writers use five method slots (221 bytes), while earlier
    engines use four (189 bytes). We validate both the version and magic,
    rather than scanning for a magic pattern inside arbitrary payload data.
    """
    candidates = ((221, 17, (8, 10, 11)), (189, 17, (8,)),
                  (222, 17, (9,)), (61, 17, (7,)),
                  (45, 1, (4, 5, 6)), (44, 0, (1, 2, 3)))
    for length, magic_at, versions in candidates:
        if size < length:
            continue
        stream.seek(size - length)
        footer = stream.read(length)
        magic, version = struct.unpack_from('<II', footer, magic_at)
        if magic != PAK_MAGIC or version not in versions:
            continue
        if magic_at and footer[magic_at - 1]:
            raise IndexErrorUnsupported('PAK directory index is encrypted')
        start, index_length = struct.unpack_from('<qq', footer, magic_at + 8)
        if (start < 0 or index_length < 0 or index_length > MAX_INDEX
                or start + index_length > size - length):
            raise IndexErrorUnsupported('Invalid PAK primary index bounds')
        return version, length, start, index_length, footer[magic_at + 24:magic_at + 44], (length == 189)
    raise IndexErrorUnsupported('Unsupported PAK footer/version')


def _legacy_pak_inventory(primary: bytes, *, version: int, index_at: int,
                          narrow_compression: bool = False) -> Inventory:
    """Read legacy v1-v9 directory records, never the resource payloads."""
    reader = Reader(primary)
    mount = reader.fstring()
    count = reader.count()
    names = []
    for _ in range(count):
        name = reader.fstring()
        offset = reader.num('q')
        compressed = reader.num('q')
        uncompressed = reader.num('q')
        method = reader.num('B' if narrow_compression else 'I')
        if method > ((4 if narrow_compression else 5) if version >= 8 else 3):
            raise IndexErrorUnsupported('Unknown legacy PAK compression method')
        if version == 1:
            reader.num('Q')  # PAK v2 removed timestamps.
        reader.take(20)  # SHA-1 of uncompressed data, not of the directory index.
        if version >= 3:
            if method:
                blocks = reader.count()
                if blocks > MAX_ENTRIES or blocks * 16 > len(primary) - reader.pos:
                    raise IndexErrorUnsupported('Invalid legacy PAK block table')
                last_end = -1
                for _ in range(blocks):
                    block_start, block_end = reader.num('q'), reader.num('q')
                    if block_start < 0 or block_end < block_start or block_start < last_end:
                        raise IndexErrorUnsupported('Invalid legacy PAK compressed block offsets')
                    last_end = block_end
            flags = reader.num('B')
            reader.num('I')  
            if flags & ~3:
                raise IndexErrorUnsupported('Unsupported legacy PAK entry flags')
            # v6+ supports tombstones; they are not actual listed resources.
            deleted = version >= 6 and bool(flags & 2)
        else:
            deleted = False
        if offset < 0 or offset >= index_at or compressed < 0 or uncompressed < 0:
            raise IndexErrorUnsupported('Legacy PAK entry lies outside resource data')
        if not deleted:
            names.append(resource_name(mount, name))
    if reader.pos != len(primary):
        raise IndexErrorUnsupported('Trailing or truncated legacy PAK index data')
    if len({name.casefold() for name in names}) != len(names):
        raise IndexErrorUnsupported('Duplicate PAK resource path')
    return Inventory(tuple(names), hashlib.sha256(primary).hexdigest(), 'PAK')


def read_pak(path: Path) -> Inventory:
    """Read legacy directory indexes and UE path-hash indexes (v1-v11)."""
    size = path.stat().st_size
    with path.open('rb') as f:
        version, footer_size, start, length, primary_sha1, narrow = _pak_footer(f, size)
        f.seek(start)
        primary = f.read(length)
        if len(primary) != length:
            raise IndexErrorUnsupported('Short PAK primary index')
        if any(primary_sha1) and hashlib.sha1(primary).digest() != primary_sha1:
            raise IndexErrorUnsupported('PAK primary index SHA-1 mismatch')
        if version < 10:
            return _legacy_pak_inventory(primary, version=version, index_at=start,
                                         narrow_compression=narrow)
        r = Reader(primary)
        mount = r.fstring()
        count = r.count()
        r.take(8)  
        has_path_hash = r.num('i')
        if has_path_hash not in (0, 1):
            raise IndexErrorUnsupported('Invalid PAK path-hash flag')
        path_hash_descriptor = None
        if has_path_hash:
            path_hash_descriptor = (r.num('q'), r.num('q'), r.take(20))
        has_directory = r.num('i')
        if has_directory not in (0, 1):
            raise IndexErrorUnsupported('Invalid PAK directory flag')
        if not has_directory:
            raise IndexErrorUnsupported('PAK has no directory index')
        directory_offset, directory_size = r.num('q'), r.num('q')
        directory_sha1 = r.take(20)  
        # EncodedPakEntriesSize is a byte length, not an entry count. Real
        # large PAKs can legitimately exceed MAX_ENTRIES bytes here.
        encoded_count = r.num('i')
        if encoded_count < 0 or encoded_count > MAX_INDEX:
            raise IndexErrorUnsupported('Invalid encoded PAK entry size')
        encoded = r.take(encoded_count)
        encoded_starts = _encoded_entry_offsets(encoded, count)
        # The primary v10/v11 index ends with the non-encoded entry count.
        # Ignoring it makes unsupported entry offsets look like valid paths.
        non_encoded_count = r.count()
        if non_encoded_count:
            raise IndexErrorUnsupported('Non-encoded PAK entries are unsupported')
        if r.pos != len(primary):
            raise IndexErrorUnsupported('Unexpected trailing PAK primary index data')
        if path_hash_descriptor is not None:
            hash_offset, hash_size, hash_sha1 = path_hash_descriptor
            if (hash_offset < 0 or hash_size < 0 or hash_size > MAX_INDEX
                    or hash_offset + hash_size > size - footer_size):
                raise IndexErrorUnsupported('Invalid PAK path-hash index bounds')
            f.seek(hash_offset)
            path_hash_bytes = f.read(hash_size)
            if len(path_hash_bytes) != hash_size:
                raise IndexErrorUnsupported('Short PAK path-hash index')
            if any(hash_sha1) and hashlib.sha1(path_hash_bytes).digest() != hash_sha1:
                raise IndexErrorUnsupported('PAK path-hash index SHA-1 mismatch')
        if directory_offset < 0 or directory_size < 0 or directory_size > MAX_INDEX or directory_offset + directory_size > size - footer_size:
            raise IndexErrorUnsupported('Invalid PAK directory bounds')
        f.seek(directory_offset)
        directory = f.read(directory_size)
        if len(directory) != directory_size:
            raise IndexErrorUnsupported('Short PAK directory')
        if any(directory_sha1) and hashlib.sha1(directory).digest() != directory_sha1:
            raise IndexErrorUnsupported('PAK directory index SHA-1 mismatch')
    r = Reader(directory)
    names = []
    folders = r.count()
    for _ in range(folders):
        folder = r.fstring()
        for _ in range(r.count()):
            name = r.fstring()
            entry_location = r.num('i')
            # Negative offsets refer to the separately serialized FPakEntry
            # table, which this metadata-only reader deliberately omits.
            if entry_location < 0 or entry_location >= encoded_count:
                raise IndexErrorUnsupported('Invalid or non-encoded PAK entry reference')
            k = bisect_left(encoded_starts, entry_location)
            if k == len(encoded_starts) or encoded_starts[k] != entry_location:
                raise IndexErrorUnsupported('PAK entry reference is not a record boundary')
            names.append(resource_name(mount, folder + name))
            if len(names) > MAX_ENTRIES:
                raise IndexErrorUnsupported('Too many PAK resources')
    if r.pos != len(directory) or len(names) != count:
        raise IndexErrorUnsupported('Incomplete PAK directory or inconsistent file count')
    if len({p.casefold() for p in names}) != len(names):
        raise IndexErrorUnsupported('Duplicate PAK resource path')
    digest = hashlib.sha256()
    digest.update(primary)
    digest.update(directory)
    fingerprint = digest.hexdigest()
    return Inventory(tuple(names), fingerprint, 'PAK')


def read_utoc(path: Path) -> Inventory:
    """Read IoStore TOC directory tree; UCAS payloads are never opened."""
    size = path.stat().st_size
    if size > MAX_INDEX:
        raise IndexErrorUnsupported('UTOC index exceeds size limit')
    # stat() can race with growth of an external file; enforce the cap on
    # bytes actually read rather than trusting the earlier size alone.
    with path.open('rb') as stream:
        data = stream.read(MAX_INDEX + 1)
    if len(data) > MAX_INDEX:
        raise IndexErrorUnsupported('UTOC index exceeds size limit')
    if len(data) < 144 or data[:16] != UTOC_MAGIC:
        raise IndexErrorUnsupported('Invalid IoStore TOC header')
    # Compute optional sections from their declared dimensions. Do not
    # interpret a signature or OnDemand trailer as a directory buffer.
    version = data[16]
    flags = data[80]
    if version < 2 or version > 9:
        raise IndexErrorUnsupported('Unsupported IoStore TOC version')
    if flags & ~0x1f:
        raise IndexErrorUnsupported('Unknown IoStore TOC flags')
    if flags & 16 and version < 6:
        raise IndexErrorUnsupported('On-demand IoStore flag incompatible with TOC version')
    header_size, entries, blocks, block_entry_size, method_count, method_len, _, directory_size, _ = struct.unpack_from('<9I', data, 20)
    if header_size < 144 or header_size > len(data) or entries > MAX_ENTRIES or blocks > MAX_ENTRIES:
        raise IndexErrorUnsupported('Invalid IoStore header dimensions')
    if block_entry_size != 12 or method_count > 128 or method_len > 128:
        raise IndexErrorUnsupported('Unknown IoStore block/method format')
    if not (flags & 8) or not directory_size:
        raise IndexErrorUnsupported('IoStore has no directory index')
    if flags & 2:
        raise IndexErrorUnsupported('IoStore index is encrypted')
    seed_count = struct.unpack_from('<I', data, 84)[0]
    nohash_count = struct.unpack_from('<I', data, 96)[0]
    if seed_count > MAX_ENTRIES or nohash_count > MAX_ENTRIES:
        raise IndexErrorUnsupported('Invalid IoStore perfect-hash counts')
    # These sections were added in TOC v4/v5. Earlier layouts must not
    # silently be interpreted with an impossible trailing hash table.
    if (version < 4 and seed_count) or (version < 5 and nohash_count):
        raise IndexErrorUnsupported('IoStore hash table is incompatible with TOC version')
    directory_at = (header_size + entries * 22 + seed_count * 4
                    + nohash_count * 4 + blocks * 12 + method_count * method_len)
    if flags & 4:  # Signed: 2 RSA signatures and one SHA-1 per compressed block.
        # A signed TOC remains listable without a private key. We validate its
        # structural boundaries, NOT its cryptographic authenticity.
        if directory_at + 4 > len(data):
            raise IndexErrorUnsupported('Truncated IoStore signature header')
        signature_size = struct.unpack_from('<i', data, directory_at)[0]
        if not 1 <= signature_size <= 8192:
            raise IndexErrorUnsupported('Invalid IoStore signature length')
        signature_section = 4 + signature_size * 2 + blocks * 20
        if directory_at + signature_section > len(data):
            raise IndexErrorUnsupported('Truncated IoStore signatures')
        directory_at += signature_section
    directory = _checked_slice(data, directory_at, directory_size)
    # Chunk metas follow the directory index. They are present even though
    # this reader never opens chunk payloads. Truncated TOCs are not complete
    # inventories. UE replaces the 21-byte meta with a padded 24-byte one in v8.
    meta_bytes = entries * (24 if version >= 8 else 21)
    meta_end = directory_at + directory_size + meta_bytes
    if meta_end > len(data):
        raise IndexErrorUnsupported('Truncated IoStore chunk metadata')
    # v6 OnDemandMetadata appended SHA-1 values for chunks and blocks. The
    # extra records were removed in v7; they do not affect the file names.
    if flags & 16 and version == 6 and meta_end + (entries + blocks) * 20 > len(data):
        raise IndexErrorUnsupported('Truncated IoStore on-demand metadata')
    r = Reader(directory)
    mount = r.fstring()
    directories = [tuple(r.num('I') for _ in range(4)) for _ in range(r.count())]
    files = [tuple(r.num('I') for _ in range(3)) for _ in range(r.count())]
    strings = [r.fstring() for _ in range(r.count())]
    if r.pos != len(directory):
        raise IndexErrorUnsupported('IoStore directory size mismatch')
    def named(i: int) -> str:
        if i >= len(strings):
            raise IndexErrorUnsupported('IoStore invalid string reference')
        return strings[i]
    result = []
    seen_dirs, seen_files = set(), set()
    def walk(first: int, parent: str, depth: int):
        if depth > MAX_DEPTH:
            raise IndexErrorUnsupported('IoStore directory nesting too deep')
        current = first
        while current != NONE:
            if current >= len(directories) or current in seen_dirs:
                raise IndexErrorUnsupported('IoStore invalid/cyclic directory tree')
            seen_dirs.add(current)
            name_id, child, sibling, file_id = directories[current]
            name = named(name_id) if name_id != NONE else ''
            here = '/'.join(p for p in (parent, name) if p)
            while file_id != NONE:
                if file_id >= len(files) or file_id in seen_files:
                    raise IndexErrorUnsupported('IoStore invalid/cyclic file chain')
                seen_files.add(file_id)
                file_name, following, chunk_id = files[file_id]
                if chunk_id >= entries:
                    raise IndexErrorUnsupported('IoStore invalid chunk reference')
                result.append(resource_name(mount, '/'.join(p for p in (here, named(file_name)) if p)))
                file_id = following
            if child != NONE:
                walk(child, here, depth + 1)
            current = sibling
    if directories:
        walk(0, '', 0)
    if len(seen_dirs) != len(directories) or len(seen_files) != len(files):
        raise IndexErrorUnsupported('Unreachable IoStore directory entries')
    if len({p.casefold() for p in result}) != len(result):
        raise IndexErrorUnsupported('Duplicate IoStore resource path')
    return Inventory(tuple(result), hashlib.sha256(directory).hexdigest(), 'IoStore')


def read_inventory(path: Path) -> Inventory:
    """Dispatch to the correct read-only reader for a container format."""
    if path.suffix.casefold() == '.pak':
        return read_pak(path)
    if path.suffix.casefold() == '.utoc':
        return read_utoc(path)
    raise IndexErrorUnsupported('Not a PAK/UTOC index file')
