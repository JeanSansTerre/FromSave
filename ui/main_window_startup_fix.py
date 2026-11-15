"""Isolated folder-list extension for the incremental compatibility branch.

The main window stays based on the known-starting version. This module adds only the
folder headers and their storage hooks at startup.
"""
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QSignalBlocker
from PySide6.QtWidgets import (
    QAbstractItemView, QInputDialog, QListWidgetItem, QMessageBox, QPushButton,
)


def _install_storage_groups() -> None:
    import storage

    marker = ".fromsave-folder"
    original_load_profiles = storage.load_profiles
    original_load_slots = storage.load_slots
    original_load_sorted_slots = storage.load_sorted_slots
    original_auto_slot_name = storage.auto_slot_name
    original_import_save = storage.import_save

    def load_profiles(game: str) -> list[str]:
        return sorted(
            p.name for p in (storage.SAVES_DIR / game).iterdir()
            if p.is_dir() and not (p / marker).exists()
        ) if game and (storage.SAVES_DIR / game).exists() else []

    def load_groups(game: str, profile: str) -> list[str]:
        profile_dir = storage.SAVES_DIR / game / profile
        if not profile_dir.exists():
            return []
        return sorted(p.name for p in profile_dir.iterdir()
                      if p.is_dir() and (p / marker).is_file())

    def create_group(game: str, profile: str, name: str) -> Path:
        name = storage.validate_entry_name(name)
        folder = storage.SAVES_DIR / game / profile / name
        folder.mkdir(parents=True, exist_ok=False)
        (folder / marker).write_text("FromSave folder\n", encoding="utf-8")
        return folder

    def _set_group(slot, folder: str):
        if slot is not None:
            slot.group = folder
        return slot

    def load_slots(game: str, profile: str):
        profile_dir = storage.SAVES_DIR / game / profile
        if not profile_dir.exists():
            return []
        slots = []
        for entry in sorted(profile_dir.iterdir()):
            if not entry.is_dir():
                continue
            if (entry / marker).is_file():
                for slot_dir in sorted(entry.iterdir()):
                    if slot_dir.is_dir() and slot_dir.name != marker:
                        slot = storage._read_slot(slot_dir, game, profile)
                        if slot:
                            slots.append(_set_group(slot, entry.name))
            else:
                slot = storage._read_slot(entry, game, profile)
                if slot:
                    slots.append(_set_group(slot, ""))
        return slots

    def load_sorted_slots(game: str, profile: str, sort: str, desc: bool):
        slots = load_slots(game, profile)
        if sort == "name":
            slots.sort(key=lambda s: (s.group.lower(), s.name.lower()), reverse=desc)
        elif sort == "created":
            slots.sort(key=lambda s: s.date_created or datetime.min, reverse=desc)
        elif sort == "modified":
            slots.sort(key=lambda s: s.date_modified or s.date_created or datetime.min, reverse=desc)
        elif sort == "custom":
            saved = original_load_sorted_slots(game, profile, "custom", desc)
            # Keep group order stable while honoring the saved order for slots.
            order = {slot.path: index for index, slot in enumerate(saved)}
            slots.sort(key=lambda slot: order.get(slot.path, len(order)))
        return slots

    def auto_slot_name(game: str, profile: str, group: str = "") -> str:
        directory = storage.SAVES_DIR / game / profile / group if group else storage.SAVES_DIR / game / profile
        return storage._unique_name(directory, "new save")

    def import_save(game: str, profile: str, slot_name: str, game_cfg, group: str = ""):
        if not group:
            return original_import_save(game, profile, slot_name, game_cfg)
        parent = storage.SAVES_DIR / game / profile / group
        if not (parent / marker).is_file():
            raise ValueError(f"Folder does not exist: {group}")
        slot_dir = parent / storage.validate_entry_name(slot_name)
        slot_dir.mkdir(parents=True, exist_ok=False)
        try:
            storage._copy_live_save(game_cfg, slot_dir)
            now = datetime.now()
            storage._write_json(slot_dir / "meta.json", storage._timestamps(now, now))
            (slot_dir / "notes.txt").write_text("", encoding="utf-8")
            return _set_group(storage._read_slot(slot_dir, game, profile), group)
        except Exception:
            import shutil
            shutil.rmtree(slot_dir, ignore_errors=True)
            raise

    storage.load_profiles = load_profiles
    storage.load_groups = load_groups
    storage.create_group = create_group
    storage.load_slots = load_slots
    storage.load_sorted_slots = load_sorted_slots
    storage.auto_slot_name = auto_slot_name
    storage.import_save = import_save


