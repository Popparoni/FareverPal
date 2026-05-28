# Farever Companion — Architecture & Build Plan

> Greenfield rewrite of the Farever QA tool. Clean, layered, read-only.
> Python (PySide6 UI) + a Rust memory-reader extension. Packages to a single
> exe. Reuses the existing extracted data (CDB sheets, icons, names, positions).
> Resumable: read this file end-to-end and you have the full design + build order.

---

## 0. Why a rewrite (what we learned from `tools/farever_qa`)

The old tool (Python/Tkinter, `tools/farever_qa/`) proved every hard mechanic
live: attach, AOB scan, the read-only player hook, HashLink reflection, the
scene-graph walk, the loot predictor, HP read (`UnitAttributes+0xF0`), and a
working DPS meter. **All of that knowledge carries forward.** What it got wrong
was *structure and polish*, and three concrete bugs:

| Old bug | Root cause (code-confirmed) | Fixed by design here |
|---|---|---|
| Some boss HP missing on DPS | enemy test was `cls == "ent.Foe"` (exact leaf match) → boss subclasses excluded; also a hard 30u radius filter dropped large/ranged bosses | classify via HL **super-chain** (`is_a "ent.Foe"`); **bosses exempt from the radius filter** and always tracked |
| Loot tables inconsistent | `BossChest` table resolved from "is a named boss in scene *right now*"; only 10 name-matched bosses; static/live merge flips a chest's table frame-to-frame; predict level varied | **deterministic** table resolution (region/dungeon context + per-chest cache); broaden boss detection; fixed predict level per source |
| Inconsistent UI | hand-drawn overlays looked good; config panel + detailed view used stock `ttk` → "meh"; no shared component kit | **one** PySide6 design system (QSS + shared widgets) used by every window |

