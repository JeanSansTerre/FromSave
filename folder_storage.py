"""Stage-1 storage compatibility for conventional, direct save-folder layouts.

This module is installed before MainWindow construction. It preserves support for
FromSave's legacy ``save_data/`` slots while allowing folder-mode saves to use the
game's directory contents directly. It also exposes folder metadata/storage helpers;
the UI is intentionally not changed in this stage.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)
_FOLDER_MARKER = ".fromsave-folder.json"
_FOLDER_FORMAT = 1
_INSTALLED = False


def install() -> None:
    """Install backward-compatible storage behavior once, before the UI starts."""
    global _INSTALLED
    if _INSTALLED:
        return

    import storage

    original_read_slot = storage._read_slot
    original_slot_save_files = storage.slot_save_files
    original_copy_live_save = storage._copy_live_save
    original_copy_to_live = storage._copy_to_live
    original_import_save = storage.import_save

    def _is_group(path: Path) -> bool:
        return path.is_dir() and (path / _FOLDER_MARKER).is_file()

    def list_folders(game: str, profile: str) -> list[str]:
        root = storage.SAVES_DIR / game / profile
        if not root.is_dir():
            return []
        return sorted((entry.name for entry in root.iterdir() if _is_group(entry)),
                      key=str.casefold)

    def create_folder(game: str, profile: str, name: str) -> Path:
        name = storage.validate_entry_name(name)
        folder = storage.SAVES_DIR / game / profile / name
        folder.mkdir(parents=True, exist_ok=False)
        try:
            (folder / _FOLDER_MARKER).write_text(
                json.dumps({"format": _FOLDER_FORMAT, "name": name}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        logger.info("Created save folder: game=%r profile=%r folder=%r", game, profile, name)
        return folder

    def rename_folder(game: str, profile: str, old_name: str, new_name: str) -> None:
        old_name = storage.validate_entry_name(old_name)
        new_name = storage.validate_entry_name(new_name)
        root = storage.SAVES_DIR / game / profile
        source = root / old_name
        if not _is_group(source):
            raise FileNotFoundError(f"Save folder not found: {old_name}")
        target = root / new_name
        if target.exists() and target != source:
            raise FileExistsError(f"An entry named '{new_name}' already exists.")
        source.rename(target)
        (target / _FOLDER_MARKER).write_text(
            json.dumps({"format": _FOLDER_FORMAT, "name": new_name}, indent=2),
            encoding="utf-8",
        )

    def _valid_data_entry(path: Path) -> bool:
        return path.name not in storage._RESERVED and not path.name.startswith(".")

    def _has_direct_save_data(slot_dir: Path) -> bool:
        try:
            for entry in slot_dir.iterdir():
                if not _valid_data_entry(entry):
                    continue
                if entry.is_file() or (entry.is_dir() and any(
                    child.is_file() for child in entry.rglob("*")
                )):
                    return True
        except OSError:
            logger.exception("Unable to inspect save slot: %s", slot_dir)
        return False

    def read_slot(slot_dir: Path, game: str, profile: str):
        if _is_group(slot_dir):
            return None
        # First use the existing reader unchanged, retaining legacy saves + metadata.
        slot = original_read_slot(slot_dir, game, profile)
        if slot is not None:
            return slot
        # Direct-layout saves without root-level files still count if nested data exists.
        if _has_direct_save_data(slot_dir):
            return original_read_slot(slot_dir, game, profile)
        return None

    def slot_save_files(slot):
        legacy = slot.path / "save_data"
        if legacy.is_dir():
            return [entry for entry in legacy.rglob("*") if entry.is_file()]
        files = []
        try:
            for entry in slot.path.rglob("*"):
                if entry.is_file() and (entry.parent != slot.path or _valid_data_entry(entry)):
                    files.append(entry)
        except OSError:
            logger.exception("Unable to list save files for slot: %s", slot.path)
        return files

    def _replace_direct_folder(src: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        for entry in dest.iterdir():
            if entry.name in storage._RESERVED:
                continue
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        for entry in src.iterdir():
            if not _valid_data_entry(entry):
                continue
            target = dest / entry.name
            shutil.copytree(entry, target) if entry.is_dir() else shutil.copy2(entry, target)

    def copy_live_save(game_cfg, dest: Path, file_name=None) -> None:
        if game_cfg.save_mode != "folder":
            original_copy_live_save(game_cfg, dest, file_name=file_name)
            return
        source = Path(game_cfg.save_path)
        if not source.is_dir():
            raise FileNotFoundError(f"Save folder not found: {source}")
        _replace_direct_folder(source, dest)

    def copy_to_live(src_dir: Path, game_cfg, file_name) -> None:
        if game_cfg.save_mode != "folder":
            original_copy_to_live(src_dir, game_cfg, file_name)
            return
        legacy = src_dir / "save_data"
        destination = Path(game_cfg.save_path)
        if legacy.is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(legacy, destination)
            return
        entries = [entry for entry in src_dir.iterdir() if _valid_data_entry(entry)]
        if not entries:
            raise FileNotFoundError(f"No folder save data in {src_dir}")
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            target = destination / entry.name
            shutil.copytree(entry, target) if entry.is_dir() else shutil.copy2(entry, target)

    def import_save(game: str, profile: str, slot_name: str, game_cfg, folder: str = ""):
        if not folder:
            return original_import_save(game, profile, slot_name, game_cfg)
        folder = storage.validate_entry_name(folder)
        parent = storage.SAVES_DIR / game / profile / folder
        if not _is_group(parent):
            raise FileNotFoundError(f"Save folder not found: {folder}")
        slot_path = parent / storage.validate_entry_name(slot_name)
        slot_path.mkdir(parents=False, exist_ok=False)
        try:
            copy_live_save(game_cfg, slot_path)
            now = datetime.now()
            storage._write_json(slot_path / "meta.json", storage._timestamps(now, now))
            (slot_path / "notes.txt").write_text("", encoding="utf-8")
            slot = original_read_slot(slot_path, game, profile)
            if slot is not None:
                slot.group = folder
            return slot
        except Exception:
            shutil.rmtree(slot_path, ignore_errors=True)
            raise

    storage._read_slot = read_slot
    storage.slot_save_files = slot_save_files
    storage._copy_live_save = copy_live_save
    storage._copy_to_live = copy_to_live
    storage.import_save = import_save
    storage.list_folders = list_folders
    storage.create_folder = create_folder
    storage.rename_folder = rename_folder
    storage.is_folder_group = _is_group
    _INSTALLED = True
