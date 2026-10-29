"""Pure, read-only selection actions for Archives context menus.

The visible UI table is a presentation layer. These helpers consume Item/Link
objects from the actual mapping preview, never parse a cell's displayed text.
"""
from __future__ import annotations

from pathlib import Path


def selected_packages(items_by_key, keys):
    """Deduplicate package selections while preserving stable source order."""
    return tuple(items_by_key[key] for key in dict.fromkeys(keys)
                 if key in items_by_key)


def source_paths(items):
    """Return real package sources (files or directory roots), without opening them."""
    return tuple(sorted({str(item.source) for item in items}, key=str.casefold))


def virtual_paths(items, links_by_key):
    """Use the SAME destinations returned by FileMapper's mapping_preview."""
    return tuple(sorted({str(link.destination)
                         for item in items for link in links_by_key.get(item.key, ())},
                        key=str.casefold))


def source_folder(item):
    """Return a folder for File Explorer, refusing paths escaping an MO2 mod.

    No filesystem changes and no symlink traversal; the underlying package
    discovery checks reparse points before mapping and GUI must not bypass it.
    """
    from .virtual_core import _assert_mod_subpath_safe, _is_reparse

    root = item.mod_root
    if root is None:
        raise ValueError('No MO2 source directory recorded for this package')
    source = Path(item.source)
    _assert_mod_subpath_safe(Path(root), source)
    if not source.exists() or _is_reparse(source):
        raise ValueError('Package source is missing or linked')
    folder = source if source.is_dir() else source.parent
    _assert_mod_subpath_safe(Path(root), folder)
    return folder


def matches_archive_search(item, links_by_key, query):
    """Filter by mod, physical source or mapped virtual paths, not table cells.

    The path column is intentionally hidden from Archives because the
    selection detail and mapping report already expose virtual destinations.
    Filtering must continue to work for every alias of a Zone Kit package.
    """
    query = query.casefold().strip()
    return (not query or query in item.mod_name.casefold() or
            query in str(item.source).casefold() or
            any(query in str(link.destination).casefold()
                for link in links_by_key.get(item.key, ())))
