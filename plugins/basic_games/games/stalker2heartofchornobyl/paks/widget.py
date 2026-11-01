"""Mod Tools tab: read-only preview of installed, mapped archives."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QItemSelectionModel, QSignalBlocker
from PyQt6.QtGui import QIcon
from .ui_sync import select_visible_mods, selected_visible_mods
from .ui_actions import (selected_packages, source_paths, virtual_paths,
                         source_folder, matches_archive_search)
from .virtual_core import format_mapping_report, package_state
from PyQt6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QLineEdit, QComboBox, QApplication, QMessageBox, QTabWidget,
    QToolButton, QMenu, QDialog, QDialogButtonBox, QPlainTextEdit,
)


class PriorityTableItem(QTableWidgetItem):
    """Sort MO2 priorities numerically rather than as strings."""
    def __lt__(self, other):
        try:
            return int(self.text()) < int(other.text())
        except (ValueError, TypeError):
            return super().__lt__(other)


class Mo2S2VirtualPaksWidget(QWidget):
    """Qt-native Archives and Diagnostics tools embedded in the MO2 UI."""
    # MO2 callbacks are native callbacks, not guaranteed to arrive in the UI
    # thread. A Qt signal safely marshals them to our QWidget's thread.
    host_changed = pyqtSignal()

    def __init__(self, parent, organizer, game):
        super().__init__(parent)
        self._organizer = organizer
        self._game = game
        self._items = []
        self._links = []
        self._warnings = []
        self._by_key = {}
        self._mapped_keys = set()
        self._signature = None
        self._refreshing = False
        self._refresh_queued = False
        self._diag_dirty = True
        self._diag_records = []
        self._left_view = None
        self._left_selection = None
        self._syncing_to_left = False
        self._syncing_from_left = False
        self.host_changed.connect(self._schedule_refresh)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1800)
        self._poll_timer.timeout.connect(self._poll_signature)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(250)
        self._refresh_timer.timeout.connect(self.refresh)
        # One MO2 right-hand tab, with native subtabs; never open tool windows.
        root_layout = QVBoxLayout(self)
        self.sections = QTabWidget(self)
        root_layout.addWidget(self.sections)
        archive_page = QWidget(self.sections)
        layout = QVBoxLayout(archive_page)
        self.sections.addTab(archive_page, 'Archives')
        from ..catalog.widget import CatalogTabs
        self.catalog_tabs = CatalogTabs(self.sections, game)
        self.catalog_tabs.navigate_mods.connect(
            lambda names: self._select_left_mods(names, force=True))
        self.sections.addTab(self.catalog_tabs.contents, 'Contents')
        self.sections.addTab(self.catalog_tabs.conflicts, 'Conflicts')
        self.diagnostics_page = QWidget(self.sections)
        diag_layout = QVBoxLayout(self.diagnostics_page)
        diag_intro = QLabel('Read-only problems in MO2 mods and physical game ~mods. '
                            'Repair is opt-in and only uses the journaled MO2 installer. '
                            'External game files are never changed.', self.diagnostics_page)
        diag_intro.setWordWrap(True)
        diag_layout.addWidget(diag_intro)
        self.diag_table = QTableWidget(0, 5, self.diagnostics_page)
        self.diag_table.setHorizontalHeaderLabels(
            ['Origin', 'Mod / file', 'State', 'Problem', 'Suggested action'])
        self.diag_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.diag_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.diag_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.diag_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.diag_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.diag_table.setColumnWidth(0, 74)
        self.diag_table.setColumnWidth(1, 180)
        self.diag_table.setColumnWidth(2, 105)
        self.diag_table.setColumnWidth(4, 260)
        self.diag_table.itemSelectionChanged.connect(self._diagnostic_selection)
        self.diag_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.diag_table.customContextMenuRequested.connect(self._diagnostics_menu)
        diag_layout.addWidget(self.diag_table)
        diag_bar = QHBoxLayout()
        self.diag_recheck = QPushButton('Recheck', self.diagnostics_page)
        self.diag_recheck.clicked.connect(self.refresh)
        diag_bar.addWidget(self.diag_recheck)
        self.diag_repair = QPushButton('Repair selected…', self.diagnostics_page)
        self.diag_repair.clicked.connect(self.repair_selected)
        diag_bar.addWidget(self.diag_repair)
        self.diag_import = QPushButton('Copy import instructions', self.diagnostics_page)
        self.diag_import.clicked.connect(self.copy_external_instructions)
        diag_bar.addWidget(self.diag_import)
        self.diag_open = QPushButton('Open game ~mods', self.diagnostics_page)
        self.diag_open.clicked.connect(self.open_external_folder)
        diag_bar.addWidget(self.diag_open)
        diag_bar.addStretch(1)
        diag_layout.addLayout(diag_bar)
        self.diag_status = QLabel('', self.diagnostics_page)
        self.diag_status.setWordWrap(True)
        diag_layout.addWidget(self.diag_status)
        self.sections.addTab(self.diagnostics_page, 'Diagnostics')
        self.sections.currentChanged.connect(self._activate_catalog)
        message = QLabel(
            '<b>Unified Virtual PakOrder</b> — classic PAK and nested Zone Kit '
            'PAK/IoStore containers all receive the same numeric _N_P rank from '
            'the left MO2 mod pane. Entire nested trees and manifests are preserved; '
            'only container filenames are virtualized. No flattening or physical rename.', self)
        message.setWordWrap(True)
        layout.addWidget(message)
        filters = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText('Filter mods or virtual paths…')
        self.search.textChanged.connect(self._apply_filter)
        filters.addWidget(self.search, 1)
        self.kind = QComboBox(self)
        self.kind.addItems(['All formats', 'Classic PAK', 'Zone Kit', 'Nested trees', 'Problems'])
        self.kind.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self.kind)
        layout.addLayout(filters)
        self.table = QTableWidget(0, 7, self)
        self.table.setHorizontalHeaderLabels([
            'MO2 priority', 'Mod', 'Format', '.pak', '.utoc', '.ucas',
            'State'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self._show_selected)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._archives_menu)
        # ResizeToContents scans every row on every refresh: for thousands of
        # packages it blocks MO2's UI thread. Stretch the mod-name column.
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column, width in enumerate((95, 180, 112, 54, 54, 54, 104)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table)
        self.selected = QLabel('Select a package to inspect its source and virtual files.', self)
        self.selected.setWordWrap(True)
        self.selected.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.selected)
        bar = QHBoxLayout()
        btn_refresh = QPushButton('Refresh', self)
        btn_refresh.setToolTip('Rebuild the Archives preview without changing MO2 files. '
                               'Normally this happens automatically.')
        btn_refresh.clicked.connect(self.refresh)
        bar.addWidget(btn_refresh)
        tools = QToolButton(self)
        tools.setText('Tools')
        tools.setToolTip('Diagnostics and manual recovery operations')
        tools.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(tools)
        menu.setToolTipsVisible(True)
        self._action_report = menu.addAction('Copy mapping report')
        self._action_report.setToolTip('Copy the full virtual file plan and mapping errors')
        self._action_report.triggered.connect(self.copy_mapping_report)
        self._action_selected = menu.addAction('Copy selected mappings')
        self._action_selected.setToolTip('Copy mappings for packages selected in Archives')
        self._action_selected.triggered.connect(self.copy_selected_paths)
        menu.addSeparator()
        self._action_preview = menu.addAction('Preview launch plan…')
        self._action_preview.setToolTip('Show the fresh ordered FileMapper preview and prelaunch checks; read-only')
        self._action_preview.triggered.connect(self.preview_launch_plan)
        menu.addSeparator()
        self._action_vfs = menu.addAction('Refresh MO2 VFS')
        self._action_vfs.setToolTip('Ask MO2 to rebuild the virtual file system; '
                                    'not necessary for a normal preview refresh')
        self._action_vfs.triggered.connect(self.force_vfs_refresh)
        menu.addSeparator()
        self._action_finalize = menu.addAction('Finalize pending installs…')
        self._action_finalize.setToolTip('Complete MO2 Merge/Replace installs '
                                         'which did not trigger onModInstalled')
        self._action_finalize.triggered.connect(self.finalize_pending)
        self._action_recover = menu.addAction('Recover interrupted updates…')
        self._action_recover.setToolTip('Review uncommitted transactions and '
                                        'confirm rollback/backup cleanup')
        self._action_recover.triggered.connect(self.recover_interrupted)
        tools.setMenu(menu)
        bar.addWidget(tools)
        bar.addStretch(1)
        layout.addLayout(bar)
        self.status = QLabel('', self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        mod_list = organizer.modList()
        for event in ('onModMoved', 'onModInstalled', 'onModRemoved'):
            callback = getattr(mod_list, event, None)
            if callable(callback):
                try:
                    callback(lambda *args: self.host_changed.emit())
                except (RuntimeError, TypeError, AttributeError):
                    pass
        on_states = getattr(mod_list, 'onModStateChanged', None)
        if callable(on_states):
            try:
                # A fixed one-argument signature selects the non-deprecated
                # MO2 overload (dict of changed mod names to states).
                on_states(self._on_mod_states_changed)
            except (RuntimeError, TypeError, AttributeError):
                pass
        callback = getattr(organizer, 'onProfileChanged', None)
        if callable(callback):
            try:
                callback(lambda *_: self.host_changed.emit())
            except (RuntimeError, TypeError, AttributeError):
                pass
        self.refresh()
        QTimer.singleShot(0, self._attach_left_view)
        # IModList exposes no onModRenamed callback. Check ONLY the cheap
        # profile/mod identity, without scanning directories or archive indices.
        # Suspend work while another right-hand MO2 tab is visible.
        self._poll_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._poll_signature)
        QTimer.singleShot(0, self._attach_and_sync_left)

    def _attach_and_sync_left(self):
        self._attach_left_view()
        self._sync_from_left()

    def _on_mod_states_changed(self, changed_mods: dict[str, object]) -> None:
        """React to MO2's current bulk mod-state notification."""
        self.host_changed.emit()

    def _schedule_refresh(self):
        # Debounce is trailing-edge: a multi-row drag or bulk toggle should
        # produce one refresh *after* the final MO2 notification.
        if self._game._setting('Auto-refresh Archives', True):
            self._refresh_timer.start(380)

    def _poll_signature(self):
        """Refresh visible archives only when their metadata signature changes."""
        if (not self._game._setting('Auto-refresh Archives', True) or
                not self.isVisible() or self._refreshing):
            return
        try:
            signature = self._game.mod_signature()
        except (OSError, RuntimeError, ValueError):
            return  # MO2 can briefly have an incomplete profile during switch
        if self._signature != signature:
            self._schedule_refresh()

    def refresh(self):
        """Rebuild Archives and Diagnostics views from a fresh mapping snapshot."""
        if self._refreshing:
            self._refresh_queued = True
            return
        self._refreshing = True
        try:
            signature_before = self._game.mod_signature()
            # A queued native event may arrive before MO2 has finished writing
            # its new list. The periodic signature check handles that race.
            items = self._game.scan_items()
            links, warnings = self._game.mapping_preview(items)
            self._items = items
            self._links, self._warnings = links, warnings
            self._by_key = {item.key: item for item in self._items}
            self._mapped_keys = {link.item_key for link in links}
            virtual_by_key = {}
            for link in links:
                virtual_by_key.setdefault(link.item_key, []).append(link)
            self._link_index = virtual_by_key
            icons = self._game.icon_directory()
            # QTableWidget relocates rows when sorting is enabled. Disable it
            # while filling and derive filter identity from UserRole afterward.
            selected_keys = {item.key for idx in self.table.selectionModel().selectedRows()
                             if (item := self._row_item(idx.row())) is not None}
            self.table.setSortingEnabled(False)
            self.table.setRowCount(len(self._items))
            for row, item in enumerate(self._items):
                state = package_state(item, self._mapped_keys)
                kind_label = ('Zone Kit' if item.kind == 'ZONEKIT' else
                              ('Flat bundle' if item.kind == 'FLAT_WRAPPER' else
                               'Nested tree' if item.kind == 'NESTED' else 'Classic PAK'))
                values = [str(item.priority), item.mod_name,
                          kind_label,
                          '✓' if '.pak' in item.files else '',
                          '✓' if '.utoc' in item.files else '',
                          '✓' if '.ucas' in item.files else '',
                          state]
                for col, value in enumerate(values):
                    cell = PriorityTableItem(value) if col == 0 else QTableWidgetItem(value)
                    cell.setToolTip(item.warning or str(item.source))
                    if col == 1:
                        cell.setData(Qt.ItemDataRole.UserRole, item.key)
                    if col == 0:
                        cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    if col in (3, 4, 5) and value:
                        cell.setIcon(QIcon(str(icons / ({3:'pak',4:'utoc',5:'ucas'}[col] + '.png'))))
                    if col == 2:
                        cell.setIcon(QIcon(str(icons / (('zonekit' if item.kind == 'ZONEKIT' else 'classic') + '.png'))))
                    self.table.setItem(row, col, cell)
            self.table.setSortingEnabled(True)
            self.table.clearSelection()  # row indices were reused for other mods
            if selected_keys:
                flags = (QItemSelectionModel.SelectionFlag.Select |
                         QItemSelectionModel.SelectionFlag.Rows)
                for row in range(self.table.rowCount()):
                    item = self._row_item(row)
                    if item is not None and item.key in selected_keys:
                        self.table.selectionModel().select(self.table.model().index(row, 0), flags)
            self._apply_filter()
            self._show_selected()
            self._attach_left_view()
            self._sync_from_left()
            self._diag_dirty = True
            if self.sections.currentIndex() == 3:
                self._refresh_diagnostics()
            self._update_tool_actions()
            if self.sections.currentIndex() in (1, 2):
                self.catalog_tabs.request(self._items, signature=signature_before)
            else:
                self.catalog_tabs.invalidate(self._items)
            prepared = len([i for i in self._items if i.managed])
            pending = len(self._items) - prepared
            warning_count = len([i for i in self._items if i.warning])
            failed_mapping_count = len([w for w in warnings if 'cannot map' in w])
            pending_count = self._game.pending_install_count()
            transaction_count = len(self._game.pending_transaction_details())
            self.status.setText(
                f'{len(self._items)} packages: {prepared} staged / {pending} invalid/direct; '
                f'{len(links)} virtual mappings; {warning_count} invalid packages; '
                f'{failed_mapping_count} mapping errors. '
                + (f'{pending_count} pending installer(s): use Finalize pending installs. '
                   if pending_count else '')
                + (f'{transaction_count} interrupted/retained transaction(s): review recovery. '
                   if transaction_count else '')
                + ('\n'.join(warnings[:4]) if warnings else ''))
            self._signature = signature_before
        except Exception as exc:
            self.status.setText(f'Unable to build virtual map: {exc}')
            logger = getattr(self._game, '_log', None)
            if callable(logger):
                logger('ERROR', f'Archives preview failed: {exc!r}')
        finally:
            self._refreshing = False
            # The reverse selection handler intentionally ignores refresh-time
            # signals. Apply the current left-pane selection only after the
            # new profile's table is fully populated.
            self._sync_from_left()
            if self._refresh_queued:
                self._refresh_queued = False
                self._schedule_refresh()

    def _activate_catalog(self, index):
        if index == 3 and self._diag_dirty:
            self._refresh_diagnostics()
        if index in (1, 2):
            if self.catalog_tabs.snapshot is None and not self.catalog_tabs._working:
                self.catalog_tabs.request(self._items)

    def _refresh_diagnostics(self):
        """Only scan physical game ~mods when Diagnostics is actually viewed."""
        try:
            records = self._game.diagnostic_records(self._items, self._links, self._warnings)
            self._diag_records = records
            self.diag_table.setSortingEnabled(False)
            self.diag_table.setRowCount(len(records))
            for row, record in enumerate(records):
                values = (record.origin, record.name, record.state,
                          record.detail, record.recommendation)
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setToolTip(str(record.path or record.detail))
                    self.diag_table.setItem(row, col, item)
            self.diag_table.setSortingEnabled(False)  # indices are stable for repair
            self.diag_table.clearSelection()
            self._diag_dirty = False
            external_count = sum(r.origin == 'Game' and r.state == 'EXTERNAL'
                                 for r in records)
            self.diag_status.setText(
                f'{len(records)} issue(s), including {external_count} physical external package(s). '
                'Nothing is changed by scanning. Select an issue for supported actions.')
            self._diagnostic_selection()
        except Exception as exc:
            self._diag_dirty = True
            self.diag_status.setText(f'Diagnostics scan failed: {exc}')

    def _selected_diagnostic(self):
        rows = self.diag_table.selectionModel().selectedRows()
        if len(rows) != 1:
            return None
        row = rows[0].row()
        return self._diag_records[row] if 0 <= row < len(self._diag_records) else None

    def _diagnostic_selection(self):
        record = self._selected_diagnostic()
        self.diag_repair.setEnabled(bool(record and record.repairable))
        self.diag_import.setEnabled(bool(record and record.origin == 'Game'
                                         and record.state == 'EXTERNAL'))

    def repair_selected(self):
        """Confirm and execute repair only for the selected eligible package."""
        record = self._selected_diagnostic()
        if record is None or not record.repairable:
            return
        try:
            plan = self._game.preview_mod_repair(record.name)
        except Exception as exc:
            self.diag_status.setText(f'Repair not available for {record.name}: {exc}')
            return
        details = ['Moves (source -> managed storage):']
        details += [f'{src}\n    -> {dst}' for src, dst in plan.moves]
        if plan.replacements:
            details += ['\nExisting packages replaced by the transaction:']
            details += [str(p) for p in plan.replacements]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle('mo2s2 — Confirm repair')
        box.setText(f'Repair {record.name}? {len(plan.moves)} operation(s), '
                    f'{len(plan.replacements)} existing paths to replace.')
        box.setInformativeText('Only files inside this MO2 mod will be moved. '
                               'The existing journaled installer provides rollback. '
                               'Make a backup if these files are important. '
                               'No real game files will be changed.')
        box.setDetailedText('\n'.join(details))
        box.setStandardButtons(QMessageBox.StandardButton.Yes |
                               QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            count = self._game.repair_mod(record.name, plan)
            self.refresh()
            self.diag_status.setText(f'Repair completed: {count} staged paths in {record.name}.')
        except Exception as exc:
            self.refresh()
            self.diag_status.setText(f'Repair failed; see transaction diagnostics: {exc}')

    def copy_external_instructions(self):
        record = self._selected_diagnostic()
        if record is None or record.origin != 'Game' or record.state != 'EXTERNAL':
            return
        instructions = (f'External STALKER 2 mod: {record.path}\n'
                        '1. Locate the original downloaded archive.\n'
                        '2. Install that archive through MO2 (mo2s2 enabled).\n'
                        '3. Verify the mod is MAPPED under Mod Tools → Archives and works in game.\n'
                        '4. Manually remove or move aside the old copy from the physical '
                        'game ~mods folder. Never delete it before verification.\n'
                        'Note: mo2s2 does not delete or import files from the game folder.')
        QApplication.clipboard().setText(instructions)
        self.diag_status.setText('Import instructions copied to clipboard.')

    def open_external_folder(self):
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        from pathlib import Path
        folder = Path(self._game.gameDirectory().absolutePath()) / 'Stalker2/Content/Paks/~mods'
        if not folder.is_dir():
            self.diag_status.setText(f'Game ~mods directory not found: {folder}')
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            self.diag_status.setText('Could not open the game ~mods directory.')

    def _update_tool_actions(self):
        selected = bool(self.table.selectionModel().selectedRows())
        self._action_selected.setEnabled(selected)
        self._action_finalize.setEnabled(self._game.pending_install_count() > 0)
        self._action_recover.setEnabled(bool(self._game.pending_transaction_details()))

    def _attach_left_view(self):
        """Reconnect if MO2 replaced its left view's selection model on profile switch."""
        try:
            view = self.window().findChild(QAbstractItemView, 'modList')
            selection = view.selectionModel() if view is not None else None
            if selection is self._left_selection:
                return
            if self._left_selection is not None:
                try:
                    self._left_selection.selectionChanged.disconnect(self._sync_from_left)
                except (RuntimeError, TypeError):
                    pass
            self._left_view, self._left_selection = view, selection
            if selection is not None:
                selection.selectionChanged.connect(self._sync_from_left)
                self._sync_from_left()
        except (RuntimeError, TypeError, AttributeError) as exc:
            logger = getattr(self._game, '_log', None)
            if callable(logger):
                logger('DEBUG', f'Cannot attach left selection observer: {exc}')

    def _sync_from_left(self, *_):
        """Reflect user-selected MO2 mods in Archives and catalog dropdowns.

        Never reorder, enable, disable or unfilter mods. Group headers and
        ambiguous display names are ignored. No automatic tab switching.
        """
        if (self._syncing_to_left or self._syncing_from_left or self._refreshing or
                not self.isVisible() or
                not self._game._setting('Sync archive selection', True) or
                self._left_view is None):
            return
        names = {item.mod_name for item in self._items}
        if not names:
            return
        # Internal and displayed names can differ for special MO2 entries.
        display_map = {}
        ambiguous = set()
        display_name = getattr(self._organizer.modList(), 'displayName', None)
        for name in names:
            shown = str(display_name(name) or name) if callable(display_name) else name
            key = shown.casefold()
            if key in display_map and display_map[key] != name:
                ambiguous.add(key)
            else:
                display_map[key] = name
        for key in ambiguous:
            display_map.pop(key, None)
        selected = selected_visible_mods(self._left_view, display_map, Qt)
        if not selected:
            return  # Preserve right-pane state when selecting a separator/no mod.
        self._syncing_from_left = True
        try:
            model = self.table.model()
            selection = self.table.selectionModel()
            flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
            # Block the table's own itemSelectionChanged signal, not the
            # QItemSelectionModel. The view still needs selectionChanged to
            # repaint the highlighted rows immediately.
            with QSignalBlocker(self.table):
                self.table.clearSelection()
                for row in range(self.table.rowCount()):
                    item = self._row_item(row)
                    if (item is not None and item.mod_name in selected and
                            not self.table.isRowHidden(row)):
                        selection.select(model.index(row, 0), flags)
            self._show_selected()
            self.catalog_tabs.focus_mod(next(iter(selected)) if len(selected) == 1 else None)
        finally:
            self._syncing_from_left = False

    def preview_launch_plan(self):
        """Read-only on-demand viewer; recompute from the live profile on request."""
        dialog = QDialog(self)
        dialog.setWindowTitle('mo2s2 — Launch plan')
        dialog.resize(930, 610)
        layout = QVBoxLayout(dialog)
        intro = QLabel('Fresh FileMapper preview for the active MO2 profile. '
                       'This does not change load order, files or mod states.', dialog)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        viewer = QPlainTextEdit(dialog)
        viewer.setReadOnly(True)
        viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(viewer)
        bar = QHBoxLayout()
        recheck = QPushButton('Recheck', dialog)
        copy = QPushButton('Copy plan', dialog)
        bar.addWidget(recheck)
        bar.addWidget(copy)
        bar.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
        buttons.rejected.connect(dialog.reject)
        bar.addWidget(buttons)
        layout.addLayout(bar)

        def update():
            try:
                viewer.setPlainText(self._game.preview_launch_plan())
            except Exception as exc:
                viewer.setPlainText(f'Unable to calculate launch plan: {exc!r}')
        recheck.clicked.connect(update)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(viewer.toPlainText()))
        update()
        dialog.exec()

    def _select_left_mods(self, names, *, force=False):
        """Select owning mods through MO2 rather than assuming stable view rows."""
        if (not names or self._syncing_from_left or
                (not force and not self._game._setting('Sync archive selection', True))):
            return
        try:
            self._syncing_to_left = True
            # The native MO2 main window owns the left ModListView named
            # "modList". QAbstractItemView exposes all standard selection APIs.
            view = self.window().findChild(QAbstractItemView, 'modList')
            modlist = self._organizer.modList()
            display_name = getattr(modlist, 'displayName', None)
            if callable(display_name):
                names = {str(display_name(name) or name) for name in names}
            select_visible_mods(view, names, Qt, QItemSelectionModel)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            logger = getattr(self._game, '_log', None)
            if callable(logger):
                logger('DEBUG', f'Could not synchronize left mod selection: {exc}')
        finally:
            self._syncing_to_left = False

    def _selected_archive_packages(self):
        keys = [item.key for idx in self.table.selectionModel().selectedRows()
                if (item := self._row_item(idx.row())) is not None]
        return selected_packages(self._by_key, keys)

    def _copy_archive_sources(self):
        items = self._selected_archive_packages()
        paths = source_paths(items)
        if paths:
            QApplication.clipboard().setText('\n'.join(paths))
            self.status.setText(f'Copied {len(paths)} physical package path(s).')

    def _copy_archive_virtual(self):
        items = self._selected_archive_packages()
        paths = virtual_paths(items, self._link_index)
        if paths:
            QApplication.clipboard().setText('\n'.join(paths))
            self.status.setText(f'Copied {len(paths)} mapped virtual destination(s).')
        else:
            self.status.setText('No mapped virtual paths for the selection.')

    def _open_local_folder(self, folder):
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            self.status.setText(f'Could not open folder: {folder}')

    def _open_archive_source(self):
        items = self._selected_archive_packages()
        if len(items) != 1:
            return
        try:
            self._open_local_folder(source_folder(items[0]))
        except (ValueError, OSError) as exc:
            self.status.setText(f'Cannot open source: {exc}')

    def _archives_menu(self, position):
        """Open contextual actions for the selected archive packages."""
        # Right click selects the package under the cursor, as standard MO2
        # tables do. Existing multi-selection is preserved if clicked inside.
        cell = self.table.itemAt(position)
        if cell is None:
            return
        if not cell.isSelected():
            self.table.selectRow(cell.row())
        items = self._selected_archive_packages()
        if not items:
            return
        menu = QMenu(self.table)
        if len(items) == 1:
            open_action = menu.addAction('Open source folder')
            open_action.triggered.connect(self._open_archive_source)
            menu.addSeparator()
        source_action = menu.addAction('Copy physical source path(s)')
        source_action.triggered.connect(self._copy_archive_sources)
        virtual_action = menu.addAction('Copy virtual destination(s)')
        virtual_action.setEnabled(bool(virtual_paths(items, self._link_index)))
        virtual_action.triggered.connect(self._copy_archive_virtual)
        mapping_action = menu.addAction('Copy selected mappings')
        mapping_action.triggered.connect(self.copy_selected_paths)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _diagnostics_menu(self, position):
        """Open actions appropriate to the selected diagnostic record."""
        cell = self.diag_table.itemAt(position)
        if cell is None:
            return
        self.diag_table.selectRow(cell.row())
        record = self._selected_diagnostic()
        if record is None:
            return
        menu = QMenu(self.diag_table)
        if record.origin == 'MO2':
            go = menu.addAction('Select mod in MO2')
            go.triggered.connect(lambda: self._select_left_mods({record.name}, force=True))
            try:
                mod = self._organizer.modList().getMod(record.name)
                if mod is not None:
                    folder = Path(mod.absolutePath())
                    from .virtual_core import _is_reparse
                    if not folder.is_dir() or _is_reparse(folder):
                        raise ValueError('Unsafe MO2 mod folder')
                    open_action = menu.addAction('Open mod folder')
                    open_action.triggered.connect(lambda: self._open_local_folder(folder))
            except (OSError, ValueError):
                pass
        elif record.origin == 'Game':
            menu.addAction('Open game ~mods', self.open_external_folder)
            if record.state == 'EXTERNAL':
                menu.addAction('Copy import instructions', self.copy_external_instructions)
        if record.repairable:
            menu.addAction('Repair selected…', self.repair_selected)
        menu.addSeparator()
        copy = menu.addAction('Copy diagnostic details')
        copy.triggered.connect(lambda: QApplication.clipboard().setText(
            f'{record.origin}: {record.name} [{record.state}]\n'
            f'{record.detail}\nSuggested: {record.recommendation}\n'
            f'Path: {record.path or "—"}'))
        menu.exec(self.diag_table.viewport().mapToGlobal(position))

    def copy_mapping_report(self):
        # Never infer mappings from table cells; use the same plan and warnings
        # that IPluginFileMapper receives when a game process is launched.
        report = format_mapping_report(self._items, self._links, self._warnings)
        QApplication.clipboard().setText(report)
        self.status.setText('mo2s2 mapping report copied to clipboard.')

    def _row_item(self, row):
        cell = self.table.item(row, 1)
        return self._by_key.get(cell.data(Qt.ItemDataRole.UserRole)) if cell else None

    def _show_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        mods = {item.mod_name for row in rows
                if (item := self._row_item(row)) is not None}
        if not self._refreshing and mods:
            self._select_left_mods(mods)
        if not self._refreshing:
            self._update_tool_actions()
        if len(rows) != 1:
            self.selected.setText(f'{len(rows)} selected packages' if rows else
                                  'Select a package to inspect its source and virtual files.')
            return
        item = self._row_item(rows[0])
        if item is None:
            return
        matches = self._link_index.get(item.key, [])
        state = item.warning or (f'{len(matches)} virtual files' if matches else 'Not mapped')
        extra_parts = sum(1 for p in item.companions
                          if p.suffix.casefold() == '.ucas' and
                          p.stem.casefold() != item.source.stem.casefold())
        extras = (f' | {extra_parts} extra UCAS partition(s)' if extra_parts else '')
        if '.sig' in item.files:
            extras += ' | signed PAK (.sig)'
        self.selected.setText(f'{item.mod_name} | {item.kind} | MO2 priority {item.priority} | {state}{extras}\n'
                              f'Source: {item.source}\n'
                              f'Virtual: {matches[0].destination if matches else "—"}')

    def copy_selected_paths(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        keys = {item.key for row in rows if (item := self._row_item(row)) is not None}
        if not keys:
            self.status.setText('Select one or more packages first.')
            return
        selected = [item for item in self._items if item.key in keys]
        aliases = [link for key in keys for link in self._link_index.get(key, [])]
        report = format_mapping_report(selected, aliases, [])
        QApplication.clipboard().setText(report)
        self.status.setText(f'Copied mappings for {len(keys)} selected packages.')

    def _apply_filter(self, *_):
        query = self.search.text().casefold().strip()
        kind_index = self.kind.currentIndex()
        for row in range(self.table.rowCount()):
            item = self._row_item(row)
            if item is None:
                self.table.setRowHidden(row, True)
                continue
            fmt = item.kind
            matches_kind = (
                kind_index == 0 or
                (kind_index == 1 and fmt == 'PAK') or
                (kind_index == 2 and fmt == 'ZONEKIT') or
                (kind_index == 3 and fmt in ('NESTED', 'FLAT_WRAPPER')) or
                (kind_index == 4 and package_state(item, self._mapped_keys) != 'MAPPED')
            )
            # Match virtual paths against the real FileMapper links, even
            # though Archives no longer renders them in a redundant column.
            matches_text = matches_archive_search(item, self._link_index, query)
            self.table.setRowHidden(row, not (matches_kind and matches_text))

    def finalize_pending(self):
        try:
            completed, moved, errors = self._game.finalize_pending_installs()
            self.refresh()
            summary = f'Finalized {completed} mod(s), {moved} staged paths.'
            if errors:
                summary += ' Failures: ' + '; '.join(errors[:5])
            self.status.setText(summary)
        except Exception as exc:
            self.status.setText(f'Could not finalize pending installs: {exc}')

    def recover_interrupted(self):
        """Offer explicit recovery for unfinished package transactions."""
        details = self._game.pending_transaction_details()
        if not details:
            self.status.setText('No interrupted or retained mo2s2 transactions found.')
            return
        # Non-journalled beta backups and corrupt journals cannot be mapped to
        # safe original paths. They are reported but never acted upon.
        summary = '\n'.join(
            f'{self._game._mod_name(mod)} / {txn.name}: {status} — {message}'
            for mod, txn, status, message in details[:15])
        if len(details) > 15:
            summary += f'\n...and {len(details) - 15} more'
        reply = QMessageBox.question(
            self, 'mo2s2 transaction recovery',
            'Back up the affected MO2 mod folders first.\n\n'
            'Prepared updates will be rolled back (new packages returned to '
            'their incoming folders); committed updates will keep their NEW '
            'versions and have old backups deleted. Unknown/legacy backups '
            'will not be touched.\n\n'
            + summary + '\n\nProceed with these actions?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            restored, cleaned, errors = self._game.recover_interrupted_updates()
            self.refresh()
            message = f'Rolled back {restored} transaction(s); cleaned {cleaned} backup(s).'
            if errors:
                message += ' Errors (left untouched): ' + '; '.join(errors[:5])
            self.status.setText(message)
        except Exception as exc:
            self.status.setText(f'Transaction recovery failed: {exc}')

    def force_vfs_refresh(self):
        try:
            self._organizer.refresh(False)
        except TypeError:
            self._organizer.refresh()
        self._refresh_timer.start(400)
