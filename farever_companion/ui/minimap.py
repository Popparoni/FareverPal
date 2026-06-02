"""Minimap overlay.

Top-down radar centred on the player: chests, gatherables, enemies and obelisks
plotted from the shared LiveModel + the static chest index. Zoomable; POIs
beyond the view clamp to the edge ring as direction markers. Right-click a POI
to mark it done (persists). Heading arrow is north-up until a heading field is
calibrated (PLAN §7)."""
from __future__ import annotations

import math

from PySide6 import QtCore, QtGui, QtWidgets

from . import theme
from .overlay_base import OverlayWindow
from ..data import icons

POLL_MS = 300     # POI rescan (heavy)
FAST_MS = 33      # position/heading repaint (cheap) -> smooth pan + rotation

# World -> map-image pixel transform (W1 / Siagarta). Derived from the community
# web map (IceCaveBear/farever-map map.js): coords = [0.89*(4096-y)-1595,
# 0.89*(x+1724)] over a 3584x5120 image. Collapses to: px = S*x + OX, py = S*y + OY.
MAP_SCALE = 0.89
MAP_OFF_X = 0.89 * 1724                       # 1534.36
MAP_OFF_Y = 5120 - (0.89 * 4096 - 1595)       # 3069.56
# camera-yaw → rotation calibration (tune live: sign flips orbit direction,
# offset aligns "up" with the camera's forward)
CAM_YAW_SIGN = 1.0
CAM_YAW_OFFSET = 0.0
_MAP_PM = None
_MAP_TRIED = False


def _map_pixmap():
    """The bundled W1 map image (lazy, cached). None if missing."""
    global _MAP_PM, _MAP_TRIED
    if not _MAP_TRIED:
        _MAP_TRIED = True
        try:
            from .. import paths
            p = paths.assets_dir() / "map" / "W1.png"
            if p.exists():
                pm = QtGui.QPixmap(str(p))
                _MAP_PM = pm if not pm.isNull() else None
        except Exception:
            _MAP_PM = None
    return _MAP_PM


