"""Compass-needle overlay for tracked targets (read-only, click-through).

A frameless, translucent window pinned to the centre of the primary screen.
The needle pivots a bit *below* the player (under their feet) and is drawn in
a ground-plane perspective with an extruded side, so it reads as an arrow
lying flat on the ground. The foreshortening follows the live camera tilt
(BaseCamera.curPitch via the TrackController) and the rotation the camera yaw,
so the plane truly sticks to the ground at any orientation. Colour encodes the
third axis: green = target on your level, red = above you, blue = below you -
so the needle gives the 2D walk direction plus up/down at a glance. Driven by
the TrackController via `set_state()`; it never reads the game itself and
never takes input.
"""
from __future__ import annotations

import math
import sys

from PySide6 import QtCore, QtGui, QtWidgets

from . import theme

SIDE = 500          # legacy-mode window size (px; fits needle + 2 text lines)
PIVOT_DY = 96       # needle pivot sits this far below the player (screen px)
DEFAULT_SQUASH = 0.55   # ground foreshortening until the camera pitch is read
R_IN = 26           # needle starts here (local plane units), centre stays clear
R_TIP = 110         # needle tip radius (local plane units)
R_DIAL = 64         # faint ground-ring radius
ARRIVE = 4.0        # world units = "you are here"
LEVEL_DZ = 3.0      # |dz| above this counts as above/below

COL_LEVEL = theme.GOOD      # target on our level
COL_ABOVE = "#ef4444"       # target above the player
COL_BELOW = "#3b82f6"       # target below the player


