import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Union

from PySide6.QtCore import QEvent, QFileSystemWatcher, QSignalBlocker, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QStatusBar, QVBoxLayout, QWidget,
)

import config
import storage
from hotkeys import GlobalHotkeyListener, GlobalTextInputListener, is_wayland_session
from ui.add_game_dialog import AddGameDialog
from ui.configure_game_dialog import ConfigureGameDialog
from ui.overlay_window import OverlayWindow
from ui.profiles_dialog import ProfilesDialog
from ui.save_path_widgets import split_paths
from ui.settings_dialog import SettingsDialog
from ui.update_flow import UpdateFlow
from ui.video_player import InlineVideoPlayer

logger = logging.getLogger(__name__)

_UI_DIR = Path(__file__).parent
_ROW_HEIGHT = 22  # one slot or folder row, in pixels
_SORT_LABELS = {"modified": "Modified", "created": "Created", "name": "Name", "custom": "Custom"}

def _hotkey_label(key: str) -> str:
    if not key:
        return ""
    return QKeySequence(key).toString(QKeySequence.NativeText) or key


def _btn_text(base: str, key: str) -> str:
    label = _hotkey_label(key)
    return f"{base}  ({label})" if label else base


def _fix_combo_first_click(combo: QComboBox) -> None:
    """Select popup items on mouse press instead of release.

    With the stylesheet applied, the popup's item geometry shifts just after it
    opens, so the first click could land on the wrong row (or none).
    """
    def _select_and_close(index) -> None:
        combo.setCurrentIndex(index.row())
        combo.hidePopup()
    combo.view().pressed.connect(_select_and_close)


