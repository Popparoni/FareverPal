# Task: validate & fix the Farever loot predictor (drop% vs rarity‑roll semantics)

Paste this whole file as the first message in a fresh Claude Code session run from
`D:\Projects\FareverFandom`. It is self‑contained.

## The problem (reproduced)
The companion's loot predictor reports per‑item probabilities that don't match
real drops. Concrete repro (run from `companion/`):

```
.venv\Scripts\python.exe -c "from farever_companion.data import loot; \
import pprint; pprint.pp(loot.predict_sorted('MunsterChuck', 30))"
# -> DM_Multispin (Rare) 1.00%, Mace_Benediction (Rare) 1.00%  — sum = 2%
```

A boss drops a weapon ~100% of the time, with the **rarity rolled** (e.g. ~1%
legendary). But we show two weapons at 1% each (2% total) at their *base* rarity.
The user's framing: "boss drop shows 1% chance for a Rare weapon, but a weapon
always drops and the 1% is only the legendary chance." So **drop‑chance and
rarity‑roll‑chance are conflated/under‑counted.**

## Leading hypotheses (verify, don't assume)
1. **"Weights" tables aren't normalized.** `loot.predict` intentionally does NOT
   normalize weighted pick‑one tables ("matching the in‑game predictor is the
   contract" — see `loot.py` docstring). If a boss table is a *pick‑one weighted*
   table, 1% + 1% are relative weights → should normalize to 50% / 50% of a
   guaranteed drop. Check the `lootTable` CDB schema for a flag (Weights / unique /
   pickOne / count) distinguishing "independent rolls" from "pick N weighted."
2. **Guaranteed‑drop structure missing.** The boss's always‑drop may live in a
   different table/field (e.g. a signature table referenced elsewhere, or a
   `count`/`guaranteed` column) that the recursive expansion doesn't model.
3. **Rarity conflation.** `rarity.promote_distribution` rolls rarity for vouchers/
   equipment, but only fires when `should_promote` recognizes the item type; if a
   boss weapon isn't classified as equipment it shows a single conflated %.
4. **CDB data incompleteness** ("scraped"): entries/levels/conds filtered out.

## What's already here (read first)
- Predictor: `companion/farever_companion/data/loot.py` — `predict()` recursively
  expands a `lootTable` id: item entry adds `proba`; sub‑table entry multiplies by
  `proba`. Honors `minLvl/maxLvl/conds`. **Does not normalize Weights.**
- Rarity roll: `companion/farever_companion/data/rarity.py` — `promote_distribution`,
  `tier_chances` (from `rarity.props.generationChance`), `should_promote`,
  `is_equipment` (walks `itemType` inheritance to MainhandWeapon/Gear/Armor/…).
- Combined view: `companion/farever_companion/core/model.py` →
  `drop_table_effective()` (applies promotion), `enemy_drop_source`,
  `chest_drop_source`.
- Source data: `data/sheets/lootTable.json` (the loot tables — INSPECT its
  `columns` header for the entry/table schema + any weight/kind flag),
  `data/sheets/item.json` (rarity, type), `data/sheets/rarity.json`
  (generationChance), `data/sheets/unit.json` (boss → loot table link),
  `data/sheets/itemType.json`.
- Wiki/derived layer: `htdocs/assets/data/items.json` (id→name, `drops`,
  `via_token`, `pool_share`). Names: `companion/farever_companion/data/names.py`.
- **Prior validation:** the predictor was matched 1:1 against the in‑game CT
  predictor (Farever.CT entry `1337094962`) — see `tools/build_ct_loot_db.py`,
  `findings/formulas.md` §2, `findings/drops.md`, `findings/drops.csv`,
  `tools/farever_qa/_re_loot.py`. KEY NUANCE: "matches the game's internal predict
  function" is NOT the same as "matches observed drops" — reconcile both. The game
  may run a guaranteed roll + use this table only for the specific item/rarity.

## Ground truth to compare against
Cross‑check with a public Farever drop database. Candidates (search/confirm):
- The community map/wiki **IceCaveBear/farever-map** (`map.js`, `assets.json`,
  `regions.json` — may list drops per source).
- The minimap mod **ramisotti13-eng/farever-minimap** docs.
- Any Farever wiki (search "Farever Shiro Games wiki drops / loot table"; the game
  is recent — May 2026). WebFetch the relevant pages.
Pick 3–5 well‑known sources (a boss like Munster Chuck, a world crate, a vault)
and compare predicted vs published item lists + drop%.

## Task
1. **Characterize**: dump several tables (a named boss, `WorldCrate`, `Vault_Z2_1`,
   a trash mob) raw + effective; note where sums ≠ 100% and where rarity looks
   conflated. Inspect the `lootTable` schema for weight/kind/count flags.
2. **Model the real mechanic**: from the CDB schema (+ `hlbc` decompiled loot code
   in `decompiled/` if present, else the bytecode) determine: independent‑roll vs
   weighted‑pick‑one tables; how rarity is actually rolled; guaranteed drops.
3. **Reconcile with the wiki** sources above; record discrepancies.
4. **Fix** `loot.py`/`rarity.py`/`drop_table_effective` so the displayed table is
   truthful: guaranteed drops show ~100% with a rarity *distribution*, weighted
   tables normalize, independent rolls stay as‑is. Keep procedural tokens honest
   ("🎲 random …", never fabricated — see `data/tokens.py`).
5. **Document** in `findings/drops.md` + `findings/formulas.md` (cite CDB
   sheet+column, and each wiki source). Update `companion/farever_companion/data/`
   accordingly.
6. **Validate**: `cd companion && .venv\Scripts\python.exe -m pytest -q` (extend
   `tests/test_loot.py` with the corrected expectations), then spot‑check a few
   tables vs the wiki.

## Constraints
- Workspace rule: everything under `D:\Projects\FareverFandom\`. `D:\SteamLibrary\…
  \Farever\` is read‑only — never write there. `extracted/`, `data/sheets/` are
  read‑only‑by‑convention; re‑export, don't edit in place.
- The companion UI is DONE and unrelated — only the data layer
  (`loot.py`/`rarity.py`/`model.drop_table_effective`) + `findings/` change here.
- Output: Markdown tables in `findings/`; machine data as JSON/CSV. Cite sources.

## Deliverable
A corrected, wiki‑validated loot predictor where boss/chest drop tables read
truthfully (drop% vs rarity% separated), documented in `findings/`, tests green.
