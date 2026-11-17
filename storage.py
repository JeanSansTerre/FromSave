import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import send2trash

from app_paths import app_dir, migrate_from_bundle

migrate_from_bundle("saves")

SAVES_DIR = app_dir() / "saves"
logger = logging.getLogger(__name__)

_RESERVED = {"meta.json", "notes.txt"}
# Older FromSave versions wrapped a copied save folder in save_data/. Other save
# managers keep it under its real name, which is what new slots now do.
_LEGACY_DATA_DIR = "save_data"
# Hidden file that marks a directory inside a profile as an organising folder rather
# than a save slot. Slots from other save managers never have it, so they stay slots.
_FOLDER_MARKER = ".fromsave_folder"
_GAME_CONFIG = "game.json"
_BACKUPS_DIR = "_backups"
_PRACTICE_START_DIR = "_practice_start"
_UNDO_DIR = "_undo"
_MAX_BACKUPS = 3

SORT_MODES = ("name", "created", "modified", "custom")

# Games whose saves/ folder ships in the release zip, with how each stores its save
# ("file" or "folder"). A fresh install configures them without asking.
BUNDLED_GAMES = {
    "Armored Core VI": "file",
    "Bloodborne": "folder",
    "Dark Souls II Scholar of the First Sin": "file",
    "Dark Souls III": "file",
    "Dark Souls Remastered": "file",
    "Demon's Souls": "folder",
    "Elden Ring": "file",
    "Sekiro": "file",
}

# Slot and profile names become directory names, so reject path separators,
# Windows-invalid characters and control characters ("a/b" would nest folders).
_BAD_NAME_CHARS = set('<>:"/\\|?*') | {chr(c) for c in range(32)}


def validate_entry_name(name: str) -> str:
    """Return the trimmed name, or raise ValueError with a user-facing message."""
    name = name.strip()
    if not name:
        raise ValueError("Name cannot be empty")
    if len(name) > 120:
        raise ValueError("Name is too long")
    if any(c in _BAD_NAME_CHARS for c in name) or name in (".", "..") or name.endswith("."):
        raise ValueError("Name contains characters that are not allowed")
    return name


@dataclass
class GameConfig:
    name: str
    save_path: str                           # "file" and "folder" modes
    save_mode: str = "file"                  # "file" | "files" | "folder"
    save_paths: list[str] = field(default_factory=list)  # "files" mode


@dataclass
class SaveSlot:
    name: str
    game: str
    profile: str
    path: Path
    date_created: Optional[datetime]
    date_modified: Optional[datetime]
    notes: str
    save_file: Optional[str]  # None for folder/files modes
    video_url: str = ""
    data_dir_name: Optional[str] = None  # folder-mode slots: name of the saved folder
    folder: str = ""  # name of the organising folder holding this slot, "" if none
    bare: bool = False  # the slot is one save file sitting directly in the profile/folder


@dataclass
class SaveFolder:
    """A folder inside a profile that groups slots. It is not a save itself."""
    name: str
    path: Path
    slots: list[SaveSlot] = field(default_factory=list)


# JSON helpers

def _read_json_dict(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to read JSON: %s", path)
        return {}


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=4), encoding="utf-8")


def _timestamps(created: datetime, modified: datetime) -> dict:
    return {
        "created": created.isoformat(timespec="seconds"),
        "modified": modified.isoformat(timespec="seconds"),
    }


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Invalid ISO datetime in metadata: %r", value)
        return None


# Games

def _game_dirs() -> list[Path]:
    if not SAVES_DIR.exists():
        return []
    return sorted(
        (d for d in SAVES_DIR.iterdir() if d.is_dir() and not d.name.startswith("_")),
        key=lambda d: d.name.lower(),
    )


def find_unconfigured_games() -> list[str]:
    """Return names of game directories that exist but have no game.json."""
    return [d.name for d in _game_dirs() if not (d / _GAME_CONFIG).exists()]


def load_games() -> list[GameConfig]:
    games = []
    for game_dir in _game_dirs():
        data = _read_json_dict(game_dir / _GAME_CONFIG)
        if not data.get("active", True):
            continue
        games.append(GameConfig(
            name=game_dir.name,
            save_path=data.get("save_path", ""),
            save_mode=data.get("save_mode", "file"),
            save_paths=data.get("save_paths", []),
        ))
    logger.info("Loaded %d game config(s) from saves directory", len(games))
    return games