class NeedleOverlay(QtWidgets.QWidget):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self._angle: float | None = None     # smoothed screen radians, cw from up
        self._dist = 0.0                     # 3D world distance (-1 = searching)
        self._dz = 0.0                       # target z - player z
        self._label = ""
        self._squash = DEFAULT_SQUASH        # smoothed ground tilt (camera pitch)
        self._scene = None                   # projected mode (see set_scene)
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.Tool
            | QtCore.Qt.WindowTransparentForInput
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setWindowOpacity(max(0.3, min(1.0, settings.opacity)))
        self.setMinimumSize(1, 1)        # tracker resizes to the game window
        self._legacy_geometry()

    def _legacy_geometry(self) -> None:
        """Centre-screen SIDE x SIDE box for the no-matrix fallback mode."""
        screen = QtWidgets.QApplication.primaryScreen().geometry()
        self.setGeometry(screen.center().x() - SIDE // 2,
                         screen.center().y() - SIDE // 2, SIDE, SIDE)

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

    # smoothing: low-pass the raw per-tick reads so tiny read/camera noise
    # doesn't visibly wobble the needle. Shortest-arc lerp for the angle (no
    # 359->0 spin); a target/label change snaps instead of sweeping. Only the
    # legacy (no-matrix) mode needs this - the projected mode inherits the
    # game camera's own smoothing.
    ALPHA_ANGLE = 0.35
    ALPHA_SQUASH = 0.20

    def set_state(self, angle: float | None, dist: float, dz: float, label: str,
                  squash: float | None = None) -> None:
        if self._scene is not None:
            self._scene = None
            self._legacy_geometry()      # back from game-window-sized mode
        if angle is not None and self._angle is not None and label == self._label:
            d = (angle - self._angle + math.pi) % (2 * math.pi) - math.pi
            angle = self._angle + (d if abs(d) > 1.0 else d * self.ALPHA_ANGLE)
        self._angle, self._dist, self._dz, self._label = angle, dist, dz, label
        if squash is not None:
            self._squash += (squash - self._squash) * self.ALPHA_SQUASH
        self.update()

    def set_scene(self, head, tail, ring, text_xy, dist: float, dz: float,
                  label: str) -> None:
        """Projected mode: `head`/`tail` are needle polygons and `ring` the
        feet circle, all as window-coordinate point lists already projected
        through the live camera matrix (head/tail may be None when on the
        target or searching). Drawn exactly where the game's ground is."""
        self._scene = (head, tail, ring, text_xy)
        self._dist, self._dz, self._label = dist, dz, label
        self.update()

    def set_searching(self, label: str) -> None:
        """Target not in the loaded scene right now: dial + label, no needle."""
        if self._scene is not None:
            self._scene = None
            self._legacy_geometry()
        self._angle, self._dist, self._dz, self._label = None, -1.0, 0.0, label
        self.update()

    def set_opacity(self, value: float) -> None:
        self.setWindowOpacity(max(0.3, min(1.0, value)))

    def _state_color(self) -> QtGui.QColor:
        if self._dz > LEVEL_DZ:
            return QtGui.QColor(COL_ABOVE)
        if self._dz < -LEVEL_DZ:
            return QtGui.QColor(COL_BELOW)
        return QtGui.QColor(COL_LEVEL)

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        if self._scene is not None:
            self._paint_projected(p)
            p.end()
            return
        cx = SIDE / 2
        px, py = cx, SIDE / 2 + PIVOT_DY     # pivot: under the player's feet
        sq = self._squash                    # live camera tilt
        searching = self._dist < 0
        here = not searching and self._dist <= ARRIVE
        color = QtGui.QColor(theme.GOOD) if here else self._state_color()

        # faint ground ring (squashed by the camera tilt -> lies on the floor)
        ring = QtGui.QColor(theme.BORDER)
        ring.setAlpha(110)
        pen = QtGui.QPen(ring, 1)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawEllipse(QtCore.QPointF(px, py), R_DIAL, R_DIAL * sq)

        if here:
            # on the spot: green ground pulse instead of a direction
            pen = QtGui.QPen(color, 3)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.drawEllipse(QtCore.QPointF(px, py), R_IN, R_IN * sq)
        elif self._angle is not None:
            self._draw_needle(p, px, py, self._angle, color, sq)

        # distance + label at a FIXED y (independent of tilt, no jitter)
        ty = py + R_TIP + 6
        p.setPen(QtGui.QColor(theme.TEXT))
        f = p.font()
        f.setFamily(theme.MONO_FONT)
        f.setPointSize(11)
        f.setBold(True)
        p.setFont(f)
        if searching:
            txt = "NOT IN SCENE"
        elif here:
            txt = "HERE"
        else:
            txt = f"{self._dist:.0f}m"
            if self._dz > LEVEL_DZ:
                txt += " ▲"
            elif self._dz < -LEVEL_DZ:
                txt += " ▼"
        p.drawText(QtCore.QRectF(0, ty, SIDE, 20), QtCore.Qt.AlignHCenter, txt)
        if self._label:
            f.setPointSize(8)
            f.setBold(False)
            p.setFont(f)
            p.setPen(QtGui.QColor(theme.MUTED))
            p.drawText(QtCore.QRectF(0, ty + 20, SIDE, 16),
                       QtCore.Qt.AlignHCenter, self._label)
        p.end()

    def _paint_projected(self, p):
        """Projected mode: polygons already in window coordinates, lying on
        the game's actual ground plane (drawn via the engine camera matrix)."""
        head, tail, ring, text_xy = self._scene
        searching = self._dist < 0
        here = not searching and self._dist <= ARRIVE
        color = QtGui.QColor(theme.GOOD) if here else self._state_color()
        keyline = QtGui.QColor(11, 14, 20, 210)

        ring_poly = QtGui.QPolygonF([QtCore.QPointF(*pt) for pt in ring])
        rc = QtGui.QColor(color if here else theme.TEXT)
        rc.setAlpha(230 if here else 120)
        pen = QtGui.QPen(rc, 3 if here else 2)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawPolygon(ring_poly)

        if head and tail:
            hp = QtGui.QPolygonF([QtCore.QPointF(*pt) for pt in head])
            tp = QtGui.QPolygonF([QtCore.QPointF(*pt) for pt in tail])
            side = QtGui.QColor(color).darker(220)
            for dy, fh, ft in ((4, side, QtGui.QColor(keyline)),
                               (0, QtGui.QColor(color), QtGui.QColor(theme.MUTED))):
                pen = QtGui.QPen(keyline, 2, QtCore.Qt.SolidLine,
                                 QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
                pen.setCosmetic(True)
                p.setPen(pen)
                p.setBrush(fh)
                p.drawPolygon(hp.translated(0, dy))
                p.setBrush(ft)
                p.drawPolygon(tp.translated(0, dy))

        # distance + label under the ring
        tx, ty = text_xy
        p.setPen(QtGui.QColor(theme.TEXT))
        f = p.font()
        f.setFamily(theme.MONO_FONT)
        f.setPointSize(11)
        f.setBold(True)
        p.setFont(f)
        if searching:
            txt = "NOT IN SCENE"
        elif here:
            txt = "HERE"
        else:
            txt = f"{self._dist:.0f}m"
            if self._dz > LEVEL_DZ:
                txt += " ▲"
            elif self._dz < -LEVEL_DZ:
                txt += " ▼"
        p.drawText(QtCore.QRectF(tx - 200, ty, 400, 20), QtCore.Qt.AlignHCenter, txt)
        if self._label:
            f.setPointSize(8)
            f.setBold(False)
            p.setFont(f)
            p.setPen(QtGui.QColor(theme.MUTED))
            p.drawText(QtCore.QRectF(tx - 200, ty + 20, 400, 16),
                       QtCore.Qt.AlignHCenter, self._label)

    @staticmethod
    def _needle_polys():
        """Needle shapes in the local ground plane, pointing -y (forward)."""
        mid = -(R_IN + (R_TIP - R_IN) * 0.30)
        head = QtGui.QPolygonF([
            QtCore.QPointF(0, -R_TIP),
            QtCore.QPointF(11, mid),
            QtCore.QPointF(0, -R_IN),
            QtCore.QPointF(-11, mid),
        ])
        tail = QtGui.QPolygonF([
            QtCore.QPointF(7, R_IN),
            QtCore.QPointF(0, R_IN + 20),
            QtCore.QPointF(-7, R_IN),
        ])
        return head, tail

    @classmethod
    def _draw_needle(cls, p, px, py, angle, color, sq):
        """Ground-plane needle: rotate in the plane, squash vertically by the
        camera tilt, and draw an extruded dark side under the top face so it
        reads as a flat 3D arrow lying on the floor. The side gets thicker the
        flatter the camera looks (straight down = no visible side)."""
        head, tail = cls._needle_polys()
        deg = math.degrees(angle)
        keyline = QtGui.QColor(11, 14, 20, 210)
        side = QtGui.QColor(color).darker(220)
        extrude = 2 + 8 * (1.0 - sq)

        def pass_(dy, fill_head, fill_tail, outline):
            p.save()
            p.translate(px, py + dy)
            p.scale(1.0, sq)
            p.rotate(deg)
            pen = QtGui.QPen(outline, 2, QtCore.Qt.SolidLine,
                             QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(fill_head)
            p.drawPolygon(head)
            p.setBrush(fill_tail)
            p.drawPolygon(tail)
            p.restore()

        # extruded side (slightly lower), then the lit top face
        pass_(extrude, side, QtGui.QColor(keyline), keyline)
        pass_(0, QtGui.QColor(color), QtGui.QColor(theme.MUTED), keyline)
