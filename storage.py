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
_GAME_CONFIG = "game.json"
_BACKUPS_DIR = "_backups"
_RUN_BACKUPS_DIR = "_run_backups"
_PRACTICE_START_DIR = "_practice_start"
_MAX_BACKUPS = 3
SORT_MODES = ("name", "created", "modified", "custom")
BUNDLED_GAMES = {
    "Armored Core VI", "Dark Souls II Scholar of the First Sin",
    "Dark Souls III", "Dark Souls Remastered", "Elden Ring", "Sekiro",
}
_BAD_NAME_CHARS = set('<>:"/\\|?*') | {chr(c) for c in range(32)}


def validate_entry_name(name: str) -> str:
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
    save_path: str
    save_mode: str = "file"
    save_paths: list[str] = field(default_factory=list)


@dataclass
class SaveSlot:
    name: str
    game: str
    profile: str
    path: Path
    date_created: Optional[datetime]
    date_modified: Optional[datetime]
    notes: str
    save_file: Optional[str]
    video_url: str = ""


def _read_json_dict(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to read JSON: %s", path)
        return {}


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=4), encoding="utf-8")


def _timestamps(created: datetime, modified: datetime) -> dict:
    return {"created": created.isoformat(timespec="seconds"),
            "modified": modified.isoformat(timespec="seconds")}


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Invalid ISO datetime in metadata: %r", value)
        return None


def _game_dirs() -> list[Path]:
    if not SAVES_DIR.exists():
        return []
    return sorted((d for d in SAVES_DIR.iterdir() if d.is_dir() and not d.name.startswith("_")),
                  key=lambda d: d.name.lower())


def find_unconfigured_games() -> list[str]:
    return [d.name for d in _game_dirs() if not (d / _GAME_CONFIG).exists()]


def load_games() -> list[GameConfig]:
    games = []
    for game_dir in _game_dirs():
        data = _read_json_dict(game_dir / _GAME_CONFIG)
        if data.get("active", True):
            games.append(GameConfig(game_dir.name, data.get("save_path", ""),
                                    data.get("save_mode", "file"), data.get("save_paths", [])))
    logger.info("Loaded %d game config(s) from saves directory", len(games))
    return games


def save_game_config(game_cfg: GameConfig) -> None:
    game_dir = SAVES_DIR / game_cfg.name
    game_dir.mkdir(parents=True, exist_ok=True)
    data = {"active": True, "save_mode": game_cfg.save_mode}
    data["save_paths" if game_cfg.save_mode == "files" else "save_path"] = (
        game_cfg.save_paths if game_cfg.save_mode == "files" else game_cfg.save_path)
    _write_json(game_dir / _GAME_CONFIG, data)
    logger.info("Wrote game config: game=%r mode=%r", game_cfg.name, game_cfg.save_mode)


def save_games(games: list[GameConfig]) -> None:
    for game_cfg in games:
        save_game_config(game_cfg)


def deactivate_game(name: str) -> None:
    game_dir = SAVES_DIR / name
    if game_dir.exists():
        data = _read_json_dict(game_dir / _GAME_CONFIG)
        data["active"] = False
        _write_json(game_dir / _GAME_CONFIG, data)


def load_profiles(game: str) -> list[str]:
    game_dir = SAVES_DIR / game
    if not game or not game_dir.exists():
        return []
    return sorted(p.name for p in game_dir.iterdir() if p.is_dir())


def create_profile(game: str, name: str) -> None:
    (SAVES_DIR / game / name).mkdir(parents=True, exist_ok=False)


def rename_profile(game: str, old_name: str, new_name: str) -> None:
    (SAVES_DIR / game / old_name).rename(SAVES_DIR / game / new_name)


def delete_profile(game: str, name: str) -> None:
    send2trash.send2trash(str(SAVES_DIR / game / name))


def _is_save_file(path: Path) -> bool:
    return path.is_file() and path.name not in _RESERVED and not path.name.startswith(".")


def _slot_data_dir(slot_path: Path) -> Path:
    """Use legacy save_data when present; otherwise saves live in the slot directory."""
    legacy = slot_path / "save_data"
    return legacy if legacy.is_dir() else slot_path


