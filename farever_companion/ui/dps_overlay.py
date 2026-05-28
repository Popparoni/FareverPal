"""DPS meter overlay (Tactical Overlay HUD).

Big live DPS, a sparkline, and a per-skill breakdown when the DamageDisplay
event source is active (crit% shown in gold), else per-target bars (HP-diff).
Bosses are always counted regardless of range (model.sample_combat handles
that). Self DPS only — by design (see PLAN §10). Read-only.
"""
from __future__ import annotations

import time
from collections import deque

from PySide6 import QtCore, QtWidgets

from . import theme
from .overlay_base import OverlayWindow
from .widgets import Sparkline, Bar
from .components import SectionHeader
from ..data import names, icons

POLL_MS = 400
HIST = 80
N_BARS = 6
N_CYCLES = 6
IDLE_GAP = 3.0          # encounter ends after this long with no new damage


def _abbr(v: float) -> str:
    v = float(v)
    return f"{v / 1000:.1f}K" if v >= 1000 else f"{v:.0f}"


class _SkillRow(QtWidgets.QWidget):
    """name + subtle fill bar + value + crit% (gold)."""
    def __init__(self):
        super().__init__()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 1, 0, 1)
        lay.setSpacing(8)
        self.name = QtWidgets.QLabel()
        self.name.setMinimumWidth(92)
        self.name.setStyleSheet(f"color:{theme.TEXT};background:transparent;")
        self.bar = Bar(theme.ACCENT)
        self.bar.setMinimumHeight(10)
        self.val = QtWidgets.QLabel()
        self.val.setObjectName("Mono")
        self.val.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.val.setMinimumWidth(54)
        self.val.setStyleSheet(f"color:{theme.TEXT};background:transparent;")
        self.crit = QtWidgets.QLabel()
        self.crit.setObjectName("Mono")
        self.crit.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.crit.setMinimumWidth(40)
        self.crit.setStyleSheet(f"color:{theme.GOLD};background:transparent;")
        lay.addWidget(self.name)
        lay.addWidget(self.bar, 1)
        lay.addWidget(self.val)
        lay.addWidget(self.crit)

    def set(self, name, frac, value, crit=""):
        self.name.setText(name)
        self.bar.set(frac)
        self.val.setText(value)
        self.crit.setText(crit)


class _HistRow(QtWidgets.QWidget):
    """One past combat cycle: dps / peak / time / kills, mono columns."""
    _COLS = (("dps", 64, theme.ACCENT), ("peak", 58, theme.MUTED),
             ("time", 44, theme.MUTED), ("k", 26, theme.GOLD))

    def __init__(self, header: bool = False):
        super().__init__()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(4)
        self._labels = {}
        for key, w, col in self._COLS:
            lbl = QtWidgets.QLabel(key.upper() if header else "")
            lbl.setObjectName("Mono")
            lbl.setFixedWidth(w)
            lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            lbl.setStyleSheet(f"color:{theme.DIM if header else col};background:transparent;")
            self._labels[key] = lbl
            lay.addWidget(lbl)
        lay.addStretch(1)

    def set(self, dps, peak, tm, k):
        self._labels["dps"].setText(dps)
        self._labels["peak"].setText(peak)
        self._labels["time"].setText(tm)
        self._labels["k"].setText(k)


