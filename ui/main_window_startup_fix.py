"""Incremental compatibility patch for the experimental folder UI branch.

The main window source currently lacks its info-panel and action-bar builders. This
module restores those methods and then adds folder handling without editing the
known-starting main_window.py implementation.
"""
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QSignalBlocker
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)


def _install_missing_builders(MainWindow) -> None:
    from ui.main_window import _InfoRow
    from ui.video_player import InlineVideoPlayer

    def _build_info_panel(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(0)

        title = QLabel("Slot details")
        title.setObjectName("panelHeader")
        layout.addWidget(title)
        layout.addSpacing(10)

        name_row = QHBoxLayout()
        self.detail_name = QLabel("—")
        self.detail_name.setObjectName("detailName")
        name_row.addWidget(self.detail_name, 1)
        self.rename_btn = QPushButton("Rename")
        self.rename_btn.setObjectName("ghostBtn")
        self.rename_btn.setEnabled(False)
        self.rename_btn.clicked.connect(self._on_rename_slot)
        name_row.addWidget(self.rename_btn)
        layout.addLayout(name_row)

        self.detail_time = QLabel("—")
        self.detail_time.setObjectName("mutedLabel")
        layout.addWidget(self.detail_time)
        layout.addSpacing(16)

        notes_label = QLabel("Notes")
        notes_label.setObjectName("fieldLabel")
        layout.addWidget(notes_label)
        layout.addSpacing(4)
        self.detail_notes = QPlainTextEdit()
        self.detail_notes.setObjectName("detailNotes")
        self.detail_notes.setPlaceholderText("No notes.")
        self.detail_notes.setMaximumHeight(90)
        self.detail_notes.textChanged.connect(self._on_notes_changed)
        layout.addWidget(self.detail_notes)
        layout.addSpacing(16)

        video_label = QLabel("Video")
        video_label.setObjectName("fieldLabel")
        layout.addWidget(video_label)
        layout.addSpacing(4)
        video_row = QHBoxLayout()
        video_row.setSpacing(6)
        self.detail_video_url = __import__("PySide6.QtWidgets", fromlist=["QLineEdit"]).QLineEdit()
        self.detail_video_url.setObjectName("detailVideoUrl")
        self.detail_video_url.setPlaceholderText("YouTube or direct video URL")
        self.detail_video_url.textChanged.connect(self._on_video_changed)
        video_row.addWidget(self.detail_video_url, 1)
        self.browse_video_btn = QPushButton("Browse")
        self.browse_video_btn.setObjectName("ghostBtn")
        self.browse_video_btn.clicked.connect(self._on_browse_video)
        video_row.addWidget(self.browse_video_btn)
        self.clear_video_btn = QPushButton("Clear")
        self.clear_video_btn.setObjectName("ghostBtn")
        self.clear_video_btn.setEnabled(False)
        self.clear_video_btn.clicked.connect(self._on_clear_video)
        video_row.addWidget(self.clear_video_btn)
        layout.addLayout(video_row)

        self._inline_player = InlineVideoPlayer()
        self._inline_player.setVisible(False)
        layout.addWidget(self._inline_player)
        layout.addSpacing(16)

        info_box = QFrame()
        info_box.setObjectName("infoBox")
        info_layout = QVBoxLayout(info_box)
        info_layout.setContentsMargins(12, 10, 12, 10)
        info_layout.setSpacing(6)
        info_title = QLabel("Game info")
        info_title.setObjectName("fieldLabel")
        info_layout.addWidget(info_title)
        self.info_save_path = _InfoRow("Save path", "—")
        self.info_created = _InfoRow("Created", "—")
        self.info_modified = _InfoRow("Modified", "—")
        self.info_file_size = _InfoRow("File size", "—")
        self.info_ro_status = _InfoRow("Practice", "—")
        for row in (self.info_save_path, self.info_created, self.info_modified,
                    self.info_file_size, self.info_ro_status):
            info_layout.addWidget(row)
        layout.addWidget(info_box)
        layout.addStretch()
        scroll.setWidget(panel)
        return scroll

    def _build_action_bar(self):
        from ui.main_window import _btn_text
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.import_btn = QPushButton(_btn_text("Import Save", self._config.hotkey_import))
        self.import_btn.setObjectName("primaryBtn")
        self.import_btn.clicked.connect(self._on_import_save)
        self.replace_btn = QPushButton("Replace Save")
        self.replace_btn.clicked.connect(self._on_replace_save)
        self.load_btn = QPushButton(_btn_text("Load Save", self._config.hotkey_load))
        self.load_btn.clicked.connect(self._on_load_save)
        self.delete_btn = QPushButton("Delete slot")
        self.delete_btn.setObjectName("dangerBtn")
        self.delete_btn.clicked.connect(self._on_delete_slot)
        for button in (self.import_btn, self.replace_btn, self.load_btn, self.delete_btn):
            bar.addWidget(button)
        bar.addStretch()
        self.ro_btn = QPushButton(_btn_text("Practice Mode", self._config.hotkey_ro_toggle))
        self.ro_btn.setObjectName("ghostBtn")
        self.ro_btn.setCheckable(True)
        self.ro_btn.setEnabled(False)
        self.ro_btn.toggled.connect(self._on_ro_toggled)
        bar.addWidget(self.ro_btn)
        self.run_mode_btn = QPushButton("Run Mode")
        self.run_mode_btn.setObjectName("ghostBtn")
        self.run_mode_btn.setCheckable(True)
        self.run_mode_btn.setToolTip(
            "Disables Load Save and Practice Mode to protect the game save,\\n"
            "and takes a rolling backup every 2 minutes (keeps last 3)."
        )
        self.run_mode_btn.toggled.connect(self._on_run_mode_toggled)
        bar.addWidget(self.run_mode_btn)
        return bar

    MainWindow._build_info_panel = _build_info_panel
    MainWindow._build_action_bar = _build_action_bar


def _install_storage_groups() -> None:
    import storage

    marker = ".fromsave-folder"
    original_import_save = storage.import_save

    def load_profiles(game: str) -> list[str]:
        game_dir = storage.SAVES_DIR / game
        if not game or not game_dir.exists():
            return []
        return sorted(p.name for p in game_dir.iterdir()
                      if p.is_dir() and not (p / marker).is_file())

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
        (folder / marker).write_text("FromSave folder\\n", encoding="utf-8")
        return folder

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
                    if slot_dir.is_dir():
                        slot = storage._read_slot(slot_dir, game, profile)
                        if slot:
                            slot.group = entry.name
                            slots.append(slot)
            else:
                slot = storage._read_slot(entry, game, profile)
                if slot:
                    slot.group = ""
                    slots.append(slot)
        return slots

    def load_sorted_slots(game: str, profile: str, sort: str, desc: bool):
        slots = load_slots(game, profile)
        if sort == "name":
            slots.sort(key=lambda slot: (slot.group.lower(), slot.name.lower()), reverse=desc)
        elif sort == "created":
            slots.sort(key=lambda slot: slot.date_created or datetime.min, reverse=desc)
        elif sort == "modified":
            slots.sort(key=lambda slot: slot.date_modified or slot.date_created or datetime.min,
                       reverse=desc)
        elif sort == "custom":
            order = {name: index for index, name in enumerate(storage.load_slot_order(game, profile))}
            slots.sort(key=lambda slot: order.get(slot.name, len(order)))
        return slots

    def auto_slot_name(game: str, profile: str, group: str = "") -> str:
        parent = storage.SAVES_DIR / game / profile / group if group else storage.SAVES_DIR / game / profile
        return storage._unique_name(parent, "new save")

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
            slot = storage._read_slot(slot_dir, game, profile)
            if slot:
                slot.group = group
            return slot
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


def _install_folder_ui() -> None:
    import storage
    from ui.main_window import MainWindow, _SlotItem, _fmt_dt, _fmt_size

    original_build_slot_panel = MainWindow._build_slot_panel

    def _build_slot_panel(self):
        panel = original_build_slot_panel(self)
        self.new_group_btn.hide()
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
        game, profile = self.game_combo.currentText(), self.profile_combo.currentText()
        self._slots = (storage.load_sorted_slots(game, profile, self._config.slot_sort,
                                                self._config.slot_sort_desc)
                       if game and profile else [])
        folders = storage.load_groups(game, profile) if game and profile else []
        grouped, ungrouped = {}, []
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
                header = QListWidgetItem(("📁 ▾ " if expanded else "📁 ▸ ") + folder)
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


def install() -> None:
    from ui.main_window import MainWindow
    _install_missing_builders(MainWindow)
    _install_storage_groups()
    _install_folder_ui()
