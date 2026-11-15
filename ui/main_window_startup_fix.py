"""Optional folder and drag/drop support layered onto the stable MainWindow UI."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QSignalBlocker
from PySide6.QtWidgets import (
    QAbstractItemView, QInputDialog, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
)


_MARKER = ".fromsave-folder"


def _install_storage_groups() -> None:
    import storage

    original_import_save = storage.import_save

    def load_profiles(game: str) -> list[str]:
        game_dir = storage.SAVES_DIR / game
        if not game or not game_dir.exists():
            return []
        return sorted(p.name for p in game_dir.iterdir()
                      if p.is_dir() and not (p / _MARKER).is_file())

    def load_groups(game: str, profile: str) -> list[str]:
        profile_dir = storage.SAVES_DIR / game / profile
        if not profile_dir.exists():
            return []
        return sorted(p.name for p in profile_dir.iterdir()
                      if p.is_dir() and (p / _MARKER).is_file())

    def create_group(game: str, profile: str, name: str) -> Path:
        name = storage.validate_entry_name(name)
        folder = storage.SAVES_DIR / game / profile / name
        folder.mkdir(parents=True, exist_ok=False)
        (folder / _MARKER).write_text("FromSave folder\n", encoding="utf-8")
        return folder

    def load_slots(game: str, profile: str):
        profile_dir = storage.SAVES_DIR / game / profile
        if not profile_dir.exists():
            return []
        slots = []
        for entry in sorted(profile_dir.iterdir()):
            if not entry.is_dir():
                continue
            if (entry / _MARKER).is_file():
                for slot_dir in sorted(entry.iterdir()):
                    if not slot_dir.is_dir():
                        continue
                    slot = storage._read_slot(slot_dir, game, profile)
                    if slot is not None:
                        slot.group = entry.name
                        slots.append(slot)
            else:
                slot = storage._read_slot(entry, game, profile)
                if slot is not None:
                    slot.group = ""
                    slots.append(slot)
        return slots

    def load_sorted_slots(game: str, profile: str, sort: str, desc: bool):
        slots = load_slots(game, profile)
        if sort == "name":
            slots.sort(key=lambda slot: (slot.group.casefold(), slot.name.casefold()), reverse=desc)
        elif sort == "created":
            slots.sort(key=lambda slot: slot.date_created or datetime.min, reverse=desc)
        elif sort == "modified":
            slots.sort(key=lambda slot: slot.date_modified or slot.date_created or datetime.min,
                       reverse=desc)
        elif sort == "custom":
            order = {name: index for index, name in
                     enumerate(storage.load_slot_order(game, profile))}
            slots.sort(key=lambda slot: order.get(slot.name, len(order)))
        return slots

    def auto_slot_name(game: str, profile: str, group: str = "") -> str:
        parent = storage.SAVES_DIR / game / profile / group if group else storage.SAVES_DIR / game / profile
        return storage._unique_name(parent, "new save")

    def import_save(game: str, profile: str, slot_name: str, game_cfg, group: str = ""):
        if not group:
            return original_import_save(game, profile, slot_name, game_cfg)
        parent = storage.SAVES_DIR / game / profile / group
        if not (parent / _MARKER).is_file():
            raise ValueError(f"Folder does not exist: {group}")
        slot_dir = parent / storage.validate_entry_name(slot_name)
        slot_dir.mkdir(parents=True, exist_ok=False)
        try:
            storage._copy_live_save(game_cfg, slot_dir)
            now = datetime.now()
            storage._write_json(slot_dir / "meta.json", storage._timestamps(now, now))
            (slot_dir / "notes.txt").write_text("", encoding="utf-8")
            slot = storage._read_slot(slot_dir, game, profile)
            if slot is not None:
                slot.group = group
            return slot
        except Exception:
            import shutil
            shutil.rmtree(slot_dir, ignore_errors=True)
            raise

    def move_slot_to_group(slot, group: str) -> None:
        """Move a slot directory to the selected folder, or to the profile root."""
        target_group = storage.validate_entry_name(group) if group else ""
        if target_group and target_group not in load_groups(slot.game, slot.profile):
            raise ValueError(f"Folder does not exist: {target_group}")
        target_parent = storage.SAVES_DIR / slot.game / slot.profile
        if target_group:
            target_parent /= target_group
        target = target_parent / slot.name
        if target == slot.path:
            return
        if target.exists():
            raise FileExistsError(f"A save named '{slot.name}' already exists in that folder.")
        target_parent.mkdir(parents=True, exist_ok=True)
        slot.path.rename(target)
        slot.path = target
        slot.group = target_group

    storage.load_profiles = load_profiles
    storage.load_groups = load_groups
    storage.create_group = create_group
    storage.load_slots = load_slots
    storage.load_sorted_slots = load_sorted_slots
    storage.auto_slot_name = auto_slot_name
    storage.import_save = import_save
    storage.move_slot_to_group = move_slot_to_group


class _FolderSaveList(QListWidget):
    """Internal drag/drop list that moves save directories into folder rows."""

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self._owner = owner
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def dropEvent(self, event) -> None:
        source_item = self.currentItem()
        if source_item is None or source_item.data(Qt.UserRole + 1) == "folder":
            event.ignore()
            return
        slot = source_item.data(Qt.UserRole)
        if slot is None:
            event.ignore()
            return

        position = event.position().toPoint()
        target_item = self.itemAt(position)
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
        self._owner._active_group = target_group
        self._owner._reload_slots(slot.name)


def install() -> None:
    """Attach folder support before MainWindow is instantiated; leave its builders intact."""
    _install_storage_groups()
    from ui.main_window import MainWindow

    original_build_slot_panel = MainWindow._build_slot_panel
    original_import_as = MainWindow._import_as
    original_on_import_save = MainWindow._on_import_save

    def build_slot_panel(self):
        panel = original_build_slot_panel(self)
        old_list = self.slot_list
        layout = panel.layout()
        old_index = layout.indexOf(old_list)
        old_list.currentRowChanged.disconnect(self._on_slot_selected)
        old_list.model().rowsMoved.disconnect(self._on_rows_moved)
        old_list.customContextMenuRequested.disconnect(self._on_slot_context_menu)
        old_list.removeEventFilter(self)
        layout.removeWidget(old_list)

        self.slot_list = _FolderSaveList(self, panel)
        self.slot_list.setAlternatingRowColors(False)
        self.slot_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.slot_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.slot_list.currentRowChanged.connect(self._on_slot_selected)
        self.slot_list.model().rowsMoved.connect(self._on_rows_moved)
        self.slot_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.slot_list.customContextMenuRequested.connect(self._on_slot_context_menu)
        self.slot_list.installEventFilter(self)
        layout.insertWidget(max(old_index, 0), self.slot_list, 1)
        old_list.deleteLater()

        self.folder_btn = QPushButton("+Folder")
        self.folder_btn.setObjectName("ghostBtn")
        self.folder_btn.setToolTip("Create a collapsible folder for saves")
        self.folder_btn.clicked.connect(self._on_new_folder)
        layout.insertWidget(max(old_index, 0), self.folder_btn)
        self._collapsed_groups = set()
        self._active_group = ""
        self._row_to_slot = {}
        return panel

    def on_new_folder(self) -> None:
        game, profile = self.game_combo.currentText(), self.profile_combo.currentText()
        if not game or not profile:
            self._notify("Select a game and profile first.")
            return
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if not ok or not name.strip():
            return
        try:
            storage.create_group(game, profile, name)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot create folder", str(exc))
            return
        self._collapsed_groups.discard(name.strip())
        self._active_group = name.strip()
        self._reload_slots()
        self._notify(f"Created folder '{name.strip()}'.")

    def import_as(self, cfg, profile: str, name: str) -> bool:
        group = getattr(self, "_active_group", "")
        parent = storage.SAVES_DIR / cfg.name / profile / group if group else storage.SAVES_DIR / cfg.name / profile
        if (parent / name).exists():
            self._notify(f"A save named '{name}' already exists here.")
            return False
        try:
            storage.import_save(cfg.name, profile, name, cfg, group=group)
        except Exception as exc:
            logger.exception("Import failed: game=%r profile=%r slot=%r group=%r",
                             cfg.name, profile, name, group)
            self._notify(f"Import failed: {exc}")
            return False
        self._reload_slots(name)
        self._notify(f"Imported '{name}'.")
        return True

    def on_import_save(self) -> None:
        cfg = self._get_game_cfg()
        if not cfg or not self._validate_game_save_path(cfg):
            return
        profile = self.profile_combo.currentText()
        if not profile:
            self._notify("No profile selected.")
            return
        group = getattr(self, "_active_group", "")
        name = (storage.auto_slot_name(cfg.name, profile, group)
                if self._config.auto_name_imports else "")
        if not self._config.auto_name_imports:
            name, ok = QInputDialog.getText(self, "Import Save", "Slot name:")
            if not ok or not name.strip():
                return
            try:
                name = storage.validate_entry_name(name)
            except ValueError as exc:
                self._notify(str(exc))
                return
        self._import_as(cfg, profile, name)

    def reload_slots(self, select_slot: str = "") -> None:
        game, profile = self.game_combo.currentText(), self.profile_combo.currentText()
        self._slots = (storage.load_sorted_slots(game, profile, self._config.slot_sort,
                                                self._config.slot_sort_desc)
                       if game and profile else [])
        folders = storage.load_groups(game, profile) if game and profile else []
        grouped: dict[str, list] = {}
        ungrouped = []
        for slot in self._slots:
            group = getattr(slot, "group", "")
            (grouped.setdefault(group, []).append(slot) if group else ungrouped.append(slot))
        self._row_to_slot = {}
        self.slot_list.setDragDropMode(
            QAbstractItemView.InternalMove if self._config.slot_sort == "custom" and not folders
            else QAbstractItemView.NoDragDrop
        )
        with QSignalBlocker(self.slot_list):
            self.slot_list.clear()
            for folder in folders:
                expanded = folder not in self._collapsed_groups
                header = QListWidgetItem(("📁 ▾ " if expanded else "📁 ▸ ") + folder)
                header.setData(Qt.UserRole + 1, "folder")
                header.setData(Qt.UserRole + 2, folder)
                header.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.slot_list.addItem(header)
                self._row_to_slot[self.slot_list.count() - 1] = None
                for slot in grouped.get(folder, []) if expanded else []:
                    item = QListWidgetItem()
                    item.setData(Qt.UserRole, slot)
                    item.setSizeHint(QSize(0, 34))
                    self.slot_list.addItem(item)
                    self.slot_list.setItemWidget(item, _SlotItem(slot.name))
                    self._row_to_slot[self.slot_list.count() - 1] = slot
            for slot in ungrouped:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, slot)
                item.setSizeHint(QSize(0, 34))
                self.slot_list.addItem(item)
                self.slot_list.setItemWidget(item, _SlotItem(slot.name))
                self._row_to_slot[self.slot_list.count() - 1] = slot
        target = next((row for row, slot in self._row_to_slot.items()
                       if slot is not None and slot.name == select_slot), None)
        if target is None:
            target = next((row for row, slot in self._row_to_slot.items() if slot is not None), -1)
        self.slot_list.setCurrentRow(target)
        if target >= 0:
            self._on_slot_selected(target)
        else:
            self._clear_detail()

    def selected_slot(self):
        return self._row_to_slot.get(self.slot_list.currentRow())

    MainWindow._build_slot_panel = build_slot_panel
    MainWindow._on_new_folder = on_new_folder
    MainWindow._import_as = import_as
    MainWindow._on_import_save = on_import_save
    MainWindow._reload_slots = reload_slots
    MainWindow._selected_slot = selected_slot
