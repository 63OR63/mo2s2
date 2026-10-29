"""Read-only launch diagnostics for active MO2 packages.

No Qt, mobase, SQLite or filesystem mutations. Suppressing the dialog must
never suppress diagnostics or MO2 log entries.
"""
from __future__ import annotations
from .virtual_core import package_state


def launch_issues(items, links, warnings, *, pending=(), transactions=(),
                  install_errors=(), mapping_error='', anchor_ok=True):
    """Return ordered, deduplicated launch warnings for active packages."""
    mapped = {link.item_key for link in links}
    issues = []
    for item in items:
        state = package_state(item, mapped)
        if state not in ('MAPPED',):
            issues.append(f'{item.mod_name}: {state} — {item.warning or item.source}')
        elif item.warning:
            issues.append(f'{item.mod_name}: {item.warning}')
    issues.extend(f'Mapping: {warning}' for warning in warnings)
    issues.extend(f'Pending install: {name}' for name in pending)
    issues.extend(f'Interrupted transaction: {name}' for name in transactions)
    issues.extend(f'Installation: {error}' for error in install_errors)
    if mapping_error:
        issues.append('Last FileMapper error: ' + mapping_error)
    if not anchor_ok:
        issues.append('Virtual filesystem anchor is unavailable')
    # Preserve order but don't show duplicate errors (e.g. status + warning).
    return list(dict.fromkeys(issues))