class DpsOverlay(OverlayWindow):
    def __init__(self, model, settings, parent=None):
        super().__init__("DPS", settings, geo_key="dps", parent=parent)
        self.model = model
        self.s = settings
        self.radius = settings.dps_radius
        self._hist: deque[float] = deque([0.0] * HIST, maxlen=HIST)

        # range control + reset in the title bar
        self._range_lbl = QtWidgets.QLabel()
        self._range_lbl.setObjectName("Mono")
        minus = QtWidgets.QPushButton("−"); minus.setObjectName("Icon")
        plus = QtWidgets.QPushButton("+"); plus.setObjectName("Icon")
        rst = QtWidgets.QPushButton(); rst.setObjectName("Icon")
        rst.setIcon(icons.ui_qicon("refresh-cw", theme.MUTED, 14))
        rst.setToolTip("Reset session")
        minus.clicked.connect(lambda: self._bump(-5))
        plus.clicked.connect(lambda: self._bump(5))
        rst.clicked.connect(self.model.dps.reset)
        for w in (rst, minus, self._range_lbl, plus):
            self.titlebar.extra.insertWidget(self.titlebar.extra.count() - 1, w)
        self._update_range()

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self.big = QtWidgets.QLabel("0"); self.big.setObjectName("Big")
        self._unit = QtWidgets.QLabel("DPS"); self._unit.setObjectName("Section")
        row.addWidget(self.big)
        row.addWidget(self._unit, 0, QtCore.Qt.AlignBottom)
        row.addStretch(1)
        self.content.addLayout(row)
        self.sub = QtWidgets.QLabel("idle"); self.sub.setObjectName("Mono")
        self.content.addWidget(self.sub)
        self.chart = Sparkline(theme.ACCENT)
        self.content.addWidget(self.chart)

        self._bars_header = SectionHeader("BY SKILL ANALYSIS")
        self.content.addWidget(self._bars_header)
        self._rows = []
        for _ in range(N_BARS):
            r = _SkillRow()
            self.content.addWidget(r)
            self._rows.append(r)

        # recent-cycle history (encounters between idle gaps)
        self._cycles: deque[dict] = deque(maxlen=N_CYCLES)
        self._prev_total = 0.0
        self._cyc: dict | None = None
        self._last_dmg_t = 0.0
        self._hist_header = SectionHeader("RECENT CYCLES")
        self.content.addWidget(self._hist_header)
        self._hist_head_row = _HistRow(header=True)
        self.content.addWidget(self._hist_head_row)
        self._hist_rows = [_HistRow() for _ in range(N_CYCLES)]
        for r in self._hist_rows:
            self.content.addWidget(r)

        self.content.addStretch(1)   # absorb slack here, not in the title bar

        self.setMinimumWidth(round(240 * self.s.dps_scale))
        self.apply_dps_mode(self.s.dps_mode)      # sets base size + section visibility
        self._retint(self._accent)                # colour bars/chart/headers to accent
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(POLL_MS)

    # --- size modes: small (number only) / medium (+graph +entities) / default --
    _MODE_SIZE = {"small": (260, 132), "medium": (300, 340), "default": (300, 520)}

    def apply_dps_mode(self, mode: str) -> None:
        mode = mode if mode in self._MODE_SIZE else "default"
        self._mode = mode
        med = mode in ("medium", "default")    # graph + per-entity bars
        full = mode == "default"               # + recent-cycle history
        self.sub.setVisible(med)
        self.chart.setVisible(med)
        self._bars_header.setVisible(med)
        for r in self._rows:
            r.setVisible(med)
        for w in (self._hist_header, self._hist_head_row, *self._hist_rows):
            w.setVisible(full)
        self._base_w, self._base_h = self._MODE_SIZE[mode]
        self.apply_scale(self._scale)          # resize to base*scale

    def _retint(self, accent: str) -> None:
        if not accent:
            return
        self.chart.set_color(accent)
        for r in self._rows:
            r.bar.set_color(accent)
        self._bars_header.set_color(accent)
        self._hist_header.set_color(accent)

    def _bump(self, d):
        self.radius = max(0, self.radius + d)
        self._update_range()

    def _update_range(self):
        self._range_lbl.setText("∞" if self.radius == 0 else str(self.radius))

    def _tick(self):
        try:
            m = self.model.sample_combat(radius=float(self.radius))
        except Exception as e:
            self.sub.setText(f"err: {e}")
            return
        dps = m.current_dps()
        self._hist.append(dps)
        self.chart.set_color(self.s.hud_accent)
        self.big.setText(f"{dps:,.0f}")
        if m.total > 0:
            extra = f"   K {m.kills}" if m.kills else ""
            self.sub.setText(
                f"PEAK {m.peak:,.0f}   TOTAL {m.total / 1000:,.1f}K / {m.duration:.0f}S{extra}")
        else:
            self.sub.setText("idle — no damage in range")
        self.chart.set_data(list(self._hist))
        if self._mode in ("medium", "default"):
            self._draw_bars(m)
        self._update_cycles(m, time.monotonic(), dps)   # keep tracking even if hidden
        if self._mode == "default":
            self._render_history()

    def _update_cycles(self, m, now, dps):
        """Track encounters non-destructively off the running total: a cycle
        starts on first damage and ends after IDLE_GAP with no new damage."""
        total = m.total
        if total < self._prev_total - 1e-6:          # session was reset
            self._cyc = None
            self._prev_total = total
        elif total > self._prev_total + 1e-6:         # damage this tick
            self._last_dmg_t = now
            if self._cyc is None:
                self._cyc = {"t0": now, "tot0": self._prev_total,
                             "k0": m.kills, "peak": 0.0}
            self._prev_total = total
        if self._cyc is not None:
            self._cyc["peak"] = max(self._cyc["peak"], dps)
            if now - self._last_dmg_t > IDLE_GAP:     # encounter ended
                dur = max(0.1, self._last_dmg_t - self._cyc["t0"])
                tot = m.total - self._cyc["tot0"]
                if tot > 0:
                    self._cycles.appendleft({
                        "dps": tot / dur, "peak": self._cyc["peak"],
                        "tot": tot, "dur": dur, "k": m.kills - self._cyc["k0"]})
                self._cyc = None

    def _render_history(self):
        for i, r in enumerate(self._hist_rows):
            if i < len(self._cycles):
                c = self._cycles[i]
                r.set(_abbr(c["dps"]), _abbr(c["peak"]), f"{c['dur']:.0f}s",
                      str(c["k"]) if c["k"] else "·")
                r.show()
            else:
                r.hide()
        self._hist_header.set_tag("" if self._cycles else "NONE YET")

    def _draw_bars(self, m):
        if m.has_events:
            self._bars_header.set_text("BY SKILL ANALYSIS")
            top = m.top_skills(N_BARS)
            mx = top[0][1].total if top else 1.0
            for i, r in enumerate(self._rows):
                if i < len(top):
                    skill, st = top[i]
                    crit = f"{100 * st.crits / st.hits:.0f}%" if st.hits else ""
                    r.set(names.any_name(skill) or skill,
                          st.total / mx if mx else 0, f"{st.total:,.0f}", crit)
                    r.show()
                else:
                    r.hide()
        else:
            self._bars_header.set_text("BY TARGET (HP-DIFF)")
            top = m.top_targets(N_BARS)
            mx = top[0][1] if top else 1.0
            for i, r in enumerate(self._rows):
                if i < len(top):
                    uid, dmg = top[i]
                    r.set(names.any_name(uid) or uid,
                          dmg / mx if mx else 0, f"{dmg:,.0f}", "")
                    r.show()
                else:
                    r.hide()

    def closeEvent(self, e):
        self._timer.stop()
        super().closeEvent(e)
