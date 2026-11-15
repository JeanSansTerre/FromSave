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
_GAME_CONFIG = "game.json"
_BACKUPS_DIR = "_backups"
_RUN_BACKUPS_DIR = "_run_backups"
_PRACTICE_START_DIR = "_practice_start"
_MAX_BACKUPS = 3

SORT_MODES = ("name", "created", "modified", "custom")

# Games whose saves/ folder ships in the release zip. They're all single-file
# saves, so a fresh install configures them as "file" mode without asking.
BUNDLED_GAMES = {
    "Armored Core VI",
    "Dark Souls II Scholar of the First Sin",
    "Dark Souls III",
    "Dark Souls Remastered",
    "Elden Ring",
    "Sekiro",
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


def slot_save_files(slot: SaveSlot) -> list[Path]:
    """All save files stored in a slot, regardless of save mode."""
    data_dir = _slot_data_dir(slot.path)
    if data_dir is not None:
        return [f for f in data_dir.rglob("*") if f.is_file()]
    return [f for f in slot.path.iterdir() if _is_save_file(f)]


def load_slots(game: str, profile: str) -> list[SaveSlot]:
    profile_dir = SAVES_DIR / game / profile
    if not profile_dir.exists():
        return []
    slots = []
    for slot_dir in sorted(profile_dir.iterdir()):
        if slot_dir.is_dir() and (slot := _read_slot(slot_dir, game, profile)):
            slots.append(slot)
    logger.debug("Loaded %d slot(s) for game=%r profile=%r", len(slots), game, profile)
    return slots


def load_sorted_slots(game: str, profile: str, sort: str, desc: bool) -> list[SaveSlot]:
    """Load slots in display order. Custom order is re-saved so it tracks added/removed slots."""
    slots = load_slots(game, profile)
    if sort == "name":
        slots.sort(key=lambda s: s.name.lower(), reverse=desc)
    elif sort == "created":
        slots.sort(key=lambda s: s.date_created or datetime.min, reverse=desc)
    elif sort == "modified":
        slots.sort(key=lambda s: s.date_modified or s.date_created or datetime.min, reverse=desc)
    elif sort == "custom":
        order = {name: i for i, name in enumerate(load_slot_order(game, profile))}
        slots.sort(key=lambda s: order.get(s.name, len(order)))
        save_slot_order(game, profile, [s.name for s in slots])
    return slots


def _guess_timestamps(slot_dir: Path, data_dir: Optional[Path]) -> tuple[datetime, datetime]:
    """Created/modified times taken from the saved files, for slots without a meta.json."""
    if data_dir is not None:
        files = [f for f in data_dir.rglob("*") if f.is_file()]
    else:
        files = [f for f in slot_dir.iterdir() if _is_save_file(f)]
    if not files:
        now = datetime.now()
        return now, now
    stats = [f.stat() for f in files]
    created = min(getattr(s, "st_birthtime", s.st_ctime) for s in stats)
    modified = max(s.st_mtime for s in stats)
    return datetime.fromtimestamp(created), datetime.fromtimestamp(modified)


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
    if text.strip() == slot.notes.strip():
        return
    (slot.path / "notes.txt").write_text(text, encoding="utf-8")
    slot.notes = text


def save_video_url(slot: SaveSlot, url: str) -> None:
    url = url.strip()
    if url == slot.video_url:
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


def delete_slot(slot: SaveSlot, soft: bool = False) -> None:
    """Delete a slot; soft sends it to the system trash instead."""
    logger.info("Deleting slot: game=%r profile=%r slot=%r soft=%s",
                slot.game, slot.profile, slot.name, soft)
    if soft:
        send2trash.send2trash(str(slot.path))
    else:
        shutil.rmtree(slot.path)


def _unique_name(folder: Path, base: str) -> str:
    name, n = base, 2
    while (folder / name).exists():
        name = f"{base} {n}"
        n += 1
    return name


def auto_slot_name(game: str, profile: str) -> str:
    return _unique_name(SAVES_DIR / game / profile, "new save")


def duplicate_slot_name(game: str, profile: str, original_name: str) -> str:
    return _unique_name(SAVES_DIR / game / profile, f"{original_name} copy")


def copy_slot_to_profile(slot: SaveSlot, target_profile: str, new_name: str) -> SaveSlot:
    """Copy a slot (with its notes) into a profile of the same game, with fresh timestamps."""
    logger.info("Copying slot: game=%r %r/%r -> %r/%r",
                slot.game, slot.profile, slot.name, target_profile, new_name)
    new_dir = SAVES_DIR / slot.game / target_profile / new_name
    shutil.copytree(slot.path, new_dir)
    meta_file = new_dir / "meta.json"
    if meta_file.exists():  # only slots that already track timestamps keep doing so
        now = datetime.now()
        _update_meta(meta_file, _timestamps(now, now))
    return _read_slot(new_dir, slot.game, target_profile)


def duplicate_slot(slot: SaveSlot, new_name: str) -> SaveSlot:
    return copy_slot_to_profile(slot, slot.profile, new_name)


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


def _replace_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
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
    if game_cfg.save_mode == "file":
        src = Path(game_cfg.save_path)
        _copy_file(src, dest / (file_name or src.name))
    elif game_cfg.save_mode == "files":
        for src in map(Path, game_cfg.save_paths):
            if src.is_file():
                _copy_file(src, dest / src.name)
            else:
                logger.warning("Configured save file missing: %s", src)
    elif game_cfg.save_mode == "folder":
        _replace_tree(Path(game_cfg.save_path), dest / (folder_name or _live_folder_name(game_cfg)))


def _copy_to_live(src_dir: Path, game_cfg: GameConfig, file_name: Optional[str]) -> None:
    """Copy a slot or snapshot folder over the live save. file_name is used in "file" mode."""
    if game_cfg.save_mode == "file":
        if not file_name or not (src_dir / file_name).is_file():
            raise FileNotFoundError(f"No save file in {src_dir}")
        _copy_file(src_dir / file_name, Path(game_cfg.save_path))
    elif game_cfg.save_mode == "files":
        for dst in map(Path, game_cfg.save_paths):
            src = src_dir / dst.name
            if src.exists():
                _copy_file(src, dst)
            else:
                logger.warning("Save file missing from %s: %s", src_dir, dst.name)
    elif game_cfg.save_mode == "folder":
        data_dir = _slot_data_dir(src_dir)
        if data_dir is None:
            raise FileNotFoundError(f"No save folder in {src_dir}")
        _replace_tree(data_dir, Path(game_cfg.save_path))


def import_save(game: str, profile: str, slot_name: str, game_cfg: GameConfig) -> SaveSlot:
    """Copy the game's live save into a new slot."""
    slot_dir = SAVES_DIR / game / profile / slot_name
    logger.info("Importing save: game=%r profile=%r slot=%r mode=%s",
                game, profile, slot_name, game_cfg.save_mode)
    slot_dir.mkdir(parents=True, exist_ok=False)
    _copy_live_save(game_cfg, slot_dir)
    # The slot holds only the game's own save, like slots made by other save managers.
    return _read_slot(slot_dir, game, profile)


def replace_save(slot: SaveSlot, game_cfg: GameConfig) -> None:
    """Overwrite a slot's contents with the game's live save."""
    logger.info("Replacing slot from live save: game=%r profile=%r slot=%r mode=%s",
                slot.game, slot.profile, slot.name, game_cfg.save_mode)
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
    _copy_to_live(slot.path, game_cfg, slot.save_file)


def take_run_backup(game_cfg: GameConfig) -> None:
    _rotating_backup(game_cfg, _RUN_BACKUPS_DIR)


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
