"""Contents and Conflicts pages inside MO2's existing Mod Tools tab.

All scanning and SQLite operations happen in a worker thread. Only a bounded
number of tree items is rendered on the GUI thread.
"""
from __future__ import annotations

from threading import Event

_SIGNATURE_UNREADABLE = object()

from PyQt6.QtCore import QObject, QThread, QTimer, Qt, QSignalBlocker, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QApplication, QMenu,
)

from .indexer import (CatalogCancelled, Snapshot, discover,
                      bounded_partner_preview, collision_losers, run_with_cache_recovery)
from .tree_actions import set_branch_expanded, navigation_targets


class _Worker(QObject):
    """Parse a catalog snapshot off the UI thread and emit bounded progress."""
    done = pyqtSignal(int, object, object)
    progress = pyqtSignal(int, int, int, str)

    def __init__(self, path, items, force, generation):
        super().__init__()
        self.path, self.items, self.force = path, list(items), force
        self.generation = generation
        self.cancelled = Event()
        self.recovery_notice = ''

    @pyqtSlot()
    def run(self):
        try:
            sources, errors = discover(self.items, is_cancelled=self.cancelled.is_set)
            if self.cancelled.is_set():
                raise CatalogCancelled()
            def build(db, recovered, notice):
                stats = db.sync(sources, force=self.force or recovered,
                    on_progress=lambda i, total, name: self.progress.emit(
                        self.generation, i, total, name),
                    is_cancelled=self.cancelled.is_set)
                if self.cancelled.is_set():
                    raise CatalogCancelled()
                snapshot = db.snapshot(sources, errors,
                    known_mods={item.mod_name for item in self.items},
                    is_cancelled=self.cancelled.is_set, repair=True)
                if db.last_cache_corrupt:
                    stats = (stats[0] + db.last_repaired,
                             max(0, stats[1] - db.last_cache_corrupt),
                             stats[2] + db.last_cache_corrupt - db.last_repaired)
                if recovered:
                    self.recovery_notice = notice or 'Damaged catalog cache quarantined and rebuilt'
                return snapshot, stats
            snapshot, stats = run_with_cache_recovery(
                self.path, build, is_cancelled=self.cancelled.is_set)
            if self.cancelled.is_set():
                raise CatalogCancelled()
            self.done.emit(self.generation, snapshot, stats)
        except CatalogCancelled:
            self.done.emit(self.generation, None, 'cancelled')
        except Exception as exc:
            self.done.emit(self.generation, None, str(exc))


