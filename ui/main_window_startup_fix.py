"""Compatibility repair for UI builder methods omitted from an experimental branch edit.

This keeps the startup path small and restores the builder methods that MainWindow.__init__
expects, without changing its save/group behavior.
"""
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)


def install() -> None:
    from ui.main_window import MainWindow, _Divider, _InfoRow

    def _build_info_panel(self):
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

        self._inline_player = self._create_inline_player()
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

    def _create_inline_player(self):
        from ui.video_player import InlineVideoPlayer
        return InlineVideoPlayer()

    def _build_action_bar(self):
        bar = QHBoxLayout()
        bar.setSpacing(6)

        self.import_btn = QPushButton("Import Save")
        self.import_btn.setObjectName("primaryBtn")
        self.import_btn.clicked.connect(self._on_import_save)
        self.replace_btn = QPushButton("Replace Save")
        self.replace_btn.clicked.connect(self._on_replace_save)
        self.load_btn = QPushButton("Load Save")
        self.load_btn.clicked.connect(self._on_load_save)
        self.delete_btn = QPushButton("Delete slot")
        self.delete_btn.setObjectName("dangerBtn")
        self.delete_btn.clicked.connect(self._on_delete_slot)
        for button in (self.import_btn, self.replace_btn, self.load_btn, self.delete_btn):
            bar.addWidget(button)
        bar.addStretch()

        self.ro_btn = QPushButton("Practice Mode")
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

    MainWindow._build_info_panel = _build_info_panel
    MainWindow._build_action_bar = _build_action_bar
