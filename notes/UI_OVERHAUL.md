# UI Overhaul — "Tactical Overlay" design system

> Executable spec for redoing the Farever Companion UI to match the approved
> mockups (user-designed in Stitch, 2026-05-27). Written so a FRESH context can
> implement it without re-deriving anything. Pairs with `PLAN.md` (architecture).
>
> Reference screenshots (image cache, may not persist — described fully below):
> `…/image-cache/584d997a-…/3.png` design tokens, `4.png` Overlays tab,
> `5.png` Loot tab, `6.png` Crosshair tab, `7.png` Entity/Loot HUD overlay,
> `8.png` DPS overlay.

## 0. How to execute (fresh context)
1. Read this file + `PLAN.md §9-10`. The engine (core/data/combat/geo/native)
   is DONE and unchanged — this overhaul is **UI only** (theme + widgets + shell
   + the two HUD overlays), plus bundling fonts/icons.
2. Work under `companion/`. App still runs today (old control panel); replace it.
3. Build/validate after each screen with the offscreen Qt smoke (see §12).

## 1. Design tokens (from screenshot 3)
Palette (replaces the current ones in `ui/theme.py`):
- **Primary / accent (cyan)** `#38BDF8`
- **Secondary (gold)** `#F0C836`
- **Tertiary (orange)** `#F1A02B`
- **Neutral** `#72787C`
- **Backgrounds:** app `#0B0D12`, surface/cards `#14171E`, raised cell `#1B2029`,
  hover `#222834`, border `#262C38`
- **Text** `#E6E8EE`, **muted** `#8B919F`
- **Rarity** (unchanged): Common `#C2C6D0`, Uncommon `#56D364`, Rare `#539BF5`,
  Epic `#C297FF`, Legendary `#F0A836`
- **Danger/enemy** `#F0556B`, **success** `#4ADE80`

Shape: **rounded** corners now (supersedes the old "sharp" pref) — cards
`radius 12`, raised cells/inputs `8`, tags `4`, toggle tracks full-pill, sidebar
items `8`. Subtle 1px borders, no gradients. Spacing: card padding 16-20, section
gap 16, control gap 8.

Type: **Inter** for UI (headline/body), **JetBrains Mono** for labels, numbers,
coords, status, table prob/seed. Section labels are UPPERCASE mono, ~11px, muted,
letter-spacing ~1.2px, often preceded by a 2px cyan tick mark.

## 2. Fonts & icons (assets to add)
- Add `companion/assets/fonts/` with **Inter** (Regular/Medium/SemiBold/Bold) and
  **JetBrains Mono** (Regular/Medium) TTFs (both OFL, redistributable). Load via
  `QFontDatabase.addApplicationFont` at startup; fall back to Segoe UI / Consolas.
- **UI chrome icons:** bundle a small **Lucide** (MIT) SVG set under
  `companion/assets/icons_ui/` and tint to theme colors at load. Needed glyphs:
  `layers` (Overlays), `swords` (Combat/DPS), `box`/`archive` (Loot),
  `crosshair` (Crosshair), `map` (Map), `terminal` (Log), `radio`/`broadcast`
  (brand), `settings`, `refresh-cw` (reset), `search`, `layout`, `chevron-down`,
  `x`, `check`, `dice-5` (procedural). Render SVG via `QtSvg`/`QSvgRenderer` →
  tinted QPixmap, cached by (name, color, size).
- **Game content icons:** keep using `data/icons.py` (item/unit/skill PNGs we
  already have). See §7 for the new tile presentation.

## 3. Shell layout (screenshots 4-6)
Main window ≈ 1000×680, rounded. Three persistent regions:
- **Left sidebar (≈210px):** brand block ("FAREVER" bold cyan + "v…-alpha" mono
  muted) at top; a vertical **nav rail** of items (icon+label): Overlays,
  Combat / DPS, Loot, Crosshair, Map, Log. Selected = raised-cell bg + 3px cyan
  left bar + cyan icon/text. Bottom of sidebar: **CURRENT CLASS** dropdown
  (Auto/Warrior/Rogue/Mage/Priest/Off).
