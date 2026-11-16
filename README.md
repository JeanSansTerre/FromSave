# FromSave Manager

A save slot manager for PC games. Import your current save, load it back at any time, and organize slots into profiles.

<img src="docs/screenshots/main_window.png" alt="FromSave Manager main window" width="700">

## Supported games

The following games come pre-configured and will appear in the game dropdown automatically:

- Elden Ring
- Dark Souls Remastered
- Dark Souls II: Scholar of the First Sin
- Dark Souls III
- Sekiro: Shadows Die Twice
- Armored Core VI
- Bloodborne (saved as a folder)
- Demon's Souls (saved as a folder)

Any other game can be added with the **+** button next to the **Game** dropdown (or via **Settings > Add game**).

## Getting started

1. Open the app and select a game from the dropdown, or add one with the **+** next to it.
2. Go to **Settings** and set the save file path for that game (not needed for a game you just added with **+**).
3. Press the **+** next to **Profile** to create a profile if the game has none yet.
4. Click **Import Save** to create your first slot.

## Linux / SteamOS

FromSave also ships as a Linux build, which runs natively on SteamOS in Desktop Mode.

1. Extract the release zip anywhere (for example a `Games/FromSave/` folder in your home directory).
2. Make the binary executable: `chmod +x FromSave`.
3. Run it directly.

The `saves/` folder sits beside the `FromSave` binary, exactly like `saves/` sits beside `FromSave.exe` on Windows.

