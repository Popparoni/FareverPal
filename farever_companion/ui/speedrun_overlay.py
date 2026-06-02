"""Speedrun timer overlay.

A big mono stopwatch driven by the global hotkeys (toggle = start/stop, plus a
reset key). While running it watches the live dungeon boss and auto-stops the
instant it dies (configurable). Tracks a per-boss best (PB) + last time,
persisted in Settings.

Out-of-process like every overlay: it only reads game memory (boss HP) to detect
the kill — it never writes.
"""
from __future__ import annotations

import threading

from PySide6 import QtCore, QtGui, QtWidgets

from . import theme
from .overlay_base import OverlayWindow
from ..core.speedrun import SpeedrunTimer, AutoStarter, ModeLatch, fmt_time
from ..data import names, icons
from ..api import FareverAPI

TICK_MS = 50           # centisecond-smooth display + boss poll while running
DETECT_EVERY = 10      # run the (heavier) dungeon detection every Nth tick (~500ms)
REARM_TICKS = 100      # ~5s after a finish, re-arm for the next run (back-to-back)


class SpeedrunOverlay(OverlayWindow):
    uploaded = QtCore.Signal(str)   # cross-thread upload-result text

    def __init__(self, model, settings, parent=None):
        super().__init__("SPEEDRUN", settings, geo_key="speedrun", parent=parent)
        self.model = model
        self.s = settings
        self.timer = SpeedrunTimer()
        self._prev_state = self.timer.state
        self._detect_ctr = 0
        self._dungeon_bid: str | None = None   # detected dungeon boss (sticky)
        self._in_dungeon = False                # boss currently present in the scene
        self._starter = AutoStarter()           # settle-baseline auto-start decision (pure)
        self._latch = ModeLatch()               # last good auto-detected difficulty this run
        self._done_ctr = 0                      # ticks since a finish (for back-to-back re-arm)
        self._uploaded = False                  # guard: one upload per finished run
        self._run_mode: str | None = None       # difficulty captured at finish
        self._run_mode_src: str | None = None    # "auto" (read from boss level) | "manual"
        self._live_mode: str | None = None       # difficulty resolved live (for the HUD readout)
        self._live_mode_src: str | None = None

        # dungeon-instance icon, shown next to the panel header when detected
        self._dg_icon = QtWidgets.QLabel()
        self._dg_icon.setFixedSize(22, 22)
        self._dg_icon.setScaledContents(True)
        self._dg_icon.hide()
        self.titlebar.extra.insertWidget(0, self._dg_icon)

        # reset button in the title bar
        rst = QtWidgets.QPushButton()
        rst.setObjectName("Icon")
        rst.setIcon(icons.ui_qicon("refresh-cw", theme.MUTED, 14))
        rst.setToolTip("Reset timer")
        rst.clicked.connect(self.reset)
        self.titlebar.extra.insertWidget(self.titlebar.extra.count() - 1, rst)

        self.uploaded.connect(self._on_uploaded)

        self.time_lbl = QtWidgets.QLabel("00:00.00")
        self.time_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self._style_time(theme.MUTED)
        self.content.addWidget(self.time_lbl)

        self.state_lbl = QtWidgets.QLabel("READY")
        self.state_lbl.setObjectName("Mono")
        self.state_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.content.addWidget(self.state_lbl)

        # Difficulty readout — shows what a finished run will upload as, and whether
        # it was read from the live boss level ("detected") or is the manual
        # fallback ("manual", tinted gold so a guess is never mistaken for a read).
        self.mode_lbl = QtWidgets.QLabel("")
        self.mode_lbl.setObjectName("Mono")
        self.mode_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.mode_lbl.hide()
        self.content.addWidget(self.mode_lbl)

        self.pb_lbl = QtWidgets.QLabel("no record yet")
        self.pb_lbl.setObjectName("Mono")
        self.pb_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.content.addWidget(self.pb_lbl)

        self.upload_lbl = QtWidgets.QLabel("")
        self.upload_lbl.setObjectName("Mono")
        self.upload_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.upload_lbl.setWordWrap(True)
        self.upload_lbl.hide()
        self.content.addWidget(self.upload_lbl)

        # Manual upload (shown after a finished run only when auto-upload is off).
        self._upload_btn = QtWidgets.QPushButton("Upload run")
        self._upload_btn.setObjectName("Accent")
        self._upload_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._upload_btn.clicked.connect(self._do_upload)
        self._upload_btn.hide()
        self.content.addWidget(self._upload_btn)

        # Fully automatic: starts on movement in a dungeon, stops on boss death,
        # uploads on finish. Manual control via hotkeys + the titlebar reset icon.
        self._base_w, self._base_h = 250, 120
        self.setMinimumWidth(210)
        self.apply_scale(self.s.speedrun_scale)

        self._poll = QtCore.QTimer(self)
        self._poll.timeout.connect(self._tick)
        self._poll.start(TICK_MS)
        self._render()

    # --- actions (hotkeys + buttons) ------------------------------------
    def toggle(self):
        self.timer.toggle()
        self._render()

    def reset(self):
        self.timer.reset()
        self._starter.reset()
        self._latch.reset()
        self._done_ctr = 0
        self._uploaded = False
        self._run_mode = None
        self._run_mode_src = None
        self.upload_lbl.hide()
        self._upload_btn.hide()
        self._render()

    # --- loop ------------------------------------------------------------
    def _tick(self):
        t = self.timer
        running = (t.state == t.RUNNING)
        auto = bool(self.s.speedrun_auto)   # one master toggle: detect + start + stop

        # When fully manual (auto off), the overlay is a plain stopwatch: no scene
        # reads, no dungeon icon, no auto start/stop.
        if not auto:
            self._dg_icon.hide()
            self._prev_state = t.state
            self._render()
            return

        # ONE boss read per tick: every tick while running (responsive auto-stop),
        # else on a slower cadence for dungeon detection. boss = (bid, present, hp).
        boss = None
        if self.model is not None:
            self._detect_ctr += 1
            detect_now = self._detect_ctr >= DETECT_EVERY
            if running or detect_now:
                if detect_now:
                    self._detect_ctr = 0
                try:
                    boss = self.model.boss_state()
                except Exception:
                    boss = None

        # Dungeon detection: remember the boss, are-we-inside flag, header icon.
        if boss is not None:
            bid, present, _hp = boss
            if bid:
                self._dungeon_bid = bid
            self._in_dungeon = bool(present)
            self._update_dungeon_icon()
            # Refresh the live difficulty readout (cheap: a single boss-level read)
            # and, while the boss is alive and fighting, latch a confident auto
            # read so the finish-frame (boss dying) can't flip Normal<->Hard.
            self._live_mode, self._live_mode_src = self._resolve_mode()
            if running:
                self._latch.observe(self._live_mode, self._live_mode_src)

        # Auto-stop on boss kill, or CANCEL if the player left the dungeon (the
        # boss despawning at full HP — e.g. going to the main menu — is not a kill).
        if running and boss is not None:
            if t.feed_boss(*boss) == t.LEFT:
                self._on_run_aborted()

        # Auto-start: only INSIDE a dungeon, and only on real physical movement
        # from a settled spawn baseline (consistent — never on the teleport-in).
        if t.state == t.READY and self.model is not None:
            self._check_auto_start()

        # Run just started → arm a fresh upload + a fresh difficulty latch.
        if t.state == t.RUNNING and self._prev_state != t.RUNNING:
            self._uploaded = False
            self._latch.reset()
            self.upload_lbl.hide()
            self._upload_btn.hide()

        # Run just finished → capture difficulty, PB, upload. Prefer the value
        # latched while the boss was alive over a fresh read (the boss is dead
        # now, so a fresh read often can't see it). PB + auto-upload only count a
        # CONFIRMED kill (a manual stop must never set a bogus PB or auto-submit).
        if t.state == t.DONE and self._prev_state != t.DONE:
            self._done_ctr = 0
            self._run_mode, self._run_mode_src = self._latch.resolve(*self._resolve_mode())
            if t.is_kill:
                self._record_best()
            self._maybe_upload()

        # Back-to-back: after a finished run, re-arm for the next one without the
        # user clicking reset — once they leave the dungeon, or after a short
        # grace so the finished time is readable first. Toggleable.
        if t.state == t.DONE and self.s.speedrun_auto_rearm:
            self._done_ctr += 1
            if (not self._in_dungeon) or self._done_ctr >= REARM_TICKS:
                self.reset()

        self._prev_state = t.state
        self._render()

    def _check_auto_start(self):
        # Delegate the (settle-baseline) decision to the pure AutoStarter so it
        # behaves identically every run: it only fires on deliberate movement
        # away from a stationary spawn baseline, never on the teleport-in jitter.
        try:
            pos = self.model.player_xyz()
        except Exception:
            pos = None
        if self._starter.feed(self._in_dungeon, pos):
            self.timer.start()

    def _update_dungeon_icon(self):
        bid = self._dungeon_bid
        if not bid:
            self._dg_icon.hide()
            return
        try:
            pm = icons.pixmap("unit", bid, 22)
            if pm is not None and not pm.isNull():
                self._dg_icon.setPixmap(pm)
                self._dg_icon.setToolTip(names.unit_name(bid) or bid)
                self._dg_icon.show()
            else:
                self._dg_icon.hide()
        except Exception:
            self._dg_icon.hide()

    def _record_best(self):
        bid = self.timer.boss_id or "_"
        cur = self.timer.last
        if cur is None:
            return
        best = self.s.speedrun_best.get(bid)
        if best is None or cur < best:
            self.s.speedrun_best[bid] = cur
            self.s.save()
            self.timer.is_new_best = True

    def _maybe_upload(self):
        """On finish: auto-upload ONLY a confirmed boss kill (and only if enabled);
        otherwise offer a manual Upload button. A finish without a detected kill
        (a manual stop, or anything that wasn't a real kill) is never auto-sent."""
        if self._uploaded:
            return
        bid = self.timer.boss_id or self._dungeon_bid
        if not bid or self.timer.last is None:
            return
        if not self.s.account_token:
            self.upload_lbl.setText("sign in (control panel) to upload")
            self.upload_lbl.setStyleSheet(f"color:{theme.MUTED};background:transparent;")
            self.upload_lbl.show()
            return
        if not self.timer.is_kill:
            # finished without a detected kill — never auto-submit; let the user
            # decide via the manual button (the leaderboard also screens times).
            self.upload_lbl.setText("no boss kill detected — upload manually if this was a real run")
            self.upload_lbl.setStyleSheet(f"color:{theme.GOLD};background:transparent;")
            self.upload_lbl.show()
            self._upload_btn.show()
            return
        if self.s.speedrun_auto_upload:
            self._do_upload()
        else:
            self._upload_btn.show()   # manual: user clicks to upload

    def _on_run_aborted(self):
        """Player left the dungeon / hit the main menu mid-run: cancel the run
        without recording a time or uploading anything."""
        self.timer.reset()
        self._starter.reset()
        self._latch.reset()
        self._done_ctr = 0
        self._uploaded = False
        self._run_mode = None
        self._run_mode_src = None
        self._upload_btn.hide()
        self.upload_lbl.setText("run cancelled — left the dungeon (no upload)")
        self.upload_lbl.setStyleSheet(f"color:{theme.MUTED};background:transparent;")
        self.upload_lbl.show()

    def _resolve_mode(self) -> tuple[str, str]:
        """(difficulty, source) to upload as. source is "auto" when read from the
        live boss level (Auto Detect Boss Run on + level readable), else "manual"
        — the fallback selector, used when auto is off OR the level can't be read
        (e.g. the level offset isn't calibrated on this build)."""
        if self.s.speedrun_auto and self.model is not None:
            try:
                m = self.model.detected_mode()
            except Exception:
                m = None
            if m in ("normal", "hard"):
                return m, "auto"
        mode = self.s.speedrun_mode if self.s.speedrun_mode in ("normal", "hard") else "normal"
        return mode, "manual"

    def _do_upload(self):
        """Push the finished run to the web leaderboard (background thread)."""
        bid = self.timer.boss_id or self._dungeon_bid
        last = self.timer.last
        if self._uploaded or not bid or last is None or not self.s.account_token:
            return
        self._uploaded = True
        self._upload_btn.hide()
        slug, time_ms = bid.lower(), int(round(last * 1000))
        base, token = self.s.api_base, self.s.account_token
        mode = self._run_mode or self._resolve_mode()[0]
        src = self._run_mode_src or "manual"
        self.upload_lbl.setText(f"uploading… ({mode} · {src})")
        self.upload_lbl.setStyleSheet(f"color:{theme.MUTED};background:transparent;")
        self.upload_lbl.show()

        co_runners = list(self.s.speedrun_corunners or [])
        # Per-run build override (Speedrun tab): when on, attach this build code;
        # otherwise the server links the player's profile featured build.
        build_code = (self.s.speedrun_build_override or "").strip() if self.s.speedrun_build_override_on else ""
        def work():
            res = FareverAPI(base, token).submit_run(
                slug, time_ms, mode=mode, co_runners=co_runners, build_code=build_code)
            if res.get("ok"):
                msg = "uploaded · pending review" if res.get("flagged") else "uploaded · live ✓"
            else:
                msg = "upload failed: " + str(res.get("error", "error"))
            self.uploaded.emit(msg)

        threading.Thread(target=work, daemon=True).start()

    def _on_uploaded(self, msg: str):
        color = theme.GOOD if "✓" in msg else (theme.GOLD if "pending" in msg else theme.DANGER)
        self.upload_lbl.setText(msg)
        self.upload_lbl.setStyleSheet(f"color:{color};background:transparent;")
        self.upload_lbl.show()

    # --- render ----------------------------------------------------------
    def _style_time(self, color: str):
        size = max(18, int(40 * float(self.s.speedrun_scale or 1.0)))  # scale with the overlay zoom
        self.time_lbl.setStyleSheet(
            f"color:{color};font-family:'{theme.MONO_FONT}','Consolas';"
            f"font-size:{size}px;font-weight:800;background:transparent;letter-spacing:1px;")

    def _render(self):
        t = self.timer
        self.time_lbl.setText(fmt_time(t.elapsed()))
        if t.state == t.RUNNING:
            self._style_time(self.s.hud_accent or theme.ACCENT)
        elif t.state == t.DONE:
            self._style_time(theme.GOLD if t.is_new_best else theme.GOOD)
        else:
            self._style_time(theme.MUTED)

        boss = names.unit_name(t.boss_id) if t.boss_id else "—"
        label = {t.READY: "READY", t.RUNNING: "RUNNING", t.DONE: "FINISHED"}[t.state]
        self.state_lbl.setText(f"{label}   ·   {boss}")
        self._render_mode()

        bid = t.boss_id or "_"
        best = self.s.speedrun_best.get(bid)
        parts = []
        if t.state == t.DONE and t.is_new_best:
            parts.append("★ NEW BEST")
        if best is not None:
            parts.append(f"PB {fmt_time(best)}")
        if t.last is not None:
            parts.append(f"LAST {fmt_time(t.last)}")
        self.pb_lbl.setText("   ·   ".join(parts) or "no record yet")

    def _render_mode(self):
        """The difficulty readout: what a finished run uploads as + how it was
        resolved. After a finish it shows the captured value; otherwise the live
        one. Hidden when there's no dungeon context to talk about."""
        t = self.timer
        if t.state == t.DONE and self._run_mode:
            mode, src = self._run_mode, (self._run_mode_src or "manual")
        else:
            mode, src = self._live_mode, self._live_mode_src
        # Show an AUTO-detected difficulty wherever it resolved (a recognized boss
        # OR the enemy-fleet read in a boss-less dungeon like the bee one). Only the
        # MANUAL fallback needs a boss/dungeon context to avoid showing in town.
        if not mode or (src != "auto" and not (self._dungeon_bid or t.boss_id)):
            self.mode_lbl.hide()
            return
        if src == "auto":
            tag, color = "detected", (self.s.hud_accent or theme.ACCENT)
            tip = "Difficulty read from the live boss level."
        else:
            tag, color = "manual", theme.GOLD
            tip = ("Manual fallback — the live boss level can't be read on this "
                   "build (level offset not calibrated), so the Upload-as selector "
                   "in the control panel is used. Set it to match your run.")
        self.mode_lbl.setText(f"{mode.upper()}  ·  {tag}")
        self.mode_lbl.setStyleSheet(f"color:{color};background:transparent;")
        self.mode_lbl.setToolTip(tip)
        self.mode_lbl.show()

    def closeEvent(self, e):
        self._poll.stop()
        super().closeEvent(e)
