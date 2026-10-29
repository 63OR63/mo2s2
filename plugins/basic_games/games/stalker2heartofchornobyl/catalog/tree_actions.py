"""Bounded, iterative tree operations shared by Contents and its tests.

No recursion: deeply nested mod paths should not overflow Python's stack.
"""
from __future__ import annotations


def set_branch_expanded(root, expanded: bool) -> int:
    """Expand/collapse a branch and all descendants, including root.

    Works with QTreeWidgetItem, without dependence on Qt types.
    Caller should block the tree's signals while applying the change.
    """
    stack = [root]
    total = 0
    while stack:
        item = stack.pop()
        item.setExpanded(expanded)
        total += 1
        for index in range(item.childCount()):
            stack.append(item.child(index))
    return total


def navigation_targets(owners, winner, section):
    """Mod names for a conflict navigation action, without changing load order."""
    owners = tuple(dict.fromkeys(owners))
    if section == 'winner':
        return (winner,) if winner in owners else ()
    if section == 'losers':
        return tuple(owner for owner in owners if owner != winner) if winner in owners else ()
    if section == 'owners':
        return owners
    return ()