Old project stays in place as reference; **all new work lives under
`companion/`**. The hard workspace rule is unchanged: `D:\SteamLibrary\…\Farever\`
is read-only — never write to it.

---

## 1. Product

A **read-only** live companion + QA tool for Farever (Heaps/HashLink, no
anti-cheat, always-online/server-authoritative). Out-of-process. Three surfaces:

1. **Control panel** (normal window): attach, settings, offline loot predictor,
   data browser, logs. Configures the overlays.
2. **Overlays** (frameless, translucent, always-on-top, click-through):
   - **Entity/loot overlay** — nearest enemies + chests with real icons,
     distance, and the closest source's full drop table by rarity.
   - **DPS meter** — live DPS, sparkline, per-target/per-skill breakdown.
   - **Minimap** — top-down map with POIs (chests, gatherables, enemies,
     obelisks), zoom, and "mark done" persistence.
3. **Detailed view** — tabbed deep-dive (entities, attributes, drops) for QA.

**Philosophy (hard):** read-only. The end goal is **zero writes** (drop the old
player hook in favour of a Rust-accelerated pure-read player scan — see §4.2).
This keeps the dev-permission story clean and removes the only behavioural
footprint. Writes remain *possible* in the native layer but are gated and unused
by default.

---

## 2. Tech decisions (locked)

| Concern | Choice | Why |
|---|---|---|
| Language (logic/UI) | **Python 3.14** | fast to build, the workload is tiny (a few hundred 8-byte reads at 5–10 Hz) |
| Memory access | **Rust extension** (`PyO3` + `maturin`, `abi3-py310`) | batches a whole scene snapshot into one FFI call; makes the pure-read player scan fast enough to drop the hook; one wheel works across Python versions |
| UI | **PySide6 6.11** (Qt, abi3) | real design system, QSS theming, translucent/click-through overlays, GPU-smooth charts; single-exe friendly |
| Packaging | **PyInstaller** one-file | users get one .exe; bundles the `.pyd`, Qt, icons, sheets |
| Process fallback | **pymem** | if the Rust ext isn't built yet, core still runs (slower) |
| Tests | **pytest** (headless) | DPS engine, loot predictor, HL parsing are pure logic — unit-tested without the game |

PyO3 and PySide6 both ship **abi3** builds → no per-Python-version rebuilds, and
PyInstaller bundles them into one exe. Python 3.14 confirmed compatible.

---

## 3. Repository layout

```
companion/
├── PLAN.md                     # this file
├── README.md                   # quickstart + build + package
├── pyproject.toml              # python project + deps
├── requirements.txt
├── build.bat                   # build rust ext (maturin) + run checks
├── package.bat                 # PyInstaller one-file build
├── native/                     # Rust memory reader (PyO3 extension `farever_native`)
│   ├── Cargo.toml
│   └── src/lib.rs              # attach, read, read_many (batch), aob_scan, alloc/write (gated)
├── farever_companion/
│   ├── __init__.py
│   ├── __main__.py             # python -m farever_companion
│   ├── app.py                  # QApplication wiring, window lifecycle
│   ├── config.py               # Settings dataclass + JSON persistence (%APPDATA%)
│   ├── paths.py                # locate repo root → sheets / icons / positions
│   ├── core/                   # process + live model (NO Qt imports)
│   │   ├── backend.py          # picks farever_native, falls back to pymem
│   │   ├── proc.py             # Proc: attach/read/scan/alloc over the backend
│   │   ├── hl.py               # HashLink reflection: types, super-chain, strings, arrays
│   │   ├── player.py           # PlayerLocator: rust scan (preferred) | hook (fallback)
│   │   ├── scene.py            # scene graph → Entity/Element snapshot (batched)
│   │   ├── attributes.py       # HP + attribute IntMap reads
│   │   └── model.py            # LiveModel: one snapshot/tick, shared by all views
│   ├── data/                   # static, process-free (CDB-backed)
│   │   ├── cdb.py              # sheet loader (cached)
│   │   ├── loot.py             # recursive predictor (port, 1:1 with old)
│   │   ├── units.py            # unit→type→table, boss detection (broadened)
│   │   ├── rarity.py           # rarity promotion / generation chance
│   │   ├── classes.py          # class relevance of items
│   │   ├── names.py            # id → display name (texts)
│   │   ├── tokens.py           # procedural-generator tokens (WorldLoot…)
│   │   └── icons.py            # id → QPixmap from htdocs/assets/icons (cached)
│   ├── combat/
│   │   └── dps.py              # DPS engine: pluggable damage source (HP-diff | events)
│   ├── geo/
│   │   ├── chests.py           # chest position index (reuse old notes JSON)
│   │   └── poi.py              # merged POI model for the minimap + map cross-check
│   └── ui/                     # PySide6 only
│       ├── theme.py            # palette + QSS (dark, flat, sharp corners)
│       ├── widgets.py          # Card, SectionHeader, IconRow, Bar, Sparkline, TitleBar, Toggle, Stepper
│       ├── overlay_base.py     # frameless + translucent + click-through base
│       ├── control_panel.py    # main window
│       ├── entity_overlay.py   # nearest enemies/chests + closest drops
│       ├── dps_overlay.py      # DPS meter overlay
│       ├── minimap.py          # minimap overlay
│       └── detail_view.py      # tabbed QA deep-dive
├── tests/
│   ├── test_loot.py            # predictor matches expected
│   ├── test_dps.py             # HP-diff engine, kill credit, rebaseline
│   ├── test_hl.py              # HL string/array/type parsing on fixtures
│   └── test_units.py           # boss detection, table resolution determinism
└── notes/                      # design scratch, RE results specific to new build
```

**Data is reused, not copied.** `paths.py` locates the repo root and resolves:
- CDB sheets → `D:\Projects\FareverFandom\data\sheets\*.json`
- icons → `D:\Projects\FareverFandom\htdocs\assets\icons\{item,unit,skill,_shared}/`
- chest positions → `tools/farever_qa/notes/chest_positions.json` (+ regenerate path documented)
For the packaged exe, `package.bat` copies the needed sheets/icons into the bundle.

---

## 4. Core design

### 4.1 Backend & Proc
`backend.py` tries `import farever_native` (Rust); if absent, wraps `pymem`.
`Proc` exposes: `attach(name)`, `read(addr,n)`, `u64/i32/f64(addr)`,
`read_many(addrs, size) -> list[bytes]` (one batched call in Rust),
`aob_scan(pattern) -> [addr]`, and gated `alloc_near/write/protect` (hook only).
No address caching across sessions (ASLR).

### 4.2 Player location — pure-read scan, no hook (method confirmed)
The player pointer has no static anchor (it's a method `this`). Old tool kept a
read-only hook because a pure Python heap scan took ~87 s. **New plan:**
`PlayerLocator.scan()` uses the Rust `find_bytes` to find heap objects whose
first qword == the `ent.Hero` `hl_type*` (rw_only, align 8) — fast in Rust — then
**selects the real local player exactly the way the competitor does** (intel
2026-05-27): the `ent.Hero` whose **`Hero.ownerPlayer.isMe == 1`**, verified
bidirectionally with **`Player.hero == Hero`** (rejects network sync proxies).
This makes the tool **100% read-only — no hook, no writes at all** (strictly
better than the competitor, who intercepts `hl_alloc_obj`; an allocator detour
is itself a code write we avoid). The old trampoline hook is retained only as a
disabled, gated last resort.

Corroborating signature (from the mod's older external version, intel
2026-05-27): the Hero struct has a **~1584-byte fingerprint** and a
**4-contiguous-double position signature** — usable to validate a scan hit
(our old offsets: 3 contiguous pos doubles @ +0x98/A0/A8; the 4th is likely
heading/`gier`). Their external scan takes **~1 min on 1–2 cores** and re-runs
on dungeon enter/exit; our targeted 8-byte `hl_type*` match (rw_only, align 8)
in Rust should be seconds, not a minute — a concrete UX win to verify in Phase 8.

Resolving the field offsets (`ownerPlayer`, `isMe`, `hero`, `isInCombat`):
prefer **HL reflection** — read `hl_type_obj.fields` (+0x20) + the runtime
object's `fields_indexes` to map a field *name* → byte offset, so offsets
survive patches (resolve by name each session). Empirical live inspection is
the fallback. See §7 for the field list to resolve.

### 4.3 HashLink reflection (`hl.py`)
Port of the proven `hlrt.py` (HL 64-bit object model): `class_of`,
`super_chain`, **`is_a(instance, ancestor)`**, `hl_string`, `array`. Offsets
validated 2026-05: type@+0, type_obj@+8, name@+0x10, super@+0x18; String
bytes@+8/len@+0x10; ArrayObj len@+8/backing@+0x10, NativeArray size@+0x10/data@+0x18
(post-2026-05-21 patch layout).

### 4.4 Scene snapshot (`scene.py`) — batched
Walk `pbase +0x58 → GameLayer`; units at `+0x128`, elements at `+0x120`.
Per unit: owner@+0x60, pos@+0x98 (x,y,z f64), unit-id String@+0x250,
attributes@+0x3D0. **Build the snapshot with one `read_many`** of all unit
base pointers, then batched field reads — turning hundreds of syscalls into a
handful. Classify with `is_a("ent.Foe")` + owner check (fixes boss inclusion).

### 4.5 Attributes / HP (`attributes.py`)
Current Health = f64 at `UnitAttributes+0xF0` (validated player + enemy). Read
in the batch. MaxHealth: no fixed offset found → track max-observed **per unit
identity**, seeded from `unit.json` stats when available, with a phase-reset
guard (a large upward jump rebaselines instead of counting negative damage).
Follow-up: locate the exact MaxHealth offset / decode the attribute IntMap.

### 4.6 LiveModel (`model.py`)
Produces **one immutable `Snapshot` per tick** (player xyz, entities, elements,
derived nearest lists, dps state). Every view reads the latest snapshot — no
view re-reads the process, so the overlay, minimap, and DPS meter are always
on the same frame and we never double-read. Tick cadence configurable
(default ~8–10 Hz, cheap thanks to batched reads).

### 4.7 DPS engine (`combat/dps.py`) — pluggable source
- **Default source: HP-diff** (port of the working engine) — team/area total,
  bosses always tracked (no radius drop), kill-credit + rebaseline guards.
- **Upgrade source: damage events via `ui.comp.DamageDisplay` (read-only).**
  Method confirmed from competitor intel (2026-05-27): the game's UI creates one
  `ui.comp.DamageDisplay` object **per floating number that renders on _your_
  screen** — i.e. already filtered to your client's outgoing/visible damage.
  Each holds a `st.skill.DamageResult` with **{ BaseSkill, amount, crit flag,
  kill flag, target ref }**. We get these **without hooking the allocator**
  (that's a code write): each tick, enumerate the live `DamageDisplay` objects
  — either by walking the HUD/UI component container (preferred, located once)
  or by a Rust `find_bytes` scan for the `DamageDisplay` `hl_type*` — read each
  one's `DamageResult`, and **dedupe by object identity** so each number is
  counted once over its ~1 s lifetime. Drop any whose `DamageResult.target ==
  myHero` (incoming/DoT). Aggregate per-skill totals, crit%, max hit, kills,
  DPS. Combat window = `Hero.isInCombat` OR-ed with a ~7 s damage-idle timeout.
  HP-diff remains the fallback when the DamageDisplay path isn't available.

### 4.8 Loot (`data/loot.py`, `data/units.py`) — deterministic
- Predictor: 1:1 port (recursive table expansion; matches old + CT).
- Boss detection broadened beyond the 10 id==table matches: add region/dungeon
  → boss mapping and a `flags`-bit check (decode the boss/elite bit).
- Chest table resolution is **pure + cached per chest id** (no dependence on
  what's in scene this instant), so the displayed table never flickers.
- Procedural `WorldLoot*` tokens shown honestly as "🎲 random" — never
  fabricated.

### 4.9 UI system (`ui/theme.py`, `ui/widgets.py`)
One QSS stylesheet (dark `#0e0f13` base, flat, **sharp corners**, cyan accent
`#38bdf8`, rarity colours from the wiki). Shared widgets so every window looks
identical: `Card`, `SectionHeader`, `IconRow` (icon+text+value), `Bar`,
`Sparkline` (QPainter), `TitleBar` (custom, draggable), `Toggle`, `Stepper`.
Overlays subclass `overlay_base.OverlayWindow` (frameless, `WA_TranslucentBackground`,
topmost, per-widget click-through via Win32 `WS_EX_TRANSPARENT` toggling).
Real game icons via `data/icons.py` (Pillow/Qt scaling, cached by (id,size)).

