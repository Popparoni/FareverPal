"""Shared per-skill table widgets — used by the Skills panel (and the DPS meter's
fallback). One row = icon + responsive name + compact bar + value + toggleable
mono columns. Kept here so the DPS-meter and Skills overlays don't duplicate it.
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from . import theme
from .widgets import Bar, fmt_pct
from .components import IconTile
from ..data import icons

# Per-skill table columns. `name` + the fill bar are always shown; these are the
# optional value columns the user toggles (persisted in Settings.dps_columns).
TABLE_COLUMNS: dict[str, tuple[str, int, str]] = {
    "total": ("TOTAL", 56, theme.TEXT),
    "pct":   ("%",     42, theme.MUTED),
    "dps":   ("DPS",   54, theme.ACCENT),
    "hits":  ("HITS",  40, theme.MUTED),
    "crit":  ("CRIT",  44, theme.GOLD),
    "max":   ("MAX",   52, theme.MUTED),
    "avg":   ("AVG",   52, theme.MUTED),
    "min":   ("MIN",   52, theme.DIM),
}
COLUMN_ORDER = list(TABLE_COLUMNS)
DEFAULT_COLUMNS = ["pct", "dps", "hits", "crit", "max"]
KIND_LABELS = [("damage", "DMG"), ("heal", "HEAL"), ("shield", "SHIELD")]

BAR_W = 52          # compact damage-scale bar so the name gets the room
VAL_W = 58          # inline value column (HP-diff total)


def abbr(v: float) -> str:
    v = float(v)
    return f"{v / 1000:.1f}K" if v >= 1000 else f"{v:.0f}"


def col_value(key: str, st, kind_total: float, dur: float) -> str:
    if key == "total":
        return abbr(st.total)
    if key == "pct":
        return fmt_pct(st.total / kind_total) if kind_total else "0%"
    if key == "dps":
        return abbr(st.total / dur) if dur > 0 else "—"
    if key == "hits":
        return str(st.hits)
    if key == "crit":
        return f"{100 * st.crit_pct:.0f}%" if st.hits else ""
    if key == "max":
        return abbr(st.max_hit)
    if key == "avg":
        return abbr(st.avg_hit)
    if key == "min":
        return abbr(st.min_hit)
    return ""


class KindTabs(QtWidgets.QWidget):
    """DMG / HEAL / SHIELD selector; a kind's tab stays hidden until data of
    that kind has been seen, so we never show an empty fabricated tab."""
    changed = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._btns: dict[str, QtWidgets.QPushButton] = {}
        self._current = "damage"
        for kind, label in KIND_LABELS:
            b = QtWidgets.QPushButton(label)
            b.setCheckable(True)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=kind: self._pick(k))
            lay.addWidget(b)
            self._btns[kind] = b
        lay.addStretch(1)
        self._btns["damage"].setChecked(True)
        self.restyle(theme.ACCENT)

    def _pick(self, kind: str) -> None:
        self._current = kind
        for k, b in self._btns.items():
            b.setChecked(k == kind)
        self.changed.emit(kind)

    def current(self) -> str:
        return self._current

    def set_available(self, kinds: set[str]) -> None:
        for kind, b in self._btns.items():
            b.setVisible(kind == "damage" or kind in kinds)
        if not self._btns[self._current].isVisible():
            self._pick("damage")

    def restyle(self, accent: str) -> None:
        qss = (f"QPushButton{{background:transparent;border:0;padding:3px 9px;"
               f"color:{theme.MUTED};font-weight:600;font-size:11px;}}"
               f"QPushButton:hover{{color:{theme.TEXT};}}"
               f"QPushButton:checked{{background:{theme.with_alpha(accent, 40)};"
               f"color:{accent};}}")
        for b in self._btns.values():
            b.setStyleSheet(qss)


class TableHeader(QtWidgets.QWidget):
    """Column-header row matching `SkillRow`'s layout."""
    def __init__(self):
        super().__init__()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 1)
        lay.setSpacing(6)
        spacer = QtWidgets.QLabel()
        spacer.setFixedWidth(18)
        lay.addWidget(spacer)
        name = QtWidgets.QLabel("SKILL")
        name.setObjectName("Mono")
        name.setStyleSheet(f"color:{theme.DIM};background:transparent;")
        lay.addWidget(name, 1)
        bar_spacer = QtWidgets.QLabel()
        bar_spacer.setFixedWidth(BAR_W)
        lay.addWidget(bar_spacer)
        self._labels: dict[str, QtWidgets.QLabel] = {}
        for key in COLUMN_ORDER:
            header, w, _col = TABLE_COLUMNS[key]
            lbl = QtWidgets.QLabel(header)
            lbl.setObjectName("Mono")
            lbl.setFixedWidth(w)
            lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            lbl.setStyleSheet(f"color:{theme.DIM};background:transparent;")
            lay.addWidget(lbl)
            self._labels[key] = lbl

    def apply_columns(self, keys: list[str]) -> None:
        for key, lbl in self._labels.items():
            lbl.setVisible(key in keys)


class SkillRow(QtWidgets.QWidget):
    """icon + responsive name + compact fill bar + value + optional mono columns.

    The name stretches with the window and elides to its live width (so widening
    the panel reveals more of the name); the bar is a small fixed-width scale."""
    def __init__(self):
        super().__init__()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 1, 0, 1)
        lay.setSpacing(6)
        self.tile = IconTile(18)
        self.name = QtWidgets.QLabel()
        self.name.setStyleSheet(f"color:{theme.TEXT};background:transparent;")
        self.bar = Bar(theme.ACCENT)
        self.bar.setMinimumHeight(10)
        self.bar.setFixedWidth(BAR_W)
        self.value = QtWidgets.QLabel()
        self.value.setObjectName("Mono")
        self.value.setFixedWidth(VAL_W)
        self.value.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.value.setStyleSheet(f"color:{theme.TEXT};background:transparent;")
        lay.addWidget(self.tile)
        lay.addWidget(self.name, 1)
        lay.addWidget(self.bar)
        lay.addWidget(self.value)
        self._full_name = ""
        self._cols: dict[str, QtWidgets.QLabel] = {}
        for key in COLUMN_ORDER:
            _header, w, col = TABLE_COLUMNS[key]
            lbl = QtWidgets.QLabel()
            lbl.setObjectName("Mono")
            lbl.setFixedWidth(w)
            lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            lbl.setStyleSheet(f"color:{col};background:transparent;")
            lay.addWidget(lbl)
            self._cols[key] = lbl

    def apply_columns(self, keys: list[str]) -> None:
        for key, lbl in self._cols.items():
            lbl.setVisible(key in keys)

    def _elide(self) -> None:
        fm = self.name.fontMetrics()
        w = self.name.width()
        w = w if w > 60 else 140
        self.name.setText(fm.elidedText(self._full_name, QtCore.Qt.ElideRight, w))
        self.name.setToolTip(self._full_name
                             if fm.horizontalAdvance(self._full_name) > w else "")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._full_name:
            self._elide()

    def set_row(self, name: str, frac: float, vals: dict[str, str],
                bar_label: str = "", sheet: str | None = None,
                id_: str | None = None, accent: str = theme.ACCENT) -> None:
        self._full_name = name
        self._elide()
        if sheet and id_ and icons.has_icon(sheet, id_):
            self.tile.set(sheet, id_, accent)
            self.tile.show()
        else:
            self.tile.hide()
        self.bar.set(frac)
        self.value.setText(bar_label)
        self.value.setVisible(bool(bar_label))
        for key, lbl in self._cols.items():
            lbl.setText(vals.get(key, ""))