def install() -> None:
    _install_storage_groups()
    import storage
    from ui.main_window import MainWindow, _SlotItem

    original_build_slot_panel = MainWindow._build_slot_panel
    original_import_as = MainWindow._import_as

    def _build_slot_panel(self):
        panel = original_build_slot_panel(self)
        self.folder_btn = QPushButton("+Folder")
        self.folder_btn.setObjectName("ghostBtn")
        self.folder_btn.setToolTip("Create a collapsible folder for save slots")
        self.folder_btn.clicked.connect(self._on_new_folder)
        panel.layout().insertWidget(0, self.folder_btn)
        self._collapsed_groups = set()
        self._active_group = ""
        self._row_to_slot = {}
        self.slot_list.itemClicked.connect(self._on_folder_item_clicked)
        return panel

    def _on_new_folder(self) -> None:
        game = self.game_combo.currentText()
        profile = self.profile_combo.currentText()
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
        self._collapsed_groups.discard(name)
        self._active_group = name
        self._reload_slots()
        self._notify(f"Created folder '{name}'. New imports will go into it.")

    def _on_folder_item_clicked(self, item: QListWidgetItem) -> None:
        if item.data(Qt.UserRole + 1) != "folder":
            return
        folder = item.data(Qt.UserRole + 2) or ""
        if folder in self._collapsed_groups:
            self._collapsed_groups.remove(folder)
        else:
            self._collapsed_groups.add(folder)
        self._reload_slots()

    def _import_as(self, cfg, profile: str, name: str) -> bool:
        group = getattr(self, "_active_group", "")
        parent = storage.SAVES_DIR / cfg.name / profile / group if group else storage.SAVES_DIR / cfg.name / profile
        if (parent / name).exists():
            self._notify(f"A save named '{name}' already exists in this location.")
            return False
        try:
            storage.import_save(cfg.name, profile, name, cfg, group=group)
        except Exception as exc:
            logger.exception("Import save failed: game=%r profile=%r slot=%r group=%r",
                             cfg.name, profile, name, group)
            self._notify(f"Import failed: {exc}")
            return False
        self._reload_slots(name)
        self._notify(f"Imported '{name}'.")
        return True

    def _on_import_save(self) -> None:
        cfg = self._get_game_cfg()
        if not cfg or not self._validate_game_save_path(cfg):
            return
        profile = self.profile_combo.currentText()
        if not profile:
            self._notify("No profile selected.")
            return
        group = getattr(self, "_active_group", "")
        if self._config.auto_name_imports:
            name = storage.auto_slot_name(cfg.name, profile, group)
        else:
            name, ok = QInputDialog.getText(self, "Import Save", "Slot name:")
            if not ok or not name.strip():
                return
            try:
                name = storage.validate_entry_name(name)
            except ValueError as exc:
                self._notify(str(exc))
                return
        self._import_as(cfg, profile, name)

    def _reload_slots(self, select_slot: str = "") -> None:
        game = self.game_combo.currentText()
        profile = self.profile_combo.currentText()
        self._slots = (storage.load_sorted_slots(game, profile, self._config.slot_sort,
                                                self._config.slot_sort_desc)
                       if game and profile else [])
        folders = storage.load_groups(game, profile) if game and profile else []
        grouped: dict[str, list] = {}
        ungrouped = []
        for slot in self._slots:
            group = getattr(slot, "group", "")
            if group:
                grouped.setdefault(group, []).append(slot)
            else:
                ungrouped.append(slot)

        self._row_to_slot = {}
        self.slot_list.setDragDropMode(
            QAbstractItemView.InternalMove
            if self._config.slot_sort == "custom" and not folders
            else QAbstractItemView.NoDragDrop
        )
        with QSignalBlocker(self.slot_list):
            self.slot_list.clear()
            for folder in folders:
                expanded = folder not in self._collapsed_groups
                prefix = "📁 ▾ " if expanded else "📁 ▸ "
                header = QListWidgetItem(prefix + folder)
                header.setData(Qt.UserRole + 1, "folder")
                header.setData(Qt.UserRole + 2, folder)
                header.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.slot_list.addItem(header)
                self._row_to_slot[self.slot_list.count() - 1] = None
                if expanded:
                    for slot in grouped.get(folder, []):
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

        selected = next((row for row, slot in self._row_to_slot.items()
                         if slot is not None and slot.name == select_slot), None)
        if selected is None:
            selected = next((row for row, slot in self._row_to_slot.items() if slot is not None), -1)
        self.slot_list.setCurrentRow(selected)
        if selected >= 0:
            self._on_slot_selected(selected)
        else:
            self._clear_detail()

    def _selected_slot(self):
        return self._row_to_slot.get(self.slot_list.currentRow())

    def _on_slot_selected(self, row: int) -> None:
        # Group header rows have no SaveSlot object. Preserve the app's normal
        # selection behavior for real slots without treating headers as saves.
        slot = self._row_to_slot.get(row)
        if slot is None:
            self._flush_notes()
            self._flush_video()
            self._clear_detail()
            return
        self._flush_notes()
        self._flush_video()
        self._current_slot = slot
        self.rename_btn.setEnabled(True)
        self.detail_name.setText(slot.name)
        self.detail_time.setText(_fmt_dt(slot.date_modified or slot.date_created))
        self.detail_notes.blockSignals(True)
        self.detail_notes.setPlainText(slot.notes)
        self.detail_notes.blockSignals(False)
        self._set_video_url(slot.video_url)
        self.info_created.set_value(_fmt_dt(slot.date_created))
        self.info_modified.set_value(_fmt_dt(slot.date_modified) if slot.date_modified else "—")
        files = storage.slot_save_files(slot)
        self.info_file_size.set_value(_fmt_size(sum(f.stat().st_size for f in files)) if files else "—")
        self._maybe_switch_practice_slot(slot)
        self._sync_ro_button()
        self._refresh_overlay()

    MainWindow._build_slot_panel = _build_slot_panel
    MainWindow._on_new_folder = _on_new_folder
    MainWindow._on_folder_item_clicked = _on_folder_item_clicked
    MainWindow._import_as = _import_as
    MainWindow._on_import_save = _on_import_save
    MainWindow._reload_slots = _reload_slots
    MainWindow._selected_slot = _selected_slot
    MainWindow._on_slot_selected = _on_slot_selected