### 4.10 Minimap (`ui/minimap.py`, `geo/poi.py`)
Top-down (x,y) canvas (QPainter) centered on the player with heading, zoom
(10–20×), POI layers (chests/gatherables/enemies/obelisks) each toggleable,
right-click "mark done" persisted to config. POIs come from: live scene
(enemies, live chests, gatherables) + static chest index + (Phase 7)
community-map cross-check.

---

## 5. Data reuse (icons + names + positions)

- **Icons:** `htdocs/assets/icons/item/<id>.png`, `unit/<id>.png`,
  `skill/<id>.png`, plus `_shared/`. 869 item / 383 unit / 617 skill icons
  already extracted. `data/icons.py` resolves id→pixmap, falls back to a
  generated placeholder.
- **Names:** from CDB `texts`/`gameTerm` sheets (port `names.py`).
- **Positions:** reuse `tools/farever_qa/notes/chest_positions.json`
  (229 chests) and the chest loot index; regen path documented.
- **CDB sheets:** all of `data/sheets/*.json` (item 877, lootTable 103,
  unit 403, unitType 24, rarity 5, attribute 77, …).

---

## 6. Build order (each step ends in something runnable/testable)

- **Phase 1 — Skeleton + data layer (headless).** `paths`, `cdb`, `loot`,
  `units`, `names`, `rarity`, `classes`, `tokens`, `geo/chests`. Unit tests
  green (loot predictor, boss detection determinism). No process, no Qt.
