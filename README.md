# Farever Pal

A **read-only**, **out-of-process** live companion + QA tool for Farever
(Heaps / HashLink). It
attaches to the running game, locates your character purely by reading memory
(no hook, no writes, no network), and shows:

- **Entity / loot overlay** — nearest enemies & chests with the real game icons,
  distance, and the closest source's full predicted drop table by rarity.
- **DPS meter** — live DPS, sparkline, per-target (HP-diff) or per-skill/crit
  (via the game's own `DamageDisplay` numbers) breakdown. Self DPS only.
- **Minimap** — top-down POI radar (chests, gatherables, enemies, obelisks),
  zoom, right-click to mark collectibles done.
- **Offline loot predictor** — any loot table at any level.

It is out-of-process and write-free, so it **cannot freeze or crash the game**
(unlike an in-process DirectX-hook overlay). See `PLAN.md` for the full design.

> Read-only by intent. Farever is online & server-authoritative; this tool only
> observes and predicts — it cannot and does not change drops or any game state.

## Layout

```
companion/
  PLAN.md            architecture + build order (read this first)
  native/            Rust memory reader (farever_native, PyO3)
  farever_companion/ the app (core / data / combat / geo / ui)
  tests/             headless logic tests
```

Data (CDB sheets, icons, names, chest positions) is reused from the workspace
(`../data/sheets`, `../htdocs/assets/...`, `../notes`) via `paths.py`.

## Run from source

```bat
:: from companion\  (a .venv with the deps is already set up)
build.bat                                  :: builds the Rust extension (farever_native)
.venv\Scripts\python.exe -m farever_companion
```

If `.venv\` is missing, recreate it:

```bat
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If you don't build the Rust extension, the app falls back to `pymem` for basic
reads, but the **player locate** (memory scan) needs `farever_native`.

## Test

```bat
.venv\Scripts\python.exe -m pytest -q
```

## Build the single-file .exe

```bat
build.bat        :: ensure the Rust ext is built first
package.bat      :: runs PyInstaller -> dist\FareverPal.exe (bundles sheets + icons + data)
```

`package.bat` contains the full PyInstaller command. The resulting
`dist\FareverPal.exe` (~305 MB, self-contained) is published as a GitHub
**release asset**, not committed to the repo.

## Use

1. Launch Farever and load fully into the world. Unload `Farever.CT` if loaded.
2. Run the app, press **Attach**. It scans for your `ent.Hero` (a few seconds).
3. Open the overlays. Drag them by their title bars; set opacity / click-through
   in the control panel.

## Status

Phases 1–5 built and headless-validated; per-skill DPS (DamageDisplay field
offsets) and the player-select `isMe` fields are **calibrated live** (PLAN §7/§8).
Live testing is the remaining step.
