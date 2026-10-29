"""MO2 game support and virtual container mapping for S.T.A.L.K.E.R. 2."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import mobase
from PyQt6.QtCore import QDir, QTimer
from PyQt6.QtWidgets import QMainWindow, QTabWidget, QMessageBox, QCheckBox, QApplication

from ..basic_game import BasicGame
from ..basic_features import BasicLocalSavegames, BasicModDataChecker, GlobPatterns
from .stalker2heartofchornobyl.paks.virtual_core import (
    Owner, scan, build_links, STORE, stage_new_install, _group_files, _group_warning,
    prune_previous_session_anchors, pending_install_dirs, windows_component_problem,
    transaction_dirs, inspect_transaction, recover_transaction, KNOWN_BASES,
    ROOT_RESERVED, WRAPPER_DOCUMENTS, WRAPPER_STANDARD_ROOTS,
    cosmetic_wrapper_payload, EXTENSIONS, is_container_filename, root_package_kind,
)
from .stalker2heartofchornobyl.paks.runtime import PreparationQueue, is_game_executable, verbosity_level
from .stalker2heartofchornobyl.paks.launch_checks import launch_issues
from .stalker2heartofchornobyl.paks.plan import format_launch_plan
from .stalker2heartofchornobyl.paks.diagnostics import (
    Diagnostic, repair_plan, assert_unchanged_plan, external_packages, package_diagnostics,
)


from .stalker2heartofchornobyl.mod_content import TYPES, classify_paths


class Mo2S2ModDataContent(mobase.ModDataContent):
    """Native MO2 content badges, just like the Bethesda game integration."""

    def __init__(self, icon_dir: Path):
        super().__init__()
        self._icons = icon_dir

    def getAllContents(self):
        return [mobase.ModDataContent.Content(
            t.id, t.title,
            t.icon if t.icon.startswith(':/') else str(self._icons / (t.icon + '.png'))
        ) for t in TYPES]

    def getContentsFor(self, filetree):
        paths = []

        def on_entry(parent, entry):
            if entry.isFile():
                paths.append(parent + entry.name())
            return mobase.IFileTree.WalkReturn.CONTINUE

        filetree.walk(on_entry, '/')
        return classify_paths(paths)


class Mo2S2ModDataChecker(BasicModDataChecker):
    """Skyrim-style installer normalization via the MO2 IFileTree interface.

    Report recognized safe layouts as VALID, including redundant game roots.
    MO2's manual installer rejects FIXABLE without invoking fix(). Both
    automatic and manual paths are normalized by the same post-install
    transaction in onModInstalled, with Auto prepare as a missed-callback
    fallback. Other loose game files retain their intended paths.
    """

    def __init__(self):
        super().__init__(GlobPatterns(valid=[STORE, 'Content', 'Paks', '~mods',
                                                'Stalker2', 'Engine', 'Binaries',
                                                'LogicMods', 'Mods', 'Config']))

    @staticmethod
    def _directory_tree(node):
        """Return a directory as an IFileTree, regardless of how MO2 exposed it.

        In mobase, IFileTree.find("dir") can return an IFileTree *already*;
        directory FileTreeEntry objects returned by iteration expose astree().
        Calling astree() on an already resolved IFileTree crashes on MO2 2.5.x.
        Do not use truthiness: an empty IFileTree is a valid directory.
        """
        if node is None or not node.isDir():
            return None
        if callable(getattr(node, 'find', None)) and callable(getattr(node, 'exists', None)) \
                and hasattr(node, '__iter__'):
            return node
        to_tree = getattr(node, 'astree', None)
        if callable(to_tree):
            result = to_tree()
            if result is not None:
                return result
        raise TypeError('MO2 directory entry cannot be viewed as IFileTree')

    @staticmethod
    def _single_wrapper(tree):
        """A single cosmetic wrapper around actual game-data roots.

        Unlike Zone Kit's <ModName>/Windows/... tree, this wrapper has
        Content/Paks, ~mods, etc directly inside it. It is safe to unwrap.
        """
        entries = [e for e in tree if e.isFile() or e.isDir()]
        folders = [e for e in entries if e.isDir()]
        # Ignore only common ancillary root files (never another directory).
        if len(folders) != 1 or any(e.isFile() and
                                      e.name().casefold() not in
                                      WRAPPER_DOCUMENTS
                                      for e in entries):
            return None
        folder = folders[0]
        child = Mo2S2ModDataChecker._directory_tree(folder)
        standard = any(child.exists(path) for path in WRAPPER_STANDARD_ROOTS)
        # A Zone Kit package must actually contain a container, not just
        # have a Windows/NewContent folder. Match the filesystem adapter.
        def zonekit_package(entry):
            if not entry.isDir():
                return False
            subtree = Mo2S2ModDataChecker._directory_tree(entry)
            for path in ('Windows/NewContent/Windows', 'Windows/OverrideContent/Windows'):
                branch = subtree.find(path)
                if branch is not None and branch.isDir():
                    nested = Mo2S2ModDataChecker._directory_tree(branch)
                    if Mo2S2ModDataChecker._contains_container(nested):
                        return True
            return False

        children = tuple((entry.name(), entry.isDir(), zonekit_package(entry))
                         for entry in child)
        if cosmetic_wrapper_payload(folder.name(), standard_root=standard,
                                    children=children):
            return folder
        return None

    @staticmethod
    def _contains_container(subtree):
        """Detect nested Zone Kit/PAK trees in installer IFileTree safely."""
        folders = [subtree]
        while folders:
            folder = folders.pop()
            for entry in folder:
                if entry.isFile() and is_container_filename(entry.name()):
                    return True
                if entry.isDir():
                    folders.append(Mo2S2ModDataChecker._directory_tree(entry))
        return False

    @staticmethod
    def _has_direct_container(tree):
        """True for a shallow folder of PAK/IoStore files, such as ZoneMinimap/.

        A folder containing a PAK *many levels down* isn't sufficient: UE
        projects and third-party tools may have incidental archive assets.
        """
        return any(entry.isFile() and is_container_filename(entry.name()) for entry in tree)

    def _candidate_paths(self, tree):
        """Find non-overlapping mod containers that can be staged after extraction."""
        # Copy the immediate children of any recognised ~mods root into one
        # private storage directory. Moving the entire ~mods directory would
        # prevent a mixed ZIP with ~mods/A and Content/Paks/~mods/B from being
        # merged automatically even though A and B are distinct packages.
        # Entire Zone Kit folders remain single children: their inner
        # Windows/NewContent and OverrideContent trees are never flattened.
        direct_roots = tuple(str(p).replace('\\', '/') for p in KNOWN_BASES[:4])
        sources = []
        for root in direct_roots:
            subtree = tree.find(root)
            if subtree is not None and subtree.isDir():
                for child in self._directory_tree(subtree):
                    # Do not capture unrelated loose files inside ~mods;
                    # only managed containers and folders containing them
                    # belong in the private FileMapper source directory.
                    is_container = child.isFile() and is_container_filename(child.name())
                    is_package = child.isDir() and self._contains_container(self._directory_tree(child))
                    if is_container or is_package:
                        sources.append((root + '/' + child.name(),
                                        f'{STORE}/Content/Paks/~mods'))
        for prefix in (*[str(p).replace('\\', '/') for p in KNOWN_BASES[4:]], ''):
            parent = tree.find(prefix) if prefix else tree
            if parent is None or not hasattr(parent, '__iter__'):
                continue
            for entry in parent:
                name = entry.name()
                rel = f'{prefix}/{name}' if prefix else name
                if entry.isFile() and is_container_filename(name):
                    sources.append((rel, f'{STORE}/Content/Paks/~mods'))
                elif prefix == '' and entry.isDir() and name.casefold() not in ROOT_RESERVED:
                    # Root-level Zone Kit package or shallow container wrapper.
                    child = self._directory_tree(entry)
                    if child and root_package_kind(
                            zonekit=(child.exists('Windows/NewContent') or
                                     child.exists('Windows/OverrideContent')),
                            direct_container=self._has_direct_container(child)):
                        # No MO2 Data Directory selection is necessary for a
                        # download wrapper with direct PAK+IoStore bundles.
                        # Keep the source folder intact and normalize virtually.
                        sources.append((rel, f'{STORE}/Content/Paks/~mods'))
        # No overlapping source paths; avoid moving individual entries when
        # the containing ~mods directory will be moved in a single operation.
        picked = []
        for src, dst in sorted(sources, key=lambda pair: (pair[0].count('/'), pair[0].casefold())):
            if not any(src.casefold() == existing.casefold() or
                       src.casefold().startswith(existing.casefold() + '/') for existing, _ in picked):
                picked.append((src, dst))
        return picked

    @staticmethod
    def _staging_conflict(tree, candidates):
        """Check all moves before mutating the installer IFileTree.

        VALID means that the common post-install transaction can succeed.
        Two supported roots can contain the same destination (notably `~mods`
        and `Content/Paks/~mods`), so reject ambiguous layouts upfront.
        """
        destinations = set()
        sources = {src.casefold() for src, _ in candidates}
        for src, dest in candidates:
            node = tree.find(src)
            if node is None:
                return f'missing source: {src}'
            target = (dest.rstrip('/') + '/' + node.name()).casefold()
            if target in destinations:
                return f'duplicate installation target: {target}'
            destinations.add(target)
            if target not in sources and tree.exists(dest.rstrip('/') + '/' + node.name()):
                return f'occupied installation target: {target}'
        return ''

    @staticmethod
    def _staged_package_problem(tree, candidates):
        """Inspect managed container bundles *before* MO2 extracts the archive.

        A VALID result must not hide a broken IoStore pair that later fails
        during onModInstalled and leaves live originals in ~mods. Inspect only
        paths selected for staging; unrelated tools and loose files are not
        treated as PAK packages.
        """
        grouped_by_directory = {}
        casefold_paths = set()

        def visit(node, relative):
            # VALID must not accept names rejected later by the real Windows
            # staging planner. Check every path part of each managed source,
            # not unrelated loose files in a mixed mod.
            for component in Path(relative).parts:
                problem = windows_component_problem(component)
                if problem:
                    raise ValueError(f'Unsupported Windows path component {component!r}: {problem}')
            if node.isFile():
                folded = relative.casefold()
                if folded in casefold_paths:
                    raise ValueError(f'Windows case-insensitive path collision: {relative}')
                casefold_paths.add(folded)
                if is_container_filename(relative):
                    grouped_by_directory.setdefault(str(Path(relative).parent).casefold(), []).append(Path(relative))
            elif node.isDir():
                # Avoid following a source tree multiple times; descendants
                # are handled only when their owning directory is visited.
                children = list(Mo2S2ModDataChecker._directory_tree(node))
                names = set()
                for child in children:
                    folded = child.name().casefold()
                    if folded in names:
                        raise ValueError(f'Windows case-insensitive directory collision: {relative}')
                    names.add(folded)
                    visit(child, relative + '/' + child.name())

        try:
            for relative, _ in candidates:
                node = tree.find(relative)
                if node is None:
                    return f'Missing installation input: {relative}'
                visit(node, relative)
        except ValueError as exc:
            return str(exc)
        for paths in grouped_by_directory.values():
            for members in _group_files(paths).values():
                issue = _group_warning(members)
                if issue:
                    return f'{issue}: {members[0]}'
        return ''

    def dataLooksValid(self, tree):
        """Accept supported package layouts for MO2 manual and automatic installs."""
        # MO2's manual installer needs VALID, not FIXABLE: it will not call
        # fix() before extraction. The shared post-install transaction is
        # responsible for stripping Stalker2 and staging the containers.
        wrapper = self._single_wrapper(tree)
        if wrapper is not None:
            subtree = self._directory_tree(wrapper)
            if any(tree.exists(entry.name()) for entry in subtree):
                return mobase.ModDataChecker.INVALID
            candidates = self._candidate_paths(subtree)
            if (self._staging_conflict(subtree, candidates) or
                    self._staged_package_problem(subtree, candidates)):
                return mobase.ModDataChecker.INVALID
            return mobase.ModDataChecker.VALID
        if tree.exists('Stalker2/Content') or tree.exists('Stalker2/Binaries'):
            game_wrapper = tree.find('Stalker2')
            if game_wrapper is None:
                return mobase.ModDataChecker.INVALID
            subtree = self._directory_tree(game_wrapper)
            if any(tree.exists(entry.name()) for entry in subtree):
                return mobase.ModDataChecker.INVALID
            candidates = self._candidate_paths(subtree)
            if (self._staging_conflict(subtree, candidates) or
                    self._staged_package_problem(subtree, candidates)):
                return mobase.ModDataChecker.INVALID
            return mobase.ModDataChecker.VALID
        candidates = self._candidate_paths(tree)
        if candidates:
            if (self._staging_conflict(tree, candidates) or
                    self._staged_package_problem(tree, candidates)):
                return mobase.ModDataChecker.INVALID
            return mobase.ModDataChecker.VALID
        return super().dataLooksValid(tree)

    @staticmethod
    def _report_error(message):
        """Report failures even when the installer checker runs without a game instance."""
        try:
            from PyQt6.QtCore import qCritical
            qCritical('[mo2s2] [ERROR] ' + message)
        except (ImportError, AttributeError, RuntimeError, TypeError):
            print('[mo2s2] [ERROR] ' + message, file=sys.stderr)

    def fix(self, tree):
        """Apply the IFileTree fix as a reversible transaction.

        MO2's in-memory installer tree is mutable: returning None after two
        successful moves used to leave a partially modified archive preview.
        Reverse completed moves before reporting failure, including unwrapping.
        No already-installed mod files are touched by this method.
        """
        completed = []

        def move(node, destination):
            old_path = node.path('/').strip('/') if callable(getattr(node, 'path', None)) else str(node.path).strip('/')
            dest_path = destination.path('/').strip('/') if callable(getattr(destination, 'path', None)) else str(destination.path).strip('/')
            new_path = (dest_path + '/' + node.name()).strip('/')
            if not node.moveTo(destination):
                raise RuntimeError(f'IFileTree refused move: {old_path} -> {new_path}')
            completed.append((old_path, new_path))

        try:
            wrapper_dir = self._single_wrapper(tree)
            if wrapper_dir is not None:
                for entry in list(self._directory_tree(wrapper_dir)):
                    if tree.exists(entry.name()):
                        raise ValueError(f'Wrapper destination occupied: {entry.name()}')
                    move(entry, tree)
            if tree.exists('Stalker2/Content') or tree.exists('Stalker2/Binaries'):
                wrapper = tree.find('Stalker2')
                if wrapper is None:
                    raise ValueError('Cannot find Stalker2 game-root wrapper')
                for entry in list(self._directory_tree(wrapper)):
                    if tree.exists(entry.name()):
                        raise ValueError(f'Game wrapper destination occupied: {entry.name()}')
                    move(entry, tree)
            candidates = self._candidate_paths(tree)
            issue = (self._staging_conflict(tree, candidates) or
                     self._staged_package_problem(tree, candidates))
            if issue:
                raise ValueError(issue)
            # A unique inbox is critical for MO2 Merge: files in an already
            # installed mod's canonical store must not be overwritten BEFORE
            # onModInstalled has an opportunity to back them up. No migration.
            incoming_path = (f'{STORE}/incoming/{uuid.uuid4().hex}/'
                             'Content/Paks/~mods') if candidates else ''
            for relative, _ in candidates:
                source = tree.find(relative)
                if source is None:
                    raise ValueError(f'Missing source: {relative}')
                move(source, tree.addDirectory(incoming_path))
            return tree
        except Exception as exc:
            failures = []
            for original, current in reversed(completed):
                try:
                    node = tree.find(current)
                    if node is None:
                        raise RuntimeError('Moved node missing')
                    parent_path = str(Path(original).parent)
                    parent = tree if parent_path == '.' else tree.addDirectory(parent_path)
                    if not node.moveTo(parent):
                        raise RuntimeError('Reverse IFileTree move declined')
                except Exception as rollback_error:
                    failures.append(f'{current}: {rollback_error}')
            if failures:
                self._report_error(f'Failed to roll back installer tree: {failures}')
            self._report_error(f'Installer fix refused: {exc}')
            return None


class Mo2S2Game(BasicGame, mobase.IPluginFileMapper, mobase.IPluginDiagnose):
    """MO2 game integration, installer normalization, VFS mapping and diagnostics."""
    Name = 'S.T.A.L.K.E.R. 2: Heart of Chornobyl Support Plugin'
    Description = 'Adds support for game S.T.A.L.K.E.R. 2: Heart of Chornobyl.'
    Author = '63OR63'
    Version = '1.0'
    GameName = 'Stalker 2: Heart of Chornobyl'
    GameShortName = 'stalker2heartofchornobyl'
    GameNexusName = 'stalker2heartofchornobyl'
    GameDocumentsDirectory = '%USERPROFILE%/AppData/Local/Stalker2'
    GameSaveExtension = 'sav'
    GameNexusId = 6944
    GameSteamId = 1643320
    GameGogId = 1529799785
    GameBinary = 'Stalker2.exe'
    GameDataPath = 'Stalker2'
    GameIniFiles = [
        '%GAME_DOCUMENTS%/Saved/Config/Windows/Game.ini',
        '%GAME_DOCUMENTS%/Saved/Config/Windows/GameUserSettings.ini',
        '%GAME_DOCUMENTS%/Saved/Config/Windows/Engine.ini',
    ]

    def __init__(self):
        BasicGame.__init__(self)
        mobase.IPluginFileMapper.__init__(self)
        mobase.IPluginDiagnose.__init__(self)
        self._organizer = None
        self._tab_created = False
        self._last_warnings: list[str] = []
        self._mapping_error: str = ''
        self._install_errors: dict[str, str] = {}
        self._install_error_roots: dict[str, tuple] = {}  # Preserve identity across renames.
        self._vfs_anchor: Path | None = None
        self._post_install_busy = False
        self._auto_queue = PreparationQueue()
        self._auto_timer = None
        self._game_running = False
        self._window = None
        self._logged_diagnostics: set[str] = set()

    def _log_unique(self, severity, message):
        if message not in self._logged_diagnostics:
            self._log(severity, message)
            if len(self._logged_diagnostics) >= 512:
                self._logged_diagnostics.clear()
            self._logged_diagnostics.add(message)

    def settings(self):
        """Native MO2 plugin settings; no separate UI or settings files."""
        return [
            mobase.PluginSetting('Verbosity',
                'Logging: 0=Errors only, 1=Normal (default), 2=Verbose, 3=Debug', 1),
            mobase.PluginSetting('Auto prepare mods',
                'Prepare newly discovered direct PAK/IoStore mods after files settle', True),
            mobase.PluginSetting('Show launch warnings',
                'Warn when active STALKER 2 mods are not mapped or contain errors', True),
            mobase.PluginSetting('Sync archive selection',
                'Synchronize visible mods in the left MO2 pane with Archives and catalog filters', True),
            mobase.PluginSetting('Auto-refresh Archives',
                'Automatically refresh the Archives preview when the mod list changes', True),
            mobase.PluginSetting('Scan external mods',
                'Show files installed directly in the real STALKER 2 ~mods folder in Diagnostics', True),
        ]

    def _setting(self, key, default):
        if self._organizer is None:
            return default
        try:
            result = self._organizer.pluginSetting(self.name(), key)
            return default if result is None else result
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return default

    def _log(self, severity, message):
        """Use Qt's message handler (captured in the MO2 log), stderr fallback."""
        levels = {'ERROR': 0, 'WARNING': 1, 'INFO': 1, 'DEBUG': 3,
                  'VERBOSE': 2}
        if verbosity_level(self._setting('Verbosity', 1)) < levels.get(severity, 1):
            return
        message = f'[mo2s2] [{severity}] {message}'
        try:
            from PyQt6 import QtCore
            fn = getattr(QtCore, {'ERROR': 'qCritical', 'WARNING': 'qWarning',
                                  'INFO': 'qInfo', 'VERBOSE': 'qInfo',
                                  'DEBUG': 'qDebug'}[severity])
            fn(message)
        except (ImportError, KeyError, AttributeError, RuntimeError, TypeError):
            print(message, file=sys.stderr)

    def catalog_path(self) -> Path:
        """Persistent SQLite resource cache, separate from profiles and mod files."""
        if self._organizer is None:
            raise RuntimeError('MO2 organizer is not initialized')
        return Path(self._organizer.pluginDataPath()) / 'mo2s2' / 'catalog-v3.sqlite3'

    def icon_directory(self) -> Path:
        return Path(__file__).parent / 'stalker2heartofchornobyl' / 'icons'

    def init(self, organizer: mobase.IOrganizer) -> bool:
        if not super().init(organizer):
            return False
        self._organizer = organizer
        # BasicGame v4.x and v5.x have slightly different registration paths.
        self._register_feature(Mo2S2ModDataChecker())
        self._register_feature(Mo2S2ModDataContent(self.icon_directory()))
        self._register_feature(BasicLocalSavegames(self))
        # VALID layout intentionally leaves normalization to the shared
        # post-install transaction for all installers (including manual/BAIN).
        # Never scan/mutate all existing mods in this callback.
        organizer.modList().onModInstalled(self._on_mod_installed)
        # Do not keep obsolete installation errors after the corresponding
        # mod is removed. This is an optional host callback (MO2 2.4+).
        on_removed = getattr(organizer.modList(), 'onModRemoved', None)
        if callable(on_removed):
            on_removed(self._on_mod_removed)
        # USVFS needs a real mapped directory so injected files appear during
        # FindFirstFile/FindNextFile in the game's ~mods directory. The empty
        # anchor belongs to MO2 plugin data, NEVER the actual game directory.
        self._vfs_anchor = Path(organizer.pluginDataPath()) / 'mo2s2' / 'empty_mods'
        # Each launch order gets an immutable empty-directory skeleton. Retire
        # the previous session's skeletons only here, never while the game is
        # running: replacing/deleting a live directory mapping is unsafe.
        try:
            prune_previous_session_anchors(self._vfs_anchor)
        except OSError as exc:
            # Old empty-directory skeletons are a cache, not game data.
            # Locked files from a prior session must not disable game support.
            self._log('ERROR', f'Unable to prune old VFS anchors: {exc}')
            self._vfs_anchor.mkdir(parents=True, exist_ok=True)
        organizer.onUserInterfaceInitialized(self._ui_initialized)
        # Called before every MO2-launched process. Do not mutate files here.
        if not organizer.onAboutToRun(self._on_about_to_run):
            self._log('ERROR', 'Failed to register launch diagnostics callback')
        on_finished = getattr(organizer, 'onFinishedRun', None)
        if callable(on_finished):
            on_finished(self._on_finished_run)
        return True

    @staticmethod
    def _mod_name(mod) -> str:
        if hasattr(mod, 'name'):
            name = mod.name
            return str(name() if callable(name) else name)
        return str(mod)

    def _on_mod_removed(self, name):
        key = self._mod_name(name).casefold()
        self._install_errors.pop(key, None)
        self._install_error_roots.pop(key, None)
        self._auto_queue.reset()  # Filesystem paths and displayed mod names may be reused.

    @staticmethod
    def _mod_folder_identity(root: Path) -> tuple:
        """NTFS directory file ID survives normal in-place MO2 folder renames.

        If a filesystem provides no inode, fall back to the absolute path.
        A copied/replaced folder is not considered the same mod by guessing.
        """
        path = Path(root)
        try:
            st = path.stat(follow_symlinks=False)
            if st.st_ino:
                return ('file-id', st.st_dev, st.st_ino)
        except OSError:
            pass
        return ('path', str(path).casefold())

    def _reconcile_install_errors(self):
        """Keep errors associated with a mod folder when MO2 renames a mod.

        Display names are mutable; staging roots and preparation signatures
        aren't. Older untracked error entries are left alone rather than
        guessing which mod owned them.
        """
        live = {self._mod_folder_identity(root): name for name, root in self._all_mods()}
        for old_key, directory in tuple(self._install_error_roots.items()):
            new_name = live.get(directory)
            if new_name is None:
                self._install_errors.pop(old_key, None)
                self._install_error_roots.pop(old_key, None)
                continue
            new_key = new_name.casefold()
            if new_key != old_key:
                existing = self._install_errors.pop(old_key, '')
                self._install_error_roots.pop(old_key, None)
                if existing:
                    detail = existing.split(': ', 1)[1] if ': ' in existing else existing
                    self._install_errors[new_key] = f'{new_name}: {detail}'
                    self._install_error_roots[new_key] = directory

    def _set_install_error(self, name: str, root: Path, error: Exception):
        key = name.casefold()
        self._install_errors[key] = f'{name}: {error}'
        self._install_error_roots[key] = self._mod_folder_identity(root)

    def _clear_install_error(self, name: str, root: Path):
        directory = self._mod_folder_identity(root)
        for key in tuple(self._install_errors):
            if key == name.casefold() or self._install_error_roots.get(key) == directory:
                self._install_errors.pop(key, None)
                self._install_error_roots.pop(key, None)

    def _apply_installation(self, name: str, root: Path, *, mod=None,
                            source: str = 'Install', approved_plan=None) -> int:
        """One execution path for MO2 Install, Auto, Merge and opt-in Repair.

        Discovery and conflict validation remain in stage_new_install(); this
        coordinator owns the game-running guard, callback reentrancy, pending
        inbox validation, MO2 notifications, and per-mod error bookkeeping.
        No UI action may bypass this path to move installed files.
        """
        if self._organizer is None:
            raise RuntimeError('MO2 organizer unavailable')
        if self._game_running or self._post_install_busy:
            raise RuntimeError('Cannot prepare while game or another installation is running')
        root = Path(root)
        self._post_install_busy = True
        try:
            if approved_plan is not None:
                assert_unchanged_plan(approved_plan)
            changed = stage_new_install(root)
            if pending_install_dirs(root):
                raise RuntimeError('Nonempty installer inbox remains after staging')
            self._auto_queue.completed(root)
            self._clear_install_error(name, root)
            if changed:
                label = 'Automatically prepared' if source == 'Auto prepare' else f'{source}: prepared'
                self._log('INFO', f'{label} {changed} staged paths: {name}')
                if mod is None:
                    mod = self._organizer.modList().getMod(name)
                if mod is not None:
                    self._organizer.modDataChanged(mod)
                tab = getattr(self, '_pak_tab', None)
                if tab is not None:
                    tab.host_changed.emit()
            return changed
        except Exception as exc:
            self._set_install_error(name, root, exc)
            self._log('ERROR', f'{source} failed: {name}: {exc}')
            raise
        finally:
            self._post_install_busy = False

    def _on_mod_installed(self, mod):
        """Normalize every MO2 installer path through the shared coordinator."""
        if self._post_install_busy or self._organizer is None:
            return
        try:
            if self._organizer.managedGame().gameShortName() != self.gameShortName():
                return
            installed = mod if hasattr(mod, 'absolutePath') else self._organizer.modList().getMod(str(mod))
            if installed is None:
                return
            root = Path(installed.absolutePath())
            self._apply_installation(self._mod_name(installed), root,
                                     mod=installed, source='MO2 install')
        except Exception as exc:
            # _apply_installation already records the per-mod error; failures
            # before it is reached are still visible in the ordinary MO2 log.
            self._log('ERROR', f'Post-install preparation failed: {exc!r}')

    def _all_mods(self):
        """Include inactive mods: preparation belongs to installation, not activation."""
        if self._organizer is None:
            return []
        modlist = self._organizer.modList()
        all_mods = getattr(modlist, 'allMods', None)
        names = all_mods() if callable(all_mods) else modlist.allModsByProfilePriority()
        result = []
        for name in names:
            try:
                mod = modlist.getMod(name)
                if mod is None:
                    continue
                root = Path(mod.absolutePath())
                if root.is_dir() and not root.is_symlink() and not (
                        getattr(root.lstat(), 'st_file_attributes', 0) & 0x400):
                    result.append((str(name), root))
            except (OSError, TypeError, ValueError, RuntimeError) as exc:
                self._log('WARNING', f'Cannot inspect installed mod {name}: {exc}')
        return result

    def _auto_prepare_tick(self):
        """UI-timer lifecycle, independent of which MO2 right tab is visible.

        One stable metadata signature per mod; failed attempts are retried only
        after the input changes. Never move files while a game is running.
        """
        if (self._organizer is None or self._post_install_busy or self._game_running or
                not self._setting('Auto prepare mods', True)):
            return
        try:
            if self._organizer.managedGame().gameShortName() != self.gameShortName():
                return
            for name, root in self._auto_queue.ready(self._all_mods(), limit=2):
                # At most two mods per tick; other eligible mods retain their
                # pending state, not accidentally marked as already handled.
                self._prepare_one(name, root)
        except Exception as exc:
            self._log('ERROR', f'Auto preparation watcher failed: {exc!r}')

    def _prepare_one(self, name: str, root: Path):
        if self._post_install_busy or self._game_running:
            return
        try:
            self._apply_installation(name, root, source='Auto prepare')
        except Exception:
            # _apply_installation records the error once; queue retries only
            # after file metadata changes, never in a tight loop.
            pass

    def _launch_issues_for(self, items, links, warnings) -> list[str]:
        """Check active-profile launch risks for a single mapping snapshot."""
        # Launch diagnostics should concern the selected profile only.
        # Disabled mods' pending installers/backups are shown in Diagnostics,
        # but must not trigger warnings for a game they are not affecting.
        self._reconcile_install_errors()
        active = {owner.name.casefold() for owner in self._owners()}
        pending = [name for name, root in self._all_mods()
                   if name.casefold() in active and pending_install_dirs(root)]
        transactions = [f'{self._mod_name(mod)} / {txn.name}: {msg}'
                        for mod, txn, _status, msg in self.pending_transaction_details()
                        if self._mod_name(mod).casefold() in active]
        errors = [value for key, value in self._install_errors.items() if key in active]
        return launch_issues(
            items, links, warnings, pending=pending, transactions=transactions,
            install_errors=errors, mapping_error='',  # fresh preflight; no stale error
            anchor_ok=self._vfs_anchor is not None and self._vfs_anchor.is_dir(),
        )

    def _launch_problems(self) -> list[str]:
        """Build a fresh read-only plan, not a stale Archives widget snapshot."""
        items = self.scan_items()
        links, warnings = self.mapping_preview(items)
        return self._launch_issues_for(items, links, warnings)

    def preview_launch_plan(self) -> str:
        """Same fresh planner and issue checks as the prelaunch callback.

        Only a preview: mappings() will validate current paths again at launch.
        """
        items = self.scan_items()
        links, warnings = self.mapping_preview(items)
        issues = self._launch_issues_for(items, links, warnings)
        return format_launch_plan(items, links, warnings, issues)

    def _on_about_to_run(self, executable: str, *args) -> bool:
        """Report errors before launching STALKER 2, without changing mappings.

        Suppression affects only the dialog; every launch error remains logged.
        MO2 also starts unrelated tools: those must not trigger game warnings.
        """
        if not is_game_executable(str(executable), Path(self.gameDirectory().absolutePath())):
            return True
        try:
            issues = self._launch_problems()
        except Exception as exc:
            issues = [f'Failed to verify mod mappings: {exc!r}']
        if issues:
            for issue in issues:
                self._log('ERROR', f'Launch check: {issue}')
            if self._setting('Show launch warnings', True):
                try:
                    app = QApplication.instance()
                    if app is not None:
                        from PyQt6.QtCore import QThread
                        if QThread.currentThread() != app.thread():
                            self._log('ERROR', 'Cannot display a dialog off the MO2 UI thread')
                            self._game_running = True
                            return True
                    dialog = QMessageBox(self._window)
                    dialog.setIcon(QMessageBox.Icon.Warning)
                    dialog.setWindowTitle('mo2s2 — STALKER 2 mod problems')
                    dialog.setText(f'{len(issues)} issue(s) detected before launching STALKER 2.')
                    dialog.setInformativeText('The game may start without some mods. '
                                              'Details are also recorded in the MO2 log.')
                    dialog.setDetailedText('\n'.join(issues))
                    dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
                    checkbox = QCheckBox('Do not show again (can be re-enabled in plugin settings)', dialog)
                    dialog.setCheckBox(checkbox)
                    dialog.exec()
                    if checkbox.isChecked():
                        self._organizer.setPluginSetting(self.name(), 'Show launch warnings', False)
                except Exception as exc:
                    self._log('ERROR', f'Cannot display launch warning: {exc!r}')
        else:
            self._log('DEBUG', 'Launch validation found no mapping problems')
        self._game_running = True
        return True

    def _on_finished_run(self, executable: str, exit_code: int, *args):
        if is_game_executable(str(executable), Path(self.gameDirectory().absolutePath())):
            self._game_running = False
            self._log('DEBUG', f'Game process exited with code {exit_code}')

    def _ui_initialized(self, window: QMainWindow):
        """Attach the native MO2 tools UI and start lifecycle synchronization."""
        if self._tab_created:
            return
        try:
            game = self._organizer.managedGame()
            if game is None or game.gameShortName() != self.gameShortName():
                return
            tabs = window.findChild(QTabWidget, 'tabWidget')
            if tabs is None:
                return
            from .stalker2heartofchornobyl.paks import Mo2S2VirtualPaksWidget
            self._window = window
            self._pak_tab = Mo2S2VirtualPaksWidget(window, self._organizer, self)
            tabs.addTab(self._pak_tab, 'Mod Tools')
            self._tab_created = True
            # Watch all installed mods even when Archives is not visible.
            self._auto_timer = QTimer(window)
            self._auto_timer.setInterval(2200)
            self._auto_timer.timeout.connect(self._auto_prepare_tick)
            self._auto_timer.start()
            QTimer.singleShot(0, self._auto_prepare_tick)
        except Exception as exc:
            self._log('ERROR', 'Cannot create Mod Tools tab: ' + repr(exc))

    def savesDirectory(self) -> QDir:
        docs = Path(self.documentsDirectory().absolutePath())
        for name in ('Saved/Steam/SaveGames/Data', 'Saved/GOG/SaveGames/Data',
                     'Saved/SaveGames/Data'):
            path = docs / name
            if path.is_dir():
                return QDir(str(path))
        return QDir(str(docs / 'Saved/Steam/SaveGames/Data'))

    def _owners(self) -> list[Owner]:
        """Collect MO2 mod owners, including disabled mods and their priorities."""
        owners: list[Owner] = []
        if self._organizer is None:
            return owners
        modlist = self._organizer.modList()
        for name in modlist.allModsByProfilePriority():
            if not (modlist.state(name) & mobase.ModState.ACTIVE):
                continue
            mod = modlist.getMod(name)
            if mod is None:
                continue
            try:
                owners.append(Owner(name, Path(mod.absolutePath()),
                                    int(modlist.priority(name))))
            except (OSError, TypeError, ValueError) as exc:
                self._log('ERROR', f'Invalid mod entry {name}: {exc}')
        return owners

    def mod_signature(self):
        """Cheap identity for UI change detection; never opens a PAK/UTOC.

        Includes name, physical root and priority, so mod rename, reorder,
        enable/disable and profile switches invalidate the preview. Directory
        metadata catches direct additions/removals in the staged mod root;
        individual deep file changes are handled by a manual Recheck.
        """
        from os import stat as os_stat
        values = []
        for owner in self._owners():
            roots = (owner.path, owner.path / '__mo2s2__')
            stamps = []
            for path in roots:
                try:
                    st = os_stat(path, follow_symlinks=False)
                    stamps.append((st.st_mtime_ns, st.st_ctime_ns))
                except OSError:
                    stamps.append(None)
            values.append((owner.name, str(owner.path), owner.priority, tuple(stamps)))
        return (str(self._organizer.profileName()), tuple(values))

    def scan_items(self):
        items = scan(self._owners())
        for item in items:
            if item.warning:
                self._log_unique('ERROR', f'{item.mod_name}: {item.warning}')
        return items

    def mapping_preview(self, items=None):
        """Derive read-only virtual mappings and warnings from a fresh scan."""
        if items is None:
            items = self.scan_items()
        links, warnings = build_links(items, Path(self.gameDirectory().absolutePath()))
        for warning in warnings:
            # When Auto prepare is enabled, an unmanaged package is a *queued
            # installation*, not a final mapping failure. MO2 may ask for
            # mappings before onUserInterfaceInitialized starts our timer.
            # Never hide real package/USVFS errors or launch-time problems.
            pending_auto = ('unmanaged ' in warning and
                            warning.endswith('; install using MO2') and
                            self._setting('Auto prepare mods', True))
            if pending_auto:
                self._log_unique('INFO', 'Pending automatic preparation: ' + warning)
            else:
                self._log_unique('ERROR', 'Mapping diagnostic: ' + warning)
        return links, warnings

    def mappings(self) -> list[mobase.Mapping]:
        """MO2 invokes this when assembling VFS for a launched process."""
        try:
            if self._organizer is None:
                return []
            managed = self._organizer.managedGame()
            if managed is None or managed.gameShortName() != self.gameShortName():
                return []
            links, warnings = self.mapping_preview()
            self._last_warnings = warnings
            self._mapping_error = ''
            # All mappings point to ABSOLUTE physical sources and game targets.
            # Place directory anchor first; see MO2 Resident Evil Requiem
            # integration PR #235. All virtual assets remain outside game disk.
            if not links:
                return []
            base = Path(self.gameDirectory().absolutePath()) / 'Stalker2/Content/Paks/~mods'
            anchor = self._vfs_anchor
            if anchor is None or not anchor.is_dir():
                raise RuntimeError('MO2 VFS anchor is not initialized')
            # File-level Zone Kit mappings need real parent directories for
            # FindFirstFile/FindNextFile. Build an EMPTY skeleton for only the
            # destinations present in this particular active profile/order.
            # The real game and installed mod files are never written here.
            import hashlib
            fingerprint = hashlib.sha256('\n'.join(sorted(
                str(x.destination).casefold() for x in links)).encode('utf-8')).hexdigest()[:16]
            mount_anchor = anchor / fingerprint
            for link in links:
                relative = link.destination.relative_to(base)
                if link.directory:
                    (mount_anchor / relative).mkdir(parents=True, exist_ok=True)
                else:
                    (mount_anchor / relative.parent).mkdir(parents=True, exist_ok=True)
            result = [mobase.Mapping(str(mount_anchor), str(base), True, False)]
            result.extend(mobase.Mapping(str(x.source), str(x.destination),
                                         x.directory, False) for x in links)
            return result
        except Exception as exc:
            self._mapping_error = str(exc)
            self._log('ERROR', f'Mapping failed; no overlay installed: {exc}')
            return []


    def pending_install_count(self) -> int:
        """Count pending installers in both active and disabled MO2 mods."""
        if self._organizer is None:
            return 0
        modlist = self._organizer.modList()
        all_mods = getattr(modlist, 'allMods', None)
        names = all_mods() if callable(all_mods) else modlist.allModsByProfilePriority()
        count = 0
        for name in names:
            try:
                mod = modlist.getMod(name)
                if mod is not None and pending_install_dirs(Path(mod.absolutePath())):
                    count += 1
            except (OSError, ValueError, TypeError):
                continue
        return count

    def finalize_pending_installs(self) -> tuple[int, int, list[str]]:
        """Finish pending MO2 Merge/Replace installs via the shared coordinator."""
        if self._organizer is None:
            return 0, 0, ['MO2 organizer unavailable']
        successes = moved = 0
        errors = []
        for name, root in self._all_mods():
            if not pending_install_dirs(root):
                continue
            try:
                mod = self._organizer.modList().getMod(name)
                n = self._apply_installation(name, root, mod=mod, source='Finalize pending')
                successes += 1
                moved += n
            except Exception as exc:
                errors.append(f'{name}: {exc}')
        return successes, moved, errors

    def pending_transaction_details(self) -> list[tuple[object, Path, str, str]]:
        """Read-only inventory, including disabled mods (they own backups too)."""
        if self._organizer is None:
            return []
        modlist = self._organizer.modList()
        all_mods = getattr(modlist, 'allMods', None)
        names = all_mods() if callable(all_mods) else modlist.allModsByProfilePriority()
        details = []
        for name in names:
            try:
                mod = modlist.getMod(name)
                if mod is None:
                    continue
                root = Path(mod.absolutePath())
                for txn in transaction_dirs(root):
                    status, message = inspect_transaction(root, txn)
                    details.append((mod, txn, status, message))
            except (OSError, TypeError, ValueError) as exc:
                self._log('ERROR', f'Cannot inspect transaction for {name}: {exc}')
        return details

    def recover_interrupted_updates(self) -> tuple[int, int, list[str]]:
        """Explicit action only: rollback uncommitted / cleanup committed.

        Return (rolled_back, cleaned, errors); unknown backups stay untouched.
        UI must obtain confirmation before invoking this method.
        """
        restored = cleaned = 0
        errors = []
        for mod, txn, status, message in self.pending_transaction_details():
            name = self._mod_name(mod)
            if status == 'manual':
                errors.append(f'{name} / {txn.name}: {message}')
                continue
            try:
                action = recover_transaction(Path(mod.absolutePath()), txn)
                if action == 'rolled_back':
                    restored += 1
                    self._organizer.modDataChanged(mod)
                else:
                    cleaned += 1
                self._clear_install_error(name, Path(mod.absolutePath()))
            except (OSError, TypeError, ValueError, RuntimeError) as exc:
                errors.append(f'{name} / {txn.name}: {exc}')
                self._log('ERROR', f'Recovery failed: {name} / {txn.name}: {exc}')
        return restored, cleaned, errors

    def diagnostic_records(self, items=None, links=None, warnings=None):
        """Consolidated read-only health inventory for the Diagnostics subtab."""
        if items is None:
            items = self.scan_items()
        if links is None or warnings is None:
            links, warnings = self.mapping_preview(items)
        external = []
        if self._setting('Scan external mods', True):
            try:
                external = external_packages(Path(self.gameDirectory().absolutePath()))
            except (OSError, ValueError) as exc:
                external = [Diagnostic('Game', '~mods', 'SCAN ERROR', str(exc),
                                       'Check game directory permissions and retry')]
        self._reconcile_install_errors()
        pending = [name for name, root in self._all_mods() if pending_install_dirs(root)]
        transactions = [f'{self._mod_name(mod)} / {txn.name}: {status}'
                        for mod, txn, status, _ in self.pending_transaction_details()]
        return package_diagnostics(
            items, links, warnings, pending=pending, transactions=transactions,
            installation_errors=self._install_errors.values(), external=external)

    def preview_mod_repair(self, name: str):
        """Validate that the target is an MO2-owned mod, never a game folder."""
        if self._organizer is None:
            raise RuntimeError('MO2 organizer unavailable')
        modlist = self._organizer.modList()
        mod = modlist.getMod(name)
        if mod is None:
            raise ValueError(f'MO2 mod not found: {name}')
        root = Path(mod.absolutePath())
        if not any(n == name and r == root for n, r in self._all_mods()):
            raise ValueError('Mod is not in the current MO2 installation')
        return repair_plan(root)

    def repair_mod(self, name: str, approved_plan):
        """Execute a confirmed, unchanged plan through the common stage path."""
        plan = self.preview_mod_repair(name)
        if plan != approved_plan:
            raise RuntimeError('Repair plan changed since confirmation; refresh and retry')
        result = self._apply_installation(name, plan.mod_root, source='Repair',
                                          approved_plan=approved_plan)
        self._log('INFO', f'Repair of {name}: prepared {result} paths')
        return result

    def activeProblems(self):
        """Return diagnosis keys for problems in the active mod configuration."""
        if self._organizer is None:
            return []
        try:
            items = self.scan_items()
        except Exception as exc:
            self._log('ERROR', f'Cannot build MO2 diagnostics: {exc!r}')
            return [5]
        problems = []
        if self._vfs_anchor is None or not self._vfs_anchor.is_dir():
            problems.append(3)
        if any(not x.managed for x in items):
            problems.append(1)
        if any(x.warning for x in items):
            problems.append(2)
        if self._install_errors:
            problems.append(4)
        if self.pending_install_count():
            problems.append(7)
        # A terminated update may leave an old-package backup. Keep it
        # untouched; a recovery decision belongs to the mod's owner.
        if self.pending_transaction_details():
            problems.append(6)
        try:
            _, plan_warnings = self.mapping_preview(items)
            if self._mapping_error or any('cannot map' in w for w in plan_warnings):
                problems.append(5)
        except (OSError, ValueError) as exc:
            self._mapping_error = str(exc)
            self._log('ERROR', f'MO2 diagnostics mapping check failed: {exc!r}')
            problems.append(5)
        return problems

    def shortDescription(self, key):
        return {1:'mo2s2: mod installed with unsupported direct archive paths',
                2:'STALKER 2: incomplete package (PAK / IoStore)',
                3:'mo2s2: virtual filesystem anchor unavailable',
                4:'mo2s2: new mod could not be prepared automatically',
                5:'mo2s2: virtual mapping plan contains errors',
                6:'mo2s2: interrupted update backup found',
                7:'mo2s2: installation awaiting finalization'}.get(key, '')

    def fullDescription(self, key):
        return {1:'Reinstall the mod using MO2 with mo2s2 enabled. '
                  'Unstaged archives are never mapped to avoid duplicate mounts.',
                2:'Inspect Mod Tools → Archives: incomplete UTOC/UCAS pairs or missing PAK anchors '
                  'are not mapped.',
                3:'The MO2 plugin-data anchor directory is missing. Reopen MO2 to recreate it. '
                  'No changes to the real game directory are necessary.',
                4:'\n'.join(list(self._install_errors.values())[-5:])
                  + '\nFix the reported package error or retry automatic preparation.',
                5:(self._mapping_error or '\n'.join(self._last_warnings[-5:]) or
                   'Open Mod Tools → Archives to inspect skipped packages and their virtual paths.'),
                6:'A journaled mo2s2 update is interrupted, or a committed backup remains. '
                  'Open Mod Tools → Archives and select Recover interrupted updates to review '
                  'and confirm rollback or cleanup. Back up the mod folder first. '
                  'Unknown or legacy transaction directories are never restored '
                  'or deleted automatically.',
                7:'The installer left an isolated pending package in __mo2s2__/incoming. '
                  'Previous packages are still intact. Open Mod Tools → Archives and use '
                  'Finalize pending installs to apply it transactionally.'}.get(key, '')

    def hasGuidedFix(self, key):
        return False

    def startGuidedFix(self, key):
        return None


def createPlugin():
    return Mo2S2Game()
