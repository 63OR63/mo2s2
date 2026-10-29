"""Content icons and type detection in the MO2 left pane.

Only recognizes loose filename/path conventions. Internal PAK assets (including
CFG {bpatch}) cannot be identified without unpacking the archives themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ContentType:
    """Identify recognized container file types for MO2 content badges."""
    id: int
    title: str
    icon: str


TYPES = (
    ContentType(201, 'mo2s2: Classic PAK package', 'classic'),
    ContentType(202, 'mo2s2: Zone Kit / nested package', 'zonekit'),
    ContentType(203, 'mo2s2: .pak', 'pak'),
    ContentType(204, 'mo2s2: .utoc', 'utoc'),
    ContentType(205, 'mo2s2: .ucas', 'ucas'),
    ContentType(206, 'mo2s2: Unreal loose assets', ':/MO/gui/content/mesh'),
    ContentType(207, 'mo2s2: Config / CFG / INI', ':/MO/gui/content/inifile'),
    ContentType(208, 'mo2s2: Scripts / UE4SS', ':/MO/gui/content/script'),
    ContentType(209, 'mo2s2: DLL / binaries', ':/MO/gui/content/skse'),
    ContentType(210, 'mo2s2: Localization', ':/MO/gui/content/string'),
    ContentType(211, 'mo2s2: Shader files', ':/MO/gui/content/texture'),
    ContentType(212, 'mo2s2: IoStore-only (review mount)', 'utoc'),
    ContentType(213, 'mo2s2: Manifest file', ':/MO/gui/content/inifile'),
    ContentType(214, 'mo2s2: Audio assets', ':/MO/gui/content/sound'),
    ContentType(215, 'mo2s2: Texture / image assets', ':/MO/gui/content/texture'),
    ContentType(216, 'mo2s2: PAK signature (.sig)', 'pak'),
)


def classify_paths(paths) -> list[int]:
    """Accept relative paths collected by IFileTree.walk()."""
    paths = [p.replace('\\', '/').lower().lstrip('/') for p in paths]
    extensions = {Path(p).suffix for p in paths}
    result = set()
    has_pak = '.pak' in extensions
    has_sig = '.sig' in extensions
    has_utoc = '.utoc' in extensions
    has_ucas = '.ucas' in extensions
    has_zone = any('/windows/newcontent/windows/' in '/' + p or
                   '/windows/overridecontent/windows/' in '/' + p for p in paths)
    has_nested = any('/~mods/' in '/' + p and p.count('/') >= 5 for p in paths)
    if has_zone or (has_nested and has_pak):
        result.add(202)
    elif has_pak or has_utoc or has_ucas:
        result.add(201)
    for flag, id_ in ((has_pak, 203), (has_utoc, 204), (has_ucas, 205)):
        if flag:
            result.add(id_)
    if has_sig:
        result.add(216)
    if (has_utoc or has_ucas) and not has_pak:
        result.add(212)
    if extensions & {'.uasset', '.uexp', '.ubulk', '.umap', '.usmap'}:
        result.add(206)
    if extensions & {'.cfg', '.ini', '.json', '.toml', '.yaml'}:
        result.add(207)
    if extensions & {'.lua', '.js', '.py'} or any('ue4ss' in p or '/logicmods/' in '/' + p for p in paths):
        result.add(208)
    if extensions & {'.dll', '.asi', '.exe'}:
        result.add(209)
    if extensions & {'.locres', '.po', '.pot'}:
        result.add(210)
    if extensions & {'.usf', '.ush', '.fx', '.fxh', '.hlsl', '.ushaderbytecode'}:
        result.add(211)
    if any(Path(p).name.startswith('manifest_ufsfiles_') for p in paths):
        result.add(213)
    if extensions & {'.wem', '.bnk', '.wav', '.ogg', '.mp3', '.flac'}:
        result.add(214)
    if extensions & {'.dds', '.png', '.jpg', '.jpeg', '.tga', '.tif', '.tiff'}:
        result.add(215)
    return sorted(result)
