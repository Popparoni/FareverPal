"""Entity overlay + a separate Drop Table window (Tactical Overlay HUD).

The entity HUD lists nearby enemies (selectable with ↑/↓ or click) and chests —
short, no endless scrolling. Selecting an enemy opens a separate, closable
**Drop Table** window showing that target's full predicted loot (rarity-grouped,
collapsible; class-usable items starred). If the selected target leaves range we
flip to the first available one. Reads the shared LiveModel. Procedural
generators shown honestly as "🎲 random X". Read-only.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from PySide6 import QtCore, QtGui, QtWidgets

from . import theme
from .overlay_base import OverlayWindow
from .components import SectionHeader, IconTile
from .widgets import fmt_pct
from ..data import names, classes, tokens, icons

POLL_MS = 350
_RARITY_ORDER = {"Legendary": 0, "Epic": 1, "Rare": 2, "Uncommon": 3, "Common": 4}


@dataclass
class RowSpec:
    sheet: str | None
    id_: str | None
    accent: str
    name: str
    name_color: str
    sub: str = ""
    value: str = ""
    bold: bool = False
    highlight: bool = False
    cb: object = None


class _EntityRow(QtWidgets.QFrame):
    """IconTile + name (+ optional mono sub-label) + right value. Pooled."""
    clicked = QtCore.Signal()

    def __init__(self, icon_size: int):
        super().__init__()
        self._highlight = ""
        self._cb = None
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(9)
        self.tile = IconTile(icon_size)
        mid = QtWidgets.QVBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)
        self.name = QtWidgets.QLabel()
        self.sub = QtWidgets.QLabel()
        self.sub.setObjectName("Mono")
        mid.addWidget(self.name)
        mid.addWidget(self.sub)
        self.value = QtWidgets.QLabel()
        self.value.setObjectName("Mono")
        self.value.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.value.setMinimumWidth(56)            # reserve room so % never clips
        self.name.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                QtWidgets.QSizePolicy.Preferred)  # name yields, value wins
        lay.addWidget(self.tile)
        lay.addLayout(mid, 1)
        lay.addWidget(self.value, 0)

    def apply(self, spec: RowSpec, icon_size: int):
        self.tile.set_size(icon_size)
        self.tile.set(spec.sheet, spec.id_, spec.accent)
        weight = "700" if spec.bold else "500"
        self.name.setText(spec.name)
        self.name.setStyleSheet(
            f"color:{spec.name_color};font-weight:{weight};background:transparent;")
        self.sub.setText(spec.sub)
        self.sub.setVisible(bool(spec.sub))
        if spec.sub:
            self.sub.setStyleSheet(f"color:{theme.MUTED};background:transparent;")
        self.value.setText(spec.value)
        self.value.setStyleSheet(f"color:{spec.name_color};background:transparent;")
        self._highlight = spec.accent if spec.highlight else ""
        self.set_callback(spec.cb)
        self.update()

    def set_callback(self, cb):
        if self._cb is not None:
            self.clicked.disconnect(self._cb)
            self._cb = None
        if cb is not None:
            self._cb = cb
            self.clicked.connect(cb)
        self.setCursor(QtCore.Qt.PointingHandCursor if cb else QtCore.Qt.ArrowCursor)

    def paintEvent(self, e):
        if self._highlight:
            p = QtGui.QPainter(self)
            p.setRenderHint(QtGui.QPainter.Antialiasing, False)
            c = QtGui.QColor(self._highlight)
            bg = QtGui.QColor(c); bg.setAlpha(30)
            p.fillRect(self.rect(), bg)
            p.fillRect(0, 0, 2, self.height(), c)     # accent left bar
            p.end()
        super().paintEvent(e)

    def mousePressEvent(self, e):
        if e.button() == QtCore.Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class _Section(QtWidgets.QWidget):
    def __init__(self, title, color):
        super().__init__()
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 5, 0, 5)
        lay.setSpacing(2)
        self.header = SectionHeader(title, color, colored_label=True)
        lay.addWidget(self.header)
        self.rows = QtWidgets.QVBoxLayout()
        self.rows.setSpacing(1)
        lay.addLayout(self.rows)
        self._pool: list[_EntityRow] = []

    def fill(self, specs: list[RowSpec], icon_size):
        while len(self._pool) < len(specs):
            r = _EntityRow(icon_size)
            self._pool.append(r)
            self.rows.addWidget(r)
        for i, spec in enumerate(specs):
            r = self._pool[i]
            r.apply(spec, icon_size)
            r.show()
        for j in range(len(specs), len(self._pool)):
            self._pool[j].hide()


def _scroll_body():
    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
    scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
    scroll.verticalScrollBar().setStyleSheet("margin:0;")
    body = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(body)
    lay.setContentsMargins(0, 0, 13, 0)   # clear the scrollbar gutter
    lay.setSpacing(0)
    scroll.setWidget(body)
    return scroll, lay


# ---------------------------------------------------------------------------
#  Drop Table — its own closable window for the selected target's loot
# ---------------------------------------------------------------------------
class DropTableOverlay(OverlayWindow):
    def __init__(self, model, settings, parent=None):
        super().__init__("DROP TABLE", settings, geo_key="droptable", parent=parent)
        self.model = model
        self.s = settings
        self._collapsed = set()  # all rarities expanded by default
        self._near = None        # the loot source (enemy or chest) being shown
        self._name = ""
        scroll, self._body = _scroll_body()
        self.box = _Section("DROPS", self.s.hud_accent or theme.ACCENT)
        self._body.addWidget(self.box)
        self._body.addStretch(1)
        self.content.addWidget(scroll, 1)
        # Wider than the entity HUD: drop rows carry long item names AND a long
        # right-side value ("Σ 0.001% · 1 · ★1"), plus the header shows the full
        # target name + "Nm · Ln" tag — at 320 the right side clipped.
        self._base_w, self._base_h = 392, 480
        self.setMinimumWidth(360)
        self.resize(392, 480)

    def set_target(self, near, name: str):
        """`near` is a model.Nearest (enemy or chest) or None; `name` is the
        readable target label."""
        self._near = near
        self._name = name or "?"
        self._render()

    def _retint(self, accent: str) -> None:
        if accent:
            self.box.header.set_color(accent)

    def _toggle(self, rarity):
        self._collapsed.symmetric_difference_update({rarity})
        self._render()

    def _active_class(self):
        pc = self.s.player_class
        return None if pc in ("Off", "Auto") else pc

    def _render(self):
        near = self._near
        self.titlebar.title.setText(f"DROP TABLE · {self._name}")
        self.box.header.set_text(self._name)
        if near is None:
            self.box.header.set_tag("NO TABLE")
            self.box.fill([], self.s.icon_size)
            return
        self.box.header.set_tag(f"{near.dist:.0f}m · L{near.level}")
        active = self._active_class()
        drops = self.model.drop_table_effective(near)
        groups = defaultdict(list)
        for item, prob, rar, _typ in drops:
            groups[rar or "Common"].append((item, prob))

        def rel(it):
            return classes.is_for_class(it, active)

        specs: list[RowSpec] = []
        first_item = True
        for rarity in sorted(groups, key=lambda r: _RARITY_ORDER.get(r, 9)):
            items = groups[rarity]
            total = sum(p for _, p in items)
            n_cls = sum(1 for it, _ in items if rel(it))
            col = theme.rarity_color(rarity)
            collapsed = rarity in self._collapsed
            caret = "▸" if collapsed else "▾"
            tag = f"  ·  ★{n_cls}" if n_cls else ""
            specs.append(RowSpec(
                None, None, col, f"{caret} {rarity.upper()}", col,
                value=f"Σ {fmt_pct(total)} · {len(items)}{tag}", bold=True,
                cb=(lambda r=rarity: self._toggle(r))))
            if collapsed:
                continue
            ordered = sorted(items, key=lambda t: (not rel(t[0]), -t[1]))
            if self.s.class_only and active:
                ordered = [t for t in ordered if rel(t[0])]
            for item, prob in ordered:
                if tokens.is_token(item):
                    specs.append(RowSpec("item", item, col,
                                         f"🎲 {names.item_name(item)}", theme.MUTED,
                                         sub="PROCEDURAL", value=fmt_pct(prob)))
                    first_item = False
                    continue
                star = rel(item)
                specs.append(RowSpec(
                    "item", item, col,
                    f"{'★ ' if star else ''}{names.item_name(item)}", col,
                    sub=(rarity.upper() if first_item else ""),
                    value=fmt_pct(prob), bold=star, highlight=first_item))
                first_item = False
        self.box.fill(specs, round(self.s.icon_size * self._scale))


# ---------------------------------------------------------------------------
#  Entity HUD — nearby enemies (selectable) + chests
# ---------------------------------------------------------------------------
class EntityOverlay(OverlayWindow):
    request_config = QtCore.Signal()

    def __init__(self, model, settings, parent=None):
        super().__init__("FAREVER · ENTITY", settings, geo_key="entity", parent=parent)
        self.model = model
        self.s = settings
        self._enemies: list = []          # [(entity, dist)]
        self._chests: list = []           # [ChestRow]
        self._sel = None                  # ("enemy", addr) | ("chest", chest_id)
        self._drop_win: DropTableOverlay | None = None
        self.setFocusPolicy(QtCore.Qt.StrongFocus)

        coords = QtWidgets.QHBoxLayout()
        self.pos = QtWidgets.QLabel("waiting for player…")
        self.pos.setObjectName("MonoText")
        tag = QtWidgets.QLabel("LIVE")
        tag.setObjectName("Mono")
        tag.setStyleSheet(
            f"color:{theme.GOOD};background:{theme.with_alpha(theme.GOOD,30)};"
            f"padding:1px 6px;font-weight:700;")
        coords.addWidget(self.pos)
        coords.addStretch(1)
        coords.addWidget(tag)
        self.content.addLayout(coords)

        scroll, self._body = _scroll_body()
        self.content.addWidget(scroll, 1)
        self.enemy_box = _Section("ENEMIES", theme.DANGER)
        self.chest_box = _Section("CHESTS", theme.CHEST)
        for b in (self.enemy_box, self.chest_box):
            self._body.addWidget(b)
        self._body.addStretch(1)

        gear = QtWidgets.QPushButton(); gear.setObjectName("Icon")
        gear.setIcon(icons.ui_qicon("settings", theme.MUTED, 16))
        gear.setToolTip("Open the config panel")
        gear.clicked.connect(self.request_config.emit)
        layb = QtWidgets.QPushButton(); layb.setObjectName("Icon")
        layb.setIcon(icons.ui_qicon("layout", theme.MUTED, 16))
        layb.setToolTip("Reset overlay to top-left")
        layb.clicked.connect(self._reset_pos)
        for b in (gear, layb):
            self.titlebar.extra.insertWidget(self.titlebar.extra.count() - 1, b)

        self._base_w, self._base_h = 320, 440
        self.setMinimumWidth(round(280 * self.s.entity_scale))
        self.apply_scale(self.s.entity_scale)     # scaled QSS + resize to base*scale
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(POLL_MS)

    def _reset_pos(self):
        self.move(60, 60)
        self.persist_geometry()

    # --- lock + opacity forward to the drop child -----------------------
    def set_locked(self, on: bool) -> None:
        super().set_locked(on)
        if self._drop_win is not None:
            self._drop_win.set_locked(on)

    def set_opacity(self, value: float) -> None:
        super().set_opacity(value)
        if self._drop_win is not None:
            self._drop_win.set_opacity(value)

    # --- selection -------------------------------------------------------
    def keyPressEvent(self, e):
        if e.key() == QtCore.Qt.Key_Up:
            self._move_selection(-1)
        elif e.key() == QtCore.Qt.Key_Down:
            self._move_selection(1)
        else:
            super().keyPressEvent(e)

    def _targets(self):
        """Ordered selectable keys: enemies first, then chests."""
        t = [("enemy", getattr(e, "addr", None)) for e, _ in self._enemies
             if getattr(e, "addr", None) is not None]
        t += [("chest", c.chest_id) for c in self._chests]
        return t

    def _selected_source(self):
        """(near, name) for the current selection, or (None, '')."""
        if self._sel is None:
            return None, ""
        kind, key = self._sel
        if kind == "enemy":
            for e, d in self._enemies:
                if getattr(e, "addr", None) == key:
                    return (self.model.enemy_drop_source(e, d, self.s.level),
                            names.unit_name(e.unit_id) or "?")
        else:
            for c in self._chests:
                if c.chest_id == key:
                    name = (names.loot_table_label(c.loot_table) if c.loot_table
                            else names.humanize(c.chest_id))
                    return self.model.chest_drop_source(c, self.s.level), name
        return None, ""

    def _move_selection(self, delta):
        targets = self._targets()
        if not targets:
            return
        try:
            i = targets.index(self._sel)
        except ValueError:
            i = 0
        i = max(0, min(len(targets) - 1, i + delta))
        self._sel = targets[i]
        self._open_drops()
        self._refresh()

    def _select(self, kind, key):
        self._sel = (kind, key)
        self._open_drops()
        self._refresh()

    # public actions for global hotkeys (selection works while the game is focused)
    def select_prev(self):
        self._move_selection(-1)

    def select_next(self):
        self._move_selection(1)

    def close_drops(self):
        if self._drop_win is not None:
            self._drop_win.close()
            self._drop_win = None

    def _retint(self, accent: str) -> None:
        # selected-row / icon-tile accents are read live each refresh; just keep
        # the open drop-table sub-window in sync.
        if self._drop_win is not None:
            self._drop_win.apply_accent(accent)

    def set_scale(self, scale):
        self.apply_scale(scale)
        if self._drop_win is not None:
            self._drop_win.apply_scale(scale)

    def _open_drops(self):
        if self._sel is None:
            return
        if self._drop_win is None or not self._drop_win.isVisible():
            self._drop_win = DropTableOverlay(self.model, self.s)
            self._drop_win.set_locked(self._locked)
            self._drop_win.set_opacity(self.s.opacity)
            self._drop_win.apply_scale(self._scale)
            self._drop_win.move(self.x() + self.width() + 8, self.y())
            self._drop_win.show()
        self._drop_win.set_target(*self._selected_source())

    def _update_drops(self):
        if self._drop_win is not None and self._drop_win.isVisible() and self._sel is not None:
            self._drop_win.set_target(*self._selected_source())

    # --- helpers ---------------------------------------------------------
    def _tick(self):
        try:
            self._refresh()
        except Exception as e:
            self.pos.setText(f"read error: {e}")

    def _refresh(self):
        xyz = self.model.player_xyz()
        if xyz is None:
            self.pos.setText("waiting for player…")
            return
        self.pos.setText(f"X {xyz[0]:.0f}   Y {xyz[1]:.0f}   Z {xyz[2]:.0f}")
        isz = round(self.s.icon_size * self._scale)

        # 1) gather both lists
        self._enemies = self.model.nearest_enemies(
            xyz, self.s.enemy_count, self.s.max_dist, self.s.enemies_only) \
            if self.s.show_enemies else []
        self._chests = self.model.nearest_chests_merged(
            xyz, self.s.chest_count, self.s.max_dist) if self.s.show_chests else []

        # 2) keep selection valid (auto-flip to first available target)
        targets = self._targets()
        if self._sel not in targets:
            self._sel = targets[0] if targets else None

        # 3) render enemies
        if self.s.show_enemies:
            self.enemy_box.show()
            specs = []
            for e, d in self._enemies:
                key = ("enemy", getattr(e, "addr", None))
                sel = key == self._sel
                col = self.s.hud_accent if sel else theme.KIND_COLOR.get(e.kind, theme.TEXT)
                specs.append(RowSpec(
                    "unit", e.unit_id, col, names.unit_name(e.unit_id) or "?", col,
                    value=f"{d:.0f}m", bold=sel, highlight=sel,
                    cb=(lambda k=key: self._select(*k))))
            self.enemy_box.fill(specs, isz)
            self.enemy_box.header.set_tag(f"{len(self._enemies)} · ↑↓ SELECT")
        else:
            self.enemy_box.hide()

        # 4) render chests (also selectable -> show their loot table)
        if self.s.show_chests:
            self.chest_box.show()
            specs = []
            for c in self._chests:
                drop_id = None
                if c.loot_table:
                    try:
                        from ..data import loot
                        pr = loot.predict_sorted(c.loot_table, c.level or self.s.level)
                        drop_id = pr[0][0] if pr else None
                    except Exception:
                        pass
                sel = ("chest", c.chest_id) == self._sel
                anomaly = bool(c.live and c.anomaly)
                color = self.s.hud_accent if (sel or anomaly) else theme.CHEST
                label = names.loot_table_label(c.loot_table) if c.loot_table \
                    else names.humanize(c.chest_id)
                sub_bits = []
                if c.state:
                    sub_bits.append(c.state.upper())
                if anomaly:
                    sub_bits.append("★ ANOMALY")
                specs.append(RowSpec(
                    "item", drop_id, theme.CHEST, label, color,
                    sub="  ·  ".join(sub_bits), value=f"{c.dist:.0f}m",
                    bold=sel, highlight=sel,
                    cb=(lambda cid=c.chest_id: self._select("chest", cid))))
            self.chest_box.fill(specs, isz)
            self.chest_box.header.set_tag(f"{len(self._chests)} · CLICK FOR LOOT")
        else:
            self.chest_box.hide()

        # 5) keep the open drop window synced with live selection / auto-flip
        self._update_drops()

    def closeEvent(self, e):
        self._timer.stop()
        if self._drop_win is not None:
            self._drop_win.close()
            self._drop_win = None
        super().closeEvent(e)
