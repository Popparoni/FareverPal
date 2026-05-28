"""User settings + persistence.

Stored as JSON under %APPDATA%/FareverCompanion/ so it survives across runs and
the packaged exe. No core/Qt imports here (pure data) so any layer can read it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / "FareverCompanion"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _settings_path() -> Path:
    return config_dir() / "settings.json"


@dataclass
class Settings:
    # what the overlays show
    show_enemies: bool = True
    show_chests: bool = True
    show_drops: bool = True
    enemies_only: bool = True
    enemy_count: int = 8
    chest_count: int = 6
    max_dist: float = 0.0            # 0 = no cap
    icon_size: int = 28
    opacity: float = 0.92
    ui_scale: float = 1.0
    click_through: bool = False
    lock_overlays: bool = False      # lock HUD position + make click-through (mouse passes to game)
    entity_scale: float = 1.0        # per-overlay UI zoom
    dps_scale: float = 1.0
    hud_accent: str = "#38bdf8"      # overlay accent/highlight color (per HUD tab)
    # drop prediction
    level: int = 20
    player_class: str = "Auto"       # Auto | Warrior | Rogue | Mage | Priest | Off
    class_only: bool = False
    rows_per_rarity: int = 6
    # dps
    dps_radius: int = 30             # bosses are always tracked regardless
    dps_window: float = 5.0
    # dps overlay size mode: small (just the DPS number), medium (number + graph
    # + per-entity bars), default (everything incl. recent-cycle history).
    dps_mode: str = "default"
    # minimap
    minimap_zoom: float = 14.0
    minimap_size: int = 320
    minimap_shape: str = "Circle"     # Circle | Square
    minimap_rotate: bool = True       # rotate map with player heading (else north-up)
    minimap_bare: bool = False        # chromeless: just the map, no titlebar/panel
    minimap_texture: bool = True      # draw the W1 world map under the POIs
    minimap_icons: bool = True        # POIs as real icons (else plain dots)
    minimap_icon_size: int = 18       # POI icon size on the minimap (px)
    # minimap layer visibility (independent of the entity overlay's show_* flags)
    minimap_enemies: bool = True
    minimap_chests: bool = True       # chests, crates & world-activity loot drops
    minimap_gatherables: bool = True
    minimap_obelisks: bool = True
    show_gatherables: bool = True
    show_obelisks: bool = True
    # crosshair overlay
    crosshair_style: str = "Cross + Dot"   # Cross + Dot | Circle | Cross | Dot | T-Shape
    crosshair_size: int = 10               # arm length (px)
    crosshair_gap: int = 4                  # center gap (px)
    crosshair_thickness: int = 2
    crosshair_dot: int = 2                  # dot radius (px)
    crosshair_color: str = "#62ff88"
    crosshair_outline: bool = True
    crosshair_opacity: float = 0.9
    # global hotkeys (system-wide; work while the game is focused)
    hotkey_loot_prev: str = "Ctrl+Alt+Z"        # select previous target
    hotkey_loot_next: str = "Ctrl+Alt+X"        # select next target
    hotkey_loot_close: str = "Ctrl+Alt+C"       # close the drop-table window
    # (avoid Ctrl+Alt+Arrows — those rotate the screen on many GPUs)
    # speedrun timer
    hotkey_speedrun_toggle: str = "Ctrl+Alt+T"   # start / stop the run timer
    hotkey_speedrun_reset: str = "Ctrl+Alt+R"    # reset to 00:00
    speedrun_scale: float = 1.0
    speedrun_best: dict = field(default_factory=dict)  # boss unit_id -> best seconds
    # speedrun automation — ONE master toggle: detect the dungeon (boss in scene),
    # auto-start the timer on the player's first move, and auto-stop on boss kill.
    speedrun_auto: bool = True
    # auto-upload finished runs to the web leaderboard (needs login + speedrun_auto;
    # when off, the overlay shows a manual Upload button after a finished run).
    speedrun_auto_upload: bool = False
    # which difficulty finished runs are uploaded as ("normal" | "hard"); manual
    # selector since the live boss level (lvl 20 = hard) isn't reliably read yet.
    # Defaults to hard — that's the difficulty most speedruns are run on.
    speedrun_mode: str = "hard"
    # --- web account (Farever Pal site link) ---
    api_base: str = "https://farever-pals.com"   # web platform base URL
    account_name: str = ""                       # logged-in username ("" = not signed in)
    account_code: str = ""                       # friend code (FRVR-XXXX-XXXX), for display
    account_token: str = ""                      # API bearer token (stored locally)
    account_avatar: str = ""                     # avatar image URL (for the top-right account button)
    # window geometry (per window key -> "x,y")
    geometry: dict = field(default_factory=dict)
    # collectibles marked done (set of ids, stored as list)
    poi_done: list = field(default_factory=list)

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(_settings_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        try:
            _settings_path().write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")
        except OSError:
            pass

    # --- poi done set helpers -------------------------------------------
    def is_done(self, poi_id: str) -> bool:
        return poi_id in self.poi_done

    def toggle_done(self, poi_id: str) -> bool:
        if poi_id in self.poi_done:
            self.poi_done.remove(poi_id)
            done = False
        else:
            self.poi_done.append(poi_id)
            done = True
        self.save()
        return done
