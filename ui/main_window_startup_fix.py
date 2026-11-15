"""Optional folder/group UI installed over the stable MainWindow implementation.

Folders are identified by a hidden marker file so folder-save slots remain
unambiguous. Existing ungrouped and legacy save_data slots continue to load.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QSignalBlocker
from PySide6.QtWidgets import (
    QAbstractItemView, QInputDialog, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
)

_MARKER = ".fromsave-folder"


def _install_storage() -> None:
    import storage

    if getattr(storage, "_folder_extension_installed", False):
        return
    original_import = storage.import_save
    original_duplicate = storage.duplicate_slot
    original_copy_profile = storage.copy_slot_to_profile

    def load_profiles(game: str) -> list[str]:
        root = storage.SAVES_DIR / game
        if not game or not root.exists():
            return []
        return sorted(p.name for p in root.iterdir()
                      if p.is_dir() and not (p / _MARKER).is_file())

    def load_groups(game: str, profile: str) -> list[str]:
        root = storage.SAVES_DIR / game / profile
        if not root.exists():
            return []
        return sorted(p.name for p in root.iterdir()
                      if p.is_dir() and (p / _MARKER).is_file())

    def create_group(game: str, profile: str, name: str) -> Path:
        name = storage.validate_entry_name(name)
        folder = storage.SAVES_DIR / game / profile / name
        folder.mkdir(parents=True, exist_ok=False)
        (folder / _MARKER).write_text("FromSave folder\n", encoding="utf-8")
        return folder

    def rename_group(game: str, profile: str, old: str, new: str) -> None:
        new = storage.validate_entry_name(new)
        if old not in load_groups(game, profile):
            raise ValueError(f"Folder does not exist: {old}")
        source = storage.SAVES_DIR / game / profile / old
        target = source.parent / new
        if target.exists() and target != source:
            raise FileExistsError(f"A folder named '{new}' already exists.")
        source.rename(target)

    def _read_slot_with_group(path: Path, game: str, profile: str, group: str):
        slot = storage._read_slot(path, game, profile)
        if slot is not None:
            slot.group = group
        return slot

    def load_slots(game: str, profile: str):
        root = storage.SAVES_DIR / game / profile
        if not root.exists():
            return []
        slots = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            if (entry / _MARKER).is_file():
                for child in sorted(entry.iterdir()):
                    if child.is_dir():
                        slot = _read_slot_with_group(child, game, profile, entry.name)
                        if slot:
                            slots.append(slot)
            else:
                slot = _read_slot_with_group(entry, game, profile, "")
                if slot:
                    slots.append(slot)
        return slots

    def import_save(game: str, profile: str, name: str, game_cfg, group: str = ""):
        if not group:
            return original_import(game, profile, name, game_cfg)
        parent = storage.SAVES_DIR / game / profile / group
        if not (parent / _MARKER).is_file():
            raise ValueError(f"Folder does not exist: {group}")
        slot_dir = parent / storage.validate_entry_name(name)
        slot_dir.mkdir(parents=True, exist_ok=False)
        try:
            storage._copy_live_save(game_cfg, slot_dir)
            now = storage.datetime.now() if hasattr(storage, "datetime") else __import__("datetime").datetime.now()
            storage._write_json(slot_dir / "meta.json", storage._timestamps(now, now))
            (slot_dir / "notes.txt").write_text("", encoding="utf-8")
            return _read_slot_with_group(slot_dir, game, profile, group)
        except Exception:
            import shutil
            shutil.rmtree(slot_dir, ignore_errors=True)
            raise

    def move_slot_to_group(slot, group: str) -> None:
        group = storage.validate_entry_name(group) if group else ""
        if group and group not in load_groups(slot.game, slot.profile):
            raise ValueError(f"Folder does not exist: {group}")
        target_parent = storage.SAVES_DIR / slot.game / slot.profile
        if group:
            target_parent /= group
        target = target_parent / slot.name
        if target == slot.path:
            return
        if target.exists():
            raise FileExistsError(f"A save named '{slot.name}' already exists there.")
        slot.path.rename(target)
        slot.path = target
        slot.group = group

    def duplicate_slot(slot, new_name: str):
        target = slot.path.parent / storage.validate_entry_name(new_name)
        import shutil
        shutil.copytree(slot.path, target)
        return _read_slot_with_group(target, slot.game, slot.profile, getattr(slot, "group", ""))

    def copy_slot_to_profile(slot, target_profile: str, new_name: str):
        target_parent = storage.SAVES_DIR / slot.game / target_profile
        group = getattr(slot, "group", "")
        if group:
            target_parent /= group
            target_parent.mkdir(parents=True, exist_ok=True)
            (target_parent / _MARKER).touch(exist_ok=True)
        target = target_parent / storage.validate_entry_name(new_name)
        import shutil
        shutil.copytree(slot.path, target)
        return _read_slot_with_group(target, slot.game, target_profile, group)

    storage.load_profiles = load_profiles
    storage.load_groups = load_groups
    storage.create_group = create_group
    storage.rename_group = rename_group
    storage.load_slots = load_slots
    storage.import_save = import_save
    storage.move_slot_to_group = move_slot_to_group
    storage.duplicate_slot = duplicate_slot
    storage.copy_slot_to_profile = copy_slot_to_profile
    storage._folder_extension_installed = True


class _FolderList(QListWidget):
    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)

    def dropEvent(self, event):
        item = self.currentItem()
        slot = item.data(Qt.UserRole) if item else None
        if slot is None or item.data(Qt.UserRole + 1) == "folder":
            event.ignore()
            return
        target_item = self.itemAt(event.position().toPoint())
        if target_item is None:
            target_group = ""
        elif target_item.data(Qt.UserRole + 1) == "folder":
            target_group = target_item.data(Qt.UserRole + 2) or ""
        else:
            target_slot = target_item.data(Qt.UserRole)
            target_group = getattr(target_slot, "group", "") if target_slot else ""
        try:
            import storage
            storage.move_slot_to_group(slot, target_group)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot move save", str(exc))
            event.ignore()
            return
        event.acceptProposedAction()
        self.owner._active_group = target_group
        self.owner._reload_slots(slot.name)


def install() -> None:
    _install_storage()
    import storage
    from ui.main_window import MainWindow, _SlotItem

    if getattr(MainWindow, "_folder_extension_installed", False):
        return
    original_build = MainWindow._build_slot_panel
    original_context = MainWindow._on_slot_context_menu

    def build_slot_panel(self):
        panel = original_build(self)
        layout = panel.layout()
        old = self.slot_list
        old_index = layout.indexOf(old)
        old.currentRowChanged.disconnect(self._on_slot_selected)
        old.model().rowsMoved.disconnect(self._on_rows_moved)
        old.customContextMenuRequested.disconnect(self._on_slot_context_menu)
        old.removeEventFilter(self)
        layout.removeWidget(old)
        self.slot_list = _FolderList(self, panel)
        self.slot_list.currentRowChanged.connect(self._on_slot_selected)
        self.slot_list.model().rowsMoved.connect(self._on_rows_moved)
        self.slot_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.slot_list.customContextMenuRequested.connect(self._on_slot_context_menu)
        self.slot_list.itemClicked.connect(self._on_folder_clicked)
        self.slot_list.installEventFilter(self)
        layout.insertWidget(old_index, self.slot_list, 1)
        old.deleteLater()
        self.folder_btn = QPushButton("+Folder")
        self.folder_btn.setObjectName("ghostBtn")
        self.folder_btn.clicked.connect(self._on_new_folder)
        layout.insertWidget(old_index, self.folder_btn)
        self._collapsed_groups = set()
        self._active_group = ""
        self._row_to_slot = {}
        return panel

    def on_new_folder(self):
        game, profile = self.game_combo.currentText(), self.profile_combo.currentText()
        if not game or not profile:
            self._notify("Select a game and profile first.")
            return
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if not ok or not name.strip():
            return
        try:
            name = storage.validate_entry_name(name)
            storage.create_group(game, profile, name)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot create folder", str(exc))
            return
        self._active_group = name
        self._reload_slots()
        self._notify(f"Created folder '{name}'.")

    def on_folder_clicked(self, item):
        if item.data(Qt.UserRole + 1) != "folder":
            return
        group = item.data(Qt.UserRole + 2) or ""
        if group in self._collapsed_groups:
            self._collapsed_groups.remove(group)
        else:
            self._collapsed_groups.add(group)
        self._reload_slots(self._current_slot_name())

    def selected_slot(self):
        return self._row_to_slot.get(self.slot_list.currentRow())

    def reload_slots(self, select_slot: str = ""):
        game, profile = self.game_combo.currentText(), self.profile_combo.currentText()
        self._slots = (storage.load_sorted_slots(game, profile, self._config.slot_sort,
                                                self._config.slot_sort_desc)
                       if game and profile else [])
        folders = storage.load_groups(game, profile) if game and profile else []
        grouped, ungrouped = {}, []
        for slot in self._slots:
            group = getattr(slot, "group", "")
            (grouped.setdefault(group, []).append(slot) if group else ungrouped.append(slot))
        self._row_to_slot = {}
        self.slot_list.setDragDropMode(QAbstractItemView.InternalMove
                                       if self._config.slot_sort == "custom" and not folders
                                       else QAbstractItemView.InternalMove)
        with QSignalBlocker(self.slot_list):
            self.slot_list.clear()
            for group in folders:
                expanded = group not in self._collapsed_groups
                head = QListWidgetItem(("📁 ▾ " if expanded else "📁 ▸ ") + group)
                head.setData(Qt.UserRole + 1, "folder")
                head.setData(Qt.UserRole + 2, group)
                head.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.slot_list.addItem(head)
                self._row_to_slot[self.slot_list.count() - 1] = None
                for slot in grouped.get(group, []) if expanded else []:
                    row = QListWidgetItem()
                    row.setData(Qt.UserRole, slot)
                    row.setSizeHint(QSize(0, 34))
                    self.slot_list.addItem(row)
                    self.slot_list.setItemWidget(row, _SlotItem(slot.name))
                    self._row_to_slot[self.slot_list.count() - 1] = slot
            for slot in ungrouped:
                row = QListWidgetItem()
                row.setData(Qt.UserRole, slot)
                row.setSizeHint(QSize(0, 34))
                self.slot_list.addItem(row)
                self.slot_list.setItemWidget(row, _SlotItem(slot.name))
                self._row_to_slot[self.slot_list.count() - 1] = slot
        selected = next((i for i, slot in self._row_to_slot.items()
                         if slot is not None and slot.name == select_slot), None)
        if selected is None:
            selected = next((i for i, slot in self._row_to_slot.items() if slot is not None), -1)
        self.slot_list.setCurrentRow(selected)
        if selected >= 0:
            self._on_slot_selected(selected)
        else:
            self._clear_detail()

    def slot_selected(self, row: int):
        slot = self._row_to_slot.get(row)
        if slot is None:
            self._flush_notes()
            self._flush_video()
            self._clear_detail()
            return
        self._current_slot = slot
        self.rename_btn.setEnabled(True)
        self.detail_name.setText(slot.name)
        self.detail_time.setText(slot.date_modified.strftime("%Y-%m-%d  %H:%M:%S")
                                 if slot.date_modified else "—")
        self.detail_notes.blockSignals(True)
        self.detail_notes.setPlainText(slot.notes)
        self.detail_notes.blockSignals(False)
        self._set_video_url(slot.video_url)
        self.info_created.set_value(slot.date_created.strftime("%Y-%m-%d  %H:%M:%S")
                                    if slot.date_created else "—")
        self.info_modified.set_value(slot.date_modified.strftime("%Y-%m-%d  %H:%M:%S")
                                     if slot.date_modified else "—")
        files = storage.slot_save_files(slot)
        size = sum(f.stat().st_size for f in files) if files else 0
        self.info_file_size.set_value(_format_size(size) if files else "—")
        self._maybe_switch_practice_slot(slot)
        self._sync_ro_button()
        self._refresh_overlay()

    def context_menu(self, pos):
        item = self.slot_list.itemAt(pos)
        if item and item.data(Qt.UserRole + 1) == "folder":
            folder = item.data(Qt.UserRole + 2)
            menu = __import__("PySide6.QtWidgets", fromlist=["QMenu"]).QMenu(self)
            menu.addAction("Rename", lambda: self._rename_folder(folder))
            menu.exec(self.slot_list.mapToGlobal(pos))
            return
        return original_context(self, pos)

    def rename_folder(self, old):
        name, ok = QInputDialog.getText(self, "Rename folder", "New folder name:", text=old)
        if not ok or not name.strip() or name.strip() == old:
            return
        try:
            storage.rename_group(self.game_combo.currentText(), self.profile_combo.currentText(), old, name)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot rename folder", str(exc))
            return
        if self._active_group == old:
            self._active_group = name.strip()
        if old in self._collapsed_groups:
            self._collapsed_groups.remove(old)
            self._collapsed_groups.add(name.strip())
        self._reload_slots()

    def import_as(self, cfg, profile, name):
        group = self._active_group
        parent = storage.SAVES_DIR / cfg.name / profile / group if group else storage.SAVES_DIR / cfg.name / profile
        if (parent / name).exists():
            self._notify(f"A save named '{name}' already exists here.")
            return False
        try:
            storage.import_save(cfg.name, profile, name, cfg, group=group)
        except Exception as exc:
            self._notify(f"Import failed: {exc}")
            return False
        self._reload_slots(name)
        self._notify(f"Imported '{name}'.")
        return True

    def on_import(self):
        cfg = self._get_game_cfg()
        if not cfg or not self._validate_game_save_path(cfg):
            return
        profile = self.profile_combo.currentText()
        if not profile:
            self._notify("No profile selected.")
            return
        group = self._active_group
        if self._config.auto_name_imports:
            name = storage.auto_slot_name(cfg.name, profile, group)
        else:
            name, ok = QInputDialog.getText(self, "Import Save", "Slot name:")
            if not ok or not name.strip():
                return
            name = storage.validate_entry_name(name)
        self._import_as(cfg, profile, name)

    MainWindow._build_slot_panel = build_slot_panel
    MainWindow._on_new_folder = on_new_folder
    MainWindow._on_folder_clicked = on_folder_clicked
    MainWindow._selected_slot = selected_slot
    MainWindow._reload_slots = reload_slots
    MainWindow._on_slot_selected = slot_selected
    MainWindow._on_slot_context_menu = context_menu
    MainWindow._rename_folder = rename_folder
    MainWindow._import_as = import_as
    MainWindow._on_import_save = on_import
    MainWindow._folder_extension_installed = True