def slot_save_files(slot: SaveSlot) -> list[Path]:
    """Return save files in either the legacy save_data layout or direct slot layout."""
    data_dir = _slot_data_dir(slot.path)
    if not data_dir.is_dir():
        return []
    files = []
    for f in data_dir.rglob("*"):
        if not f.is_file():
            continue
        # These are FromSave's root-level slot metadata, never game save data.
        if data_dir == slot.path and f.parent == slot.path and f.name in _RESERVED:
            continue
        files.append(f)
    return files


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


def _read_slot(slot_dir: Path, game: str, profile: str) -> Optional[SaveSlot]:
    meta_file, notes_file = slot_dir / "meta.json", slot_dir / "notes.txt"
    legacy_folder = (slot_dir / "save_data").is_dir()
    # Direct-layout folder saves may contain only subdirectories, so count any
    # non-metadata directory as save data as well as root-level save files.
    data_dirs = [p for p in slot_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    save_file = None if legacy_folder or data_dirs else next(
        (f.name for f in slot_dir.iterdir() if _is_save_file(f)), None)
    has_direct_data = any(p.name not in _RESERVED and not p.name.startswith(".")
                          for p in slot_dir.iterdir())
    if not has_direct_data and not meta_file.exists():
        return None
    if not meta_file.exists():
        if save_file:
            stat = (slot_dir / save_file).stat()
            created = datetime.fromtimestamp(getattr(stat, "st_birthtime", stat.st_ctime))
            modified = datetime.fromtimestamp(stat.st_mtime)
        else:
            created = modified = datetime.now()
        _write_json(meta_file, _timestamps(created, modified))
    if not notes_file.exists():
        notes_file.write_text("", encoding="utf-8")
    meta = _read_json_dict(meta_file)
    try:
        notes = notes_file.read_text(encoding="utf-8").strip()
    except OSError:
        logger.exception("Failed to read notes: %s", notes_file)
        notes = ""
    return SaveSlot(slot_dir.name, game, profile, slot_dir,
                    _parse_iso(meta.get("created")), _parse_iso(meta.get("modified")),
                    notes, save_file, meta.get("video_url", ""))


def _update_meta(meta_file: Path, updates: dict) -> None:
    meta = _read_json_dict(meta_file)
    meta.update(updates)
    _write_json(meta_file, meta)


def save_notes(slot: SaveSlot, text: str) -> None:
    (slot.path / "notes.txt").write_text(text, encoding="utf-8")
    slot.notes = text


def save_video_url(slot: SaveSlot, url: str) -> None:
    url = url.strip()
    _update_meta(slot.path / "meta.json", {"video_url": url})
    slot.video_url = url


def rename_slot(slot: SaveSlot, new_name: str) -> None:
    new_path = slot.path.parent / new_name
    slot.path.rename(new_path)
    slot.name, slot.path = new_name, new_path


def delete_slot(slot: SaveSlot, soft: bool = False) -> None:
    if soft:
        send2trash.send2trash(str(slot.path))
    else:
        shutil.rmtree(slot.path)


def _unique_name(folder: Path, base: str) -> str:
    name, n = base, 2
    while (folder / name).exists():
        name, n = f"{base} {n}", n + 1
    return name


def auto_slot_name(game: str, profile: str) -> str:
    return _unique_name(SAVES_DIR / game / profile, "new save")


def duplicate_slot_name(game: str, profile: str, original_name: str) -> str:
    return _unique_name(SAVES_DIR / game / profile, f"{original_name} copy")


def copy_slot_to_profile(slot: SaveSlot, target_profile: str, new_name: str) -> SaveSlot:
    new_dir = SAVES_DIR / slot.game / target_profile / new_name
    shutil.copytree(slot.path, new_dir)
    now = datetime.now()
    _update_meta(new_dir / "meta.json", _timestamps(now, now))
    return _read_slot(new_dir, slot.game, target_profile)


def duplicate_slot(slot: SaveSlot, new_name: str) -> SaveSlot:
    return copy_slot_to_profile(slot, slot.profile, new_name)


def load_slot_order(game: str, profile: str) -> list[str]:
    order_file = SAVES_DIR / game / profile / "order.json"
    if not order_file.exists():
        return []
    try:
        return json.loads(order_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to read slot order: %s", order_file)
        return []


def save_slot_order(game: str, profile: str, names: list[str]) -> None:
    _write_json(SAVES_DIR / game / profile / "order.json", names)


def _has_save_path(game_cfg: GameConfig) -> bool:
    return bool(game_cfg.save_paths if game_cfg.save_mode == "files" else game_cfg.save_path)


def _live_save_exists(game_cfg: GameConfig) -> bool:
    if game_cfg.save_mode == "files":
        return any(Path(p).exists() for p in game_cfg.save_paths)
    return bool(game_cfg.save_path) and Path(game_cfg.save_path).exists()


def _copy_file(src: Path, dst: Path) -> None:
    logger.debug("Copying %s -> %s", src, dst)
    shutil.copy2(src, dst)


def _replace_tree(src: Path, dst: Path, *, exclude_slot_metadata: bool = False) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    logger.debug("Copying folder %s -> %s", src, dst)
    ignore = None
    if exclude_slot_metadata:
        ignore = lambda directory, names: list(_RESERVED.intersection(names)) if Path(directory) == src else []
    shutil.copytree(src, dst, ignore=ignore)


def _replace_slot_contents(src: Path, dest: Path) -> None:
    """Replace a slot's save data but preserve FromSave's root metadata files."""
    dest.mkdir(parents=True, exist_ok=True)
    for child in dest.iterdir():
        if child.name in _RESERVED:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    ignore = lambda directory, names: list(_RESERVED.intersection(names)) if Path(directory) == src else []
    shutil.copytree(src, dest, dirs_exist_ok=True, ignore=ignore)


def _copy_live_save(game_cfg: GameConfig, dest: Path, file_name: Optional[str] = None) -> None:
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
        _replace_slot_contents(Path(game_cfg.save_path), dest)


def _copy_to_live(src_dir: Path, game_cfg: GameConfig, file_name: Optional[str]) -> None:
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
        legacy = src_dir / "save_data"
        data_dir = legacy if legacy.is_dir() else src_dir
        if data_dir == src_dir:
            _replace_tree(data_dir, Path(game_cfg.save_path), exclude_slot_metadata=True)
        else:
            _replace_tree(data_dir, Path(game_cfg.save_path))


def import_save(game: str, profile: str, slot_name: str, game_cfg: GameConfig) -> SaveSlot:
    slot_dir = SAVES_DIR / game / profile / slot_name
    logger.info("Importing save: game=%r profile=%r slot=%r mode=%s", game, profile, slot_name, game_cfg.save_mode)
    slot_dir.mkdir(parents=True, exist_ok=False)
    try:
        _copy_live_save(game_cfg, slot_dir)
        now = datetime.now()
        _write_json(slot_dir / "meta.json", _timestamps(now, now))
        (slot_dir / "notes.txt").write_text("", encoding="utf-8")
        return _read_slot(slot_dir, game, profile)
    except Exception:
        shutil.rmtree(slot_dir, ignore_errors=True)
        raise


def replace_save(slot: SaveSlot, game_cfg: GameConfig) -> None:
    _copy_live_save(game_cfg, slot.path, file_name=slot.save_file)
    now = datetime.now()
    meta_file = slot.path / "meta.json"
    meta = _read_json_dict(meta_file)
    created = _parse_iso(meta.get("created")) or now
    _update_meta(meta_file, _timestamps(created, now))
    slot.date_modified = now


def load_save(slot: SaveSlot, game_cfg: GameConfig, *, make_backup: bool = True) -> None:
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
    if not _live_save_exists(game_cfg):
        logger.warning("Skipping %s backup; no live save for game=%r", dir_name, game_cfg.name)
        return
    root = SAVES_DIR / dir_name / game_cfg.name
    dest = root / datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
    _copy_live_save(game_cfg, dest)
    for old in sorted(d for d in root.iterdir() if d.is_dir())[:-_MAX_BACKUPS]:
        shutil.rmtree(old)


def snapshot_practice_start(game_cfg: GameConfig) -> None:
    if not _live_save_exists(game_cfg):
        logger.warning("Skipping practice-start snapshot; no live save for game=%r", game_cfg.name)
        return
    dest = SAVES_DIR / _PRACTICE_START_DIR / game_cfg.name
    if dest.exists():
        shutil.rmtree(dest)
    _copy_live_save(game_cfg, dest)


def restore_practice_start(game_cfg: GameConfig) -> None:
    src_dir = SAVES_DIR / _PRACTICE_START_DIR / game_cfg.name
    if not src_dir.exists() or not _has_save_path(game_cfg):
        logger.warning("No practice-start snapshot to restore for game=%r", game_cfg.name)
        return
    first_file = next((f.name for f in sorted(src_dir.iterdir()) if f.is_file()), None)
    _copy_to_live(src_dir, game_cfg, first_file)
