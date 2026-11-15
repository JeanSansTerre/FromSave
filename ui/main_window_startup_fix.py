"""Small, isolated folder-list UI extension for the incremental compatibility branch.

The base MainWindow remains untouched so this feature can be tested independently.
"""
from PySide6.QtCore import QSize, Qt, QSignalBlocker
from PySide6.QtWidgets import (
    QAbstractItemView, QInputDialog, QListWidgetItem, QMessageBox, QPushButton,
)


def install() -> None:
    import storage
    from ui.main_window import MainWindow, _SlotItem

    original_build_slot_panel = MainWindow._build_slot_panel
    original_import_as = MainWindow._import_as

    def _build_slot_panel(self):
        panel = original_build_slot_panel(self)
        # Put the creation action next to the save list, not in the top game/profile row.
        if hasattr(self, "new_group_btn"):
            self.new_group_btn.hide()
        self.folder_btn = QPushButton("+Folder")
        self.folder_btn.setObjectName("ghostBtn")
        self.folder_btn.setToolTip("Create a collapsible folder for save slots")
        self.folder_btn.clicked.connect(self._on_new_folder)
        panel.layout().insertWidget(0, self.folder_btn)
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
        except (ValueError, FileExistsError) as exc:
            QMessageBox.warning(self, "Cannot create folder", str(exc))
            return
        except Exception as exc:
            QMessageBox.warning(self, "Cannot create folder", str(exc))
            return
        self._collapsed_groups.discard(name)
        self._active_group = name
        self._reload_slots()
        self._notify(f"Created folder '{name}'. New imports will go into it.")

    def _list_item_clicked(self, item: QListWidgetItem) -> None:
        if item.data(Qt.UserRole + 1) != "folder":
            return
        folder = item.data(Qt.UserRole + 2) or ""
        self._active_group = folder
        if folder in self._collapsed_groups:
            self._collapsed_groups.remove(folder)
        else:
            self._collapsed_groups.add(folder)
        self._reload_slots()

    def _import_as(self, cfg, profile: str, name: str, group: str = "") -> bool:
        target_group = group or getattr(self, "_active_group", "")
        return original_import_as(self, cfg, profile, name, target_group)

    def _reload_slots(self, select_slot: str = "") -> None:
        game_name = self.game_combo.currentText()
        profile_name = self.profile_combo.currentText()
        self._slots = (
            storage.load_sorted_slots(game_name, profile_name,
                                      self._config.slot_sort, self._config.slot_sort_desc)
            if game_name and profile_name else []
        )
        folders = storage.load_groups(game_name, profile_name) if game_name and profile_name else []
        self._row_to_slot = {}
        # Reordering list rows would mix folder headers with saves; custom drag order
        # remains available only for legacy profiles without folders.
        self.slot_list.setDragDropMode(
            QAbstractItemView.InternalMove
            if self._config.slot_sort == "custom" and not folders
            else QAbstractItemView.NoDragDrop
        )

        grouped: dict[str, list[storage.SaveSlot]] = {}
        ungrouped: list[storage.SaveSlot] = []
        for slot in self._slots:
            (grouped.setdefault(slot.group, []).append(slot) if slot.group else ungrouped.append(slot))

        with QSignalBlocker(self.slot_list):
            self.slot_list.clear()
            for folder in folders:
                expanded = folder not in self._collapsed_groups
                prefix = "📁 ▾ " if expanded else "📁 ▸ "
                item = QListWidgetItem(prefix + folder)
                item.setData(Qt.UserRole + 1, "folder")
                item.setData(Qt.UserRole + 2, folder)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.slot_list.addItem(item)
                self._row_to_slot[self.slot_list.count() - 1] = None
                if not expanded:
                    continue
                # Preserve the order returned by storage.load_sorted_slots. Do not
                # re-sort here: doing so silently broke the sort dropdown.
                for slot in grouped.get(folder, []):
                    row = QListWidgetItem()
                    row.setData(Qt.UserRole, slot)
                    row.setData(Qt.UserRole + 1, "slot")
                    row.setSizeHint(QSize(0, 34))
                    self.slot_list.addItem(row)
                    self.slot_list.setItemWidget(row, _SlotItem(slot.name))
                    self._row_to_slot[self.slot_list.count() - 1] = slot

            for slot in ungrouped:
                row = QListWidgetItem()
                row.setData(Qt.UserRole, slot)
                row.setData(Qt.UserRole + 1, "slot")
                row.setSizeHint(QSize(0, 34))
                self.slot_list.addItem(row)
                self.slot_list.setItemWidget(row, _SlotItem(slot.name))
                self._row_to_slot[self.slot_list.count() - 1] = slot

        selected = next((row for row, slot in self._row_to_slot.items()
                         if slot and slot.name == select_slot), None)
        if selected is None:
            selected = next((row for row, slot in self._row_to_slot.items() if slot), -1)
        self.slot_list.setCurrentRow(selected)
        if selected >= 0:
            self._on_slot_selected(selected)
        else:
            self._clear_detail()

    def _selected_slot(self):
        return self._row_to_slot.get(self.slot_list.currentRow())

    MainWindow._build_slot_panel = _build_slot_panel
    MainWindow._on_new_folder = _on_new_folder
    MainWindow._on_list_item_clicked = _list_item_clicked
    MainWindow._import_as = _import_as
    MainWindow._reload_slots = _reload_slots
    MainWindow._selected_slot = _selected_slot