def save_game_config(game_cfg: GameConfig) -> None:
    game_dir = SAVES_DIR / game_cfg.name
    game_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {"active": True, "save_mode": game_cfg.save_mode}
    if game_cfg.save_mode == "files":
        data["save_paths"] = game_cfg.save_paths
    else:
        data["save_path"] = game_cfg.save_path
    _write_json(game_dir / _GAME_CONFIG, data)
    logger.info("Wrote game config: game=%r mode=%r", game_cfg.name, game_cfg.save_mode)


def save_games(games: list[GameConfig]) -> None:
    for game_cfg in games:
        save_game_config(game_cfg)


def deactivate_game(name: str) -> None:
    """Hide a game from the game list without deleting its saves."""
    game_dir = SAVES_DIR / name
    if not game_dir.exists():
        return
    data = _read_json_dict(game_dir / _GAME_CONFIG)
    data["active"] = False
    _write_json(game_dir / _GAME_CONFIG, data)
    logger.info("Deactivated game: %r", name)


# Profiles

def load_profiles(game: str) -> list[str]:
    game_dir = SAVES_DIR / game
    if not game or not game_dir.exists():
        return []
    return sorted(p.name for p in game_dir.iterdir() if p.is_dir())


def create_profile(game: str, name: str) -> None:
    logger.info("Creating profile: game=%r profile=%r", game, name)
    (SAVES_DIR / game / name).mkdir(parents=True, exist_ok=False)


def rename_profile(game: str, old_name: str, new_name: str) -> None:
    logger.info("Renaming profile: game=%r %r -> %r", game, old_name, new_name)
    (SAVES_DIR / game / old_name).rename(SAVES_DIR / game / new_name)


def delete_profile(game: str, name: str) -> None:
    logger.info("Deleting profile to trash: game=%r profile=%r", game, name)
    send2trash.send2trash(str(SAVES_DIR / game / name))


# Slots

def _is_save_file(path: Path) -> bool:
    return path.is_file() and path.name not in _RESERVED and not path.name.startswith(".")


