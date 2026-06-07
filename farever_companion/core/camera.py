"""Engine camera reader: the live world->screen view-projection matrix.

Path (all resolved by field name through HL reflection, validated live
2026-06-07): GameApp(hxd.App).s3d -> h3d.scene.Scene.camera -> h3d.Camera.m
(h3d.Matrix, _11.._44 as f64). `m` is the combined view-proj matrix the game
itself projects with, so anything projected through it lands exactly where
the game draws it - any camera yaw/tilt/zoom/lead, no calibration.
Read-only; degrades to None while loading or before the chain resolves.
"""
from __future__ import annotations

import struct

from .proc import ProcError

_FIELDS = [f"_{r}{c}" for r in (1, 2, 3, 4) for c in (1, 2, 3, 4)]


class ViewCamera:
    def __init__(self, proc, hl, app):
        self.proc = proc
        self.hl = hl
        self.app = app                  # GameAppLocator
        self._m_offs: list[int] | None = None   # _11.._44 field offsets

    def _follow(self, obj: int | None, field: str) -> int | None:
        if not obj:
            return None
        tp = self.hl.ptr(obj)
        if not tp:
            return None
        off = self.hl.field_offset(tp, field)
        if off is None:
            return None
        return self.hl.ptr(obj + off)

    def _matrix_obj(self) -> int | None:
        gameapp = self.app.locate()
        s3d = self._follow(gameapp, "s3d")
        cam = self._follow(s3d, "camera")
        return self._follow(cam, "m")

    def matrix(self) -> list[float] | None:
        """The current view-proj matrix as a flat row-major list of 16, or
        None while unresolved (loading screen, no game)."""
        try:
            m = self._matrix_obj()
            if not m:
                return None
            if self._m_offs is None:
                tp = self.hl.ptr(m)
                offs = [self.hl.field_offset(tp, f) for f in _FIELDS]
                if any(o is None for o in offs):
                    return None
                self._m_offs = offs
            lo = min(self._m_offs)
            hi = max(self._m_offs) + 8
            blk = self.proc.read(m + lo, hi - lo)
            return [struct.unpack_from("<d", blk, o - lo)[0] for o in self._m_offs]
        except ProcError:
            return None
