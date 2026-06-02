from __future__ import annotations

from PySide6 import QtWidgets
from .. import components as C
from ...data import names
from ...data import icons

class CombatPageMixin:
    def _page_combat(self):
        page, v = self._page_container()
        card = C.OverlayCard("swords", "Open DPS Meter",
                             "Big live DPS, sparkline, survivability, recent cycles.")
        card.toggled.connect(lambda on: self._request_overlay("dps", on))
        self._register_card("dps", card)
        v.addWidget(card)

        from ...config import experimental_enabled
        if experimental_enabled():
            skl = C.OverlayCard("layers", "Open Skill Breakdown",
                                "Per-skill table — icons, real names, crit%, columns. "
                                "(experimental — samples live numbers, can undercount)")
            skl.toggled.connect(lambda on: self._request_overlay("skills", on))
            self._register_card("skills", skl)
            v.addWidget(skl)

        size_seg = C.SegmentedControl(
            ["Small", "Medium", "Full"],
            {"small": "Small", "medium": "Medium"}.get(self.s.dps_mode, "Full"))
        size_seg.currentChanged.connect(self._set_dps_mode)
        v.addWidget(C.Field("Meter size", size_seg))
        size_note = QtWidgets.QLabel(
            "Small = number · Medium = + graph · Full = + survivability.")
        size_note.setObjectName("Muted")
        size_note.setWordWrap(True)
        v.addWidget(size_note)

        v.addWidget(C.SectionHeader("Tracking"))
        row = QtWidgets.QGridLayout()
        row.setHorizontalSpacing(12)
        rng = C.Stepper(self.s.dps_radius, 0, 999, 5)
        rng.valueChanged.connect(lambda v_: self._set("dps_radius", v_))
        win = C.Stepper(int(self.s.dps_window), 1, 30, 1, suffix="s")
        win.valueChanged.connect(lambda v_: self._set("dps_window", float(v_)))
        row.addWidget(C.Field("Tracking range", rng), 0, 0)
        row.addWidget(C.Field("Rolling window", win), 0, 1)
        v.addLayout(row)
        dsc = C.SliderRow("Overlay scale", 70, 160, int(self.s.dps_scale * 100),
                          lambda x: f"{x / 100:.2f}x")
        dsc.valueChanged.connect(self._set_dps_scale)
        v.addWidget(dsc)

        # release builds hide the experimental Skill Breakdown panel
        self._dps_col_toggles = {}
        if experimental_enabled():
            from ..skill_table import COLUMN_ORDER
            v.addWidget(C.SectionHeader("Skill table columns"))
            col_grid = QtWidgets.QGridLayout()
            col_grid.setHorizontalSpacing(8)
            col_grid.setVerticalSpacing(6)
            chosen = set(self.s.dps_columns or [])
            labels = {"total": "Total", "pct": "Percent", "dps": "DPS", "hits": "Hits",
                      "crit": "Crit %", "max": "Max hit", "avg": "Avg hit", "min": "Min hit"}
            for i, key in enumerate(COLUMN_ORDER):
                t = C.LabeledToggle(labels.get(key, key.title()), key in chosen)
                t.toggled.connect(lambda _on, k=key: self._set_dps_columns())
                self._dps_col_toggles[key] = t
                col_grid.addWidget(t, i // 2, i % 2)
            v.addLayout(col_grid)

        surv = C.LabeledToggle("Survivability strip (incoming DTPS · HP · death recap)",
                               self.s.dps_survival)
        surv.toggled.connect(lambda on: self._set("dps_survival", on))
        v.addWidget(surv)

        if experimental_enabled():
            note_txt = (
                "Self DPS only. Bosses always tracked. Per-skill capture is "
                "experimental and can undercount; the total and by-enemy stay exact.")
        else:
            note_txt = (
                "Self DPS only, with a per-enemy breakdown. Bosses always tracked.")
        note = QtWidgets.QLabel(note_txt)
        note.setObjectName("Muted")
        note.setWordWrap(True)
        v.addWidget(note)
        v.addStretch(1)
        return page