- **Top bar:** brand-radio icon + "Farever Companion" title (left); **ATTACH**
  (cyan filled) + **DETACH** (outlined) (right). Status dot/pill moves here too:
  green "Attached PID … · player @ 0x…" or grey "detached".
- **Body:** a `QStackedWidget` switched by the nav (NOT QTabWidget — it's a left
  rail). One page per nav item.
- **Bottom status strip (mono, muted):** left `■ READ-ONLY · BACKEND: NATIVE ·
  PER-SKILL DPS: CALIBRATING|LIVE`; right `DOCS  SUPPORT  API` (links, optional).

## 4. Component library (new — `ui/widgets.py` / a new `ui/components.py`)
Build these as reusable styled widgets; every screen composes them:
- **ToggleSwitch** (animated pill; off=raised cell, on=cyan; emits toggled).
- **Stepper** `[−] value [+]` (square −/+ buttons, centered mono value, min/max/step).
- **SegmentedControl** (row of options in a bordered group; selected = cyan-tint
  + cyan text). Used for crosshair style.
- **Slider row** (label left, cyan-handle slider, right-aligned mono value/%).
- **RarityTag** (small uppercase mono pill; rarity-colored text + tinted bg +
  thin border).
- **IconTile** (see §7 — game icon on a rarity/faction-tinted rounded square).
- **NavItem** (sidebar entry; selected state).
- **OverlayCard** (big toggle card: icon, title, description, ToggleSwitch;
  active = cyan left border).
- **InfoCard** (icon + label + value, used for the two Loot footer cards).
- **ColorSwatch** (already have `ColorButton` — restyle to the chip+label look,
  e.g. "CYAN" + swatch).
- **SectionHeader** (uppercase mono + cyan tick mark) — already exists, restyle.
- **Card** (rounded surface container) — exists, add radius.

## 5. Screen specs
### Overlays (screenshot 4)
- 3 **OverlayCard**s in a row: ENTITY & LOOT / DPS METER / MINIMAP (icon, title,
  one-line desc, toggle to open/close that overlay). Desc copy per §8.
- **DISPLAY CONFIGURATION** (section): 2-col grid of label+ToggleSwitch cells:
  Enemies, Chests, Gatherables, Closest drops, Enemies only, Class-relevant only.
- **SIZING & METRICS** (section): Steppers — ENEMIES SHOWN, CHESTS SHOWN,
  ICON SIZE (PX), PREDICT LEVEL; then OVERLAY OPACITY slider (%), UI SCALE slider (×).
- Wire all to `Settings` + live-push to open overlays (opacity, accent already
  live). Class dropdown lives in the sidebar bottom.

### Combat / DPS (its overlay is screenshot 6)
- "Open DPS Meter" OverlayCard, Steppers for Tracking range + Rolling window,
  and a truthful note: "Self DPS only. Per-skill/crit shown when the DamageDisplay
  source is calibrated, else team-total via HP-diff."

### Loot (screenshot 5)
- Headline "DROP SIMULATION" + subtitle. Right indicator "OFFLINE PREDICTOR"
  (NOT "probability engine"/"hardware randomizer" — see §8).
- **LOOT SOURCE** dropdown showing **readable names** (`names.loot_table_label`,
  already implemented; store id as item data). **LEVEL** stepper. **Predict** (cyan).
- **DROP TABLE RESULTS** table: columns PROB. | ITEM | RARITY | TYPE.
  - PROB = cyan mono % (use `fmt_pct`).
  - ITEM = IconTile + readable name (`names.item_name`) colored by rarity;
    procedural tokens shown italic as "🎲 random …".
  - RARITY = RarityTag. TYPE = `names.humanize(type)` (VOUCHER/WEAPON/MATERIAL/…).
  - Replace the fake "SEED: 82A-X9" with nothing, or "TABLE: <id>" mono muted.
