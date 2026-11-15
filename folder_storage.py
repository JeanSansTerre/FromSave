"""Folder/group persistence helpers for FromSave save slots.

This is stage 1 of folder support: it provides the on-disk folder metadata and
safe storage operations without changing the existing UI or slot-list behavior.
A group is a real directory under a profile, distinguished by a small metadata
file so it cannot be confused with a folder-mode save slot.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Optional

import storage

logger = logging.getLogger(__name__)
_FOLDER_METADATA = ".fromsave-folder.json"
_FOLDER_FORMAT = 1


def _profile_dir(game: str, profile: str) -> Path:
    game = storage.validate_entry_name(game)
    profile = storage.validate_entry_name(profile)
    return storage.SAVES_DIR / game / profile


def _folder_path(game: str, profile: str, folder: str) -> Path:
    folder = storage.validate_entry_name(folder)
    return _profile_dir(game, profile) / folder


def is_folder_group(path: Path) -> bool:
    """Whether path is a FromSave group directory (not a save-slot directory)."""
    return path.is_dir() and (path / _FOLDER_METADATA).is_file()


def list_folders(game: str, profile: str) -> list[str]:
    """List marked folders for the selected game/profile, in case-insensitive order."""
    root = _profile_dir(game, profile)
    if not root.is_dir():
        return []
    return sorted((entry.name for entry in root.iterdir() if is_folder_group(entry)),
                  key=str.casefold)


def create_folder(game: str, profile: str, name: str) -> Path:
    """Create an empty group folder and its identifying metadata file."""
    name = storage.validate_entry_name(name)
    folder = _profile_dir(game, profile) / name
    folder.mkdir(parents=True, exist_ok=False)
    try:
        metadata = {"format": _FOLDER_FORMAT, "name": name}
        (folder / _FOLDER_METADATA).write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    logger.info("Created save folder: game=%r profile=%r folder=%r", game, profile, name)
    return folder


def rename_folder(game: str, profile: str, old_name: str, new_name: str) -> None:
    """Rename an existing group directory without touching its contained saves."""
    old_name = storage.validate_entry_name(old_name)
    new_name = storage.validate_entry_name(new_name)
    root = _profile_dir(game, profile)
    source = root / old_name
    if not is_folder_group(source):
        raise FileNotFoundError(f"Save folder not found: {old_name}")
    target = root / new_name
    if target.exists() and target != source:
        raise FileExistsError(f"An entry named '{new_name}' already exists.")
    source.rename(target)
    metadata_path = target / _FOLDER_METADATA
    metadata_path.write_text(json.dumps({"format": _FOLDER_FORMAT, "name": new_name}, indent=2),
                             encoding="utf-8")
    logger.info("Renamed save folder: game=%r profile=%r %r -> %r",
                game, profile, old_name, new_name)


def group_for_slot(slot: storage.SaveSlot) -> str:
    """Return the group name recorded on a slot object, defaulting to ungrouped."""
    return getattr(slot, "group", "") or ""


def load_group_slots(game: str, profile: str, folder: str) -> list[storage.SaveSlot]:
    """Read slots within a marked group; legacy/unmarked directories are not groups."""
    path = _folder_path(game, profile, folder)
    if not is_folder_group(path):
        return []
    slots = []
    for slot_dir in sorted(path.iterdir(), key=lambda item: item.name.casefold()):
        if not slot_dir.is_dir() or slot_dir.name == _FOLDER_METADATA:
            continue
        slot = storage._read_slot(slot_dir, game, profile)
        if slot is not None:
            slot.group = folder
            slots.append(slot)
    return slots


def move_slot(slot: storage.SaveSlot, folder: str = "") -> None:
    """Move a slot between profile root and a marked group, refusing overwrites."""
    target_folder = storage.validate_entry_name(folder) if folder else ""
    profile_root = _profile_dir(slot.game, slot.profile)
    if target_folder:
        target_parent = profile_root / target_folder
        if not is_folder_group(target_parent):
            raise FileNotFoundError(f"Save folder not found: {target_folder}")
    else:
        target_parent = profile_root

    target = target_parent / slot.name
    if target == slot.path:
        slot.group = target_folder
        return
    if target.exists():
        raise FileExistsError(f"A save named '{slot.name}' already exists in that location.")

    slot.path.rename(target)
    slot.path = target
    slot.group = target_folder
    logger.info("Moved save slot: game=%r profile=%r slot=%r folder=%r",
                slot.game, slot.profile, slot.name, target_folder)


def import_save(game: str, profile: str, slot_name: str, game_cfg,
                folder: str = "") -> storage.SaveSlot:
    """Import to a group when folder is given; otherwise delegate unchanged."""
    if not folder:
        return storage.import_save(game, profile, slot_name, game_cfg)
    folder_path = _folder_path(game, profile, folder)
    if not is_folder_group(folder_path):
        raise FileNotFoundError(f"Save folder not found: {folder}")
    slot_name = storage.validate_entry_name(slot_name)
    slot_path = folder_path / slot_name
    slot_path.mkdir(parents=False, exist_ok=False)
    try:
        storage._copy_live_save(game_cfg, slot_path)
        now = __import__("datetime").datetime.now()
        storage._write_json(slot_path / "meta.json", storage._timestamps(now, now))
        (slot_path / "notes.txt").write_text("", encoding="utf-8")
        slot = storage._read_slot(slot_path, game, profile)
        if slot is None:
            raise ValueError("The imported save did not produce a readable slot.")
        slot.group = folder
        return slot
    except Exception:
        shutil.rmtree(slot_path, ignore_errors=True)
        raise


def auto_folder_slot_name(game: str, profile: str, folder: str, base: str = "new save") -> str:
    """Choose an unused slot name inside a marked folder."""
    parent = _folder_path(game, profile, folder)
    if not is_folder_group(parent):
        raise FileNotFoundError(f"Save folder not found: {folder}")
    return storage._unique_name(parent, base)


def duplicate_slot_name_in_folder(slot: storage.SaveSlot, base: Optional[str] = None) -> str:
    """Choose a duplicate name beside the original slot, preserving its group."""
    return storage._unique_name(slot.path.parent, base or f"{slot.name} copy")