> **Note:** Global hotkeys are not supported under Wayland (SteamOS Desktop Mode's default session). This is a limitation of the hotkey library FromSave uses. Hotkeys still work normally while the FromSave window is focused.

## Manually importing existing saves

If you already have save files backed up somewhere, you can drop them straight into the `saves` folder and the app will detect them automatically without using the Import button.

### Where the saves folder is

The `saves` folder is in the **same folder as `FromSave.exe`**:

```
FromSave.exe
saves/
```

### Folder structure

```
saves/
└── <Game Name>/
    └── <Profile Name>/
        └── <Slot Name>/
            └── your_save_file
```

| Folder           | What it becomes in the app                                                        |
|------------------|-----------------------------------------------------------------------------------|
| `<Game Name>`    | Must match the game name exactly. Pre-configured games already have folders here. |
| `<Profile Name>` | The profile that appears in the Profile dropdown                                  |
| `<Slot Name>`    | The name of the save slot                                                         |
| `your_save_file` | Your actual save file, placed directly inside the slot folder                     |

### Elden Ring example

Elden Ring's save file is named `ER0000.sl2`.

Say you have two saves you want to import, one from your first playthrough and one from a randomizer run, and you want them in separate profiles.

Create this structure inside your `saves` folder:

```
saves/
└── Elden Ring/
    ├── Main/
    │   ├── First Clear/
    │   │   └── ER0000.sl2
    │   └── Before Malenia/
    │       └── ER0000.sl2
    └── Randomizer/
        ├── Start/
        │   └── ER0000.sl2
        └── Mountaintops/
            └── ER0000.sl2
```

Once copied in, open the app (or switch to a different game and back). Your profiles and slots will appear exactly as named above.

### Notes

- You do **not** need to create `meta.json` or `notes.txt`, and the app does not add them to your slots. They only appear if you write notes or add a video link to a slot. Created and modified times are read from the save files themselves.
- For games saved as a whole folder, the slot mirrors what is inside the folder you set as the save path. For example, with the path set to `.../savedata`, a slot holds `<Slot Name>/BLUS30443DEMONSS005/...`. Loading puts those folders back inside `savedata`, leaving anything else there alone. Slots made by earlier versions of this app (`save_data/` or the folder's own name as a wrapper) still load.
- The slot folder name is what you will see in the slot list, so name it something meaningful.
- Multiple save files in one slot folder are supported (for games with more than one save file).
- After copying files in, **switch to a different game and back** in the app to refresh the list, or restart the app.

## Folders

Click **+ Folder** above the slot list to create a folder in the current profile. Drag a slot onto a folder to file it there, or drag it onto a top-level slot (or use **Remove from folder** in its right-click menu) to take it out. To move several slots at once, select them with Ctrl-click or Shift-click and drag them together, or right-click the selection and use **Move to folder**. Load, Replace, Delete and the other single-slot actions need exactly one slot selected. Double-click a folder to collapse or expand it (a single click only highlights it), and right-click it to rename or delete it. A folder only groups slots, so it can't be loaded into the game.

On disk a folder is a directory inside the profile that contains a hidden `.fromsave_folder` file, so slots from other save managers are never mistaken for folders. Slot and folder names are unique within a profile.

## Keyboard shortcuts

With the slot list focused: **F2** renames the selected slot or folder, **Delete** deletes the selected slot(s) or folder (a folder with slots in it asks first). **Ctrl+Z**, only while the FromSave window is active, undoes the last import, rename, move, new folder or delete (up to 20 steps). Load, Replace and Practice Mode are not covered; the game's save is protected by the automatic backups instead.

Importing follows the layout other save managers use: for a game saved as a folder, the slot contains what is inside the save folder (for Demon's Souls, the `BLUS...` save directory); for a single save file, the slot contains a copy of that file. The type is detected from what is actually at the save path, so a wrongly chosen save type no longer makes import fail.

## Practice Mode

Practice Mode locks a slot so the game can't overwrite it. When you enable it:

1. Select the slot you want to practice from.
2. Click the **Practice Mode** toggle.
3. The app immediately loads that slot into the game and then watches the save file. Any time the game tries to save, the app restores the slot, discarding the new data.

The game's current save is backed up (to `saves/_practice_start/<Game Name>/`) before the overwrite. When you disable Practice Mode, that save is automatically restored, putting the game back to where it was before you started practicing.

To save progress normally again, disable Practice Mode before saving in-game. While Practice Mode is on, selecting or loading another slot of the same game switches protection to that slot.

> **Note:** Practice Mode may not work correctly on all games, since it depends on how each game handles its save files.

## Run Mode

Run Mode is designed for active playthroughs where you want safety backups without accidentally losing your current save. When enabled:

- **Load Save** and **Practice Mode** are both disabled, so the game save can't be overwritten while you're playing.
- The app automatically takes a rolling backup of the game's save file every 2 minutes (to `saves/_run_backups/<Game Name>/<timestamp>/`), keeping the last 3 backups.

Toggle **Run Mode** on before starting a serious run and off when you're done. The backups are separate from your named slots and are not visible in the slot list. They're there in case the game corrupts or deletes your save mid-session.

## Backups

The app creates a few kinds of automatic backups. They all live inside the `saves` folder next to your named slots, but don't show up in the slot list. Unless noted otherwise, they're only there for you to restore by hand.

| Backup                 | Location                                      | When it's taken                                                               | Kept                                    | Restored automatically?             |
|------------------------|-----------------------------------------------|-------------------------------------------------------------------------------|-----------------------------------------|-------------------------------------|
| Load Save backup       | `saves/_backups/<Game Name>/<timestamp>/`     | Every time you click **Load Save**, right before the live save is overwritten | Last 3                                  | No                                  |
| Practice Mode snapshot | `saves/_practice_start/<Game Name>/`          | Once, when Practice Mode is enabled                                           | 1 (overwritten each time you enable it) | Yes, when Practice Mode is disabled |
| Run Mode backup        | `saves/_run_backups/<Game Name>/<timestamp>/` | Every 2 minutes while Run Mode is on                                          | Last 3                                  | No                                  |

To recover from a `_backups` or `_run_backups` snapshot, open the relevant timestamped folder and copy the save file(s) back to the game's save location by hand.

## Hotkeys & overlay

Under **Settings > Hotkeys** you can bind keys for Import save, Load save, Replace save, Practice Mode, Next slot, and Previous slot.

- By default, these only work while the app window is focused.
- Check **Enable global hotkeys** to make them work system-wide, even while tabbed into the game. On macOS this requires granting the app Accessibility permission. On SteamOS/Linux (Wayland), global hotkeys are not currently supported, hotkeys work only while the FromSave window is focused.

A separate **Overlay** can be shown over the game (default hotkey: **Insert**) with its own independent set of hotkeys, active only while the overlay is visible. It's a small, draggable, semi-transparent panel showing the current game, profile, and nearby slots, plus its own opacity slider in Settings.

> **Note:** The overlay will probably not work with games running in exclusive fullscreen mode. If possible, try using **borderless fullscreen/windowed mode** instead.

<img src="docs/screenshots/overlay_ingame.png" alt="Overlay shown over Elden Ring" width="500">

<table>
  <tr>
    <td><img src="docs/screenshots/hotkey_settings.png" alt="Hotkey settings" width="380"><br>Settings &gt; Hotkeys</td>
    <td><img src="docs/screenshots/overlay_settings.png" alt="Overlay settings" width="380"><br>Settings &gt; Overlay</td>
  </tr>
</table>

## Notes and videos

The details panel (slot name, notes, video link, and game info) is hidden by default so the slot list can use the whole window. Tick **Settings > Behaviour > Show details panel** to bring it back next to the slot list. Notes and video links are stored in a slot's `notes.txt` and `meta.json`.

## Checking for updates

FromSave can check GitHub for newer releases and update itself in place.

- Click **Check for Updates** in Settings to check manually, or enable **Automatically check for updates when opening** to check on every launch.
- If an update is found, downloading and applying it replaces the app files and restarts FromSave automatically.

## Adding a new game

1. Click the **+** next to the **Game** dropdown (or open **Settings** and click **Add game**).
2. Enter the game name and select the save type (single file, multiple files, or folder).
3. Set the path to the game's save file or folder.
4. Click **Add**. From the main window the game is added and selected right away; in Settings, click **Save** afterwards.

A folder for the game is created in `saves/` automatically. Then press the **+** next to **Profile** to create its first profile.

<img src="docs/screenshots/game_paths.png" alt="Game save paths in Settings" width="550">

## Icon attribution

App icon based on [Floppy disc icons](https://www.flaticon.com/free-icons/floppy-disc) by IYAHICON - Flaticon. Modified from the original.
