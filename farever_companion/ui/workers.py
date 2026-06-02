from __future__ import annotations

from PySide6 import QtCore


class CallWorker(QtCore.QThread):
    done = QtCore.Signal(str, object)
    progress = QtCore.Signal(int)

    def __init__(self, fn, tag: str = ""):
        super().__init__()
        self._fn = fn
        self._tag = tag

    def run(self):
        try:
            res = self._fn(self)
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        self.done.emit(self._tag, res)
