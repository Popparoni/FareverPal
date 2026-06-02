from __future__ import annotations

from PySide6 import QtGui, QtWidgets
from .. import components as C
from ...data import loot

class EntityPageMixin:
    def _page_entity(self):
        page, v = self._page_container()
        cols = QtWidgets.QHBoxLayout()
        cols.setSpacing(24)

        left = QtWidgets.QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(C.SectionHeader("Display Configuration"))
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        toggles = [
            ("show_enemies", "Enemies"), ("show_chests", "Chests"),
            ("show_gatherables", "Gatherables"), ("show_drops", "Closest drops"),
            ("enemies_only", "Enemies only"), ("class_only", "Class-relevant only"),
        ]
        for i, (attr, label) in enumerate(toggles):
            t = C.LabeledToggle(label, getattr(self.s, attr))
            t.toggled.connect(lambda on, a=attr: self._set(a, on))
            grid.addWidget(t, i // 2, i % 2)
        left.addLayout(grid)
        left.addStretch(1)

        right = QtWidgets.QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(C.SectionHeader("Sizing & Metrics"))
        srow = QtWidgets.QGridLayout()
        srow.setHorizontalSpacing(12)
        srow.setVerticalSpacing(10)
        steppers = [
            ("enemy_count", "Enemies shown", 1, 30),
            ("chest_count", "Chests shown", 1, 30),
            ("icon_size", "Icon size (px)", 12, 64),
            ("level", "Predict level", 1, 60),
        ]
        for i, (attr, label, lo, hi) in enumerate(steppers):
            st = C.Stepper(getattr(self.s, attr), lo, hi)
            st.valueChanged.connect(lambda v_, a=attr: self._set(a, v_))
            srow.addWidget(C.Field(label, st), i // 2, i % 2)
        right.addLayout(srow)
        right.addStretch(1)

        cols.addLayout(left, 1)
        cols.addLayout(right, 1)
        v.addLayout(cols)

        esc = C.SliderRow("Overlay scale", 70, 160, int(self.s.entity_scale * 100),
                          lambda x: f"{x / 100:.2f}x")
        esc.valueChanged.connect(self._set_entity_scale)
        v.addWidget(esc)

        v.addWidget(C.SectionHeader("Loot Hotkeys (global · work while in-game)"))
        hk = QtWidgets.QGridLayout()
        hk.setHorizontalSpacing(12)
        for i, (attr, label) in enumerate([
                ("hotkey_loot_prev", "Prev target"),
                ("hotkey_loot_next", "Next target"),
                ("hotkey_loot_close", "Close loot")]):
            edit = QtWidgets.QKeySequenceEdit(QtGui.QKeySequence(getattr(self.s, attr)))
            try:
                edit.setMaximumSequenceLength(1)
            except (AttributeError, TypeError):
                pass
            edit.keySequenceChanged.connect(lambda seq, a=attr: self._set_hotkey(a, seq))
            hk.addWidget(C.Field(label, edit), 0, i)
        v.addLayout(hk)
        v.addStretch(1)
        return page