def _set_combo_items(combo: QComboBox, items: list[str], select: str = "") -> None:
    """Replace combo's items without emitting signals, selecting `select` or the first item."""
    with QSignalBlocker(combo):
        combo.clear()
        combo.addItems(items)
        if items:
            combo.setCurrentIndex(max(0, combo.findText(select)))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FromSave Manager")

        self._config = config.load_config()
        self._games = storage.load_games()
        # What the slot list shows, one entry per row: folders and the slots under them.
        self._rows: list[Union[storage.SaveFolder, storage.SaveSlot]] = []
        self._folders: list[storage.SaveFolder] = []
        self._loose_slots: list[storage.SaveSlot] = []
        self._collapsed: set[tuple[str, str, str]] = set()  # (game, profile, folder)
        self._current_slot: Optional[storage.SaveSlot] = None
        self._actual_video_url: str = ""

        self._notes_save_timer = QTimer(self)
        self._notes_save_timer.setSingleShot(True)
        self._notes_save_timer.setInterval(600)
        self._notes_save_timer.timeout.connect(self._flush_notes)

        self._video_save_timer = QTimer(self)
        self._video_save_timer.setSingleShot(True)
        self._video_save_timer.setInterval(600)
        self._video_save_timer.timeout.connect(self._flush_video)

        self._run_mode = False
        self._run_backup_timer = QTimer(self)
        self._run_backup_timer.setInterval(2 * 60 * 1000)
        self._run_backup_timer.timeout.connect(self._on_run_backup_tick)

        self._guard_watcher = QFileSystemWatcher(self)
        self._guard_watcher.fileChanged.connect(self._on_guarded_file_changed)
        self._guard_slot: Optional[storage.SaveSlot] = None
        self._guard_cfg: Optional[storage.GameConfig] = None

        self._confirm_dialog: Optional[QMessageBox] = None
        self._confirm_action: Optional[str] = None  # "replace" | "delete" | "lock"
        self._protect_warning_shown: bool = False
        self._protect_warning_open: bool = False

        self._global_hotkeys = GlobalHotkeyListener(self)
        self._global_hotkeys.triggered.connect(self._on_main_hotkey)
        self._global_hotkeys_started = False
        self._shortcuts: list[QShortcut] = []
        # Ctrl+Z history: (label, undo function). Only active while this window has focus.
        self._undo_stack: list[tuple[str, Callable[[], None]]] = []
        storage.clear_undo_stash()
        self._overlay_toggle_hotkey = GlobalHotkeyListener(self)
        self._overlay_toggle_hotkey.triggered.connect(self._toggle_overlay)
        self._overlay_toggle_shortcut: Optional[QShortcut] = None
        self._overlay_action_hotkeys = GlobalHotkeyListener(self)
        self._overlay_action_hotkeys.triggered.connect(self._on_overlay_hotkey)

        # The overlay's rename and import prompts share one text listener.
        self._overlay_text_mode: str = "rename"  # "rename" | "import"
        self._overlay_rename_input = GlobalTextInputListener(self)
        # No parent, so minimizing the main window doesn't hide the overlay.
        self._overlay = OverlayWindow(on_moved=self._on_overlay_moved)
        self._overlay_rename_input.text_changed.connect(self._overlay.set_rename_text)
        self._overlay_rename_input.submitted.connect(self._on_overlay_text_submitted)
        self._overlay_rename_input.cancelled.connect(self._cancel_overlay_text_entry)

        self._build_ui()
        self._apply_info_panel()
        w, h = self._config.window_width, self._config.window_height
        self.resize(w if w > 0 else 700, h if h > 0 else 580)

        self._main_actions = {
            "import": self._on_import_save,
            "load": self._on_load_save,
            "replace": self._on_replace_save,
            "ro_toggle": self.ro_btn.toggle,
            "next_slot": self._select_next_slot,
            "prev_slot": self._select_prev_slot,
        }
        self._overlay_actions = {
            **self._main_actions,
            "import": self._on_overlay_import_triggered,
            "rename": self._begin_overlay_rename,
        }

        self._load_data(self._config.last_game, self._config.last_profile, self._config.last_slot)
        self.apply_stylesheet()
        self._apply_hotkeys()
        self._apply_overlay_toggle_hotkey()
        QTimer.singleShot(0, self._prompt_unconfigured_games)
        self._startup_update = UpdateFlow(self, quiet=True)
        if self._config.check_updates_on_startup:
            QTimer.singleShot(0, self._startup_update.start)

    # UI construction

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        # Brief colour-coded banner for a finished load or replace.
        self._flash_label = QLabel(root)
        self._flash_label.setAlignment(Qt.AlignCenter)
        self._flash_label.hide()
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._flash_label.hide)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(14, 12, 14, 10)
        content_layout.setSpacing(10)
        outer.addWidget(content, 1)

        content_layout.addLayout(self._build_top_bar())
        content_layout.addWidget(_Divider())

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)

        self._slot_panel = self._build_slot_panel()
        self._info_panel = self._build_info_panel()
        splitter.addWidget(self._slot_panel)
        splitter.addWidget(self._info_panel)

        splitter.setSizes([310, 580])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        content_layout.addWidget(splitter, 1)

        content_layout.addWidget(_Divider())
        content_layout.addLayout(self._build_action_bar())

        self.status_bar = QStatusBar()
        self.status_bar.setSizeGripEnabled(True)
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready.")

    def _build_top_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(10)

        game_label = QLabel("Game")
        game_label.setObjectName("fieldLabel")
        bar.addWidget(game_label)

        self.game_combo = QComboBox()
        self.game_combo.setMinimumWidth(220)
        self.game_combo.setMaximumWidth(320)
        self.game_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.game_combo.currentIndexChanged.connect(self._on_game_changed)
        _fix_combo_first_click(self.game_combo)
        bar.addWidget(self.game_combo)

        self.add_game_btn = QPushButton()
        self.add_game_btn.setObjectName("ghostBtn")
        self.add_game_btn.setProperty("iconOnly", True)
        self.add_game_btn.setIcon(QIcon(str(_UI_DIR / "plus.svg")))
        self.add_game_btn.setIconSize(QSize(14, 14))
        self.add_game_btn.setFixedWidth(36)
        self.add_game_btn.setToolTip("Add a game")
        self.add_game_btn.clicked.connect(self._on_add_game)
        bar.addWidget(self.add_game_btn)

        bar.addSpacing(12)

        profile_label = QLabel("Profile")
        profile_label.setObjectName("fieldLabel")
        bar.addWidget(profile_label)

        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(140)
        self.profile_combo.setMaximumWidth(220)
        self.profile_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        _fix_combo_first_click(self.profile_combo)
        bar.addWidget(self.profile_combo)

        self.manage_profiles_btn = QPushButton()
        self.manage_profiles_btn.setObjectName("ghostBtn")
        self.manage_profiles_btn.setProperty("iconOnly", True)
        self.manage_profiles_btn.setIcon(QIcon(str(_UI_DIR / "plus.svg")))
        self.manage_profiles_btn.setIconSize(QSize(14, 14))
        self.manage_profiles_btn.setFixedWidth(36)
        self.manage_profiles_btn.setToolTip("Add or manage profiles")
        self.manage_profiles_btn.clicked.connect(self._on_manage_profiles)
        bar.addWidget(self.manage_profiles_btn)

        bar.addStretch()

        self.settings_btn = QPushButton(" Settings")
        self.settings_btn.setIcon(QIcon(str(_UI_DIR / "settings.svg")))
        self.settings_btn.setIconSize(QSize(14, 14))
        self.settings_btn.setObjectName("ghostBtn")
        self.settings_btn.clicked.connect(self._on_open_settings)
        bar.addWidget(self.settings_btn)

        return bar

    def _build_slot_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(6)

        self.add_folder_btn = QPushButton("+ Folder")
        self.add_folder_btn.setObjectName("ghostBtn")
        self.add_folder_btn.setIcon(QIcon(str(_UI_DIR / "folder.svg")))
        self.add_folder_btn.setIconSize(QSize(14, 14))
        self.add_folder_btn.setToolTip("Create a folder to group slots in this profile")
        self.add_folder_btn.clicked.connect(self._on_add_folder)
        header.addWidget(self.add_folder_btn)
        header.addStretch()

        sort_label = QLabel("Sort by")
        sort_label.setObjectName("fieldLabel")
        header.addWidget(sort_label)

        self.sort_combo = QComboBox()
        for mode, label in _SORT_LABELS.items():
            self.sort_combo.addItem(label, mode)
        self.sort_combo.setFixedWidth(115)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        _fix_combo_first_click(self.sort_combo)
        header.addWidget(self.sort_combo)

        self._icon_sort_desc = QIcon(str(_UI_DIR / "sort_desc.svg"))
        self._icon_sort_asc = QIcon(str(_UI_DIR / "sort_asc.svg"))

        self.sort_dir_btn = QPushButton()
        self.sort_dir_btn.setIconSize(QSize(16, 16))
        self.sort_dir_btn.setObjectName("ghostBtn")
        self.sort_dir_btn.setFixedWidth(36)
        self.sort_dir_btn.setToolTip("Toggle ascending / descending")
        self.sort_dir_btn.clicked.connect(self._on_sort_dir_toggled)
        header.addWidget(self.sort_dir_btn)
        self._sync_sort_widgets()

        layout.addLayout(header)

        self.slot_list = _SlotListWidget()
        self.slot_list.setAlternatingRowColors(False)
        self.slot_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.slot_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Select several slots (Ctrl/Shift-click) and drag them onto a folder to file them
        # there, or onto a top-level slot to take them out; in Custom sort the drop
        # position also sets their order.
        self.slot_list.setDragEnabled(True)
        self.slot_list.setAcceptDrops(True)
        self.slot_list.setDropIndicatorShown(True)
        self.slot_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.slot_list.entries_dropped.connect(self._on_entries_dropped)
        self.slot_list.currentRowChanged.connect(self._on_slot_selected)
        self.slot_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.slot_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.slot_list.customContextMenuRequested.connect(self._on_slot_context_menu)
        self.slot_list.installEventFilter(self)
        # F2 renames and Delete deletes the selected slot(s) or folder while the list has focus.
        for key, handler in (("F2", self._rename_selected), ("Delete", self._delete_selected)):
            sc = QShortcut(QKeySequence(key), self.slot_list)
            sc.setContext(Qt.WidgetShortcut)
            sc.activated.connect(handler)
        undo_sc = QShortcut(QKeySequence("Ctrl+Z"), self)
        undo_sc.setContext(Qt.WindowShortcut)
        undo_sc.activated.connect(self._undo_last)
        layout.addWidget(self.slot_list, 1)

        return panel

    def _build_info_panel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(0)

        lbl = QLabel("Slot details")
        lbl.setObjectName("panelHeader")
        layout.addWidget(lbl)
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

        notes_lbl = QLabel("Notes")
        notes_lbl.setObjectName("fieldLabel")
        layout.addWidget(notes_lbl)
        layout.addSpacing(4)

        self.detail_notes = QPlainTextEdit()
        self.detail_notes.setObjectName("detailNotes")
        self.detail_notes.setPlaceholderText("No notes.")
        self.detail_notes.setMaximumHeight(90)
        self.detail_notes.textChanged.connect(self._on_notes_changed)
        layout.addWidget(self.detail_notes)

        layout.addSpacing(16)

        video_lbl = QLabel("Video")
        video_lbl.setObjectName("fieldLabel")
        layout.addWidget(video_lbl)
        layout.addSpacing(4)

        video_row = QHBoxLayout()
        video_row.setSpacing(6)
        self.detail_video_url = QLineEdit()
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

        for row in (self.info_save_path, self.info_created, self.info_modified, self.info_file_size, self.info_ro_status):
            info_layout.addWidget(row)

        layout.addWidget(info_box)
        layout.addStretch()

        scroll.setWidget(panel)
        return scroll

    def _build_action_bar(self) -> QHBoxLayout:
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

        bar.addWidget(self.import_btn)
        bar.addWidget(self.replace_btn)
        bar.addWidget(self.load_btn)
        bar.addWidget(self.delete_btn)
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
            "Disables Load Save and Practice Mode to protect the game save,\n"
            "and takes a rolling backup every 2 minutes (keeps last 3)."
        )
        self.run_mode_btn.toggled.connect(self._on_run_mode_toggled)
        bar.addWidget(self.run_mode_btn)

        return bar

    # Data loading

    def _prompt_unconfigured_games(self) -> None:
        changed = False
        for name in storage.find_unconfigured_games():
            if name in storage.BUNDLED_GAMES:
                storage.save_game_config(storage.GameConfig(name, "", storage.BUNDLED_GAMES[name]))
                changed = True
                self.status_bar.showMessage(
                    f"'{name}' configured automatically — open Settings to set the save path.", 8000
                )
                continue
            dlg = ConfigureGameDialog(name, self)
            if dlg.exec() != QDialog.Accepted:
                continue
            storage.save_game_config(storage.GameConfig(name, "", dlg.result_mode))
            changed = True
            self.status_bar.showMessage(
                f"'{name}' configured as {dlg.result_mode} — open Settings to set the save path.", 8000
            )
        if changed:
            self._games = storage.load_games()
            self._load_data(self.game_combo.currentText(), self.profile_combo.currentText(),
                            self._current_slot_name())

    def _current_slot_name(self) -> str:
        return self._current_slot.name if self._current_slot else ""

    def _load_data(self, game: str = "", profile: str = "", slot: str = "") -> None:
        """Repopulate the game and profile combos and the slot list, keeping the given selection."""
        _set_combo_items(self.game_combo, [g.name for g in self._games], game)
        self._reload_profiles(profile)
        self._update_save_path_info()
        self._reload_slots(slot)

    def _reload_profiles(self, select: str = "") -> None:
        previous = self.profile_combo.currentText()
        _set_combo_items(self.profile_combo, storage.load_profiles(self.game_combo.currentText()), select)
        if self.profile_combo.currentText() != previous:
            self._disable_practice_mode_for_context_switch()

    def _update_save_path_info(self) -> None:
        self.info_save_path.setVisible(not self._config.hide_paths)
        self.info_save_path.set_value(_game_path_display(self._get_game_cfg()))

    def _disable_practice_mode_for_context_switch(self) -> None:
        """Practice Mode guards one game's live save, so switching game or profile turns it
        off (which also restores the pre-practice save)."""
        if self.ro_btn.isChecked():
            self.ro_btn.setChecked(False)

    def _on_game_changed(self, _: int) -> None:
        self._disable_practice_mode_for_context_switch()
        self._reload_profiles()
        self._update_save_path_info()
        self._reload_slots()

    def _on_profile_changed(self, _: int) -> None:
        self._disable_practice_mode_for_context_switch()
        self._reload_slots()

    def _sync_sort_widgets(self) -> None:
        with QSignalBlocker(self.sort_combo):
            self.sort_combo.setCurrentIndex(max(0, self.sort_combo.findData(self._config.slot_sort)))
        self.sort_dir_btn.setIcon(
            self._icon_sort_desc if self._config.slot_sort_desc else self._icon_sort_asc
        )
        self.sort_dir_btn.setEnabled(self._config.slot_sort != "custom")

    def _on_sort_changed(self, _: int) -> None:
        mode = self.sort_combo.currentData()
        if mode == self._config.slot_sort:
            return
        self._config.slot_sort = mode
        config.save_config(self._config)
        self._sync_sort_widgets()
        self._reload_slots(self._current_slot_name())
        if mode == "custom":
            self.status_bar.showMessage("Custom order — drag slots to rearrange.", 4000)

    def _on_sort_dir_toggled(self) -> None:
        self._config.slot_sort_desc = not self._config.slot_sort_desc
        config.save_config(self._config)
        self._sync_sort_widgets()
        self._reload_slots(self._current_slot_name())

    def _reload_slots(self, select_slot: str = "", select_folder: str = "",
                      also_select: tuple[str, ...] = ()) -> None:
        """Re-read the profile from disk and rebuild the list, selecting a slot (or folder)."""
        game_name = self.game_combo.currentText()
        profile_name = self.profile_combo.currentText()
        if game_name and profile_name:
            self._folders, self._loose_slots = storage.load_profile_tree(
                game_name, profile_name, self._config.slot_sort, self._config.slot_sort_desc)
        else:
            self._folders, self._loose_slots = [], []
        self._populate_list(select_slot, select_folder, also_select)

    def _populate_list(self, select_slot: str = "", select_folder: str = "",
                       also_select: tuple[str, ...] = ()) -> None:
        """Rebuild the list rows from the loaded folders and slots (no disk access)."""
        game_name = self.game_combo.currentText()
        profile_name = self.profile_combo.currentText()

        # Never hide the slot that has to be selected inside a collapsed folder.
        for folder in self._folders:
            if any(s.name == select_slot for s in folder.slots):
                self._collapsed.discard((game_name, profile_name, folder.name))

        self._rows = []
        for folder in self._folders:
            self._rows.append(folder)
            if (game_name, profile_name, folder.name) not in self._collapsed:
                self._rows.extend(folder.slots)
        self._rows.extend(self._loose_slots)

        with QSignalBlocker(self.slot_list):
            self.slot_list.clear()
            for entry in self._rows:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, entry)
                item.setSizeHint(QSize(0, _ROW_HEIGHT))
                if isinstance(entry, storage.SaveFolder):
                    expanded = (game_name, profile_name, entry.name) not in self._collapsed
                    # Folders can be dropped onto but are not dragged themselves.
                    item.setFlags(item.flags() & ~Qt.ItemIsDragEnabled)
                    self.slot_list.addItem(item)
                    self.slot_list.setItemWidget(item, _FolderItem(entry.name, expanded))
                else:
                    self.slot_list.addItem(item)
                    self.slot_list.setItemWidget(item, _SlotItem(entry.name, bool(entry.folder)))

        target = -1
        for row, entry in enumerate(self._rows):
            if select_slot and isinstance(entry, storage.SaveSlot) and entry.name == select_slot:
                target = row
                break
            if select_folder and isinstance(entry, storage.SaveFolder) and entry.name == select_folder:
                target = row
                break
        if target < 0 and not select_folder:
            target = next((r for r, e in enumerate(self._rows) if isinstance(e, storage.SaveSlot)), -1)

        if target >= 0:
            self.slot_list.setCurrentRow(target)
            self._on_slot_selected(target)
            # Keep a multi-selection (e.g. the slots that were just moved) selected.
            for row, entry in enumerate(self._rows):
                if isinstance(entry, storage.SaveSlot) and entry.name in also_select:
                    self.slot_list.item(row).setSelected(True)
        else:
            self._clear_detail()

    def _visible_slots(self) -> list[storage.SaveSlot]:
        """Slots currently shown in the list (not those hidden in collapsed folders)."""
        return [e for e in self._rows if isinstance(e, storage.SaveSlot)]

    def _selected_entry(self) -> Optional[Union[storage.SaveFolder, storage.SaveSlot]]:
        row = self.slot_list.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _selected_slots(self) -> list[storage.SaveSlot]:
        """Every selected slot, in list order (folders are left out)."""
        rows = sorted(self.slot_list.row(item) for item in self.slot_list.selectedItems())
        return [self._rows[r] for r in rows
                if 0 <= r < len(self._rows) and isinstance(self._rows[r], storage.SaveSlot)]

    def _multi_selected(self) -> bool:
        return len(self.slot_list.selectedItems()) > 1

    def _selected_slot(self) -> Optional[storage.SaveSlot]:
        """The selected slot, or None when nothing, a folder, or several rows are selected.
        Load, replace, delete and the like act on one slot only."""
        if self._multi_selected():
            return None
        entry = self._selected_entry()
        return entry if isinstance(entry, storage.SaveSlot) else None

    def _no_slot_text(self) -> str:
        return "Select a single slot first." if self._multi_selected() else "No slot selected."

    def _select_next_slot(self) -> None:
        for row in range(self.slot_list.currentRow() + 1, len(self._rows)):
            if isinstance(self._rows[row], storage.SaveSlot):
                self.slot_list.setCurrentRow(row)
                return

    def _select_prev_slot(self) -> None:
        for row in range(self.slot_list.currentRow() - 1, -1, -1):
            if isinstance(self._rows[row], storage.SaveSlot):
                self.slot_list.setCurrentRow(row)
                return

    # Event handlers

    def _on_slot_selected(self, row: int) -> None:
        self._flush_notes()
        self._flush_video()
        slot = self._rows[row] if 0 <= row < len(self._rows) else None
        if not isinstance(slot, storage.SaveSlot):
            self._clear_detail()  # nothing selected, or a folder (folders have no save)
            return
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

    def _maybe_switch_practice_slot(self, slot: storage.SaveSlot) -> None:
        """While Practice Mode is on, protect whichever slot of the same game is selected."""
        if self._guard_slot is None or self._guard_cfg is None:
            return
        if self._guard_slot.path == slot.path:
            return
        game_cfg = self._get_game_cfg()
        if not game_cfg or game_cfg.name != self._guard_cfg.name:
            return  # switching game disables practice mode separately
        try:
            storage.load_save(slot, game_cfg, make_backup=False)
        except OSError as e:
            logger.exception("Practice Mode: failed to switch slot: slot=%r", slot.name)
            self._notify(f"Practice Mode: failed to switch — {e}")
            return
        self._guard_slot = slot
        self._notify(f"Practice mode now protecting '{slot.name}'.")

    def _clear_detail(self) -> None:
        self._current_slot = None
        self.rename_btn.setEnabled(False)
        self.detail_name.setText("—")
        self.detail_time.setText("—")
        self.detail_notes.blockSignals(True)
        self.detail_notes.setPlainText("")
        self.detail_notes.blockSignals(False)
        self._set_video_url("")
        self.info_created.set_value("—")
        self.info_modified.set_value("—")
        self.info_file_size.set_value("—")
        self._sync_ro_button()
        self._refresh_overlay()

    def _on_notes_changed(self) -> None:
        self._notes_save_timer.start()

    def _flush_notes(self) -> None:
        self._notes_save_timer.stop()
        if self._current_slot is None:
            return
        if not self._current_slot.path.exists():
            return
        storage.save_notes(self._current_slot, self.detail_notes.toPlainText())
        self.status_bar.showMessage("Notes saved.", 2000)

    def _set_video_url(self, url: str) -> None:
        self._actual_video_url = url
        parsed = QUrl(url.strip()) if url.strip() else QUrl()
        display = Path(parsed.toLocalFile()).name if parsed.isLocalFile() else url
        self.detail_video_url.blockSignals(True)
        self.detail_video_url.setText(display)
        self.detail_video_url.blockSignals(False)
        has_url = bool(url.strip())
        self.clear_video_btn.setEnabled(has_url)
        if has_url and self._config.show_details:  # a hidden panel never loads a video
            if url.strip() != self._inline_player.url:
                self._inline_player.load(url.strip())
            self._inline_player.setVisible(True)
            QTimer.singleShot(50, self.update)
        else:
            self._inline_player.unload()
            self._inline_player.setVisible(False)
        QTimer.singleShot(0, self._sync_minimum_size)

    def _on_video_changed(self) -> None:
        text = self.detail_video_url.text().strip()
        if not QUrl(text).isLocalFile():
            self._actual_video_url = text
        self.clear_video_btn.setEnabled(bool(self._actual_video_url))
        self._video_save_timer.start()

    def _flush_video(self) -> None:
        self._video_save_timer.stop()
        if self._current_slot is None:
            return
        if not self._current_slot.path.exists():
            return
        url = self._actual_video_url
        if url != self._current_slot.video_url:
            storage.save_video_url(self._current_slot, url)
            self.status_bar.showMessage("Video link saved.", 2000)
        self._set_video_url(url)

    def _on_browse_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select video file", "",
            "Video files (*.mp4 *.webm *.ogg *.mov *.m4v *.mkv);;All files (*)",
        )
        if path:
            self._set_video_url(QUrl.fromLocalFile(path).toString())
            self._video_save_timer.start()

    def _on_clear_video(self) -> None:
        self._inline_player.unload()
        self._inline_player.setVisible(False)
        self.detail_video_url.clear()
        self._flush_video()
        QTimer.singleShot(0, self._sync_minimum_size)

    def _flash(self, text: str, color: str) -> None:
        """Show a coloured banner over the window for a moment (only while it is visible)."""
        if not self.isVisible() or self.isMinimized():
            return
        label = self._flash_label
        label.setText(text)
        label.setStyleSheet(
            f"background-color: {color}; color: #ffffff; font-size: 20px; font-weight: 700;"
            "border-radius: 10px; padding: 14px 32px;")
        label.adjustSize()
        parent = label.parentWidget()
        label.move((parent.width() - label.width()) // 2, max(12, parent.height() // 8))
        label.raise_()
        label.show()
        self._flash_timer.start(1500)

    def _notify(self, text: str, timeout: int = 0) -> None:
        """Show a status message, mirrored in the overlay while it's visible."""
        self.status_bar.showMessage(text, timeout)
        if self._overlay.isVisible():
            self._overlay.show_message(text)

    _LOCK_CONFIRM_MESSAGE = (
        "Enable practice mode?\n\nThis will immediately overwrite the game's current save with "
        "this slot and keep restoring it any time the game tries to save. The game will not be "
        "able to save progress while practice mode is on."
    )

    def _sync_ro_button(self) -> None:
        """Show Practice Mode's on/off state; it can always be turned off, whatever is selected."""
        is_active = self._guard_slot is not None
        save_files = storage.slot_save_files(self._current_slot) if self._current_slot else []
        if is_active:
            self.info_ro_status.set_value("Active")
        elif save_files:
            self.info_ro_status.set_value("Inactive")
        else:
            self.info_ro_status.set_value("—")
        with QSignalBlocker(self.ro_btn):
            self.ro_btn.setChecked(is_active)
        self.ro_btn.setText(self._ro_btn_text(is_active))
        self.ro_btn.setEnabled(not self._run_mode and (is_active or bool(save_files)))

    def _on_ro_toggled(self, checked: bool) -> None:
        if self._confirm_dialog is not None and self._confirm_action == "lock":
            # The hotkey was pressed again while the enable confirmation is open:
            # that means "confirm" (see _confirm), not "turn off".
            self._confirm("lock", "Practice Mode", self._LOCK_CONFIRM_MESSAGE,
                          disable_key="confirm_lock_slot")
            with QSignalBlocker(self.ro_btn):
                self.ro_btn.setChecked(True)
            return

        if not checked:
            guard_cfg = self._guard_cfg
            guard_slot = self._guard_slot
            self._guard_slot = None
            self._guard_cfg = None
            watched = self._guard_watcher.files()
            if watched:
                self._guard_watcher.removePaths(watched)
            if guard_cfg is not None:
                try:
                    storage.restore_practice_start(guard_cfg)
                except OSError as e:
                    logger.exception("Practice Mode: failed to restore pre-practice save: game=%r", guard_cfg.name)
                    self._sync_ro_button()
                    self._notify(f"Practice mode off — restore failed: {e}")
                    return
            self._sync_ro_button()
            label = guard_slot.name if guard_slot is not None else "save"
            self._notify(f"'{label}' — practice mode is off — save restored.")
            return

        if self._run_mode:
            self._sync_ro_button()
            self._notify("Run mode is on — disable it before using Practice Mode.")
            return
        warning_shown = False
        if not self._config.protect_warning_acknowledged and not self._protect_warning_shown:
            if self._protect_warning_open:
                # Hotkey pressed again while the warning is open; ignore it.
                with QSignalBlocker(self.ro_btn):
                    self.ro_btn.setChecked(True)
                return
            if self._overlay.isVisible():
                self._notify("Check the main window — a one-time Practice Mode warning needs your attention.")
            self._protect_warning_open = True
            try:
                confirmed = self._show_protect_warning()
            finally:
                self._protect_warning_open = False
            if not confirmed:
                self._sync_ro_button()
                return
            warning_shown = True
        if not warning_shown and self._config.confirm_lock_slot:
            if self._overlay.isVisible() and self._confirm_dialog is None:
                key = _hotkey_label(self._config.overlay_hotkey_ro_toggle) or _hotkey_label(self._config.hotkey_ro_toggle)
                self._notify(f"Press {key} again to confirm enabling Practice Mode." if key
                             else "Trigger Practice Mode again to confirm enabling it.")
            if not self._confirm("lock", "Practice Mode", self._LOCK_CONFIRM_MESSAGE,
                                 disable_key="confirm_lock_slot"):
                self._sync_ro_button()
                return
        if not self._current_slot or self._multi_selected():
            self._sync_ro_button()
            self._notify(self._no_slot_text())
            return
        save_files = storage.slot_save_files(self._current_slot)
        if not save_files:
            self._sync_ro_button()
            return
        game_cfg = self._get_game_cfg()
        if not game_cfg:
            self._sync_ro_button()
            return

        self._guard_slot = self._current_slot
        self._guard_cfg = game_cfg
        try:
            storage.snapshot_practice_start(self._guard_cfg)
            storage.load_save(self._guard_slot, self._guard_cfg, make_backup=False)
        except OSError as e:
            logger.exception("Practice Mode: failed to apply slot on activate: slot=%r", self._current_slot.name)
            self._guard_slot = None
            self._guard_cfg = None
            self._sync_ro_button()
            self._notify(f"Practice Mode: failed to apply — {e}")
            return
        live_paths = [str(f) for f in _live_game_files(game_cfg)]
        if live_paths:
            self._guard_watcher.addPaths(live_paths)

        n = len(save_files)
        label = save_files[0].name if n == 1 else f"{n} files"
        self._sync_ro_button()
        self._notify(f"'{label}' — practice mode is on.")

    def _show_protect_warning(self) -> bool:
        """Show the one-time Practice Mode warning. Returns whether the user confirmed."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Practice Mode — heads up")
        dlg.setMinimumWidth(420)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(14)
        layout.setContentsMargins(20, 20, 20, 16)

        msg = QLabel(
            "<b>Enabling practice mode immediately overwrites the game's current save with the "
            "selected slot, then keeps restoring it any time the game tries to change it.</b><br><br>"
            "Your current save is kept as a backup before the overwrite. The game will not be able to "
            "save progress while practice mode is on — any new save data the game tries to write "
            "will be lost. Disable practice mode before saving in-game.<br><br>"
            "This feature may not work correctly on all games, behaviour depends on how the "
            "game handles save files and has not been tested with every title."
        )
        msg.setWordWrap(True)
        msg.setTextFormat(Qt.RichText)
        layout.addWidget(msg)

        never_checkbox = QCheckBox("Don't show this again")
        layout.addWidget(never_checkbox)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        confirm_btn = QPushButton("Got it")
        confirm_btn.setObjectName("primaryBtn")
        confirm_btn.setDefault(True)
        btn_row.addWidget(confirm_btn)
        layout.addLayout(btn_row)

        def _on_confirm():
            if never_checkbox.isChecked():
                self._config.protect_warning_acknowledged = True
                config.save_config(self._config)
            else:
                self._protect_warning_shown = True
            dlg.accept()

        confirm_btn.clicked.connect(_on_confirm)
        return dlg.exec() == QDialog.Accepted

    def _on_guarded_file_changed(self, path: str) -> None:
        if self._guard_slot is None or self._guard_cfg is None:
            return
        # Stop watching so our own restore doesn't retrigger this, and give the
        # game time to finish writing before restoring.
        self._guard_watcher.removePath(path)
        QTimer.singleShot(500, lambda: self._do_guard_restore(path))

    def _do_guard_restore(self, path: str) -> None:
        if self._guard_slot is None or self._guard_cfg is None:
            return
        try:
            storage.load_save(self._guard_slot, self._guard_cfg, make_backup=False)
            self._notify(f"Protected: restored '{self._guard_slot.name}'.", 4000)
        except OSError as e:
            logger.exception("Lock restore failed: slot=%r path=%s", self._guard_slot.name, path)
            self._notify(f"Lock: restore failed — {e}")
        self._guard_watcher.addPath(path)

    # ── Undo ──
    def _push_undo(self, label: str, fn: Callable[[], None]) -> None:
        self._undo_stack.append((label, fn))
        del self._undo_stack[:-20]

    def _undo_last(self) -> None:
        if not self._undo_stack:
            self._notify("Nothing to undo.")
            return
        label, fn = self._undo_stack.pop()
        self._flush_notes()
        self._flush_video()
        self._current_slot = None
        try:
            fn()
        except Exception as e:
            logger.exception("Undo failed: %s", label)
            self._notify(f"Could not undo '{label}': {e}")
            return
        self._reload_slots()
        self._notify(f"Undid: {label}.")

    def _rename_selected(self) -> None:
        entry = self._selected_entry()
        if isinstance(entry, storage.SaveFolder):
            self._on_rename_folder(entry)
        elif isinstance(entry, storage.SaveSlot):
            self._on_rename_slot()

    def _delete_selected(self) -> None:
        """Delete key: the selected slots, or the selected folder."""
        entry = self._selected_entry()
        if isinstance(entry, storage.SaveFolder) and not self._multi_selected():
            self._on_delete_folder(entry)
            return
        slots = self._selected_slots()
        if len(slots) > 1:
            self._delete_many(slots)
        elif slots:
            self._on_delete_slot()

    def _delete_many(self, slots: list[storage.SaveSlot]) -> None:
        soft = self._config.soft_delete
        if self._config.confirm_delete:
            msg = (f"Move {len(slots)} slots to trash?" if soft
                   else f"Permanently delete {len(slots)} slots?")
            if not self._confirm("delete", "Delete slots", msg, disable_key="confirm_delete"):
                return
        self._flush_notes()
        self._flush_video()
        self._current_slot = None
        done: list[tuple[Path, Path]] = []
        failed = ""
        for slot in slots:
            try:
                stash = storage.stash_for_undo(slot.path)
                try:
                    storage.delete_slot(slot, soft=soft)
                except Exception:
                    storage.discard_stash(stash)
                    raise
                done.append((stash, slot.path))
            except Exception as e:
                logger.exception("Delete slot failed: %r", slot.name)
                failed = f"'{slot.name}': {e}"
        if done:
            def undo(items=tuple(done)) -> None:
                for stash, dest in items:
                    storage.restore_stash(stash, dest)
            self._push_undo(f"delete {len(done)} slots", undo)
        self._reload_slots()
        self._notify(f"Delete failed: {failed}" if failed else f"Deleted {len(done)} slots.")

    def _on_import_save(self) -> None:
        cfg = self._get_game_cfg()
        if not cfg or not self._validate_game_save_path(cfg):
            return
        profile = self.profile_combo.currentText()
        if not profile:
            self._notify("No profile selected.")
            return
        if self._config.auto_name_imports:
            name = storage.auto_slot_name(cfg.name, profile)
        else:
            name, ok = QInputDialog.getText(self, "Import Save", "Slot name:")
            if not ok or not name.strip():
                return
            try:
                name = storage.validate_entry_name(name)
            except ValueError as e:
                self._notify(str(e))
                return
        self._import_as(cfg, profile, name)

    def _import_as(self, cfg: storage.GameConfig, profile: str, name: str) -> bool:
        """Import the live save as a new slot and select it. Returns whether it succeeded."""
        if storage.name_taken(cfg.name, profile, name):
            self._notify(f"A slot or folder named '{name}' already exists.")
            return False
        try:
            storage.import_save(cfg.name, profile, name, cfg)
        except Exception as e:
            logger.exception("Import save failed: game=%r profile=%r slot=%r", cfg.name, profile, name)
            self._notify(f"Import failed: {e}")
            return False
        slot_path = storage.SAVES_DIR / cfg.name / profile / name
        self._push_undo(f"import '{name}'",
                        lambda: storage.remove_path(slot_path))
        self._reload_slots(name)
        self._notify(f"Imported '{name}'.")
        return True

    def _confirm(self, action: str, title: str, message: str,
                 disable_key: Optional[str] = None) -> bool:
        """Show a Yes/No dialog that the same hotkey can confirm.

        If a dialog for the same action is already open, this accepts it and returns
        False (the pending call proceeds). If disable_key is given, a "Don't ask again"
        checkbox sets that Config field to False.
        """
        if self._confirm_dialog is not None:
            if self._confirm_action == action:
                btn = self._confirm_dialog.button(QMessageBox.Yes)
                if btn:
                    btn.click()
            return False

        dlg = QMessageBox(self)
        dlg.setWindowTitle(title)
        dlg.setText(message)
        dlg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        # Delete and replace default to Yes (Enter confirms); other prompts stay on No.
        dlg.setDefaultButton(QMessageBox.Yes if action in ("delete", "replace") else QMessageBox.No)

        checkbox: Optional[QCheckBox] = None
        if disable_key:
            checkbox = QCheckBox("Don't ask again")
            dlg.setCheckBox(checkbox)

        self._confirm_dialog = dlg
        self._confirm_action = action
        result = dlg.exec()
        self._confirm_dialog = None
        self._confirm_action = None

        confirmed = result == QMessageBox.Yes
        if confirmed and checkbox is not None and checkbox.isChecked():
            setattr(self._config, disable_key, False)
            config.save_config(self._config)

        return confirmed

    def _on_replace_save(self) -> None:
        slot = self._selected_slot()
        if slot is None:
            self._notify(self._no_slot_text())
            return
        cfg = self._get_game_cfg()
        if not cfg or not self._validate_game_save_path(cfg):
            return
        if self._guard_slot is not None and self._guard_slot.path == slot.path:
            self._notify(f"'{slot.name}' has practice mode active — disable it before replacing.")
            return
        if self._config.confirm_replace:
            if not self._confirm("replace", "Replace save",
                                 f"Overwrite '{slot.name}' with the current game save?\n\nThis cannot be undone.",
                                 disable_key="confirm_replace"):
                return
        try:
            storage.replace_save(slot, cfg)
        except Exception as e:
            logger.exception("Replace save failed: game=%r profile=%r slot=%r",
                             slot.game, slot.profile, slot.name)
            self._notify(f"Replace failed: {e}")
            return
        self._reload_slots(slot.name)
        self._notify(f"Replaced '{slot.name}'.")
        self._flash("Replace Successful", "#d9822b")

    def _on_load_save(self) -> None:
        if self._run_mode:
            self._notify("Run mode is on — disable it before loading a save.")
            return
        slot = self._selected_slot()
        if slot is None:
            self._notify(self._no_slot_text())
            return
        cfg = self._get_game_cfg()
        if not cfg or not self._validate_game_save_path(cfg):
            return
        try:
            storage.load_save(slot, cfg)
        except Exception as e:
            logger.exception("Load save failed: game=%r profile=%r slot=%r",
                             slot.game, slot.profile, slot.name)
            self._notify(f"Load failed: {e}")
            return

        # Practice Mode now protects the slot that was just loaded.
        if self._guard_cfg is not None and self._guard_cfg.name == cfg.name:
            self._guard_slot = slot

        self._notify(f"Loaded '{slot.name}' to game save.")
        self._flash("Load Successful", "#2e9e5b")

    def _on_delete_slot(self) -> None:
        slot = self._selected_slot()
        if slot is None:
            self.status_bar.showMessage(self._no_slot_text())
            return
        visible = self._visible_slots()
        index = visible.index(slot)
        neighbour = (visible[index + 1] if index + 1 < len(visible)
                     else visible[index - 1] if index > 0 else None)
        soft = self._config.soft_delete
        if self._config.confirm_delete:
            msg = (f"Move '{slot.name}' to trash?" if soft
                   else f"Permanently delete '{slot.name}'?\n\nThis cannot be undone.")
            if not self._confirm("delete", "Delete slot", msg, disable_key="confirm_delete"):
                return
        self._flush_notes()
        self._flush_video()
        self._current_slot = None
        try:
            stash = storage.stash_for_undo(slot.path)
            try:
                storage.delete_slot(slot, soft=soft)
            except Exception:
                storage.discard_stash(stash)
                raise
        except Exception as e:
            logger.exception("Delete slot failed: game=%r profile=%r slot=%r soft=%s",
                             slot.game, slot.profile, slot.name, soft)
            self.status_bar.showMessage(f"Delete failed: {e}")
            return
        slot_path = slot.path
        self._push_undo(f"delete '{slot.name}'",
                        lambda: storage.restore_stash(stash, slot_path))
        self._reload_slots(neighbour.name if neighbour else "")
        self.status_bar.showMessage(f"{'Moved to trash' if soft else 'Deleted'}: '{slot.name}'.")

    def _on_add_game(self) -> None:
        dlg = AddGameDialog(self)
        if not dlg.exec():
            return
        name, mode, path_text = dlg.result
        try:
            name = storage.validate_entry_name(name)
        except ValueError as e:
            self._notify(str(e))
            return
        if any(g.name.lower() == name.lower() for g in self._games):
            self._notify(f"'{name}' is already in the game list.")
            return
        if mode == "files":
            game_cfg = storage.GameConfig(name, "", "files", split_paths(path_text))
        else:
            game_cfg = storage.GameConfig(name, path_text, mode)
        try:
            storage.save_game_config(game_cfg)
        except OSError as e:
            logger.exception("Add game failed: game=%r mode=%r", name, mode)
            self._notify(f"Could not add game: {e}")
            return
        logger.info("Game added from main window: game=%r mode=%r", name, mode)
        self._games = storage.load_games()
        self._disable_practice_mode_for_context_switch()
        self._load_data(name)
        if self.profile_combo.count():
            self._notify(f"Added '{name}'.")
        else:
            self._notify(f"Added '{name}'. Press + next to Profile to create a profile.")

    def _on_manage_profiles(self) -> None:
        game = self.game_combo.currentText()
        if not game:
            return
        previous = self.profile_combo.currentText()
        ProfilesDialog(game, self).exec()
        self._flush_notes()
        self._flush_video()
        self._current_slot = None
        self._reload_profiles(previous)
        self._reload_slots()

    def _on_rename_slot(self) -> None:
        slot = self._selected_slot()
        if slot is None:
            return
        name, ok = QInputDialog.getText(self, "Rename slot", "New name:", text=slot.name)
        if not ok or not name.strip() or name.strip() == slot.name:
            return
        self._rename_current_slot(name.strip())

    def _rename_current_slot(self, name: str) -> bool:
        """Rename the selected slot and return whether a rename occurred."""
        slot = self._selected_slot()
        name = name.strip()
        if slot is None or not name or name == slot.name:
            return False
        try:
            name = storage.validate_entry_name(name)
        except ValueError as e:
            self._notify(str(e))
            return False
        # Names are unique across the whole profile, folders included. Ignoring the slot's
        # own name allows case-only renames on case-insensitive filesystems.
        game_name = self.game_combo.currentText()
        profile_name = self.profile_combo.currentText()
        if storage.name_taken(game_name, profile_name, name, ignore=slot.name):
            self._notify(f"A slot or folder named '{name}' already exists.")
            return False
        old_name = slot.name
        try:
            storage.rename_slot(slot, name)
        except Exception as e:
            logger.exception("Rename slot failed: game=%r profile=%r %r -> %r",
                             slot.game, slot.profile, slot.name, name)
            self._notify(f"Rename failed: {e}")
            return False
        # Keep the slot's position in the custom order, even if another sort is active.
        order = storage.load_slot_order(game_name, profile_name)
        if old_name in order:
            storage.save_slot_order(game_name, profile_name,
                                    [name if n == old_name else n for n in order])
        new_path = slot.path
        self._push_undo(f"rename '{old_name}'",
                        lambda: new_path.rename(new_path.parent / old_name))
        self._reload_slots(name)
        self._notify(f"Renamed to '{name}'.")
        return True

    def _begin_overlay_text_entry(self, initial_text: str) -> bool:
        """Show the overlay's name editor and start capturing keystrokes for it."""
        self._overlay.begin_rename(initial_text)
        # Stop the action hotkeys first: the text listener suppresses keystrokes on
        # Windows, so they'd miss the trigger key's release and think it's held.
        self._overlay_action_hotkeys.stop()
        if self._overlay_rename_input.start(initial_text):
            return True
        self._overlay.end_rename()
        self._start_overlay_action_hotkeys()
        self._notify("Global keyboard input is unavailable.")
        return False

    def _begin_overlay_rename(self) -> None:
        if slot := self._selected_slot():
            self._overlay_text_mode = "rename"
            self._begin_overlay_text_entry(slot.name)

    def _on_overlay_import_triggered(self) -> None:
        """Import directly when auto-naming, otherwise ask for the name in the overlay."""
        if self._config.auto_name_imports:
            self._on_import_save()
            return
        cfg = self._get_game_cfg()
        if not cfg or not self._validate_game_save_path(cfg):
            return
        if not self.profile_combo.currentText():
            self._notify("No profile selected.")
            return
        self._overlay_text_mode = "import"
        self._begin_overlay_text_entry("")

    def _on_overlay_text_submitted(self, name: str) -> None:
        if self._overlay_text_mode == "import":
            self._on_overlay_import_requested(name)
        else:
            self._on_overlay_rename_requested(name)

    def _on_overlay_rename_requested(self, name: str) -> None:
        self._overlay_rename_input.stop()
        name = name.strip()
        slot = self._selected_slot()
        if not name or (slot is not None and name == slot.name):
            self._finish_overlay_text_entry()
            return
        if self._rename_current_slot(name):
            self._finish_overlay_text_entry()
            self._refresh_overlay()
        elif self._overlay.isVisible():
            self._overlay_rename_input.start(name)  # keep the prompt open to fix the name

    def _on_overlay_import_requested(self, name: str) -> None:
        self._overlay_rename_input.stop()
        name = name.strip()
        cfg = self._get_game_cfg()
        profile = self.profile_combo.currentText()
        if not name or not cfg or not profile:
            self._finish_overlay_text_entry()
            return
        try:
            name = storage.validate_entry_name(name)
        except ValueError as e:
            self._notify(str(e))
            if self._overlay.isVisible():
                self._overlay_rename_input.start(name)
            return
        if self._import_as(cfg, profile, name) or not self._overlay.isVisible():
            self._finish_overlay_text_entry()
            self._refresh_overlay()
        else:
            self._overlay_rename_input.start(name)

    def _cancel_overlay_text_entry(self) -> None:
        self._overlay_rename_input.stop()
        self._finish_overlay_text_entry()

    def _finish_overlay_text_entry(self) -> None:
        self._overlay.end_rename()
        if self._overlay.isVisible():
            self._start_overlay_action_hotkeys()

    def _on_slot_context_menu(self, pos) -> None:
        item = self.slot_list.itemAt(pos)
        if not item:
            return
        row = self.slot_list.row(item)
        if not item.isSelected():  # right-clicking inside a selection keeps it
            self.slot_list.setCurrentRow(row)

        menu = QMenu(self)
        entry = self._rows[row]
        if isinstance(entry, storage.SaveFolder):
            # A folder only organises slots: it can be renamed but never loaded.
            menu.addAction("Rename", lambda: self._on_rename_folder(entry))
            delete = menu.addAction("Delete folder", lambda: self._on_delete_folder(entry))
            if entry.slots:
                delete.setEnabled(False)
                delete.setToolTip("Move or delete the slots inside first")
            menu.exec(self.slot_list.mapToGlobal(pos))
            return

        selected = self._selected_slots()
        if len(selected) > 1 and entry in selected:
            # Several slots selected: only moving them makes sense.
            self._add_move_menu(menu, selected)
            menu.exec(self.slot_list.mapToGlobal(pos))
            return

        menu.addAction("Rename", self._on_rename_slot)
        menu.addAction("Load save", self._on_load_save)
        if self._current_slot and self._current_slot.video_url.strip():
            url = self._current_slot.video_url.strip()
            menu.addAction("Open video in browser", lambda: QDesktopServices.openUrl(QUrl(url)))
        menu.addSeparator()
        menu.addAction("Duplicate", self._on_duplicate_slot)
        menu.addAction("Copy to profile…", lambda: self._on_copy_to_profile(move=False))
        menu.addAction("Move to profile…", lambda: self._on_copy_to_profile(move=True))
        self._add_move_menu(menu, [entry])
        menu.addSeparator()
        menu.addAction("Delete", self._on_delete_slot)
        menu.exec(self.slot_list.mapToGlobal(pos))

    # Folders

    def _folder_key(self, name: str) -> tuple[str, str, str]:
        return (self.game_combo.currentText(), self.profile_combo.currentText(), name)

    def _on_add_folder(self) -> None:
        game, profile = self.game_combo.currentText(), self.profile_combo.currentText()
        if not game or not profile:
            self._notify("No profile selected.")
            return
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if not ok or not name.strip():
            return
        try:
            folder = storage.create_folder(game, profile, name)
        except ValueError as e:
            self._notify(str(e))
            return
        except OSError as e:
            logger.exception("Create folder failed: game=%r profile=%r folder=%r", game, profile, name)
            self._notify(f"Could not create folder: {e}")
            return
        self._push_undo(f"new folder '{folder.name}'",
                        lambda: shutil.rmtree(folder.path))
        self._reload_slots(select_folder=folder.name)
        self._notify(f"Created folder '{folder.name}'.")

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        """Double-clicking a folder collapses or expands it (one click only highlights it)."""
        entry = item.data(Qt.UserRole)
        if not isinstance(entry, storage.SaveFolder):
            return
        key = self._folder_key(entry.name)
        if key in self._collapsed:
            self._collapsed.discard(key)
        else:
            self._collapsed.add(key)
        # Rebuild after the click has finished, since it replaces the clicked row.
        QTimer.singleShot(0, lambda: self._populate_list(select_folder=entry.name))

    def _on_rename_folder(self, folder: storage.SaveFolder) -> None:
        name, ok = QInputDialog.getText(self, "Rename folder", "New name:", text=folder.name)
        name = name.strip()
        if not ok or not name or name == folder.name:
            return
        game, profile = self.game_combo.currentText(), self.profile_combo.currentText()
        try:
            storage.rename_folder(game, profile, folder.name, name)
        except ValueError as e:
            self._notify(str(e))
            return
        except OSError as e:
            logger.exception("Rename folder failed: game=%r profile=%r %r -> %r",
                             game, profile, folder.name, name)
            self._notify(f"Rename failed: {e}")
            return
        if self._folder_key(folder.name) in self._collapsed:
            self._collapsed.discard(self._folder_key(folder.name))
            self._collapsed.add(self._folder_key(name))
        old_name = folder.name
        self._push_undo(f"rename folder '{old_name}'",
                        lambda: storage.rename_folder(game, profile, name, old_name))
        self._reload_slots(self._current_slot_name(), select_folder=name)
        self._notify(f"Renamed folder to '{name}'.")

    def _on_delete_folder(self, folder: storage.SaveFolder) -> None:
        game, profile = self.game_combo.currentText(), self.profile_combo.currentText()
        path = storage.SAVES_DIR / game / profile / folder.name
        count = len(folder.slots)
        if count and not self._confirm(
                "delete", "Delete folder",
                f"Delete folder '{folder.name}' and the {count} slot(s) in it?\n\n"
                "You can undo this with Ctrl+Z."):
            return
        self._flush_notes()
        self._flush_video()
        self._current_slot = None
        try:
            stash = storage.stash_for_undo(path)
            try:
                storage.delete_folder(game, profile, folder.name)
            except Exception:
                storage.discard_stash(stash)
                raise
        except (ValueError, OSError) as e:
            self._notify(str(e))
            return
        self._push_undo(f"delete folder '{folder.name}'",
                        lambda: storage.restore_stash(stash, path))
        self._collapsed.discard(self._folder_key(folder.name))
        self._reload_slots()
        self._notify(f"Deleted folder '{folder.name}'.")

    def _add_move_menu(self, menu: QMenu, slots: list[storage.SaveSlot]) -> None:
        """Add a "Move to folder" submenu for the given slots."""
        title = "Move to folder" if len(slots) == 1 else f"Move {len(slots)} slots to folder"
        sub_menu = menu.addMenu(title)
        for folder in self._folders:
            if all(s.folder == folder.name for s in slots):
                continue
            sub_menu.addAction(
                folder.name,
                lambda _checked=False, name=folder.name: self._move_slots(slots, name))
        if any(s.folder for s in slots):
            if not sub_menu.isEmpty():
                sub_menu.addSeparator()
            sub_menu.addAction("Remove from folder", lambda: self._move_slots(slots, ""))
        sub_menu.setEnabled(not sub_menu.isEmpty())

    def _move_slots(self, slots: list[storage.SaveSlot], folder: str,
                    before: str = "", after: bool = False) -> None:
        """Move slots into a folder ("" = top level). In Custom sort, `before` is the slot
        they should sit next to (after it if `after`)."""
        game, profile = self.game_combo.currentText(), self.profile_combo.currentText()
        self._flush_notes()
        self._flush_video()
        names = tuple(s.name for s in slots)
        if before in names:  # dropped onto one of the dragged slots
            before = ""
        moved, failed = 0, []
        moves: list[tuple[str, str, str]] = []
        for slot in slots:
            if slot.folder == folder:
                continue
            old_path = slot.path
            old_folder = slot.folder
            try:
                storage.move_slot_to_folder(slot, folder)
            except (ValueError, OSError) as e:
                logger.exception("Move slot failed: game=%r profile=%r slot=%r -> folder %r",
                                 game, profile, slot.name, folder)
                failed.append(f"'{slot.name}': {e}")
                continue
            moved += 1
            moves.append((slot.name, old_folder, folder))
            # Practice Mode keeps its own copy of the slot, which has just changed location.
            if self._guard_slot is not None and self._guard_slot.path == old_path:
                self._guard_slot.path = slot.path
                self._guard_slot.folder = slot.folder
        if folder:
            self._collapsed.discard(self._folder_key(folder))
        if moves:
            def undo_move(items=tuple(moves)) -> None:
                for name, src_folder, dst_folder in items:
                    storage.move_slot_by_name(game, profile, name, dst_folder, src_folder)
            self._push_undo("move" if len(moves) == 1 else f"move {len(moves)} slots", undo_move)
        if self._config.slot_sort == "custom" and before:
            self._place_in_custom_order([s.name for s in slots if s.folder == folder],
                                        folder, before, after)
        self._reload_slots(names[0] if names else "", also_select=names)
        if failed:
            self._notify(f"Could not move {len(failed)} slot(s): {failed[0]}")
        elif moved:
            where = f"into '{folder}'" if folder else "out of their folder"
            what = f"'{slots[0].name}'" if len(slots) == 1 else f"{moved} slots"
            self._notify(f"Moved {what} {where}.")

    def _place_in_custom_order(self, names: list[str], folder: str, ref: str, after: bool) -> None:
        game, profile = self.game_combo.currentText(), self.profile_combo.currentText()
        folders, loose = storage.load_profile_tree(game, profile, "custom", False)
        container = next((f.slots for f in folders if f.name == folder), None) if folder else loose
        if container is None or ref in names:
            return
        moving = [s for s in container if s.name in names]
        if not moving:
            return
        rest = [s for s in container if s.name not in names]
        index = next((i for i, s in enumerate(rest) if s.name == ref), len(rest))
        index += 1 if after else 0
        container[:] = rest[:index] + moving + rest[index:]
        storage.save_tree_order(game, profile, folders, loose)

    def _on_entries_dropped(self, source_rows: list, target_row: int, where: str) -> None:
        """Rows were dragged onto another: onto a folder files the slots there, onto a
        slot puts them in that slot's folder (or the top level). Folders are never moved."""
        slots = [self._rows[r] for r in source_rows
                 if 0 <= r < len(self._rows) and isinstance(self._rows[r], storage.SaveSlot)]
        if not slots:
            return
        target = self._rows[target_row] if 0 <= target_row < len(self._rows) else None
        if isinstance(target, storage.SaveFolder):
            folder, before, after = target.name, "", False
        elif isinstance(target, storage.SaveSlot):
            folder, before, after = target.folder, target.name, where == "below"
        else:
            folder, before, after = "", "", False
        # Let the drag finish before the list is rebuilt under it.
        QTimer.singleShot(0, lambda: self._move_slots(slots, folder, before, after))

    def _on_duplicate_slot(self) -> None:
        slot = self._selected_slot()
        if slot is None:
            return
        new_name = storage.duplicate_slot_name(slot.game, slot.profile, slot.name)
        try:
            storage.duplicate_slot(slot, new_name)
        except Exception as e:
            logger.exception("Duplicate slot failed: game=%r profile=%r slot=%r",
                             slot.game, slot.profile, slot.name)
            self.status_bar.showMessage(f"Duplicate failed: {e}")
            return
        self._reload_slots(new_name)
        self.status_bar.showMessage(f"Duplicated as '{new_name}'.")

    def _on_copy_to_profile(self, move: bool = False) -> None:
        slot = self._selected_slot()
        if slot is None:
            return
        game = slot.game
        other_profiles = [p for p in storage.load_profiles(game) if p != slot.profile]
        if not other_profiles:
            self.status_bar.showMessage("No other profiles exist for this game.")
            return

        verb = "Move" if move else "Copy"
        if len(other_profiles) == 1:
            target_profile = other_profiles[0]
        else:
            target_profile, ok = QInputDialog.getItem(
                self, f"{verb} to profile", "Target profile:", other_profiles, 0, False
            )
            if not ok:
                return

        new_name = slot.name
        if storage.name_taken(game, target_profile, new_name):
            new_name = storage.duplicate_slot_name(game, target_profile, slot.name)

        try:
            storage.copy_slot_to_profile(slot, target_profile, new_name)
        except Exception as e:
            logger.exception("Copy slot to profile failed: game=%r %r/%r -> %r/%r",
                             slot.game, slot.profile, slot.name, target_profile, new_name)
            self.status_bar.showMessage(f"{verb} failed: {e}")
            return

        if move:
            self._flush_notes()
            self._flush_video()
            self._current_slot = None
            try:
                storage.delete_slot(slot, soft=self._config.soft_delete)
            except Exception as e:
                logger.exception("Move: delete after copy failed: game=%r %r/%r -> %r",
                                 slot.game, slot.profile, slot.name, target_profile)
                self.status_bar.showMessage(f"Copy succeeded but delete failed: {e}")
                return
            self._reload_slots()
            self.status_bar.showMessage(f"Moved '{slot.name}' to profile '{target_profile}'.")
        else:
            suffix = f" as '{new_name}'." if new_name != slot.name else "."
            self.status_bar.showMessage(f"Copied '{slot.name}' to profile '{target_profile}'{suffix}")

    def _on_run_mode_toggled(self, on: bool) -> None:
        self._run_mode = on
        self.load_btn.setEnabled(not on)
        if on:
            if self.ro_btn.isChecked():
                self.ro_btn.setChecked(False)
            self._sync_ro_button()
            self._run_backup_timer.start()
            self._on_run_backup_tick()
            self.status_bar.showMessage(
                "Run mode on — load and lock disabled, backing up every 2 min.", 5000
            )
        else:
            self._run_backup_timer.stop()
            self._sync_ro_button()
            self.status_bar.showMessage("Run mode off.", 3000)

    def _on_run_backup_tick(self) -> None:
        cfg = self._get_game_cfg()
        if not cfg:
            return
        try:
            storage.take_run_backup(cfg)
            self.status_bar.showMessage("Run backup saved.", 2000)
        except Exception as e:
            logger.exception("Run backup failed: game=%r", cfg.name)
            self.status_bar.showMessage(f"Run backup failed: {e}", 3000)

    def show_first_run_dialog(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("No settings found")
        dlg.setMinimumWidth(380)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(14)
        layout.setContentsMargins(20, 20, 20, 16)

        msg = QLabel(
            "No settings found. Would you like to configure the application?<br><br>"
            "You'll need to add your games and their save file paths before you can get started."
        )
        msg.setWordWrap(True)
        msg.setTextFormat(Qt.RichText)
        layout.addWidget(msg)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        later_btn = QPushButton("Maybe later")
        later_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(later_btn)
        open_btn = QPushButton("Open Settings")
        open_btn.setObjectName("primaryBtn")
        open_btn.setDefault(True)
        open_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(open_btn)
        layout.addLayout(btn_row)

        if dlg.exec() == QDialog.Accepted:
            self._on_open_settings()

    def _stop_main_hotkeys(self) -> None:
        self._global_hotkeys.stop()
        self._global_hotkeys_started = False
        for sc in self._shortcuts:
            sc.setEnabled(False)

    def _suspend_hotkeys(self) -> None:
        """Disable every hotkey, so pressing one while recording it in Settings does nothing."""
        self._stop_main_hotkeys()
        self._overlay_toggle_hotkey.stop()
        self._overlay_action_hotkeys.stop()
        if self._overlay_toggle_shortcut is not None:
            self._overlay_toggle_shortcut.setEnabled(False)

    def _restore_hotkeys(self) -> None:
        """Re-enable hotkeys after _suspend_hotkeys(), respecting the overlay's state."""
        if self._overlay.isVisible():
            self._sync_overlay_focus_hotkeys()
        else:
            self._apply_hotkeys()
        self._apply_overlay_toggle_hotkey()

    def _sync_overlay_focus_hotkeys(self) -> None:
        """While the overlay is shown, use the main hotkeys when this window is focused
        and the overlay's global hotkeys otherwise, never both.

        Both sets default to the same keys and pynput ignores focus, so running
        both would fire each action twice.
        """
        if not self._overlay.isVisible():
            return
        # Minimizing doesn't always send an ActivationChange, so check it explicitly.
        if self.isActiveWindow() and not self.isMinimized():
            self._overlay_action_hotkeys.stop()
            self._apply_hotkeys()
        else:
            self._stop_main_hotkeys()
            self._start_overlay_action_hotkeys()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.ActivationChange, QEvent.Type.WindowStateChange):
            self._sync_overlay_focus_hotkeys()

    def _on_main_hotkey(self, action: str) -> None:
        self._main_actions[action]()

    def _on_overlay_hotkey(self, action: str) -> None:
        self._overlay_actions[action]()

    def _main_hotkeys(self) -> dict[str, str]:
        cfg = self._config
        return {
            "import": cfg.hotkey_import,
            "load": cfg.hotkey_load,
            "replace": cfg.hotkey_replace,
            "ro_toggle": cfg.hotkey_ro_toggle,
            "next_slot": cfg.hotkey_next_slot,
            "prev_slot": cfg.hotkey_prev_slot,
        }

    def _overlay_hotkeys(self) -> dict[str, str]:
        cfg = self._config
        return {
            "import": cfg.overlay_hotkey_import,
            "load": cfg.overlay_hotkey_load,
            "replace": cfg.overlay_hotkey_replace,
            "rename": cfg.overlay_hotkey_rename,
            "ro_toggle": cfg.overlay_hotkey_ro_toggle,
            "next_slot": cfg.overlay_hotkey_next_slot,
            "prev_slot": cfg.overlay_hotkey_prev_slot,
        }

    def _apply_overlay_toggle_hotkey(self) -> None:
        if self._overlay_toggle_shortcut is not None:
            self._overlay_toggle_shortcut.setEnabled(False)
            self._overlay_toggle_shortcut.deleteLater()
            self._overlay_toggle_shortcut = None

        key = self._config.hotkey_toggle_overlay
        if not self._overlay_toggle_hotkey.start({"toggle_overlay": key}) and key:
            # No global hotkeys available; fall back to a shortcut that works while focused.
            self._overlay_toggle_shortcut = QShortcut(QKeySequence(key), self)
            self._overlay_toggle_shortcut.activated.connect(self._toggle_overlay)

    def _start_overlay_action_hotkeys(self) -> None:
        self._overlay_action_hotkeys.start(self._overlay_hotkeys())

    def _toggle_overlay(self) -> None:
        if self._overlay.isVisible():
            self._hide_overlay()
        else:
            self._show_overlay()

    def _show_overlay(self) -> None:
        self._overlay.set_opacity(self._config.overlay_opacity)
        self._overlay.show_at_saved_or_default(self._config.overlay_pos_x, self._config.overlay_pos_y)
        self._overlay.show()
        self._refresh_overlay()
        self._sync_overlay_focus_hotkeys()

    def _hide_overlay(self) -> None:
        self._overlay_action_hotkeys.stop()
        self._overlay_rename_input.stop()
        self._overlay.end_rename()
        self._overlay.hide()
        self._apply_hotkeys()
        self._apply_overlay_toggle_hotkey()

    def _overlay_hotkeys_line(self) -> str:
        cfg = self._config
        labels = [
            ("Import", cfg.overlay_hotkey_import),
            ("Load", cfg.overlay_hotkey_load),
            ("Replace", cfg.overlay_hotkey_replace),
            ("Rename", cfg.overlay_hotkey_rename),
            ("Practice", cfg.overlay_hotkey_ro_toggle),
        ]
        parts = [f"{label} {_hotkey_label(key)}" for label, key in labels if key]

        prev_hk = _hotkey_label(cfg.overlay_hotkey_prev_slot)
        next_hk = _hotkey_label(cfg.overlay_hotkey_next_slot)
        nav = []
        if prev_hk:
            nav.append(f"▲ {prev_hk}")
        if next_hk:
            nav.append(f"{next_hk} ▼")
        if nav:
            parts.append(" ".join(nav))

        if cfg.hotkey_toggle_overlay:
            parts.append(f"Hide {_hotkey_label(cfg.hotkey_toggle_overlay)}")
        return "   ".join(parts)

    def _refresh_overlay(self) -> None:
        if not self._overlay.isVisible():
            return
        slots = self._visible_slots()
        current = self._selected_slot()
        self._overlay.update_content(
            game=self.game_combo.currentText(),
            profile=self.profile_combo.currentText(),
            slot_names=[s.name for s in slots],
            current_row=slots.index(current) if current in slots else -1,
            hotkeys_line=self._overlay_hotkeys_line(),
        )

    def _on_overlay_moved(self, x: int, y: int) -> None:
        self._config.overlay_pos_x = x
        self._config.overlay_pos_y = y
        config.save_config(self._config)

    def _on_open_settings(self) -> None:
        self._suspend_hotkeys()
        prev_game = self.game_combo.currentText()
        prev_profile = self.profile_combo.currentText()
        prev_slot = self._current_slot_name()

        self._games = storage.load_games()
        dlg = SettingsDialog(self._config, self._games, self)
        if not dlg.exec():
            self._restore_hotkeys()
            return

        self._config = dlg.result_config
        config.save_config(self._config)
        self._overlay.set_opacity(self._config.overlay_opacity)
        self._games = dlg.result_games
        storage.save_games(self._games)
        active_names = {g.name for g in self._games}
        for name in dlg.removed_game_names:
            if name not in active_names:
                storage.deactivate_game(name)
        logger.info("Settings saved by user")

        self._load_data(prev_game, prev_profile, prev_slot)
        if self.game_combo.currentText() != prev_game:
            self._disable_practice_mode_for_context_switch()
        self._apply_info_panel()
        self._restore_hotkeys()
        self.status_bar.showMessage("Settings saved.")

    def _ro_btn_text(self, is_on: bool) -> str:
        return _btn_text("Practicing" if is_on else "Practice Mode", self._config.hotkey_ro_toggle)

    def _apply_hotkeys(self) -> None:
        for sc in self._shortcuts:
            sc.setEnabled(False)
            sc.deleteLater()
        self._shortcuts = []

        cfg = self._config
        self.import_btn.setText(_btn_text("Import Save", cfg.hotkey_import))
        self.load_btn.setText(_btn_text("Load Save", cfg.hotkey_load))
        self.replace_btn.setText(_btn_text("Replace Save", cfg.hotkey_replace))
        self.ro_btn.setText(self._ro_btn_text(self.ro_btn.isChecked()))

        hotkeys = self._main_hotkeys()
        started = cfg.global_hotkeys_enabled and self._global_hotkeys.start(hotkeys)
        self._global_hotkeys_started = started
        if started:
            return

        # Fall back to in-app shortcuts. Never add them alongside the global
        # listener, which also fires while focused, or actions double-trigger.
        self._global_hotkeys.stop()
        for action, key in hotkeys.items():
            if key:
                sc = QShortcut(QKeySequence(key), self)
                sc.activated.connect(self._main_actions[action])
                self._shortcuts.append(sc)
        if cfg.global_hotkeys_enabled and any(hotkeys.values()):
            if is_wayland_session():
                msg = "Global hotkeys aren't supported under Wayland (SteamOS) — using in-app shortcuts while focused."
            else:
                msg = "Global hotkeys unavailable — grant Accessibility permission in System Settings and restart."
            self.status_bar.showMessage(msg, 6000)

    def eventFilter(self, obj, event) -> bool:
        # If next/prev slot are bound to the list's own navigation keys (e.g. Up/Down),
        # the global hotkey and the list would both move the selection. Let only the
        # hotkey handle it.
        if obj is self.slot_list and event.type() == QEvent.Type.KeyPress and self._global_hotkeys_started:
            seq = QKeySequence(event.keyCombination()).toString()
            if seq and seq in (self._config.hotkey_next_slot, self._config.hotkey_prev_slot):
                return True
        return super().eventFilter(obj, event)

    def _apply_info_panel(self) -> None:
        show = self._config.show_details
        if not show:
            self._inline_player.unload()
        self._info_panel.setVisible(show)
        # Next to the details the slot list is a column; without them it fills the window.
        self._slot_panel.setMinimumWidth(340 if show else 0)
        self._slot_panel.setMaximumWidth(380 if show else 16777215)
        self._slot_panel.layout().setContentsMargins(0, 0, 8 if show else 0, 0)
        QTimer.singleShot(0, self._sync_minimum_size)

    def _sync_minimum_size(self) -> None:
        hint = self.minimumSizeHint()
        self.setMinimumSize(hint)
        w = max(self.width(), hint.width())
        h = max(self.height(), hint.height())
        if w != self.width() or h != self.height():
            self.resize(w, h)

    def _get_game_cfg(self) -> Optional[storage.GameConfig]:
        game_name = self.game_combo.currentText()
        return next((g for g in self._games if g.name == game_name), None)

    def _validate_game_save_path(self, cfg: storage.GameConfig) -> bool:
        if cfg.save_mode == "files":
            if not cfg.save_paths:
                self._notify("No save files configured for this game.")
                return False
            missing = [p for p in cfg.save_paths if not Path(p).exists()]
            if missing:
                path_str = "(path hidden)" if self._config.hide_paths else missing[0]
                self._notify(f"Save file not found: {path_str}")
                return False
            return True
        if not cfg.save_path:
            self._notify("Save path is not configured for this game.")
            return False
        src = Path(cfg.save_path)
        if not src.exists():
            path_str = "(path hidden)" if self._config.hide_paths else str(src)
            self._notify(f"Save path not found: {path_str}")
            return False
        return True

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._inline_player.update_height(self.height())

    def closeEvent(self, event) -> None:
        logger.info("Application closing: game=%r profile=%r slot=%r",
                    self.game_combo.currentText(),
                    self.profile_combo.currentText(),
                    self._current_slot_name())
        self._run_backup_timer.stop()
        self._global_hotkeys.stop()
        self._overlay_toggle_hotkey.stop()
        self._overlay_action_hotkeys.stop()
        self._overlay_rename_input.stop()
        self._overlay.close()
        self._flush_notes()
        self._flush_video()
        self._config.last_game = self.game_combo.currentText()
        self._config.last_profile = self.profile_combo.currentText()
        self._config.last_slot = self._current_slot_name()
        self._config.window_width = self.width()
        self._config.window_height = self.height()
        config.save_config(self._config)
        super().closeEvent(event)

    def apply_stylesheet(self) -> None:
        path = _UI_DIR / "main_window.qtt"
        try:
            self.setStyleSheet(path.read_text().replace("{ui_dir}", _UI_DIR.as_posix()))
        except FileNotFoundError:
            logger.warning("Stylesheet not found: %s", path)


def _game_path_display(cfg: Optional[storage.GameConfig]) -> str:
    if not cfg:
        return "—"
    if cfg.save_mode == "files":
        n = len(cfg.save_paths)
        return f"{n} file{'s' if n != 1 else ''}" if n else "—"
    return cfg.save_path or "—"


def _live_game_files(game_cfg: storage.GameConfig) -> list[Path]:
    """The live save files that Practice Mode watches."""
    if game_cfg.save_mode == "file":
        return [Path(game_cfg.save_path)]
    if game_cfg.save_mode == "files":
        return [Path(p) for p in game_cfg.save_paths]
    if game_cfg.save_mode == "folder":
        folder = Path(game_cfg.save_path)
        if folder.exists():
            return [f for f in folder.rglob("*") if f.is_file()]
    return []


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d  %H:%M:%S")


class _SlotItem(QWidget):
    def __init__(self, name: str, indented: bool = False) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        # Slots inside a folder are shifted right so they read as its children.
        layout.setContentsMargins(34 if indented else 12, 2, 12, 2)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet("font-size: 13px; font-weight: 500; color: #d4cfc8;")
        layout.addWidget(name_lbl)


class _FolderItem(QWidget):
    """List row for a folder: collapse arrow, folder icon and name."""

    def __init__(self, name: str, expanded: bool) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 2, 12, 2)
        layout.setSpacing(6)

        arrow = QLabel("▾" if expanded else "▸")
        arrow.setFixedWidth(12)
        arrow.setStyleSheet("font-size: 12px; color: #888899;")
        layout.addWidget(arrow)

        icon = QLabel()
        icon.setPixmap(QIcon(str(_UI_DIR / "folder.svg")).pixmap(16, 16))
        icon.setFixedWidth(18)
        layout.addWidget(icon)

        name_lbl = QLabel(name)
        name_lbl.setStyleSheet("font-size: 13px; font-weight: 600; color: #d4cfc8;")
        layout.addWidget(name_lbl, 1)


