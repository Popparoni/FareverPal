"""Center-screen crosshair overlay (read-only, click-through).

A frameless, translucent, always-on-top window pinned to the centre of the
primary screen that draws a configurable crosshair. Purely cosmetic — it never
reads the game or takes input (always click-through). The same `paint` routine
backs the live preview in the control panel.
"""
from __future__ import annotations

import sys

from PySide6 import QtCore, QtGui, QtWidgets


def paint(painter: QtGui.QPainter, cx: float, cy: float, s) -> None:
    """Draw the crosshair centred at (cx, cy) using Settings `s`."""
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    color = QtGui.QColor(s.crosshair_color)
    size = max(0, s.crosshair_size)
    gap = max(0, s.crosshair_gap)
    th = max(1, s.crosshair_thickness)
    style = s.crosshair_style
    dot = max(0, s.crosshair_dot)

    # T-Shape draws only the horizontal arms + the bottom (stem), omitting the
    # top arm — a "⊤" so the target above the centre stays unobscured.
    arms = "rld" if style == "T-Shape" else "rldu"

    def draw_lines(pen_color, width):
        pen = QtGui.QPen(pen_color, width)
        pen.setCapStyle(QtCore.Qt.FlatCap)
        painter.setPen(pen)
        if "r" in arms:
            painter.drawLine(QtCore.QPointF(cx + gap, cy), QtCore.QPointF(cx + gap + size, cy))
        if "l" in arms:
            painter.drawLine(QtCore.QPointF(cx - gap, cy), QtCore.QPointF(cx - gap - size, cy))
        if "d" in arms:
            painter.drawLine(QtCore.QPointF(cx, cy + gap), QtCore.QPointF(cx, cy + gap + size))
        if "u" in arms:
            painter.drawLine(QtCore.QPointF(cx, cy - gap), QtCore.QPointF(cx, cy - gap - size))

    has_lines = style in ("Cross", "Cross + Dot", "T-Shape")
    if has_lines:
        if s.crosshair_outline:
            draw_lines(QtGui.QColor(0, 0, 0, 200), th + 2)
        draw_lines(color, th)

    if style == "Circle":
        r = gap + size
        if s.crosshair_outline:
            painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 200), th + 2))
            painter.drawEllipse(QtCore.QPointF(cx, cy), r, r)
        painter.setPen(QtGui.QPen(color, th))
        painter.drawEllipse(QtCore.QPointF(cx, cy), r, r)

    if dot > 0 and style in ("Dot", "Cross + Dot"):
        if s.crosshair_outline:
            painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 200), 1))
            painter.setBrush(QtGui.QColor(0, 0, 0, 200))
            painter.drawEllipse(QtCore.QPointF(cx, cy), dot + 1, dot + 1)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QtCore.QPointF(cx, cy), dot, dot)


def _extent(s) -> int:
    """Half-size the window needs to fit the crosshair + outline."""
    return s.crosshair_gap + max(s.crosshair_size, s.crosshair_dot) + s.crosshair_thickness + 6


class CrosshairCanvas(QtWidgets.QWidget):
    """A fixed-size preview box that paints the crosshair centred."""
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setMinimumSize(180, 180)

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#0b0c10"))
        paint(p, self.width() / 2, self.height() / 2, self.s)
        p.end()


class CrosshairOverlay(QtWidgets.QWidget):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.Tool
            | QtCore.Qt.WindowTransparentForInput
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setWindowOpacity(max(0.2, min(1.0, settings.crosshair_opacity)))
        self.recenter()

    def recenter(self) -> None:
        ext = _extent(self.s)
        side = ext * 2
        screen = QtWidgets.QApplication.primaryScreen().geometry()
        self.setFixedSize(side, side)
        self.move(screen.center().x() - ext, screen.center().y() - ext)

    def apply_settings(self) -> None:
        self.setWindowOpacity(max(0.2, min(1.0, self.s.crosshair_opacity)))
        self.recenter()
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        # belt-and-suspenders click-through on Windows
        if sys.platform.startswith("win"):
            import ctypes
            GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x80000, 0x20
            hwnd = int(self.winId())
            u = ctypes.windll.user32
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT)

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        paint(p, self.width() / 2, self.height() / 2, self.s)
        p.end()