- Two footer **InfoCard**s, truthful: "DATA — CDB sheets (offline, N tables)" and
  "PREDICTOR — deterministic recursive expansion". (Drop "Live Realm"/"RNG".)

### Crosshair (screenshot 6 file = `6.png`)
- "Enable crosshair overlay" toggle (opens/closes `CrosshairOverlay`, already built).
- **GEOMETRIC PARAMETERS:** SegmentedControl Style = **Cross + Dot | Circle | Cross
  | Dot | T-Shape** (ADD "T-Shape": vertical + horizontal-top arms only — extend
  `crosshair.paint`). Steppers: Size, Center Gap, Thickness, Dot Size.
- **RENDERING OPTIONS:** Outline toggle, Opacity slider, **Primary Color**
  ColorSwatch (chip + name).
- **LIVE PREVIEW** panel on the right using `CrosshairCanvas` (already built),
  with a small mono coord caption. "SAVE PRESET" button optional (could persist a
  named preset to Settings; nice-to-have).
- All crosshair settings already exist in `Settings`; wire + live-update preview &
  open overlay (`apply_settings`). Crosshair needs NO game attach.

### Map
- "Open Minimap" OverlayCard. SegmentedControl **Shape = Circle | Square**
  (`minimap_shape`, already implemented). Zoom slider, Size stepper, toggles for
  Gatherables / Obelisks. Note: "Right-click a marker to mark it done."

### Log
- Full-height mono `QPlainTextEdit` on surface bg, timestamped lines.

## 6. HUD overlay specs
### Entity / Loot HUD (screenshot 7)
Compact ~260px rounded panel. Title bar: heading/coords id + ✕. Coords line
`X … Y … Z …` mono + small tag. Sections with colored mono headers + right tag:
**ENEMIES** (danger) "N TOTAL", **CHESTS** (gold) "NEARBY", **CLOSEST — DROPS**
(cyan) "LOOT_SCAN". Rows = IconTile + name (rarity/kind colored) + optional small
mono sub-label (e.g. "LEGENDARY ARTIFACT") + right value (distance / %). Closest's
top row is highlighted (rarity-tinted box). Footer: truthful `■ READ-ONLY ·
v…` + gear/layout icon buttons (open config / toggle). **Do NOT print "KERNEL
HOOKED"** — we have no kernel hook (see §8).

### DPS HUD (screenshot 8)
~210px panel. Title `DPS  [− range +]  ⟲`. Big danger number + "DPS". Mono sub
`PEAK …  TOTAL …K / …S  K <kills>`. Cyan sparkline. **BY SKILL ANALYSIS**: rows
of skill name + subtle fill bar + value + crit% (gold). Footer mono. (Already
mostly built; restyle to tokens + the bar/crit layout.)

## 7. Icon presentation (user's note — REPLACES the mockup's SVG glyphs)
Mockups use generic SVG glyphs for enemies/loot; instead use **our real game
icons inside a rounded-rect tile with a rarity/kind-colored background**:
- `IconTile(sheet, id, size, accent)`: rounded square (radius 6), background =
  accent color at ~18% alpha, 1px border = accent at ~55%, game PNG centered with
  ~2px padding (via `data/icons.py`).
- **Items/drops:** accent = rarity color. **Enemies:** accent = faction color if
  known else danger. **Chests:** accent = gold. **Skills (DPS):** accent = cyan.
- Missing PNG → tile still shows (tinted square placeholder). This is the standard
  row leading element across the Loot table and both HUDs.