def _slot_data_dir(slot_dir: Path) -> Optional[Path]:
    """The saved game folder inside a folder-mode slot, or None for file/files-mode slots.

    Accepts the legacy save_data/ wrapper, or a single sub-folder under its real name
    (the layout other save managers use). A slot that holds loose save files is never
    folder-mode.
    """
    legacy = slot_dir / _LEGACY_DATA_DIR
    if legacy.is_dir():
        return legacy
    try:
        children = sorted(slot_dir.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return None
    if any(_is_save_file(c) for c in children):
        return None
    return next((c for c in children if c.is_dir() and not c.name.startswith(".")), None)


def _payload_entries(slot_dir: Path) -> list[Path]:
    """Everything in a slot that belongs to the game save (not meta.json, notes, hidden files)."""
    try:
        return [c for c in sorted(slot_dir.iterdir(), key=lambda p: p.name.lower())
                if c.name not in _RESERVED and not c.name.startswith(".")]
    except OSError:
        return []


def _payload_files(slot_dir: Path) -> list[Path]:
    files: list[Path] = []
    for entry in _payload_entries(slot_dir):
        files.extend([entry] if entry.is_file() else [f for f in entry.rglob("*") if f.is_file()])
    return files


def slot_save_files(slot: SaveSlot) -> list[Path]:
    """All save files stored in a slot, regardless of save mode."""
    if slot.bare:
        return [slot.path] if slot.path.is_file() else []
    data_dir = _slot_data_dir(slot.path)
    if data_dir is not None:
        return _payload_files(slot.path)
    return [f for f in slot.path.iterdir() if _is_save_file(f)]


def _live_names(game: str) -> tuple[str, set[str]]:
    """The game's save type and the file names its live save(s) have."""
    data = _read_json_dict(SAVES_DIR / game / _GAME_CONFIG)
    paths = [data.get("save_path", "")] + list(data.get("save_paths", []))
    return data.get("save_mode", "file"), {Path(p).name for p in paths if p}


def _looks_like_group(path: Path) -> bool:
    """Whether an unmarked directory in a profile is a group of slots rather than one slot.

    Folders made in Explorer or by other save managers carry no marker, so this goes by
    what is inside and by the game's save type. A directory that holds a save itself is
    a slot; one that only holds slots (or nothing yet) is a folder.
    """
    if (path / "meta.json").exists() or (path / "notes.txt").exists() \
            or (path / _LEGACY_DATA_DIR).is_dir():
        return False
    try:
        kids = [c for c in path.iterdir()
                if c.name not in _RESERVED and not c.name.startswith(".")]
    except OSError:
        return False
    if not kids:
        return True  # an empty directory is a new folder
    files = [k for k in kids if k.is_file()]
    dirs = [k for k in kids if k.is_dir()]
    mode, live_names = _live_names(path.parent.parent.name)
    if any(d.name in live_names for d in dirs):
        return False  # a slot that stores the save folder under its own name
    if mode == "folder" or (mode != "files" and not live_names):
        # Folder games, or games whose save type isn't known yet.
        if files:
            return False  # loose files: a slot mirroring the save folder itself
        # A slot holds the save directory, which holds files; a folder holds slots instead.
        try:
            return not any(any(_is_save_file(x) for x in d.iterdir()) for d in dirs)
        except OSError:
            return False
    # Single-file games: a slot holds the game's own file, never sub-directories.
    if any(f.name in live_names for f in files):
        return False
    if files and not live_names:
        return False  # no way to tell, so keep treating it as a slot
    return True


def _is_folder_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    if (path / _FOLDER_MARKER).exists():
        return True
    # Unmarked directories directly inside a profile may still be folders.
    return path.parent.parent.parent == SAVES_DIR and _looks_like_group(path)


def _adopt_folder(path: Path) -> None:
    """Mark a detected folder so it stays a folder whatever it ends up containing."""
    marker = path / _FOLDER_MARKER
    if not marker.exists():
        try:
            marker.write_text("", encoding="utf-8")
        except OSError:
            logger.warning("Could not mark folder: %s", path)


def _read_folder_slots(folder_dir: Path, game: str, profile: str) -> list[SaveSlot]:
    slots = []
    for slot_dir in sorted(folder_dir.iterdir()):
        if slot_dir.name.startswith("."):
            continue
        if slot := _read_entry(slot_dir, game, profile):
            slot.folder = folder_dir.name if _is_folder_dir(folder_dir) else ""
            slots.append(slot)
    return slots


def load_slots(game: str, profile: str) -> list[SaveSlot]:
    """Every slot of a profile, including the ones inside folders (names are unique)."""
    profile_dir = SAVES_DIR / game / profile
    if not profile_dir.exists():
        return []
    slots = []
    for entry in sorted(profile_dir.iterdir()):
        if entry.is_dir() and _is_folder_dir(entry):
            _adopt_folder(entry)
            slots.extend(_read_folder_slots(entry, game, profile))
        elif slot := _read_entry(entry, game, profile):
            slots.append(slot)
    logger.debug("Loaded %d slot(s) for game=%r profile=%r", len(slots), game, profile)
    return slots


def _sort_slots(slots: list[SaveSlot], sort: str, desc: bool, order: dict[str, int]) -> None:
    if sort == "name":
        slots.sort(key=lambda s: s.name.lower(), reverse=desc)
    elif sort == "created":
        slots.sort(key=lambda s: s.date_created or datetime.min, reverse=desc)
    elif sort == "modified":
        slots.sort(key=lambda s: s.date_modified or s.date_created or datetime.min, reverse=desc)
    elif sort == "custom":
        slots.sort(key=lambda s: order.get(s.name, len(order)))


def load_sorted_slots(game: str, profile: str, sort: str, desc: bool) -> list[SaveSlot]:
    """Load slots in display order. Custom order is re-saved so it tracks added/removed slots."""
    slots = load_slots(game, profile)
    order = {name: i for i, name in enumerate(load_slot_order(game, profile))}
    _sort_slots(slots, sort, desc, order)
    if sort == "custom":
        save_slot_order(game, profile, [s.name for s in slots])
    return slots


def load_profile_tree(game: str, profile: str, sort: str,
                      desc: bool) -> tuple[list[SaveFolder], list[SaveSlot]]:
    """A profile as (folders, slots not in any folder), each sorted in display order.

    Folders come first. They are ordered by name (or by the custom order); slots are
    ordered by the chosen sort, inside folders as well as at the top level.
    """
    profile_dir = SAVES_DIR / game / profile
    folders: list[SaveFolder] = []
    loose: list[SaveSlot] = []
    if not game or not profile or not profile_dir.exists():
        return folders, loose
    for entry in sorted(profile_dir.iterdir()):
        if entry.is_dir() and _is_folder_dir(entry):
            _adopt_folder(entry)
            folders.append(SaveFolder(entry.name, entry, _read_folder_slots(entry, game, profile)))
        elif slot := _read_entry(entry, game, profile):
            loose.append(slot)

    order = {name: i for i, name in enumerate(load_slot_order(game, profile))}
    for folder in folders:
        _sort_slots(folder.slots, sort, desc, order)
    _sort_slots(loose, sort, desc, order)
    if sort == "custom":
        folders.sort(key=lambda f: order.get(f.name, len(order)))
    else:
        folders.sort(key=lambda f: f.name.lower(), reverse=desc and sort == "name")
    if sort == "custom":
        save_tree_order(game, profile, folders, loose)
    return folders, loose


def save_tree_order(game: str, profile: str, folders: list[SaveFolder],
                    loose: list[SaveSlot]) -> None:
    """Persist the custom order. Names are unique per profile, so one flat list covers
    folders, the slots inside them and top-level slots."""
    names = []
    for folder in folders:
        names.append(folder.name)
        names.extend(s.name for s in folder.slots)
    names.extend(s.name for s in loose)
    save_slot_order(game, profile, names)


# Folders. They only organise slots: a folder is never loaded into the game, and a
# slot lives directly in its profile or in exactly one folder (no nesting).

def _profile_names(game: str, profile: str) -> set[str]:
    """Lower-cased names already used by slots and folders anywhere in a profile."""
    profile_dir = SAVES_DIR / game / profile
    names: set[str] = set()
    if not profile_dir.exists():
        return names
    for entry in profile_dir.iterdir():
        if not entry.is_dir() and not _is_bare_save(entry):
            continue
        names.add(entry.name.lower())
        if entry.is_dir() and _is_folder_dir(entry):
            names.update(d.name.lower() for d in entry.iterdir()
                         if d.is_dir() or _is_bare_save(d))
    return names


def name_taken(game: str, profile: str, name: str, ignore: str = "") -> bool:
    """Whether a slot or folder called `name` exists in the profile (case-insensitive).
    `ignore` is the entry's own current name, for renames."""
    names = _profile_names(game, profile)
    if ignore:
        names.discard(ignore.lower())
    return name.lower() in names


def create_folder(game: str, profile: str, name: str) -> SaveFolder:
    name = validate_entry_name(name)
    if name_taken(game, profile, name):
        raise ValueError(f"'{name}' already exists in this profile")
    logger.info("Creating folder: game=%r profile=%r folder=%r", game, profile, name)
    path = SAVES_DIR / game / profile / name
    path.mkdir(parents=True, exist_ok=False)
    (path / _FOLDER_MARKER).write_text("", encoding="utf-8")
    return SaveFolder(name, path, [])


def rename_folder(game: str, profile: str, old_name: str, new_name: str) -> None:
    new_name = validate_entry_name(new_name)
    if name_taken(game, profile, new_name, ignore=old_name):
        raise ValueError(f"'{new_name}' already exists in this profile")
    logger.info("Renaming folder: game=%r profile=%r %r -> %r", game, profile, old_name, new_name)
    profile_dir = SAVES_DIR / game / profile
    (profile_dir / old_name).rename(profile_dir / new_name)
    order = load_slot_order(game, profile)
    if old_name in order:
        save_slot_order(game, profile, [new_name if n == old_name else n for n in order])


def delete_empty_folder(game: str, profile: str, name: str) -> None:
    """Remove a folder, but only if it holds no slots."""
    path = SAVES_DIR / game / profile / name
    if not _is_folder_dir(path):
        raise ValueError(f"'{name}' is not a folder")
    if any(d.is_dir() or _is_bare_save(d) for d in path.iterdir()):
        raise ValueError("Only empty folders can be deleted")
    logger.info("Deleting empty folder: game=%r profile=%r folder=%r", game, profile, name)
    shutil.rmtree(path)


def delete_folder(game: str, profile: str, name: str) -> None:
    """Remove a folder together with every slot inside it."""
    path = SAVES_DIR / game / profile / name
    if not _is_folder_dir(path):
        raise ValueError(f"'{name}' is not a folder")
    logger.info("Deleting folder with contents: game=%r profile=%r folder=%r", game, profile, name)
    shutil.rmtree(path)


def stash_for_undo(path: Path) -> Path:
    """Copy a slot or folder into saves/_undo so a delete can be taken back."""
    stash = SAVES_DIR / _UNDO_DIR / datetime.now().strftime("%Y%m%d_%H%M%S_%f") / path.name
    stash.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        shutil.copytree(path, stash)
    else:
        shutil.copy2(path, stash)
    return stash


def restore_stash(stash: Path, dest: Path) -> None:
    """Put a stashed slot or folder back where it was."""
    if dest.exists():
        raise FileExistsError(f"'{dest.name}' already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(stash), str(dest))
    shutil.rmtree(stash.parent, ignore_errors=True)


def discard_stash(stash: Path) -> None:
    shutil.rmtree(stash.parent, ignore_errors=True)


def clear_undo_stash() -> None:
    shutil.rmtree(SAVES_DIR / _UNDO_DIR, ignore_errors=True)


def move_slot_by_name(game: str, profile: str, name: str, from_folder: str, to_folder: str) -> None:
    """Move a slot between folders by name ("" = top level); used to undo a move."""
    base = SAVES_DIR / game / profile
    src = (base / from_folder if from_folder else base) / name
    dst_dir = base / to_folder if to_folder else base
    if (dst_dir / name).exists():
        raise FileExistsError(f"'{name}' already exists there")
    src.rename(dst_dir / name)


def move_slot_to_folder(slot: SaveSlot, folder: str) -> SaveSlot:
    """Move a slot into a folder of its profile, or out to the top level if folder is ""."""
    profile_dir = SAVES_DIR / slot.game / slot.profile
    target_parent = profile_dir / folder if folder else profile_dir
    if folder and not _is_folder_dir(target_parent):
        raise ValueError(f"'{folder}' is not a folder")
    if slot.path.parent == target_parent:
        return slot
    new_path = target_parent / slot.name
    if new_path.exists():
        raise ValueError(f"'{slot.name}' already exists there")
    logger.info("Moving slot: game=%r profile=%r %r/%r -> %r",
                slot.game, slot.profile, slot.folder, slot.name, folder)
    slot.path.rename(new_path)
    slot.path = new_path
    slot.folder = folder
    return slot


def _guess_timestamps(slot_dir: Path, data_dir: Optional[Path]) -> tuple[datetime, datetime]:
    """Created/modified times taken from the saved files, for slots without a meta.json."""
    if data_dir is not None:
        files = _payload_files(slot_dir)
    else:
        files = [f for f in slot_dir.iterdir() if _is_save_file(f)]
    if not files:
        now = datetime.now()
        return now, now
    stats = [f.stat() for f in files]
    created = min(getattr(s, "st_birthtime", s.st_ctime) for s in stats)
    modified = max(s.st_mtime for s in stats)
    return datetime.fromtimestamp(created), datetime.fromtimestamp(modified)


_NOT_SAVES = {"order.json", "desktop.ini", "thumbs.db", ".ds_store"}


def _is_bare_save(path: Path) -> bool:
    """A single save file placed directly in a profile, named after its slot."""
    return _is_save_file(path) and path.name.lower() not in _NOT_SAVES


def _read_bare_slot(path: Path, game: str, profile: str) -> SaveSlot:
    st = path.stat()
    modified = datetime.fromtimestamp(st.st_mtime)
    created = datetime.fromtimestamp(min(st.st_ctime, st.st_mtime))
    return SaveSlot(name=path.name, game=game, profile=profile, path=path,
                    date_created=created, date_modified=modified, notes="",
                    save_file=path.name, bare=True)


def _read_entry(entry: Path, game: str, profile: str) -> Optional[SaveSlot]:
    if entry.is_dir():
        return _read_slot(entry, game, profile)
    if _is_bare_save(entry):
        return _read_bare_slot(entry, game, profile)
    return None


def _read_slot(slot_dir: Path, game: str, profile: str) -> Optional[SaveSlot]:
    meta_file = slot_dir / "meta.json"
    notes_file = slot_dir / "notes.txt"
    data_dir = _slot_data_dir(slot_dir)
    save_file = None if data_dir else next(
        (f.name for f in slot_dir.iterdir() if _is_save_file(f)), None
    )

    # An empty folder is only a slot if it was explicitly created (has meta.json).
    if data_dir is None and save_file is None and not meta_file.exists():
        return None

    # meta.json and notes.txt are optional extras: slots made by other save managers
    # have neither, and they are not created just by looking at a slot.
    meta = _read_json_dict(meta_file)
    created = _parse_iso(meta.get("created"))
    modified = _parse_iso(meta.get("modified"))
    if created is None or modified is None:
        guessed_created, guessed_modified = _guess_timestamps(slot_dir, data_dir)
        created = created or guessed_created
        modified = modified or guessed_modified

    notes = ""
    if notes_file.exists():
        try:
            notes = notes_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            logger.exception("Failed to read notes: %s", notes_file)

    return SaveSlot(
        name=slot_dir.name,
        game=game,
        profile=profile,
        path=slot_dir,
        date_created=created,
        date_modified=modified,
        notes=notes,
        save_file=save_file,
        video_url=meta.get("video_url", ""),
        data_dir_name=data_dir.name if data_dir else None,
    )


def _update_meta(meta_file: Path, updates: dict) -> None:
    meta = _read_json_dict(meta_file)
    meta.update(updates)
    _write_json(meta_file, meta)


def save_notes(slot: SaveSlot, text: str) -> None:
    # Only touch the slot when the notes actually changed, so browsing slots
    # never adds a notes.txt to them.
    if text.strip() == slot.notes.strip() or slot.bare:  # a bare file has nowhere to keep notes
        return
    (slot.path / "notes.txt").write_text(text, encoding="utf-8")
    slot.notes = text


def save_video_url(slot: SaveSlot, url: str) -> None:
    url = url.strip()
    if url == slot.video_url or slot.bare:
        return
    _update_meta(slot.path / "meta.json", {"video_url": url})
    slot.video_url = url


def rename_slot(slot: SaveSlot, new_name: str) -> None:
    """Rename a slot's directory and update the slot object in place."""
    logger.info("Renaming slot: game=%r profile=%r %r -> %r",
                slot.game, slot.profile, slot.name, new_name)
    new_path = slot.path.parent / new_name
    slot.path.rename(new_path)
    slot.name = new_name
    slot.path = new_path


def remove_path(path: Path) -> None:
    """Delete a slot file or directory."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def delete_slot(slot: SaveSlot, soft: bool = False) -> None:
    """Delete a slot; soft sends it to the system trash instead."""
    logger.info("Deleting slot: game=%r profile=%r slot=%r soft=%s",
                slot.game, slot.profile, slot.name, soft)
    if soft:
        send2trash.send2trash(str(slot.path))
    else:
        remove_path(slot.path)


def _unique_name(game: str, profile: str, base: str) -> str:
    name, n = base, 2
    while name_taken(game, profile, name):
        name = f"{base} {n}"
        n += 1
    return name


def auto_slot_name(game: str, profile: str) -> str:
    return _unique_name(game, profile, "new save")


def duplicate_slot_name(game: str, profile: str, original_name: str) -> str:
    return _unique_name(game, profile, f"{original_name} copy")


def copy_slot_to_profile(slot: SaveSlot, target_profile: str, new_name: str,
                         folder: str = "") -> SaveSlot:
    """Copy a slot (with its notes) into a profile of the same game, with fresh timestamps.

    The copy lands at the top level of the target profile, or in `folder` if given.
    """
    logger.info("Copying slot: game=%r %r/%r -> %r/%r",
                slot.game, slot.profile, slot.name, target_profile, new_name)
    target_dir = SAVES_DIR / slot.game / target_profile
    if folder:
        target_dir = target_dir / folder
    new_dir = target_dir / new_name
    if slot.bare:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(slot.path, new_dir)
        copy = _read_bare_slot(new_dir, slot.game, target_profile)
        copy.folder = folder
        return copy
    shutil.copytree(slot.path, new_dir)
    meta_file = new_dir / "meta.json"
    if meta_file.exists():  # only slots that already track timestamps keep doing so
        now = datetime.now()
        _update_meta(meta_file, _timestamps(now, now))
    copy = _read_slot(new_dir, slot.game, target_profile)
    copy.folder = folder
    return copy


def duplicate_slot(slot: SaveSlot, new_name: str) -> SaveSlot:
    """Copy a slot next to the original, in the same folder."""
    return copy_slot_to_profile(slot, slot.profile, new_name, folder=slot.folder)


def load_slot_order(game: str, profile: str) -> list[str]:
    order_file = SAVES_DIR / game / profile / "order.json"
    if not order_file.exists():
        return []
    try:
        with open(order_file, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to read slot order: %s", order_file)
        return []


def save_slot_order(game: str, profile: str, names: list[str]) -> None:
    _write_json(SAVES_DIR / game / profile / "order.json", names)


# Copying between slots and the live game save.
#
# A slot or snapshot folder holds the save file itself ("file" mode), each
# listed file ("files" mode), or a copy of the folder under its own name ("folder"
# mode; older FromSave versions called it save_data/).

def _has_save_path(game_cfg: GameConfig) -> bool:
    return bool(game_cfg.save_paths if game_cfg.save_mode == "files" else game_cfg.save_path)


def _live_save_exists(game_cfg: GameConfig) -> bool:
    if game_cfg.save_mode == "files":
        return any(Path(p).exists() for p in game_cfg.save_paths)
    return bool(game_cfg.save_path) and Path(game_cfg.save_path).exists()


def _copy_file(src: Path, dst: Path) -> None:
    logger.debug("Copying %s -> %s", src, dst)
    shutil.copy2(src, dst)


def _replace_tree_or_file(src: Path, dst: Path) -> None:
    if dst.is_dir() and not dst.is_symlink():
        shutil.rmtree(dst)
    _copy_file(src, dst)


def _effective_mode(game_cfg: GameConfig) -> str:
    """The save type to use, trusting what is actually on disk over the configured type.

    A game set to "file" whose path is a directory (or "folder" whose path is a file)
    would otherwise fail; the live path itself tells us which kind of save it is.
    """
    if game_cfg.save_mode in ("file", "folder") and game_cfg.save_path:
        live = Path(game_cfg.save_path)
        if live.is_dir():
            return "folder"
        if live.is_file():
            return "file"
    return game_cfg.save_mode


def _replace_tree(src: Path, dst: Path) -> None:
    if dst.is_dir() and not dst.is_symlink():
        shutil.rmtree(dst)
    elif dst.exists() or dst.is_symlink():
        dst.unlink()
    logger.debug("Copying folder %s -> %s", src, dst)
    shutil.copytree(src, dst)


def _live_folder_name(game_cfg: GameConfig) -> str:
    """Name a copied save folder is stored under inside a slot."""
    name = Path(game_cfg.save_path).name
    if not name or name.startswith(".") or name in _RESERVED:
        return _LEGACY_DATA_DIR
    return name


def _copy_live_save(game_cfg: GameConfig, dest: Path, file_name: Optional[str] = None,
                    folder_name: Optional[str] = None) -> None:
    """Copy the live save into dest.

    file_name overrides the stored name in "file" mode; folder_name does the same
    for the saved folder in "folder" mode.
    """
    dest.mkdir(parents=True, exist_ok=True)
    mode = _effective_mode(game_cfg)
    if mode == "file":
        src = Path(game_cfg.save_path)
        _copy_file(src, dest / (file_name or src.name))
    elif mode == "files":
        for src in map(Path, game_cfg.save_paths):
            if src.is_file():
                _copy_file(src, dest / src.name)
            else:
                logger.warning("Configured save file missing: %s", src)
    elif mode == "folder":
        # The slot mirrors what is inside the configured folder, so pointing the game at
        # ".../savedata" gives Save n/<SaveDirectory>/..., like other save managers.
        live = Path(game_cfg.save_path)
        target = dest
        existing = _payload_entries(dest)
        if (dest / _LEGACY_DATA_DIR).is_dir():  # keep the layout an existing slot already uses
            target = dest / _LEGACY_DATA_DIR
        elif len(existing) == 1 and existing[0].is_dir() and existing[0].name == live.name:
            target = existing[0]
        for old in _payload_entries(target):
            shutil.rmtree(old) if old.is_dir() else old.unlink()
        for child in sorted(live.iterdir()):
            if child.is_dir():
                _replace_tree(child, target / child.name)
            else:
                _copy_file(child, target / child.name)


def _copy_to_live(src_dir: Path, game_cfg: GameConfig, file_name: Optional[str]) -> None:
    """Copy a slot or snapshot folder over the live save. file_name is used in "file" mode."""
    mode = _effective_mode(game_cfg)
    if mode in ("file", "folder") and not Path(game_cfg.save_path).exists():
        # Live path missing: go by what the slot actually holds.
        if _slot_data_dir(src_dir) is not None:
            mode = "folder"
        elif file_name and (src_dir / file_name).is_file():
            mode = "file"
    if mode == "file":
        if not file_name or not (src_dir / file_name).is_file():
            raise FileNotFoundError(f"No save file in {src_dir}")
        _copy_file(src_dir / file_name, Path(game_cfg.save_path))
    elif mode == "files":
        for dst in map(Path, game_cfg.save_paths):
            src = src_dir / dst.name
            if src.exists():
                _copy_file(src, dst)
            else:
                logger.warning("Save file missing from %s: %s", src_dir, dst.name)
    elif mode == "folder":
        live = Path(game_cfg.save_path)
        legacy = src_dir / _LEGACY_DATA_DIR
        entries = _payload_entries(src_dir)
        wipe = False  # whether the whole live folder is replaced, not just the saved items
        if legacy.is_dir():  # older FromSave slots wrapped the folder in save_data/
            entries, wipe = _payload_entries(legacy), True
        elif len(entries) == 1 and entries[0].is_dir() and entries[0].name == live.name:
            entries, wipe = _payload_entries(entries[0]), True  # the folder stored under its own name
        elif any(e.is_file() for e in entries):
            wipe = True  # the saved folder is the save path itself, so loose files are all of it
        if not entries:
            raise FileNotFoundError(f"No save data in {src_dir}")
        live.mkdir(parents=True, exist_ok=True)
        if wipe:
            for old in live.iterdir():
                shutil.rmtree(old) if old.is_dir() and not old.is_symlink() else old.unlink()
        for entry in entries:  # replace each saved item, leaving anything else alone
            if entry.is_dir():
                _replace_tree(entry, live / entry.name)
            else:
                _replace_tree_or_file(entry, live / entry.name)


def import_save(game: str, profile: str, slot_name: str, game_cfg: GameConfig) -> SaveSlot:
    """Copy the game's live save into a new slot."""
    if name_taken(game, profile, slot_name):
        raise FileExistsError(f"A slot or folder named {slot_name!r} already exists")
    slot_dir = SAVES_DIR / game / profile / slot_name
    logger.info("Importing save: game=%r profile=%r slot=%r mode=%s",
                game, profile, slot_name, game_cfg.save_mode)
    if _effective_mode(game_cfg) == "file":
        # A single save file becomes the slot itself: Profile/<slot name>, like other managers.
        live = Path(game_cfg.save_path)
        if not live.is_file():
            raise FileNotFoundError(f"Save file not found: {live}")
        slot_dir.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(live, slot_dir)
        return _read_bare_slot(slot_dir, game, profile)
    slot_dir.mkdir(parents=True, exist_ok=False)
    try:
        _copy_live_save(game_cfg, slot_dir)
    except Exception:
        shutil.rmtree(slot_dir, ignore_errors=True)  # don't leave an empty slot behind
        raise
    # The slot holds only the game's own save, like slots made by other save managers.
    return _read_slot(slot_dir, game, profile)


def replace_save(slot: SaveSlot, game_cfg: GameConfig) -> None:
    """Overwrite a slot's contents with the game's live save."""
    logger.info("Replacing slot from live save: game=%r profile=%r slot=%r mode=%s",
                slot.game, slot.profile, slot.name, game_cfg.save_mode)
    if slot.bare:
        _copy_file(Path(game_cfg.save_path), slot.path)
        slot.date_modified = datetime.now()
        return
    _copy_live_save(game_cfg, slot.path, file_name=slot.save_file, folder_name=slot.data_dir_name)
    now = datetime.now()
    meta_file = slot.path / "meta.json"
    if meta_file.exists():  # only slots that already track timestamps keep doing so
        meta = _read_json_dict(meta_file)
        created = _parse_iso(meta.get("created")) or now
        _update_meta(meta_file, _timestamps(created, now))
    slot.date_modified = now


def load_save(slot: SaveSlot, game_cfg: GameConfig, *, make_backup: bool = True) -> None:
    """Copy a slot's save over the game's live save, backing up the live save first."""
    logger.info("Loading slot to live save: game=%r profile=%r slot=%r mode=%s make_backup=%s",
                slot.game, slot.profile, slot.name, game_cfg.save_mode, make_backup)
    if not _has_save_path(game_cfg):
        raise ValueError(f"Save path is not configured for game {game_cfg.name!r}")
    if make_backup:
        try:
            _rotating_backup(game_cfg, _BACKUPS_DIR)
        except OSError:
            logger.exception("Failed to back up live save before load; continuing")
    if slot.bare:
        if _effective_mode(game_cfg) != "file":
            raise ValueError("This slot is a single save file, but the game's save path is not a file")
        _copy_file(slot.path, Path(game_cfg.save_path))
        return
    _copy_to_live(slot.path, game_cfg, slot.save_file)


def _rotating_backup(game_cfg: GameConfig, dir_name: str) -> None:
    """Snapshot the live save into a timestamped folder, keeping the newest few."""
    if not _live_save_exists(game_cfg):
        logger.warning("Skipping %s backup; no live save for game=%r", dir_name, game_cfg.name)
        return
    root = SAVES_DIR / dir_name / game_cfg.name
    dest = root / datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
    logger.info("Backing up live save: %s", dest)
    _copy_live_save(game_cfg, dest)
    for old in sorted(d for d in root.iterdir() if d.is_dir())[:-_MAX_BACKUPS]:
        logger.info("Pruning old backup: %s", old)
        shutil.rmtree(old)


def snapshot_practice_start(game_cfg: GameConfig) -> None:
    """Save the live save from before practice mode, replacing any previous snapshot."""
    if not _live_save_exists(game_cfg):
        logger.warning("Skipping practice-start snapshot; no live save for game=%r", game_cfg.name)
        return
    dest = SAVES_DIR / _PRACTICE_START_DIR / game_cfg.name
    if dest.exists():
        shutil.rmtree(dest)
    logger.info("Taking practice-start snapshot: %s", dest)
    _copy_live_save(game_cfg, dest)


def restore_practice_start(game_cfg: GameConfig) -> None:
    """Put the pre-practice snapshot back as the live save."""
    src_dir = SAVES_DIR / _PRACTICE_START_DIR / game_cfg.name
    if not src_dir.exists() or not _has_save_path(game_cfg):
        logger.warning("No practice-start snapshot to restore for game=%r", game_cfg.name)
        return
    logger.info("Restoring practice-start snapshot: %s", src_dir)
    first_file = next((f.name for f in sorted(src_dir.iterdir()) if f.is_file()), None)
    _copy_to_live(src_dir, game_cfg, first_file)