class _View(QWidget):
    """Present resource contents and collisions through Qt-native tree widgets."""
    def __init__(self, parent, controller, conflicts=False):
        super().__init__(parent)
        self.controller = controller
        self.is_conflicts = conflicts
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel('Mod:', self))
        self.mod = QComboBox(self)
        self.mod.setMinimumWidth(155)
        self.mod.currentIndexChanged.connect(self.show_results)
        self.mod.activated.connect(self._mod_user_activated)
        toolbar.addWidget(self.mod, 1)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText('Filter resource paths or mod names…' if conflicts else
                                       'Search resource paths across indexed mods…')
        toolbar.addWidget(self.search, 2)
        if conflicts:
            self.mode = QComboBox(self)
            self.mode.addItems(['Overlapping files', 'Fully overwritten mods'])
            self.mode.currentIndexChanged.connect(self.show_results)
        layout.addLayout(toolbar)
        if conflicts:
            layout.addWidget(self.mode)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.show_results)
        self.search.textChanged.connect(lambda *_: self.timer.start())
        self.tree = QTreeWidget(self)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setHeaderLabels(['Resource path', 'Owners'] if not conflicts else
                                  ['Resource or mod', 'Winner', 'Losers'])
        self.tree.setColumnWidth(0, 430)
        self.tree.itemExpanded.connect(self._branch_toggled)
        self.tree.itemCollapsed.connect(self._branch_toggled)
        if not conflicts:
            self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.tree.customContextMenuRequested.connect(self._contents_context_menu)
        if conflicts:
            self.tree.itemDoubleClicked.connect(self._conflict_double_clicked)
            self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.tree.customContextMenuRequested.connect(self._conflict_context_menu)
        self._rendered_contents = False
        layout.addWidget(self.tree)
        foot = QHBoxLayout()
        if not conflicts:
            expand = QPushButton('Expand all', self)
            expand.setToolTip('Expand every visible branch. Shift+expand a branch expands its descendants.')
            expand.clicked.connect(self.tree.expandAll)
            foot.addWidget(expand)
            collapse = QPushButton('Collapse all', self)
            collapse.setToolTip('Collapse every visible branch. Shift+collapse a branch collapses its descendants.')
            collapse.clicked.connect(self.tree.collapseAll)
            foot.addWidget(collapse)
        self.status = QLabel('Indexing begins when this section is opened.', self)
        self.status.setWordWrap(True)
        foot.addWidget(self.status, 1)
        btn = QPushButton('Recheck indices', self)
        btn.setToolTip('Force rereading all archive directory indices')
        btn.clicked.connect(lambda: controller.request(controller.items, force=True))
        self.force = btn
        foot.addWidget(btn)
        report = QPushButton('Copy index report', self)
        report.setToolTip('Copy indexed container and conflict statistics, including parsing errors')
        report.clicked.connect(controller.copy_report)
        foot.addWidget(report)
        layout.addLayout(foot)

    def _mod_user_activated(self, *_):
        if not self.controller._ensure_current_snapshot():
            return
        self.controller._focus_pending = False  # Preserve the user's explicit catalog selection.
        mod = self.mod.currentData()
        if mod:
            self.controller.navigate_mods.emit((mod,))

    def _conflict_targets(self, item, section):
        if not self.controller._ensure_current_snapshot():
            return ()
        payload = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(payload, tuple) or not payload:
            return ()
        if payload[0] == 'mod':
            return (payload[1],) if section == 'owners' else ()
        if payload[0] == 'collision':
            return navigation_targets(payload[1], payload[2], section)
        return ()

    def _conflict_double_clicked(self, item, column):
        section = ('winner' if column == 1 else
                   'losers' if column == 2 else 'owners')
        targets = self._conflict_targets(item, section)
        if targets:
            self.controller.navigate_mods.emit(targets)

    def _contents_context_menu(self, pos):
        """Read-only resource actions using the indexed full path, not tree text."""
        if not self.controller._ensure_current_snapshot():
            return
        item = self.tree.itemAt(pos)
        if item is None:
            return
        payload = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(payload, tuple) or len(payload) != 3 or payload[0] != 'resource':
            return  # Folders and placeholders are not resource records.
        _, path, owners = payload
        menu = QMenu(self.tree)
        copy_path = menu.addAction('Copy resource path')
        copy_path.triggered.connect(
            lambda *_: (QApplication.clipboard().setText(path)
                       if self.controller._ensure_current_snapshot() else None))
        copy_owners = menu.addAction('Copy owning mods')
        copy_owners.triggered.connect(
            lambda *_: (QApplication.clipboard().setText('\n'.join(owners))
                       if self.controller._ensure_current_snapshot() else None))
        if owners:
            menu.addSeparator()
            go = menu.addAction('Select owning mods in MO2')
            go.triggered.connect(
                lambda *_: (self.controller.navigate_mods.emit(owners)
                           if self.controller._ensure_current_snapshot() else None))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _conflict_context_menu(self, pos):
        if not self.controller._ensure_current_snapshot():
            return
        item = self.tree.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self.tree)
        for label, section in (('Select winner in MO2', 'winner'),
                               ('Select losers in MO2', 'losers'),
                               ('Select all owners in MO2', 'owners')):
            targets = self._conflict_targets(item, section)
            if targets:
                action = menu.addAction(label)
                action.triggered.connect(
                    lambda checked=False, targets=targets: (
                        self.controller.navigate_mods.emit(targets)
                        if self.controller._ensure_current_snapshot() else None))
        if not menu.actions():
            return
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _branch_toggled(self, item):
        # Triggered by clicking the tree disclosure arrow while holding Shift.
        # Blocking signals avoids recursively reentering this slot and creating
        # thousands of Qt events on large catalog trees.
        if not QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
            return
        with QSignalBlocker(self.tree):
            self.tree.setUpdatesEnabled(False)
            try:
                set_branch_expanded(item, item.isExpanded())
            finally:
                self.tree.setUpdatesEnabled(True)

    def _expanded_paths(self):
        """Remember explicitly expanded folder branches across auto refresh."""
        result = set()
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item.childCount() and item.isExpanded():
                result.add(item.data(0, Qt.ItemDataRole.UserRole))
            for i in range(item.childCount()):
                stack.append(item.child(i))
        return result

    def set_snapshot(self, snapshot):
        before = self.controller._focused_mod if self.controller._focus_pending else self.mod.currentData()
        self.mod.blockSignals(True)
        self.mod.clear()
        self.mod.addItem('All active mods', None)
        for name in sorted(set(snapshot.mods) | set(snapshot.errors), key=str.casefold):
            self.mod.addItem(name, name)
        index = self.mod.findData(before)
        if index >= 0:
            self.mod.setCurrentIndex(index)
        self.mod.blockSignals(False)
        self.show_results()

    def show_results(self, *_):
        if not self.controller._ensure_current_snapshot():
            return
        snap = self.controller.snapshot
        if snap is None:
            return
        previous_expanded = (self._expanded_paths() if not self.is_conflicts
                             and self._rendered_contents else None)
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        mod = self.mod.currentData()
        needle = self.search.text().strip().casefold()
        if not self.is_conflicts:
            paths, count = snap.results(mod, needle, 800)
            self._tree_contents(snap, paths, previous_expanded)
            self._rendered_contents = True
            shown = len(paths)
        elif self.mode.currentIndex() == 1:
            self.tree.setHeaderLabels(['Fully overwritten mod', 'Resources', ''])
            names = [m for m in sorted(snap.mods, key=str.casefold)
                     if snap.fully_overwritten(m) is True and (mod is None or m == mod)
                     and needle in m.casefold()]
            count, shown = len(names), min(500, len(names))
            for name in names[:500]:
                node = QTreeWidgetItem(self.tree, [name, str(len(snap.mods[name])), ''])
                node.setData(0, Qt.ItemDataRole.UserRole, ('mod', name))
        else:
            if self.is_conflicts:
                self.tree.setHeaderLabels(['Resource or mod', 'Winner', 'Losers'])
            overlaps, count = snap.collision_results(mod, needle, limit=600)
            shown = len(overlaps)
            self._tree_conflicts(overlaps, mod)
        self.tree.setUpdatesEnabled(True)
        errors = list(snap.errors.get(mod, ())) if mod else [
            f'{m}: {e}' for m, issues in snap.errors.items() for e in issues]
        errors.extend([e for e in snap.discovery_errors if mod is None or e.startswith(mod + ':')])
        suffix = (f' · {len(errors)} unindexed/invalid: {errors[0][:115]}' if errors else '')
        self.status.setText(f'{self.controller.info} · {count} results (shown {shown})' + suffix)

    def _tree_contents(self, snapshot, paths, expanded=None):
        folders = {}
        for path in paths:
            parts = path.split('/')
            parent = self.tree.invisibleRootItem()
            key = ''
            for part in parts[:-1]:
                key += '/' + part.casefold()
                if key not in folders:
                    folder = QTreeWidgetItem(parent, [part, ''])
                    folder.setData(0, Qt.ItemDataRole.UserRole, key)
                    folders[key] = folder
                parent = folders[key]
            owner_names = snapshot.owner_names(path)
            item = QTreeWidgetItem(parent, [parts[-1], ', '.join(owner_names)])
            item.setData(0, Qt.ItemDataRole.UserRole, ('resource', path, tuple(owner_names)))
            item.setToolTip(0, path)
        if expanded is None:
            if len(paths) <= 100:
                self.tree.expandAll()
        else:
            with QSignalBlocker(self.tree):
                for key in expanded:
                    if key in folders:
                        folders[key].setExpanded(True)

    def _tree_conflicts(self, overlaps, mod):
        if mod is None:
            for c in overlaps:
                node = QTreeWidgetItem(self.tree, [c.path, c.winner or 'Undetermined',
                                                    collision_losers(c)])
                node.setData(0, Qt.ItemDataRole.UserRole, ('collision', c.owners, c.winner))
            return
        groups, partners_total, truncated = bounded_partner_preview(overlaps, mod)
        for partner, entries, count in groups:
            heading_label = (f'{partner} ({count})' if len(entries) == count else
                             f'{partner} ({len(entries)} of {count} shown)')
            heading = QTreeWidgetItem(self.tree, [heading_label, '', ''])
            heading.setData(0, Qt.ItemDataRole.UserRole, ('mod', partner))
            for c in entries:
                node = QTreeWidgetItem(heading, [c.path, c.winner or 'Undetermined',
                                                  collision_losers(c)])
                node.setData(0, Qt.ItemDataRole.UserRole, ('collision', c.owners, c.winner))
        if truncated:
            self.tree.invisibleRootItem().addChild(QTreeWidgetItem(
                [f'Display limited: {len(groups)} of {partners_total} partner mods',
                 '', 'Use search to narrow overlaps']))