## 8. Copy honesty fixes (REQUIRED — read-only/ethics stance)
The mockups contain flavor that overclaims; replace with truthful copy so the UI
never implies cheating/hooks we don't do:
- "KERNEL HOOKED" → **"READ-ONLY"** (we use no hook at all now).
- "fog-of-war bypass" (minimap desc) → **"top-down POI radar"**.
- "PROBABILITY ENGINE ACTIVE" → **"OFFLINE PREDICTOR"**.
- "Hardware randomizer: CALIBRATED", "SEED: 82A-X9", "Loot data synchronized with
  Live Realm 04", "RNG ENTROPY" → drop; use **"CDB sheets (offline)"** /
  **"deterministic predictor"**. We do not seed or touch the server RNG.
- "ACTIVE INSTANCE: 0x… SYNCED" (DPS footer) → **"READ-ONLY"** or omit.
Keep "PER-SKILL DPS: CALIBRATING/LIVE" (truthful), "BACKEND: NATIVE" (truthful).

## 9. File change map
- `ui/theme.py` — new tokens + QSS (rounded radii, sidebar, segmented, toggle,
  stepper, tags, mono labels). Add a font-loading helper.
- `ui/components.py` (NEW) — ToggleSwitch, Stepper, SegmentedControl, NavItem,
  OverlayCard, InfoCard, RarityTag, IconTile, SliderRow. (Or extend `widgets.py`.)
- `ui/control_panel.py` — REWRITE as sidebar + QStackedWidget shell with the 6
  pages above; wire Settings; open/close overlays + crosshair (no attach needed
  for crosshair); readable loot combo + readable result rows + RarityTag + IconTile.
- `ui/entity_overlay.py` — restyle to screenshot 7 (IconTile rows, sub-labels,
  truthful footer). Logic already correct.
- `ui/dps_overlay.py` — restyle to screenshot 8 (skill rows + crit% gold, footer).
- `ui/crosshair.py` — add "T-Shape" style; rest done.
- `ui/minimap.py` — done (circle/square already in).
- `data/icons.py` — add `tile(sheet,id,size,accent)` + a `ui_icon(name,color,size)`
  SVG loader (QtSvg). Add `PySide6` QtSvg (ships with Essentials).
- `app.py` — load fonts; set window/exe icon.
- `package.bat` — also bundle `assets/fonts` + `assets/icons_ui`.

## 10. Already DONE (keepers — don't redo)
- Readable names: `names.humanize`, `names.loot_table_label` (wired into entity
  overlay + ready for Loot tab).
- Crosshair overlay + preview canvas: `ui/crosshair.py` + all `crosshair_*`
  settings in `config.py`.
- Minimap circle/square mask: `minimap_shape` setting + `minimap.py` paint.
- Color customization: `Settings.hud_accent` (overlays read it live) +
  `widgets.ColorButton`; `crosshair_color`.
- Engine, predictor, DPS, native reader, tests — all unchanged and green.

## 11. Icons/assets to request from the user
We're mostly covered: **game content** = our extracted set (item 869 / unit 383 /
skill 617); **UI chrome** = bundled Lucide (MIT). So only export if handy:
1. A **Farever app/logo mark** (square, ≥256px PNG or SVG) for the title bar +
   the .exe icon.
2. Any **faction icons / currency icons** if you want them on tiles and they're
   not already in `htdocs/assets/icons` (we have affinity/status/itemType sets;
   confirm faction + currency exist).
3. Optional: if you dislike Lucide for the 6 nav glyphs, send your own
   monochrome SVGs for Overlays/Combat/Loot/Crosshair/Map/Log.
Everything else (rarity tags, type text, steppers, toggles) is drawn, no assets.

## 12. Validate (each step)
```
cd companion
QT_QPA_PLATFORM=offscreen .venv\Scripts\python.exe -c "from PySide6 import QtWidgets; from farever_companion.config import Settings; from farever_companion.ui import theme; from farever_companion.ui.control_panel import ControlPanel; app=QtWidgets.QApplication([]); theme.apply(app); w=ControlPanel(Settings()); w.show(); print('ok')"
.venv\Scripts\python.exe -m pytest -q
```
Then a live pass on Windows with the game running (Phase 8 in PLAN.md).
```
