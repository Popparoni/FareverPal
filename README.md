# Farever Pal

A **read-only by default**, **out-of-process** live companion + QA tool for
Farever (Heaps / HashLink). By default it attaches to the running game and
locates your character purely by **reading** memory — no writes, no code
injection, no network — and shows:

- **Entity / loot overlay** — nearest enemies & chests with the real game icons,
  distance, and the closest source's full predicted drop table by rarity.
- **DPS meter** — live DPS, sparkline, per-target (HP-diff) or per-skill/crit
  (via the game's own `DamageDisplay` numbers) breakdown. Self DPS only.
- **Minimap** — top-down POI radar (chests, gatherables, enemies, obelisks),
  zoom, right-click to mark collectibles done.
- **Offline loot predictor** — any loot table at any level.

It is out-of-process and (by default) write-free, so it **cannot freeze or
crash the game** (unlike an in-process DirectX-hook overlay).

> Read-only by intent. Farever is online & server-authoritative; this tool only
> observes and predicts — it cannot and does not change drops or any game state.

> **Optional opt-in hook.** There is one non-default fast-path for locating the
> player that installs a tiny, self-restoring code detour in the running
> process — it *writes* to the game's code in memory (it reads no game data and
> touches no save files, and is reverted on detach). It is **off by default**;
> the shipped tool is pure-read. Enable it only if you understand the tradeoff
> by setting the environment variable `FAREVER_ENABLE_HOOK=1`. With it off, the
> tool never writes to the game.

## Layout

```
companion/
  native/            Rust memory reader (farever_native, PyO3)
  farever_companion/ the app (core / data / combat / geo / ui)
  tests/             headless logic tests
  CREDITS.md         third-party attribution
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

1. Launch Farever and load fully into the world. If you have another memory
   tool attached to the game, unload it first to avoid a conflict.
2. Run the app, press **Attach**. It scans for your `ent.Hero` (a few seconds).
3. Open the overlays. Drag them by their title bars; set opacity / click-through
   in the control panel.

## Status

Phases 1–5 built and headless-validated; per-skill DPS (DamageDisplay field
offsets) and the player-select `isMe` fields are **calibrated live**.
Live testing is the remaining step.

## Credits & third-party work

See [`CREDITS.md`](CREDITS.md) for full attribution. In short:

- **Farever** is made by its developers — this is an unofficial community fan
  tool. Please support the game on Steam. Taken down / stripped on request.
- The minimap's world→pixel coordinate transform is derived from the community
  web map **[farever-map](https://github.com/IceCaveBear/farever-map)** by
  **IceCaveBear**. Credit and thanks to them.
- Built on the **Heaps** engine + **HashLink** VM (the game's stack); bundled
  fonts (Inter, JetBrains Mono) and Lucide icons under their own licenses.
