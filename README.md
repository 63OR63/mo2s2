# S.T.A.L.K.E.R. 2: Heart of Chornobyl Support Plugin

Game support plugin for [Mod Organizer 2](https://github.com/ModOrganizer2/modorganizer).

Integrates S.T.A.L.K.E.R. 2: Heart of Chornobyl into Mod Organizer 2, providing native mod installation, virtual load order management, conflict detection, and diagnostic tools.

## Features

### Mod Installation

- Supports PAK (`.pak`) and IoStore (`.utoc` / `.ucas`) mods.
- Handles different archive layouts automatically.
- Supports standard, manual, and FOMOD installations.
- Normalizes mod structures for consistent installation and management.
- Provides repair tools for improperly structured installations.

### Load Order Management

- Integrates mod priorities with MO2's virtual file system.
- Maps active mod containers without modifying or repacking their contents.
- Automatically updates mappings when mods are enabled, disabled, renamed, or reordered.
- Detects unmanaged mod files outside MO2 without modifying them.

### Additional Mod Tools

A dedicated **Mod Tools** tab provides:

- **Archives** — Browse installed PAK and IoStore containers.
- **Contents** — Explore indexed resources without extracting archives.
- **Conflicts** — Identify overlapping resources and determine file-level winners and losers.
- **Diagnostics** — Inspect installation issues, mapping errors, and warnings.

## Compatibility

This plugin was developed and tested with **Mod Organizer 2 v2.5.3b12 (beta)**, available through the [official MO2 Discord server](https://discord.gg/ewUVAqyrQX).

## Installation

1. Install **Mod Organizer 2** with the **Basic Games** plugin collection.
2. Download the latest plugin release.
3. Extract the archive into your Mod Organizer 2 installation directory, preserving the included folder structure.
4. Restart MO2.
5. Select **S.T.A.L.K.E.R. 2: Heart of Chornobyl** when configuring your game instance.

The plugin files should be located at:

    MO2/
    └── plugins/
        └── basic_games/
            └── games/
                ├── game_stalker2heartofchornobyl.py
                └── stalker2heartofchornobyl/

## Usage

Install and manage mods through MO2 as you would for other supported games.

Use the left pane to enable, disable, and reorder mods. The plugin automatically handles supported container mappings and updates the virtual load order.

Open the **Mod Tools** tab to inspect installed archives, browse their contents, review file conflicts, and diagnose installation problems.

If a mod requires repair, review the proposed changes before applying them.

## Limitations

- Conflict detection operates at the file level. Internal CFG changes and bPatch conflicts are not analyzed.
- The plugin does not merge, modify, or repack PAK contents.
- Load order management does not override the game's internal CFG patch ordering rules.

## Troubleshooting

Check the MO2 application log for messages prefixed with `[mo2s2]`.

When reporting an issue, include the relevant log entries, a description of the problem, and the original mod archive if the issue involves installation or archive structure.

## Notes

The plugin may create internal `__mo2s2__` directories to manage normalized mod structures. These directories should not be modified manually.

Original PAK and IoStore container contents remain unchanged.

## Credits

Developed by **[63OR63](https://github.com/63OR63)**.

AI tools were used during development and testing.

Licensed under the [MIT License](LICENSE).
