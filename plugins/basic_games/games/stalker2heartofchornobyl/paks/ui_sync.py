"""Select owning MO2 mods through its existing Qt model, never by row number.

MO2's left view is a filtered, sortable, potentially grouped QTreeView.
The public IModList API has no select/highlight method, and native
ModListView::setHighlightedMods requires a C++ std::set<QString> metatype.
Instead navigate the visible view model with standard Qt selection APIs.
Do not alter filters, mod states, order or widget focus.
"""
from __future__ import annotations


def select_visible_mods(view, names, qt, selection_flags) -> int:
    """Select visible mod-name rows, return count; don't clear on no matches.

    `qt` and `selection_flags` are the Qt and QItemSelectionModel enums. This
    small adapter is headlessly testable and needs no dependencies on MO2.
    """
    target = {str(name).casefold() for name in names if name}
    if not target or view is None:
        return 0
    model = view.model()
    selection = view.selectionModel()
    if model is None or selection is None:
        return 0
    root = model.index(0, 0).parent()  # An invalid parent works even with nested proxy models.
    stack = [(root, 0)]
    found = []
    # All model indexes here are VIEW indexes: no priority/row assumptions.
    while stack and target:
        parent, row = stack.pop()
        if row >= model.rowCount(parent):
            continue
        stack.append((parent, row + 1))
        index = model.index(row, 0, parent)
        if not index.isValid():
            continue
        name = model.data(index, qt.ItemDataRole.DisplayRole)
        if name is not None and str(name).casefold() in target:
            # Compare Qt DisplayRole in the already-filtered/grouped view.
            found.append(index)
            target.discard(str(name).casefold())
        if model.hasChildren(index):
            stack.append((index, 0))
    if not found:
        return 0  # Preserve selection when MO2 filters hide all target mods.
    clear_select = (selection_flags.SelectionFlag.Clear |
                    selection_flags.SelectionFlag.Select |
                    selection_flags.SelectionFlag.Rows)
    add_select = (selection_flags.SelectionFlag.Select |
                  selection_flags.SelectionFlag.Rows)
    for i, index in enumerate(found):
        ancestor = index.parent()
        while ancestor.isValid():
            expand = getattr(view, 'expand', None)
            if callable(expand):
                expand(ancestor)
            ancestor = ancestor.parent()
        selection.select(index, clear_select if i == 0 else add_select)
    # Only change the current row and viewport; keep keyboard focus in Archives.
    selection.setCurrentIndex(found[0], selection_flags.SelectionFlag.NoUpdate)
    view.scrollTo(found[0])
    return len(found)


def selected_visible_mods(view, display_to_internal, qt) -> set[str]:
    """Translate selected visible MO2 rows to unambiguous internal mod names.

    The input is a mapping of case-folded MO2 display names to internal names.
    Separators, generated group headers and ambiguous display names are ignored.
    Read from the *view* model, not an underlying source model or priority index.
    """
    if view is None:
        return set()
    model, selection = view.model(), view.selectionModel()
    if model is None or selection is None:
        return set()
    result = set()
    for index in selection.selectedRows(0):
        if not index.isValid():
            continue
        display = model.data(index, qt.ItemDataRole.DisplayRole)
        if display is None:
            continue
        internal = display_to_internal.get(str(display).casefold())
        if internal is not None:
            result.add(internal)
    return result
