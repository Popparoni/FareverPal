from __future__ import annotations

from PySide6 import QtWidgets
from .. import components as C
from ..crosshair import CrosshairCanvas

class CrosshairPageMixin:
    def _page_crosshair(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QHBoxLayout(page)
        outer.setContentsMargins(22, 20, 22, 20)
        outer.setSpacing(18)

        left = QtWidgets.QVBoxLayout()
        left.setSpacing(16)
        enable = C.LabeledToggle("Enable crosshair overlay",
                                 self.overlays.get("crosshair") is not None)
        enable.toggled.connect(lambda on: self._request_overlay("crosshair", on))
        self._register_card("crosshair", enable)
        left.addWidget(enable)

        left.addWidget(C.SectionHeader("Geometric Parameters"))
        self.ch_style = C.SegmentedControl(
            ["Cross + Dot", "Circle", "Cross", "Dot", "T-Shape"], self.s.crosshair_style)
        self.ch_style.currentChanged.connect(lambda t: self._set_crosshair("crosshair_style", t))
        left.addWidget(C.Field("Style", self.ch_style))

        g = QtWidgets.QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(10)
        ch_steppers = [
            ("crosshair_size", "Size", 0, 40),
            ("crosshair_gap", "Center gap", 0, 30),
            ("crosshair_thickness", "Thickness", 1, 10),
            ("crosshair_dot", "Dot size", 0, 12),
        ]
        for i, (attr, label, lo, hi) in enumerate(ch_steppers):
            st = C.Stepper(getattr(self.s, attr), lo, hi)
            st.valueChanged.connect(lambda v_, a=attr: self._set_crosshair(a, v_))
            g.addWidget(C.Field(label, st), i // 2, i % 2)
        left.addLayout(g)

        left.addWidget(C.SectionHeader("Rendering Options"))
        outline = C.LabeledToggle("Outline", self.s.crosshair_outline)
        outline.toggled.connect(lambda on: self._set_crosshair("crosshair_outline", on))
        left.addWidget(outline)
        opac = C.SliderRow("Opacity", 20, 100, int(self.s.crosshair_opacity * 100),
                           lambda x: f"{x}%")
        opac.valueChanged.connect(lambda x: self._set_crosshair("crosshair_opacity", x / 100.0))
        left.addWidget(opac)
        swatch = C.ColorSwatch(self.s.crosshair_color)
        swatch.changed.connect(lambda c: self._set_crosshair("crosshair_color", c))
        left.addWidget(C.Field("Primary color", swatch))
        left.addStretch(1)

        right = QtWidgets.QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(C.SectionHeader("Live Preview"))
        self.ch_preview = CrosshairCanvas(self.s)
        prev_frame = QtWidgets.QFrame()
        prev_frame.setObjectName("Cell")
        pf = QtWidgets.QVBoxLayout(prev_frame)
        pf.setContentsMargins(8, 8, 8, 8)
        pf.addWidget(self.ch_preview, 1)
        right.addWidget(prev_frame, 1)
        self.ch_coord = QtWidgets.QLabel("CENTER 0,0 · NO GAME ATTACH NEEDED")
        self.ch_coord.setObjectName("Mono")
        right.addWidget(self.ch_coord)

        outer.addLayout(left, 3)
        outer.addLayout(right, 2)
        return page

    def _set_crosshair(self, attr, value):
        setattr(self.s, attr, value)
        self.s.save()
        if hasattr(self, "ch_preview"):
            self.ch_preview.update()
        ov = self.overlays.get("crosshair")
        if ov is not None:
            ov.apply_settings()