class _Canvas(QtWidgets.QWidget):
    def __init__(self, model, settings):
        super().__init__()
        self.model = model
        self.s = settings
        self.setMinimumSize(220, 220)
        self._pois: list[tuple[float, float, str, str, str]] = []  # x,y,kind,label,id
        self._px = self._py = 0.0
        self._heading: float | None = None    # movement facing
        self._cam_yaw: float | None = None    # free-look camera yaw
        self._drag = None

    def _read_player(self) -> bool:
        xyz = self.model.player_xyz()
        if xyz is None:
            return False
        self._px, self._py, _ = xyz
        self._heading = self.model.player_heading()       # movement facing (arrow)
        self._cam_yaw = self.model.camera_yaw()           # free-look yaw (map rotation)
        return True

    def refresh_fast(self):
        """Cheap: player position + camera yaw / heading (a few tiny reads) so
        the map pans/rotates smoothly. Runs at ~30fps; negligible cost."""
        if self._read_player():
            self.update()

    def refresh(self):
        """Heavy: rebuild the POI list (enemy/chest/gatherable scan). Runs at a
        low rate; the fast tick keeps motion smooth in between."""
        if not self._read_player():
            self._pois = []
            self.update()
            return
        xyz = (self._px, self._py, 0.0)
        pois = []
        s = self.s
        # static + live chests / crates / world-activity loot drops
        if s.minimap_chests:
            try:
                for r in self.model.nearest_chests_merged(xyz, n=60):
                    c = next((c for c in self.model.chests if c.chest_id == r.chest_id), None)
                    if c:
                        pois.append((c.x, c.y, "chest", r.loot_table or r.chest_id, r.chest_id))
            except Exception:
                pass
        # live entities (each layer independently toggleable)
        try:
            if s.minimap_enemies:
                for e in self.model.enemies():
                    pois.append((e.x, e.y, "enemy", e.unit_id or "?", f"e{e.addr}"))
            if s.minimap_gatherables:
                for g in self.model.gatherables():
                    pois.append((g.x, g.y, "gatherable", g.elem_id or "?", g.elem_id or ""))
            if s.minimap_obelisks:
                for o in self.model.obelisks():
                    pois.append((o.x, o.y, "obelisk", o.elem_id or "obelisk",
                                 o.elem_id or f"ob{o.addr}"))
        except Exception:
            pass
        self._pois = pois
        self.update()

    def _scale(self):
        view = max(20.0, self.s.minimap_zoom * 20.0)   # world-units radius shown
        return (min(self.width(), self.height()) / 2 - 6) / view

    def _phi(self):
        """Rotation applied so the view direction points up. Prefer the free-look
        CAMERA yaw (mouse orbit); fall back to movement heading; 0 = north-up."""
        if not self.s.minimap_rotate:
            return 0.0
        if self._cam_yaw is not None:
            return math.pi / 2 - (CAM_YAW_SIGN * self._cam_yaw + CAM_YAW_OFFSET)
        if self._heading is not None:
            return math.pi / 2 - self._heading
        return 0.0

    def _rel(self, wx, wy, scale, phi):
        """World point -> rotated screen-space delta (dx right, dy up)."""
        dx = (wx - self._px) * scale
        dy = (wy - self._py) * scale
        if phi:
            c, s = math.cos(phi), math.sin(phi)
            dx, dy = dx * c - dy * s, dx * s + dy * c
        return dx, dy

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        rad = min(w, h) / 2 - 4
        square = self.s.minimap_shape == "Square"
        # backdrop + clip
        p.setBrush(QtGui.QColor(theme.PANEL))
        p.setPen(QtGui.QColor(theme.BORDER))
        # clip with a QPainterPath (transform-aware, so the rotated map texture
        # below is clipped correctly — a QRegion clip is not)
        clip = QtGui.QPainterPath()
        if square:
            box = QtCore.QRectF(cx - rad, cy - rad, rad * 2, rad * 2)
            p.drawRect(box)
            clip.addRect(box)
        else:
            p.drawEllipse(QtCore.QPointF(cx, cy), rad, rad)
            clip.addEllipse(QtCore.QPointF(cx, cy), rad, rad)
        p.setClipPath(clip)
        scale = self._scale()
        phi = self._phi()
        # world map texture (under the rings/POIs)
        if self.s.minimap_texture:
            self._draw_map(p, cx, cy, scale, phi)
        # rings (outline only — NoBrush, else they'd fill over the map texture)
        p.setPen(QtGui.QColor(theme.BORDER))
        p.setBrush(QtCore.Qt.NoBrush)
        for f in (0.5, 1.0):
            if square:
                p.drawRect(QtCore.QRectF(cx - rad * f, cy - rad * f, rad * 2 * f, rad * 2 * f))
            else:
                p.drawEllipse(QtCore.QPointF(cx, cy), rad * f, rad * f)
        # POIs
        for (wx, wy, kind, label, poi_id) in self._pois:
            dx, dy = self._rel(wx, wy, scale, phi)
            edge = False
            if square:
                m = max(abs(dx), abs(dy))
                if m > rad - 4:
                    if m == 0:
                        continue
                    k = (rad - 4) / m
                    dx *= k; dy *= k
                    edge = True
            else:
                dist = math.hypot(dx, dy)
                if dist > rad - 4:
                    if dist == 0:
                        continue
                    k = (rad - 4) / dist
                    dx *= k; dy *= k
                    edge = True
            sx, sy = cx + dx, cy - dy
            done = bool(poi_id and self.s.is_done(poi_id))
            pm = self._poi_pixmap(kind, label, self.s.minimap_icon_size) \
                if (self.s.minimap_icons and not edge) else None
            if pm is not None:
                if done:
                    p.setOpacity(0.4)
                z = pm.width()
                p.drawPixmap(int(sx - z / 2), int(sy - z / 2), pm)
                if done:
                    p.setOpacity(1.0)
            else:
                col = QtGui.QColor(theme.KIND_COLOR.get(kind, theme.TEXT))
                if done:
                    col.setAlpha(70)
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(col)
                p.drawEllipse(QtCore.QPointF(sx, sy), 3 if edge else 4, 3 if edge else 4)
        # compass + player (drawn unclipped so edge letters aren't cut)
        p.setClipping(False)
        self._draw_compass(p, cx, cy, rad, phi)
        # player marker + facing arrow (uses the live Highlight color)
        accent = QtGui.QColor(self.s.hud_accent)
        fwd = (self._heading + phi) if self._heading is not None else (math.pi / 2)
        p.setBrush(accent)
        p.setPen(QtCore.Qt.NoPen)
        p.drawEllipse(QtCore.QPointF(cx, cy), 4, 4)
        p.setPen(QtGui.QPen(accent, 2))
        p.drawLine(QtCore.QPointF(cx, cy),
                   QtCore.QPointF(cx + math.cos(fwd) * 13, cy - math.sin(fwd) * 13))
        p.end()

    def _draw_map(self, p, cx, cy, scale, phi):
        """Blit the world map so the player sits at centre, facing up, matching
        the POI projection. image px = (S*x+OX, S*y+OY); we map image->screen
        with the same rotate(phi)+scale the POIs use and let Qt clip."""
        pm = _map_pixmap()
        if pm is None:
            return
        s, A = scale, MAP_SCALE
        cosf, sinf = math.cos(phi), math.sin(phi)
        u0 = -MAP_OFF_X / A - self._px      # world x at image px 0
        v0 = -MAP_OFF_Y / A - self._py
        t = QtGui.QTransform(
            s * cosf / A, -s * sinf / A,    # m11, m12  (coeffs of image x)
            -s * sinf / A, -s * cosf / A,   # m21, m22  (coeffs of image y)
            cx + s * cosf * u0 - s * sinf * v0,
            cy - s * sinf * u0 - s * cosf * v0)
        p.save()
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        p.setTransform(t, True)
        p.drawPixmap(0, 0, pm)
        p.restore()
        # subtle veil so POIs/markers read clearly over the art
        veil = QtGui.QColor(theme.BG); veil.setAlpha(70)
        p.fillRect(self.rect(), veil)

    # bundled flat marker icons (assets/map_icons) per layer
    _MARKER = {"chest": "chest", "gatherable": "mining", "obelisk": "portal"}

    def _poi_pixmap(self, kind, label, size):
        """A POI marker pixmap: enemies show the real per-unit game icon (organic
        outline); chests/crates, gatherables and obelisks use the bundled flat
        marker icons (treasure / pickaxe / portal). Falls back to a tinted UI
        glyph, then to a plain dot."""
        if kind == "enemy" and label and icons.has_icon("unit", label):
            return icons.outlined("unit", label, size, theme.DANGER)
        name = self._MARKER.get(kind)
        if name:
            pm = icons.marker(name, size)
            if pm is not None:
                return pm
        glyph = {"chest": "box", "obelisk": "radio", "gatherable": "dice-5",
                 "enemy": "swords"}.get(kind)
        if glyph:
            return icons.ui_icon(glyph, theme.KIND_COLOR.get(kind, theme.TEXT), size)
        return None

    def _draw_compass(self, p, cx, cy, rad, phi):
        r = rad - 11
        f = p.font(); f.setPointSize(8); f.setBold(True); p.setFont(f)
        for label, ang, col in (("N", math.pi / 2, theme.DANGER),
                                ("E", 0.0, theme.MUTED),
                                ("S", -math.pi / 2, theme.MUTED),
                                ("W", math.pi, theme.MUTED)):
            a = ang + phi
            lx = cx + math.cos(a) * r
            ly = cy - math.sin(a) * r
            p.setPen(QtGui.QColor(col))
            p.drawText(QtCore.QRectF(lx - 8, ly - 8, 16, 16),
                       QtCore.Qt.AlignCenter, label)

    def mousePressEvent(self, e):
        if e.button() == QtCore.Qt.RightButton:
            self._mark_done(e)
            return
        if e.button() == QtCore.Qt.LeftButton and not getattr(self.window(), "_locked", False):
            # drag the window from the map body (the only grip in bare mode)
            self._drag = e.globalPosition().toPoint() - self.window().frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & QtCore.Qt.LeftButton:
            self.window().move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e):
        if self._drag is not None:
            self._drag = None
            win = self.window()
            if hasattr(win, "persist_geometry"):
                win.persist_geometry()

    def _mark_done(self, e):
        # mark the nearest plotted POI done (hit-test in the same rotated space)
        cx, cy = self.width() / 2, self.height() / 2
        scale = self._scale()
        phi = self._phi()
        best, bestd = None, 12.0
        for (wx, wy, _k, _l, poi_id) in self._pois:
            dx, dy = self._rel(wx, wy, scale, phi)
            sx, sy = cx + dx, cy - dy
            d = math.hypot(sx - e.position().x(), sy - e.position().y())
            if d < bestd and poi_id:
                best, bestd = poi_id, d
        if best:
            self.s.toggle_done(best)
            self.update()