class _SlotListWidget(QListWidget):
    """Slot/folder list that reports drops instead of moving rows itself.

    The window moves the slots on disk and rebuilds the list, so the list never
    shows something that differs from the folders on disk.
    """

    # dragged rows, target row (-1 when dropped on empty space),
    # "on" / "above" / "below" / "viewport"
    entries_dropped = Signal(list, int, str)

    def dropEvent(self, event) -> None:
        rows = sorted(self.row(item) for item in self.selectedItems())
        if not rows:
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        target_row = self.row(item) if item is not None else -1
        indicator = self.dropIndicatorPosition()
        if indicator == QAbstractItemView.DropIndicatorPosition.OnItem:
            where = "on"
        elif indicator == QAbstractItemView.DropIndicatorPosition.AboveItem:
            where = "above"
        elif indicator == QAbstractItemView.DropIndicatorPosition.BelowItem:
            where = "below"
        else:
            where = "viewport"
        # Not handled by Qt, so it can't remove or duplicate the dragged rows.
        event.ignore()
        self.entries_dropped.emit(rows, target_row, where)


class _InfoRow(QWidget):
    """Label + value pair inside the info box."""

    def __init__(self, label: str, value: str) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._lbl = QLabel(label)
        self._lbl.setObjectName("fieldLabel")
        self._lbl.setFixedWidth(72)
        layout.addWidget(self._lbl)

        self._val = QLabel(value)
        self._val.setStyleSheet("color: #9999aa; font-size: 12px;")
        self._val.setWordWrap(True)
        layout.addWidget(self._val, 1)

    def set_value(self, value: str) -> None:
        self._val.setText(value)


class _Divider(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.HLine)
        self.setFixedHeight(1)
        self.setStyleSheet("background: #2a2a32; border: none;")