- **Phase 2 — Rust native ext.** `native/` crate: attach/read/read_many/
  aob_scan via PyO3; `maturin develop` builds the `.pyd`. `backend.py` selects
  it; `proc.py` over it. Smoke test reads module base `MZ`.
- **Phase 3 — Core live model.** `hl`, `player` (rust scan first), `scene`
  (batched), `attributes`, `model`. Headless CLI dump of the live snapshot to
  prove parity with the old tool.
- **Phase 4 — UI system + control panel.** `theme`, `widgets`, `overlay_base`,
  `control_panel` (attach, settings, offline predictor, log). Runs without the
  game (predictor works offline).
- **Phase 5 — Overlays.** `entity_overlay`, `dps_overlay` (HP-diff source,
  bosses tracked), `minimap`. Wired to the shared LiveModel snapshot.
- **Phase 6 — DPS event source (research).** Locate the read-only damage-event
  seam; implement `DamageEventSource`; upgrade the DPS UI to per-skill/crit.
- **Phase 7 — Map cross-check + packaging.** Cross-check positions vs
  questlog.gg / icecavebear maps; fill spawn gaps. `package.bat` → one-file exe.
- **Phase 8 — Live tests.** Run against the live game; verify boss HP/DPS,
  loot consistency, overlay/minimap, per the checklist in §8.