class MinimapOverlay(OverlayWindow):
    def __init__(self, model, settings, parent=None):
        super().__init__("MAP", settings, geo_key="minimap", parent=parent)
        self.s = settings
        zin = QtWidgets.QPushButton("+"); zin.setObjectName("Icon")
        zout = QtWidgets.QPushButton("−"); zout.setObjectName("Icon")
        zin.clicked.connect(lambda: self._zoom(1.25))
        zout.clicked.connect(lambda: self._zoom(0.8))
        for wdg in (zout, zin):
            self.titlebar.extra.insertWidget(self.titlebar.extra.count() - 1, wdg)

        self.canvas = _Canvas(model, settings)
        self.content.addWidget(self.canvas, 1)
        self._hint = QtWidgets.QLabel("right-click a marker = mark done · drag map to move")
        self._hint.setObjectName("Muted")
        self.content.addWidget(self._hint)

        sz = settings.minimap_size
        self.resize(sz, sz + 40)
        if settings.minimap_bare:
            self.set_bare(True)
        # two cadences: fast = smooth pan/rotate (cheap reads); slow = POI rescan
        self._fast = QtCore.QTimer(self)
        self._fast.timeout.connect(self.canvas.refresh_fast)
        self._fast.start(FAST_MS)
        self._slow = QtCore.QTimer(self)
        self._slow.timeout.connect(self.canvas.refresh)
        self._slow.start(POLL_MS)
        self.canvas.refresh()   # initial POIs immediately

    def set_bare(self, on: bool) -> None:
        """Chromeless: hide the titlebar, hint, and card panel — just the map.
        Drag the map body to move it (when unlocked)."""
        self.titlebar.setVisible(not on)
        self._hint.setVisible(not on)
        if on:
            self._frame.setStyleSheet("background:transparent;border:0;")
            self.content.setContentsMargins(0, 0, 0, 0)
        else:
            self._frame.setStyleSheet("")     # revert to the QSS #Card look
            self.content.setContentsMargins(8, 6, 8, 8)
        self.canvas.update()

    def _zoom(self, f):
        self.s.minimap_zoom = max(2.0, min(60.0, self.s.minimap_zoom * f))
        self.s.save()
        self.canvas.update()

    def closeEvent(self, e):
        self._fast.stop()
        self._slow.stop()
        super().closeEvent(e)