class CatalogTabs(QObject):
    """Shared catalog worker, two independent Qt-native views, no tool windows."""
    navigate_mods = pyqtSignal(object)

    def __init__(self, parent, game):
        super().__init__(parent)
        self.game = game
        self.contents = _View(parent, self)
        self.conflicts = _View(parent, self, conflicts=True)
        self.snapshot: Snapshot | None = None
        self.info = ''
        self.items = []
        self._focused_mod = None
        self._focus_pending = False
        self._pending = None
        self._working = False
        self._thread = None
        self._worker = None
        self._closing = False
        self._generation = 0
        self._catalog_signature = None
        self._pending_signature = None
        self._deferred_signature = None
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown)
        # QThread is a child of this controller. If MO2 closes while a large
        # TOC is still parsing, QObject would otherwise destroy a live thread.
        parent.destroyed.connect(self._shutdown)

    def _live_signature(self):
        """Read cheap MO2 identity; never inspect PAK payloads in the UI."""
        getter = getattr(self.game, 'mod_signature', None)
        if not callable(getter):
            return None  # Allows host-free unit-test stubs.
        try:
            return getter()
        except (OSError, RuntimeError, ValueError, TypeError):
            return _SIGNATURE_UNREADABLE

    def _signature_current(self):
        if self._catalog_signature is None:
            return True  # Older host/test doubles without identity API.
        return self._live_signature() == self._catalog_signature

    def _ensure_current_snapshot(self):
        """Reject stale Contents/Conflicts even before MO2 emits its callback."""
        if self.snapshot is None:
            return False
        if self._signature_current():
            return True
        self.invalidate(self.items)
        return False

    def focus_mod(self, name):
        """Follow one left-pane mod (or All for multi-selection), no tab switching."""
        self._focused_mod = name
        self._focus_pending = True
        if not self._ensure_current_snapshot():
            return
        for view in (self.contents, self.conflicts):
            index = view.mod.findData(name)
            if index >= 0 and index != view.mod.currentIndex():
                view.mod.setCurrentIndex(index)

    def _shutdown(self, *_):
        # Qt can signal both application shutdown and parent destruction.
        if self._closing:
            return
        self._closing = True
        self._pending = None
        self._pending_signature = None
        self._deferred_signature = None
        # Keep a local reference: finished may be delivered synchronously
        # during quit() and clear self._thread before wait() is reached.
        thread = self._thread
        worker = self._worker
        if thread is not None and thread.isRunning():
            if worker is not None:
                worker.cancelled.set()
            thread.quit()
            thread.wait()

    def _same_index_request(self, items, *, force, signature):
        """Do not cancel an identical in-flight index or lose queued force scans.

        Identity includes both the MO2 profile signature and discovered packages:
        a repaired physical mod can change without a profile-order change.
        Unreadable signatures never authorize reuse of cached work.
        """
        if (not self._working or signature is None or
                signature is _SIGNATURE_UNREADABLE):
            return False
        if self._pending is not None:
            pending_items, pending_force = self._pending
            if items != pending_items or signature != self._pending_signature:
                return False
            if force and not pending_force:
                self._pending = (pending_items, True)
            return True
        if signature != self._catalog_signature or items != self.items:
            return False
        worker = self._worker
        return (worker is not None and not worker.cancelled.is_set() and
                (not force or worker.force))

    def request(self, items, *, force=False, signature=None):
        """Schedule the latest catalog refresh and supersede obsolete workers."""
        if self._closing:
            return
        requested = list(items)
        identity = signature if signature is not None else self._live_signature()
        if self._same_index_request(requested, force=force, signature=identity):
            return
        self._deferred_signature = None
        self.items = requested
        self._catalog_signature = identity
        self._generation += 1  # Reject late progress/results from previous jobs.
        generation = self._generation
        self.snapshot = None
        for view in (self.contents, self.conflicts):
            view.tree.clear()
        if self._working:
            # If a forced reindex was already queued, an ordinary event must
            # not downgrade it while the old worker is being cancelled.
            queued_force = self._pending[1] if self._pending else False
            self._pending = (self.items, force or queued_force)
            self._pending_signature = identity
            if self._worker is not None:
                self._worker.cancelled.set()
            for view in (self.contents, self.conflicts):
                view.status.setText('Profile changed; cancelling outdated index task…')
            return
        self._working = True
        for view in (self.contents, self.conflicts):
            view.force.setEnabled(False)
            view.status.setText('Checking cached indices…')
        thread = self._thread = QThread(self)
        worker = self._worker = _Worker(self.game.catalog_path(), self.items,
                                       force, generation)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._progress)
        worker.done.connect(self._finished)
        # Quit must not depend on the GUI thread processing a queued signal:
        # shutdown can block the GUI while waiting for this worker to finish.
        worker.done.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: self._thread_ended(thread))
        thread.start()

    def invalidate(self, items):
        """Defer indexing from Archives, discarding only changed inventories."""
        if self._closing:
            return
        requested = list(items)
        identity = self._live_signature()
        if (identity is not None and identity is not _SIGNATURE_UNREADABLE
                and requested == self.items):
            if self._working:
                if self._pending is not None:
                    if (requested == self._pending[0] and
                            identity == self._pending_signature):
                        return
                elif (identity == self._catalog_signature and
                        self._worker is not None and
                        not self._worker.cancelled.is_set()):
                    return
            elif ((self.snapshot is not None and
                   identity == self._catalog_signature) or
                  (self.snapshot is None and
                   identity == self._deferred_signature)):
                return

        self.items = requested
        self._deferred_signature = identity
        self._catalog_signature = None
        self._generation += 1
        self.snapshot = None
        for view in (self.contents, self.conflicts):
            view.tree.clear()
            view.status.setText('Mod list changed; open Contents or Conflicts to update.')
        if self._working:
            queued_force = self._pending[1] if self._pending else False
            self._pending = (self.items, queued_force)
            self._pending_signature = identity
            if self._worker is not None:
                self._worker.cancelled.set()

    def copy_report(self):
        if self.snapshot is not None and not self._ensure_current_snapshot():
            return
        if self.snapshot is None:
            for view in (self.contents, self.conflicts):
                view.status.setText('No catalog snapshot yet. Open Contents and wait for indexing.')
            return
        QApplication.clipboard().setText(self.snapshot.diagnostic_report())

    def _progress(self, generation, index, total, name):
        if (self._closing or generation != self._generation or
                self._pending is not None):
            return
        if total and (index % 10 == 0 or index == total):
            for view in (self.contents, self.conflicts):
                view.status.setText(f'Indexing: {index}/{total} · {name}')

    def _finished(self, generation, snapshot, stats):
        """Accept results only from the currently active worker generation."""
        # Qt can deliver an old done() even after the new thread has started.
        # Only the current generation can ever publish a Snapshot.
        if (self._closing or generation != self._generation or
                self._pending is not None):
            return
        if not self._signature_current():
            self.invalidate(self.items)
            return
        if snapshot is None:
            if stats != 'cancelled':
                logger = getattr(self.game, '_log', None)
                if callable(logger):
                    logger('ERROR', f'Resource catalog failed: {stats}')
            for view in (self.contents, self.conflicts):
                view.status.setText(f'Catalog failed: {stats}')
            return
        self.snapshot = snapshot
        if self._worker is not None and self._worker.recovery_notice:
            logger_recovery = getattr(self.game, '_log', None)
            if callable(logger_recovery):
                logger_recovery('WARNING', self._worker.recovery_notice)
        logger = getattr(self.game, '_log_unique', None)
        if callable(logger):
            for mod_name, messages in snapshot.errors.items():
                for message in messages:
                    logger('ERROR', f'Catalog index for {mod_name}: {message}')
            for error in snapshot.discovery_errors:
                logger('ERROR', f'Catalog discovery: {error}')
        read, reused, errors = stats
        self.info = (f'{read} scanned, {reused} cached, {errors} failed this pass; '
                     f'{sum(map(len, snapshot.errors.values())) + len(snapshot.discovery_errors)} incomplete')
        for view in (self.contents, self.conflicts):
            view.set_snapshot(snapshot)

    def _thread_ended(self, thread=None):
        # A queued Qt signal from an older thread must not clear the newer one.
        if thread is not None and thread is not self._thread:
            return
        self._working = False
        self._thread = self._worker = None
        if self._closing:
            return
        for view in (self.contents, self.conflicts):
            view.force.setEnabled(True)
        if self._pending:
            items, force = self._pending
            signature = self._pending_signature
            self._pending = None
            self._pending_signature = None
            self.request(items, force=force, signature=signature)
