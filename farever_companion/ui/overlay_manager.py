"""Overlay window manager, owns the HUD overlay widgets + their open/close,
global opacity/lock, and the toggle-cards that mirror each overlay's open state.

Pulled out of ControlPanel: the panel registers each overlay toggle card and
hands in the live model on attach, then drives everything through this object.
It communicates UI-ward only via signals (`log`, `request_config`) so it holds no
reference back to the panel. Read-only: the overlays only consume the model.
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from .entity_overlay import EntityOverlay
from .dps_overlay import DpsOverlay
from .skill_overlay import SkillOverlay
from .minimap import MinimapOverlay
from .speedrun_overlay import SpeedrunOverlay
from .crosshair import CrosshairOverlay

# The game HUD overlays that share the global opacity + lock (crosshair has its
# own opacity/click-through). One place so new overlays inherit both.
HUD_OVERLAYS = ("entity", "dps", "skills", "map", "speedrun")

# Overlay key -> widget class (crosshair is special-cased: no model).
_OVERLAY_CLASSES = {
    "entity": EntityOverlay, "dps": DpsOverlay, "skills": SkillOverlay,
    "map": MinimapOverlay, "speedrun": SpeedrunOverlay,
}


class OverlayManager(QtCore.QObject):
    log = QtCore.Signal(str)            # status-log line for the panel
    request_config = QtCore.Signal()    # an overlay asked to raise the control panel

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.model = None       # set by the panel on attach; None when detached
        self.overlays: dict[str, QtWidgets.QWidget | None] = {}
        self.cards: dict[str, list] = {}
        # account collection state (None = signed out / unknown); the entity
        # overlay marks wild companions that aren't collected yet
        self.collection_owned: set | None = None

    # --- toggle cards ----------------------------------------------------
    def register_card(self, key: str, card) -> None:
        self.cards.setdefault(key, []).append(card)

    def set_cards_enabled(self, on: bool) -> None:
        """Cards that need a live process (entity/dps/skills/map) are gated until
        attach+locate succeeds. Crosshair stays available (cosmetic)."""
        for key in ("entity", "dps", "skills", "map"):
            for card in self.cards.get(key, []):
                card.setEnabled(on)

    def sync_cards(self, key: str) -> None:
        ov = self.overlays.get(key)
        visible = bool(ov is not None and ov.isVisible())
        for card in self.cards.get(key, []):
            card.set_checked_silent(visible)

    def get(self, key: str):
        return self.overlays.get(key)

    # --- open / close ----------------------------------------------------
    def _make(self, key: str):
        if key == "crosshair":
            return CrosshairOverlay(self.s)
        ov = _OVERLAY_CLASSES[key](self.model, self.s)
        if hasattr(ov, "request_config"):
            ov.request_config.connect(self.request_config)
        if hasattr(ov, "set_collection_owned"):
            ov.set_collection_owned(self.collection_owned)
        return ov

    def request(self, key: str, on: bool) -> None:
        if not on:
            ov = self.overlays.get(key)
            if ov is not None:
                ov.close()      # WA_DeleteOnClose -> _on_closed
            return
        if key != "crosshair" and (self.model is None or self.model.player_addr is None):
            self.log.emit("Attach and locate the player first.")
            self.sync_cards(key)   # revert the toggle
            return
        ov = self.overlays.get(key)
        if ov is not None and ov.isVisible():
            return
        ov = self._make(key)
        ov.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        ov.destroyed.connect(lambda *_: self._on_closed(key))
        ov.show()
        if self.s.lock_overlays and hasattr(ov, "set_locked"):
            ov.set_locked(True)
        self.overlays[key] = ov
        self.sync_cards(key)
        self.log.emit(f"Opened {key} overlay.")

    def _on_closed(self, key: str) -> None:
        self.overlays[key] = None
        self.sync_cards(key)

    def close_all(self) -> None:
        """Tear down every open overlay (detach / app close)."""
        for key, ov in list(self.overlays.items()):
            if ov is not None:
                ov.close()
            self.overlays[key] = None

    # --- global HUD settings ---------------------------------------------
    def set_opacity(self, value) -> None:
        self.s.opacity = value / 100.0
        self.s.save()
        for key in HUD_OVERLAYS:
            ov = self.overlays.get(key)
            if ov and ov.isVisible():
                ov.set_opacity(self.s.opacity)   # forwards to child windows (drop table)

    def set_lock(self, on: bool) -> None:
        self.s.lock_overlays = on
        self.s.save()
        for key in HUD_OVERLAYS:
            ov = self.overlays.get(key)
            if ov is not None and hasattr(ov, "set_locked"):
                ov.set_locked(on)
        self.log.emit("Overlays LOCKED (click-through)." if on
                      else "Overlays unlocked (interactive).")

    def set_collection_owned(self, owned: set | None) -> None:
        self.collection_owned = owned
        for ov in self.overlays.values():
            if ov is not None and hasattr(ov, "set_collection_owned"):
                ov.set_collection_owned(owned)

    def apply_accent(self, color) -> None:
        for ov in self.overlays.values():
            if ov is not None and hasattr(ov, "apply_accent"):
                ov.apply_accent(color)