Per the user's autonomy preference, build Phases 1–5 straight through; Phases
6–7 are research/iteration; Phase 8 needs the game running.

---

## 7. Carried-forward constants (validated, from the old project)

AOB (full-process, x64, all "should be unique"):
- `player`: `4D 8B 1A 4D 8B 5B ?? 49 8B CA 8B 55 ?? 4D 8B C1` (hook fallback only)

Scene-graph offsets (HL 64-bit):
- GameObject +0x58 → GameLayer; GameLayer +0x128 → units ArrayObj,
  +0x120 → elements ArrayObj, +0x110/+0x118 other interactible lists.
- Unit: +0x60 owner, +0x98/A0/A8 xyz (f64), +0x250 unit-id String,
  +0x3D0 UnitAttributes; Attributes +0xF0 → current Health (f64).
- Element/interactible: +0x268 id String, +0x290 state String.
- Player struct: +0x98/A0/A8 xyz, +0xF8 camera height.

HL classes + fields to resolve live (from competitor intel 2026-05-27; resolve
offsets by reflection or live inspection):
- `ent.Hero`: `ownerPlayer` (→ `st.Player`), `isInCombat` (bool).
- `st.Player`: `isMe` (bool/int), `hero` (→ `ent.Hero`, for the bidirectional check).
- `ui.comp.DamageDisplay`: `damageResult` (→ `st.skill.DamageResult`).
- `st.skill.DamageResult`: skill (→ `BaseSkill`/skill id), `amount` (damage),
  `crit` (flag), `kill`/`isKill` (flag), `target` (→ unit; == myHero ⇒ incoming).
HL reflection for offsets: `hl_type_obj.fields` @ +0x20 (array of
`hl_obj_field{ name*, type*, hashed_name }`), byte offsets from the runtime
object's `fields_indexes`. Resolve by field NAME each session (patch-robust).

Named bosses (id == lootTable id), the convention base set:
`Nepsilon, Reblochonk, Gatsbee, Crabgantua, MunsterChuck, Mokshi, SpongeBlob,
Golcano, Cleodora, Ratsar`. (Broaden via flags-bit + region map in Phase 3/8.)

unitType→lootTable: Crimson→Crimson, Spirit→Spirits, Slime→Slime, Bee/Swarowl→Bee,
Sprouts→Sprouts, Wolf→Wolf, Boar→Boar, Skunk→Skunk, Coyote→Coyotes,
Manfish→Manfish, Crab→Crab, Golem→EarthGolems, FireGolems/WindGolems/
WaterGolems→self, Kobold→Kobold; Human/Demon/Ogre/Critter/Mount/Totem/
Environment→None (no trash table).

