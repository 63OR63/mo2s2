"""Read-only human-readable prelaunch view of the actual FileMapper preview.

No assumptions about PAK internal contents or guaranteed Unreal mount order.
The game may change files between this preview and the next MO2 launch.
"""
from __future__ import annotations

from .virtual_core import package_state


def format_launch_plan(items, links, warnings, issues=()) -> str:
    """Summarize physical sources, virtual destinations and planner warnings."""
    by_key = {}
    for link in links:
        by_key.setdefault(link.item_key, []).append(link)
    mapped = set(by_key)
    lines = [
        'mo2s2 — STALKER 2 launch plan (read-only preview)',
        f'Active packages: {len(items)}  |  Virtual mappings: {len(links)}',
        f'Mapped packages: {sum(package_state(i, mapped) == "MAPPED" for i in items)}  |  '
        f'Not mapped: {sum(package_state(i, mapped) != "MAPPED" for i in items)}',
        '',
        'MO2 priority determines the numeric _N_P class. Higher priorities receive higher classes.',
        'This is a predicted VFS file plan, not a claim about Unreal asset-level precedence.',
        '',
    ]
    for item in sorted(items, key=lambda i: (i.priority, i.mod_name.casefold(), i.key)):
        aliases = by_key.get(item.key, ())
        rank = f'_{aliases[0].number}_P' if aliases else '—'
        state = package_state(item, mapped)
        lines.append(f'[{item.priority:>5}] [{rank:>9}] {state:<10} {item.mod_name}  ({item.kind})')
        lines.append(f'        Source: {item.source}')
        lines.append(f'        {len(aliases)} virtual file alias(es)')
        for link in aliases[:3]:
            lines.append(f'        => {link.destination}')
        if len(aliases) > 3:
            lines.append(f'        ... {len(aliases) - 3} more (see Copy mapping report)')
        if item.warning:
            lines.append(f'        Problem: {item.warning}')
    if warnings:
        lines.extend(['', f'FileMapper warnings ({len(warnings)}):'])
        lines.extend(f'  - {warning}' for warning in warnings)
    if issues:
        lines.extend(['', f'Prelaunch issues ({len(issues)}):'])
        lines.extend(f'  - {issue}' for issue in issues)
    elif warnings:
        lines.extend(['', 'Prelaunch checks: see FileMapper warnings above.'])
    else:
        lines.extend(['', 'Prelaunch checks: no detected issues.'])
    lines.extend(['', 'Recomputed on each game launch; files and profiles may change after preview.',
                  'For exact source => destination pairs use Archives → Tools → Copy mapping report.'])
    return '\n'.join(lines) + '\n'