Server-authoritative truth: loot **cannot be forced** from the client (proven
exhaustively in the old project). This tool **observes/predicts only**.

---

## 8. Live-test checklist (Phase 8)

1. Launch Farever; run the companion; **Attach**. Status shows PID; player
   located (rust scan, no hook) — confirm "read-only, no writes."
2. Walk: player xyz + nearest lists + minimap update smoothly.
3. **Boss HP/DPS:** engage each of the 10 named bosses + a few elites. DPS
   meter shows non-zero for *every* boss (the old "some bosses missing" bug is
   gone). Confirm boss is tracked even when >30u away.
4. **Loot consistency:** approach the same chest repeatedly / from different
   states — the shown table is stable. Boss-chest table resolves deterministically.
5. **UI:** all windows share one look; overlays are click-through where
   expected and draggable by their title bars.
6. Open a chest / kill an enemy → observed drops within statistical
   expectation of the prediction.
7. (Phase 6) per-skill DPS matches the floating numbers.

---

## 9. Decision log

- **Rust now, not "maybe later":** user decision. It earns its place by (a)
  batching per-frame reads and (b) enabling the pure-read player scan that lets
  us drop the last write — not by raw speed at low poll rates.
- **PySide6 over Tkinter/DearPyGui:** user decision; one design system, real
  overlays, single-exe via PyInstaller, abi3 so 3.14 is fine.
- **No in-process injection / DirectX hook:** rejected. The competitor's
  in-process model carries crash risk + a bigger footprint; out-of-process
  read-only keeps the dev-permission story clean. We borrow its *ideas*
  (minimap, per-skill DPS via event observation), not its injection.
- **"Binary invisibility" clarified:** the competitor's "GC-invisible worker"
  is an *in-process stability* fix (don't let HashLink's GC stop-the-world race
  the reader), not anti-cheat. Our out-of-process analog: guard pointer-chain
  reads against GC moves (reject implausible, re-read on miss).
- **Loot forcing stays closed:** server-authoritative; observe/predict only.

---

## 10. Competitive validation & what we do better (Reddit thread, 2026-05-27)

The minimap mod's author independently recommends **our exact architecture**:
for online-safety, "either an external overlay, or **a separate memory reader
that feeds its own UI/app**." We're the second. Read-only, no writes, no
network, no input automation, no other-players' data — same ethics, cleaner.

**In-process failure modes we structurally avoid.** The mod (D3D12 Present hook
via Kiero+MinHook, in-process ImGui) generated a long tail of crash/freeze
reports: freezes on hearthstone/zone change, "unauthorized access violation"
crashes ~15 min in, the overlay hanging and slowing the *whole game*, the mouse
being grabbed mid-fight, a black screen until F7. These are all consequences of
living inside the game's process/render thread. **Our out-of-process reader +
separate translucent Qt window cannot freeze or crash the game**, and input
never routes through us. This is our headline reliability advantage — keep it.

**Trust/distribution.** Two users reported AV flagging a **trojan in the mod's
`dinput8.dll`** (likely an injector-DLL heuristic, possibly a repackaged build).
Our tool is **open Python source + a Rust extension the user builds locally** —
no opaque injected binary to trust. Ship source + a reproducible build; if we
distribute the PyInstaller exe, document the build so it's verifiable.

**Confirmed scope facts.** DPS is **self-only by design** (the game only creates
a `DamageDisplay` for *your* visible numbers — party DPS is impossible this way;
don't promise it). No anti-cheat ships with Farever (no AC process, nothing in
Steam's AC field), but the dev (Shiro) has made **no official statement** on
overlays/client mods — so keep the read-only stance and the take-down-on-request
posture.

**Feature backlog from the community (post-MVP):** compass rose
(Elder-Scrolls-style heading strip), party-member positions on the minimap,
center crosshair, per-collectible check-off (already planned). Minimap + DPS +
loot remain the core.
```
