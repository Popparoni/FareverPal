"""Control panel — the main window (Tactical Overlay shell).

A left nav rail + a QStackedWidget body: Overlays, Combat/DPS, Loot, Crosshair,
Map, Log. Attaches to the game (pure-read player locate on a worker thread),
configures the overlays, and runs the offline loot predictor. Owns the single
shared LiveModel so every overlay reads one source of truth. The crosshair is
cosmetic and needs no attach.
"""
from __future__ import annotations

import datetime
import threading
import time
import webbrowser

from PySide6 import QtCore, QtGui, QtWidgets

from . import theme
from . import components as C
from .widgets import fmt_pct
from .entity_overlay import EntityOverlay
from .dps_overlay import DpsOverlay
from .skill_overlay import SkillOverlay
from .minimap import MinimapOverlay
from .speedrun_overlay import SpeedrunOverlay
from .crosshair import CrosshairOverlay, CrosshairCanvas
from ..config import Settings
from ..core.proc import Proc, ProcError, backend_name, find_pid
from ..core.model import LiveModel
from ..core.autoattach import AutoAttach, Act
from ..core import updater
from ..data import loot, names, tokens
from ..data import icons
from .. import __version__
from .. import paths
from ..api import FareverAPI, oauth_login

# nav: key, icon, label
NAV = [
    ("overlays", "layers", "Overlays"),
    ("entity", "search", "Entity"),
    ("combat", "swords", "Combat / DPS"),
    ("speedrun", "timer", "Speedrun"),
    ("loot", "box", "Loot"),
    ("crosshair", "crosshair", "Crosshair"),
    ("map", "map", "Map"),
    ("friends", "users", "Friends"),
    ("log", "terminal", "Log"),
]
CLASSES = ["Auto", "Warrior", "Rogue", "Mage", "Priest", "Off"]
# the game HUD overlays that share the global opacity + lock (crosshair has its
# own opacity/click-through). Keep this in one place so new overlays inherit both.
HUD_OVERLAYS = ("entity", "dps", "skills", "map", "speedrun")


class _LocateWorker(QtCore.QThread):
    done = QtCore.Signal(object)   # address, None, or Exception

    def __init__(self, model: LiveModel):
        super().__init__()
        self.model = model

    def run(self):
        try:
            self.done.emit(self.model.locate_player())
        except Exception as e:
            self.done.emit(e)


class _LoginWorker(QtCore.QThread):
    """Username/password → bearer token (off the UI thread). Emits a result dict."""
    done = QtCore.Signal(dict)

    def __init__(self, base_url: str, username: str, password: str):
        super().__init__()
        self.base_url, self.username, self.password = base_url, username, password

    def run(self):
        api = FareverAPI(self.base_url)
        res = api.login(self.username, self.password)
        if res.get("ok") and res.get("token"):
            # Pull the full synced profile (for accent color etc.).
            me = FareverAPI(self.base_url, res["token"]).me()
            if me.get("ok") and isinstance(me.get("user"), dict):
                res["user"] = {**res.get("user", {}), **me["user"]}
        self.done.emit(res)


class _OAuthWorker(QtCore.QThread):
    """Browser OAuth (Google/Discord) → bearer token (off the UI thread)."""
    done = QtCore.Signal(dict)

    def __init__(self, base_url: str, provider: str):
        super().__init__()
        self.base_url, self.provider = base_url, provider

    def run(self):
        res = oauth_login(self.base_url, self.provider)
        if res.get("ok") and res.get("token"):
            me = FareverAPI(self.base_url, res["token"]).me()
            if me.get("ok") and isinstance(me.get("user"), dict):
                res["user"] = me["user"]
        self.done.emit(res)


class _AvatarLoader(QtCore.QThread):
    """Download an avatar image off the UI thread. Emits a QPixmap (or None)."""
    done = QtCore.Signal(object)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            import urllib.request
            req = urllib.request.Request(self.url, headers={"User-Agent": "FareverPal-Companion"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = r.read()
            pm = QtGui.QPixmap()
            pm.loadFromData(data)
            self.done.emit(pm if not pm.isNull() else None)
        except Exception:
            self.done.emit(None)


class _FriendsWorker(QtCore.QThread):
    """Off-thread: send a presence heartbeat, then fetch the friends list.
    Emits the friends_list() result dict (with an extra 'share' echo)."""
    done = QtCore.Signal(dict)

    def __init__(self, base_url: str, token: str, share: bool):
        super().__init__()
        self.base_url, self.token, self.share = base_url, token, share

    def run(self):
        api = FareverAPI(self.base_url, self.token)
        api.presence_ping(self.share)          # mark us "online (companion)"
        res = api.friends_list()
        self.done.emit(res if isinstance(res, dict) else {"ok": False})


class _ApiWorker(QtCore.QThread):
    """Off-thread one-shot API call. `fn(api) -> dict`; emits the result dict
    (or {'ok': False} on failure). A `tag` rides along so one slot can route
    several callers."""
    done = QtCore.Signal(str, dict)

    def __init__(self, base_url: str, token: str, fn, tag: str = ""):
        super().__init__()
        self.base_url, self.token, self.fn, self.tag = base_url, token, fn, tag

    def run(self):
        try:
            res = self.fn(FareverAPI(self.base_url, self.token))
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        self.done.emit(self.tag, res if isinstance(res, dict) else {"ok": False})


class _UpdateCheckWorker(QtCore.QThread):
    """Off-thread: ask the web oracle for the latest release. Emits an
    updater.UpdateInfo if a newer version exists, else None."""
    done = QtCore.Signal(object)

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url

    def run(self):
        try:
            self.done.emit(updater.check(FareverAPI(self.base_url)))
        except Exception:
            self.done.emit(None)


class _UpdateDownloadWorker(QtCore.QThread):
    """Off-thread: download + stage the new exe. Emits progress(0..100) and
    finally done(Path | Exception)."""
    progress = QtCore.Signal(int)
    done = QtCore.Signal(object)

    def __init__(self, info):
        super().__init__()
        self.info = info

    def run(self):
        def cb(d, t):
            if t:
                self.progress.emit(int(d * 100 / t))
        try:
            self.done.emit(updater.download_and_stage(self.info, cb))
        except Exception as e:
            self.done.emit(e)


class LoginDialog(QtWidgets.QDialog):
    """Sign-in dialog mirroring the web's guest/login style (dark, sharp)."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.result_user: dict | None = None
        self.result_token: str = ""
        self._worker: _LoginWorker | None = None

        self.setWindowTitle("Sign in to Farever Pal")
        self.setObjectName("Card")
        self.setMinimumWidth(360)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(22, 20, 22, 20)
        v.setSpacing(12)

        head = QtWidgets.QLabel("Sign in")
        head.setObjectName("H1")
        v.addWidget(head)
        sub = QtWidgets.QLabel("Use your farever-pals.com account to sync runs + settings.")
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        v.addWidget(sub)

        # One-click sign-in (opens the browser; account is created automatically).
        self._oauth_worker: _OAuthWorker | None = None
        # Solid brand fills with explicit foreground — high contrast, readable.
        self.btn_google = self._provider_button(
            "Continue with Google", "google", bg="#ffffff", fg="#1f2328", hover="#e8eaed",
            icon=icons.brand_qicon("google-g", 18))
        self.btn_discord = self._provider_button(
            "Continue with Discord", "discord", bg="#5865f2", fg="#ffffff", hover="#4752c4",
            icon=icons.ui_qicon("discord", "#ffffff", 18))
        v.addWidget(self.btn_google)
        v.addWidget(self.btn_discord)

        div = QtWidgets.QLabel("or sign in with a password")
        div.setObjectName("Muted")
        div.setAlignment(QtCore.Qt.AlignCenter)
        v.addWidget(div)

        self.user = QtWidgets.QLineEdit()
        self.user.setPlaceholderText("Username")
        v.addWidget(self.user)
        self.pw = QtWidgets.QLineEdit()
        self.pw.setPlaceholderText("Password")
        self.pw.setEchoMode(QtWidgets.QLineEdit.Password)
        self.pw.returnPressed.connect(self._submit)
        v.addWidget(self.pw)

        self.err = QtWidgets.QLabel("")
        self.err.setStyleSheet(f"color:{theme.DANGER};background:transparent;")
        self.err.setWordWrap(True)
        self.err.hide()
        v.addWidget(self.err)

        self.btn = QtWidgets.QPushButton("Sign in")
        self.btn.setObjectName("Accent")
        self.btn.setMinimumHeight(34)
        self.btn.clicked.connect(self._submit)
        v.addWidget(self.btn)

        web = QtWidgets.QLabel(
            "Google/Discord create your account automatically. Prefer the website? "
            f"<a href='{self.s.api_base}/login.php' style='color:{theme.ACCENT};'>Open farever-pals.com</a>.")
        web.setObjectName("Muted")
        web.setWordWrap(True)
        web.setOpenExternalLinks(False)
        web.linkActivated.connect(lambda u: webbrowser.open(u))
        v.addWidget(web)

    def _provider_button(self, label: str, provider: str, *, bg: str, fg: str,
                         hover: str, icon=None) -> QtWidgets.QPushButton:
        b = QtWidgets.QPushButton(label)
        b.setMinimumHeight(36)
        b.setCursor(QtCore.Qt.PointingHandCursor)
        if icon is not None:
            b.setIcon(icon)
            b.setIconSize(QtCore.QSize(18, 18))
        # Don't let these grab the dialog's default-button highlight (that's what
        # painted the first one solid yellow); the password "Sign in" stays default.
        b.setAutoDefault(False)
        b.setDefault(False)
        b.setStyleSheet(
            f"QPushButton{{background:{bg};color:{fg};border:none;font-weight:600;}}"
            f"QPushButton:hover{{background:{hover};}}"
            f"QPushButton:disabled{{background:{bg};color:{fg}99;}}")
        b.clicked.connect(lambda: self._start_oauth(provider))
        return b

    def _set_oauth_busy(self, busy: bool, active: str = ""):
        for prov, btn in (("google", self.btn_google), ("discord", self.btn_discord)):
            btn.setEnabled(not busy)
            base = f"Continue with {prov.capitalize()}"
            btn.setText("Waiting for browser…" if (busy and prov == active) else base)
        self.user.setEnabled(not busy)
        self.pw.setEnabled(not busy)
        self.btn.setEnabled(not busy)

    def _start_oauth(self, provider: str):
        if self._oauth_worker is not None:
            return
        self.err.hide()
        self._set_oauth_busy(True, provider)
        self._oauth_worker = _OAuthWorker(self.s.api_base, provider)
        self._oauth_worker.done.connect(self._on_oauth_done)
        self._oauth_worker.start()

    def _on_oauth_done(self, res: dict):
        self._oauth_worker = None
        self._set_oauth_busy(False)
        if res.get("ok") and res.get("token"):
            self.result_token = res["token"]
            self.result_user = res.get("user", {})
            self.accept()
            return
        errs = {
            "timeout": "Timed out waiting for the browser. Try again.",
            "browser": "Could not open your browser. Sign in on the website instead.",
            "loopback": "Couldn't open the local sign-in port. Try again.",
            "unconfigured": "This sign-in method isn't enabled on the server yet.",
            "network": "Could not reach farever-pals.com. Check your connection.",
        }
        self._show_err(errs.get(res.get("error", ""), "Sign-in failed. Please try again."))

    def _submit(self):
        u, p = self.user.text().strip(), self.pw.text()
        if not u or not p:
            self._show_err("Enter your username and password.")
            return
        self.err.hide()
        self.btn.setEnabled(False)
        self.btn.setText("Signing in…")
        self._worker = _LoginWorker(self.s.api_base, u, p)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, res: dict):
        self.btn.setEnabled(True)
        self.btn.setText("Sign in")
        if res.get("ok") and res.get("token"):
            self.result_token = res["token"]
            self.result_user = res.get("user", {})
            self.accept()
            return
        errs = {
            "bad_credentials": "Wrong username or password.",
            "missing_credentials": "Enter your username and password.",
            "network": "Could not reach farever-pals.com. Check your connection.",
        }
        self._show_err(errs.get(res.get("error", ""), "Sign-in failed. Please try again."))

    def _show_err(self, msg: str):
        self.err.setText(msg)
        self.err.show()


class ControlPanel(QtWidgets.QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.s = settings
        self.proc: Proc | None = None
        self.model: LiveModel | None = None
        # key -> overlay widget (or None). 'crosshair' lives here too.
        self.overlays: dict[str, QtWidgets.QWidget | None] = {}
        # key -> [cards/toggles that mirror that overlay's open state]
        self._overlay_cards: dict[str, list] = {}
        self._worker: _LocateWorker | None = None
        # locate-progress feedback (the pure-read scan can take a while)
        self._locate_t0: float | None = None
        self._locate_timer = QtCore.QTimer(self)
        self._locate_timer.setInterval(500)
        self._locate_timer.timeout.connect(self._tick_locate)
        self._nav_items: dict[str, C.NavItem] = {}
        # friends + presence
        self._friends: list = []
        self._friends_worker: _FriendsWorker | None = None

        self.setWindowTitle("Farever Pal — by Escanor")
        self.setMinimumSize(1000, 640)
        self.resize(1180, 740)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        root.addWidget(self._build_main(), 1)

        self._select_nav("overlays")
        self._set_overlay_cards_enabled(False)   # gated until attached + located

        # system-wide hotkeys for loot-target selection (work while game-focused)
        from .hotkeys import GlobalHotkeys
        self.hotkeys = GlobalHotkeys(QtWidgets.QApplication.instance())
        self.hotkeys.triggered.connect(self._on_hotkey)
        self._register_hotkeys()

        # Friends presence: heartbeat + list refresh while signed in (the app
        # being open = "online (companion)"). Cheap; runs off the UI thread.
        self._friends_timer = QtCore.QTimer(self)
        self._friends_timer.setInterval(45_000)
        self._friends_timer.timeout.connect(self._friends_poll)
        self._refresh_friends_gating()

        # Auto-attach watcher: a cheap 2 s poll that attaches when Farever opens,
        # keeps trying to locate the player until they're in-world, and detaches
        # when the game closes — all via the existing manual paths (read-only).
        self._auto = AutoAttach()
        self._attach_busy = False
        self._located_shown = False        # has the UI applied the 'located' transition?
        self._attach_cooldown = 0          # ticks to skip after an auto-attach failure
        self._attach_timer = QtCore.QTimer(self)
        self._attach_timer.setInterval(2000)
        self._attach_timer.timeout.connect(self._attach_tick)
        self._attach_timer.start()
        self._refresh_attach_status()

        # Self-update: clear any leftover *.old from a prior update, then check
        # GitHub Releases (via the website) for a newer exe — off the UI thread.
        updater.cleanup_old()
        self._update_info = None
        self._update_check_worker: _UpdateCheckWorker | None = None
        self._update_dl_worker: _UpdateDownloadWorker | None = None
        self._updating = False
        if self.s.auto_check_updates and self.s.api_base:
            self._update_check_worker = _UpdateCheckWorker(self.s.api_base)
            self._update_check_worker.done.connect(self._on_update_found)
            self._update_check_worker.start()

        self.log(f"Read-only. Memory backend: {backend_name()}. Press Attach "
                 "(if another memory tool is running, unload it first to avoid "
                 "interference). The crosshair needs no attach.")

    def _brand_pixmap(self, size: int) -> QtGui.QPixmap:
        """The moustache brand mark for the header, tinted to the current accent on
        a transparent background. The bundled app_icon is opaque (moustache on a
        dark #0e0f13 square), so we derive an alpha mask from each pixel's distance
        to that background colour and refill it with the accent."""
        for name in ("app_icon.png", "app_icon.ico"):
            p = paths.assets_dir() / name
            if p.exists():
                img = QtGui.QImage(str(p))
                if not img.isNull():
                    return self._tint_brand(img, theme.ACCENT, size)
        return icons.ui_icon("radio", theme.ACCENT, size)

    @staticmethod
    def _tint_brand(img: QtGui.QImage, color: str, size: int) -> QtGui.QPixmap:
        scaled = img.convertToFormat(QtGui.QImage.Format_ARGB32).scaled(
            size, size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        accent = QtGui.QColor(color)
        ar, ag, ab = accent.red(), accent.green(), accent.blue()
        # If the source is already transparent (the moustache shape on a clear bg),
        # use its alpha as the mask; otherwise derive a mask from each pixel's
        # distance to the baked dark background (#0e0f13).
        transparent_src = scaled.pixelColor(0, 0).alpha() < 250
        br, bg, bb = 14, 15, 19
        out = QtGui.QImage(scaled.size(), QtGui.QImage.Format_ARGB32)
        out.fill(QtCore.Qt.transparent)
        for y in range(scaled.height()):
            for x in range(scaled.width()):
                c = scaled.pixelColor(x, y)
                if transparent_src:
                    a = c.alpha()
                else:
                    d = max(abs(c.red() - br), abs(c.green() - bg), abs(c.blue() - bb))
                    a = max(0, min(255, int(d * 1.6)))
                if a:
                    out.setPixelColor(x, y, QtGui.QColor(ar, ag, ab, a))
        return QtGui.QPixmap.fromImage(out)

    # --- sidebar ---------------------------------------------------------
    def _build_sidebar(self):
        bar = QtWidgets.QFrame()
        bar.setObjectName("Sidebar")
        bar.setFixedWidth(212)
        lay = QtWidgets.QVBoxLayout(bar)
        lay.setContentsMargins(14, 16, 14, 14)
        lay.setSpacing(4)

        brand = QtWidgets.QLabel("FAREVER PAL")
        brand.setObjectName("Brand")
        ver = QtWidgets.QLabel(f"by Escanor · v{__version__}")
        ver.setObjectName("Mono")
        lay.addWidget(brand)
        lay.addWidget(ver)
        # Update pill — hidden until the startup check finds a newer release.
        self._update_btn = QtWidgets.QPushButton("")
        self._update_btn.setObjectName("Accent")
        self._update_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._update_btn.setVisible(False)
        self._update_btn.clicked.connect(self._on_update_clicked)
        lay.addWidget(self._update_btn)
        lay.addSpacing(18)

        for key, icon, label in NAV:
            item = C.NavItem(key, icon, label)
            item.clicked.connect(self._select_nav)
            self._nav_items[key] = item
            lay.addWidget(item)

        lay.addStretch(1)

        cls_lbl = QtWidgets.QLabel("CURRENT CLASS")
        cls_lbl.setObjectName("FieldLabel")
        self.class_combo = QtWidgets.QComboBox()
        self.class_combo.addItems(CLASSES)
        self.class_combo.setCurrentText(self.s.player_class)
        self.class_combo.currentTextChanged.connect(lambda v: self._set("player_class", v))
        lay.addWidget(cls_lbl)
        lay.addWidget(self.class_combo)
        # (account moved to a top-right button in the top bar — keeps the sidebar uncluttered)
        return bar

    # --- web account (top-right button + dropdown) -----------------------
    def _build_account_host(self) -> QtWidgets.QWidget:
        """The container in the top bar that holds the account button; rebuilt by
        `_refresh_account_button` on sign in/out."""
        self._acct_host = QtWidgets.QWidget()
        self._acct_host.setStyleSheet("background:transparent;")   # else it paints a dark box
        self._acct_box = QtWidgets.QHBoxLayout(self._acct_host)
        self._acct_box.setContentsMargins(0, 0, 0, 0)
        self._acct_box.setSpacing(0)
        self._avatar_pm: QtGui.QPixmap | None = None
        self._avatar_url_loaded = ""
        self._avatar_loader: _AvatarLoader | None = None
        return self._acct_host

    def _refresh_account_button(self):
        while self._acct_box.count():
            it = self._acct_box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        if self.s.account_token and self.s.account_name:
            btn = QtWidgets.QPushButton(f"  {self.s.account_name}")
            btn.setObjectName("Outline")
            btn.setCursor(QtCore.Qt.PointingHandCursor)
            btn.setMinimumHeight(32)
            btn.setIcon(QtGui.QIcon(self._account_avatar_pixmap(20)))
            btn.setIconSize(QtCore.QSize(20, 20))
            btn.clicked.connect(self._show_account_menu)
            self._acct_btn = btn
            self._acct_box.addWidget(btn)
            self._load_avatar()              # async; swaps in the real image when ready
        else:
            btn = QtWidgets.QPushButton("SIGN IN")
            btn.setObjectName("Outline")
            btn.setCursor(QtCore.Qt.PointingHandCursor)
            btn.setMinimumHeight(32)
            btn.clicked.connect(self._open_login)
            self._acct_btn = btn
            self._acct_box.addWidget(btn)
        # Sign in/out flips auto-upload availability — refresh its gating + the
        # profile-build readout on the Speedrun tab.
        self._refresh_speedrun_gating()
        if hasattr(self, "_build_profile_lbl"):
            self._refresh_profile_build()
        self._refresh_friends_gating()

    def _account_avatar_pixmap(self, size: int) -> QtGui.QPixmap:
        """The downloaded avatar if we have it, else a sharp accent-tinted tile
        with the account's initial."""
        if self._avatar_pm is not None and not self._avatar_pm.isNull():
            return self._avatar_pm.scaled(size, size, QtCore.Qt.KeepAspectRatioByExpanding,
                                          QtCore.Qt.SmoothTransformation)
        pm = QtGui.QPixmap(size, size)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing, False)
        p.fillRect(0, 0, size, size, QtGui.QColor(theme.with_alpha(theme.ACCENT, 60)))
        p.setPen(QtGui.QColor(theme.ACCENT))
        p.drawRect(0, 0, size - 1, size - 1)
        initial = (self.s.account_name[:1] or "?").upper()
        f = QtGui.QFont(theme.UI_FONT, max(8, int(size * 0.5)))
        f.setBold(True)
        p.setFont(f)
        p.drawText(pm.rect(), QtCore.Qt.AlignCenter, initial)
        p.end()
        return pm

    def _load_avatar(self):
        url = self.s.account_avatar
        if not url or url == self._avatar_url_loaded:
            return
        self._avatar_url_loaded = url
        self._avatar_loader = _AvatarLoader(url)
        self._avatar_loader.done.connect(self._on_avatar_loaded)
        self._avatar_loader.start()

    def _on_avatar_loaded(self, pm):
        if pm is None or pm.isNull():
            return
        self._avatar_pm = pm
        if self.s.account_token and hasattr(self, "_acct_btn"):
            self._acct_btn.setIcon(QtGui.QIcon(self._account_avatar_pixmap(20)))

    def _show_account_menu(self):
        m = QtWidgets.QMenu(self)
        if self.s.account_code:
            act_prof = m.addAction("View profile on web")
            act_prof.triggered.connect(
                lambda: webbrowser.open(f"{self.s.api_base}/u.php?c={self.s.account_code}"))
        act_out = m.addAction("Sign out")
        act_out.triggered.connect(self._sign_out)
        m.exec(self._acct_btn.mapToGlobal(self._acct_btn.rect().bottomLeft()))

    def _avatar_url_for(self, user: dict) -> str:
        path = (user or {}).get("avatar_path")
        if not path:
            return ""
        from urllib.parse import quote
        return f"{self.s.api_base}/uploads/avatars/{quote(str(path))}"

    def _open_login(self):
        dlg = LoginDialog(self.s, self)
        if dlg.exec() == QtWidgets.QDialog.Accepted and dlg.result_user:
            self.s.account_name = dlg.result_user.get("username", "")
            self.s.account_code = dlg.result_user.get("public_id", "")
            self.s.account_token = dlg.result_token
            self.s.account_avatar = self._avatar_url_for(dlg.result_user)
            self.s.save()
            self._avatar_pm = None
            self._avatar_url_loaded = ""
            self._refresh_account_button()
            self.log(f"Signed in as {self.s.account_name}.")
            # Sync the highlight color from the account (if it has one).
            accent = dlg.result_user.get("accent_color")
            if accent:
                self._set_accent(accent)
            else:
                # push our current accent up to the account so it follows us.
                self._sync_accent_to_account()

    def _sign_out(self):
        self.s.account_name = ""
        self.s.account_code = ""
        self.s.account_token = ""
        self.s.account_avatar = ""
        self.s.save()
        self._avatar_pm = None
        self._avatar_url_loaded = ""
        self._refresh_account_button()
        self.log("Signed out.")

    def _sync_accent_to_account(self):
        if not self.s.account_token:
            return
        api = FareverAPI(self.s.api_base, self.s.account_token)
        accent = self.s.hud_accent
        threading.Thread(target=lambda: api.set_accent(accent), daemon=True).start()

    # --- main column -----------------------------------------------------
    def _build_main(self):
        col = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._build_topbar())
        self.stack = QtWidgets.QStackedWidget()
        self._pages = {}
        for key, _icon, _label in NAV:
            page = getattr(self, f"_page_{key}")()
            self._pages[key] = self.stack.addWidget(self._scroll(page)) \
                if key != "log" else self.stack.addWidget(page)
        v.addWidget(self.stack, 1)
        return col

    def _scroll(self, inner):
        sa = QtWidgets.QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QtWidgets.QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        sa.setWidget(inner)
        return sa

    def _build_topbar(self):
        bar = QtWidgets.QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(58)
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(18, 0, 18, 0)
        lay.setSpacing(10)
        self._logo = logo = QtWidgets.QLabel()
        logo.setFixedSize(22, 22)
        logo.setPixmap(self._brand_pixmap(22))
        title = QtWidgets.QLabel("Farever Pal")
        title.setObjectName("Title")
        lay.addWidget(logo)
        lay.addWidget(title)
        lay.addSpacing(8)
        self.status = QtWidgets.QLabel()
        self.status.setObjectName("Mono")
        lay.addWidget(self.status)
        # Indeterminate "busy" bar shown only while the pure-read locate scan is
        # running (it can take a while; the scan gives no real %, so it's a
        # marquee, not a percentage). Hidden whenever we're not actively locating.
        self._locate_bar = QtWidgets.QProgressBar()
        self._locate_bar.setRange(0, 0)            # indeterminate / marquee
        self._locate_bar.setTextVisible(False)
        self._locate_bar.setFixedSize(96, 6)
        self._locate_bar.setObjectName("LocateBar")
        self._locate_bar.setStyleSheet(
            f"QProgressBar{{background:{theme.PANEL};border:0;border-radius:0;}}"
            f"QProgressBar::chunk{{background:{theme.ACCENT};border-radius:0;}}")
        self._locate_bar.setVisible(False)
        lay.addWidget(self._locate_bar)
        lay.addStretch(1)
        # account button (sign in / avatar + name)
        lay.addWidget(self._build_account_host())
        # Attach/detach is fully automatic (watches for Farever) — no buttons or
        # toggle; the status label above is the single source of truth for state.
        self._refresh_account_button()
        self._set_status(False, "detached")
        return bar

    def _build_statusstrip(self):
        strip = QtWidgets.QFrame()
        strip.setObjectName("StatusStrip")
        strip.setFixedHeight(28)
        lay = QtWidgets.QHBoxLayout(strip)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(10)
        dot = QtWidgets.QFrame()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet(f"background:{theme.ACCENT};border:0;")
        self._strip_left = QtWidgets.QLabel()
        self._strip_left.setObjectName("Mono")
        right = QtWidgets.QLabel("DOCS   SUPPORT   API")
        right.setObjectName("Mono")
        lay.addWidget(dot)
        lay.addWidget(self._strip_left)
        lay.addStretch(1)
        lay.addWidget(right)
        self._refresh_strip()
        return strip

    def _refresh_strip(self):
        self._strip_left.setText(
            f"READ-ONLY · BACKEND: {backend_name().upper()} · "
            "PER-SKILL DPS: CALIBRATING")

    # --- nav -------------------------------------------------------------
    def _select_nav(self, key: str):
        order = [k for k, _, _ in NAV]
        for k, item in self._nav_items.items():
            item.setSelected(k == key)
        self.stack.setCurrentIndex(order.index(key))
        # Freshen friend presence/list when opening a page that shows it.
        if key in ("friends", "speedrun") and self.s.account_token:
            self._friends_poll()

    # ====================================================================
    #  Pages
    # ====================================================================
    def _register_card(self, key: str, card):
        self._overlay_cards.setdefault(key, []).append(card)

    def _page_container(self, title: str | None = None):
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(22, 20, 22, 20)
        v.setSpacing(16)
        return page, v

    # ---- Overlays -------------------------------------------------------
    def _page_overlays(self):
        page, v = self._page_container()
        cards = QtWidgets.QGridLayout()
        cards.setSpacing(12)
        from ..config import experimental_enabled
        specs = [
            ("entity", "layers", "Entity & Loot",
             "Nearby enemies, chests, and the closest drop table."),
            ("dps", "swords", "DPS Meter",
             "Big live self-DPS, survivability, recent cycles."),
            # Skill Breakdown is experimental (incomplete + slow to calibrate) — kept
            # out of release builds; FAREVER_EXPERIMENTAL=1 exposes it. See config.
            *([("skills", "layers", "Skill Breakdown",
                "Per-skill damage table — icons, real names, crit%. (experimental)")]
              if experimental_enabled() else []),
            ("map", "map", "Minimap",
             "Top-down POI radar of chests, foes, and gatherables."),
            ("crosshair", "crosshair", "Crosshair",
             "Custom aiming reticle drawn over the screen centre."),
            ("speedrun", "timer", "Speedrun",
             "Run timer — start by hotkey, auto-stops on boss kill."),
        ]
        for i, (key, icon, t, d) in enumerate(specs):
            card = C.OverlayCard(icon, t, d)
            card.toggled.connect(lambda on, k=key: self._request_overlay(k, on))
            self._register_card(key, card)
            cards.addWidget(card, i // 3, i % 3)
        v.addLayout(cards)

        # Global overlay settings (apply to every HUD; per-overlay config lives
        # on each overlay's own tab).
        v.addWidget(C.SectionHeader("Overlay Settings"))
        self.lock_toggle = C.LabeledToggle("Lock overlays (click-through + fixed position)",
                                           self.s.lock_overlays)
        self.lock_toggle.toggled.connect(self._set_lock)
        v.addWidget(self.lock_toggle)
        op = C.SliderRow("Overlay opacity", 30, 100, int(self.s.opacity * 100),
                         lambda x: f"{x}%")
        op.valueChanged.connect(self._set_opacity)
        v.addWidget(op)
        hl = C.ColorSwatch(self.s.hud_accent)
        hl.changed.connect(self._set_accent)
        v.addWidget(C.Field("Highlight color", hl))
        v.addStretch(1)
        return page

    # ---- Entity (entity & loot overlay config) --------------------------
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

    # --- global hotkeys --------------------------------------------------
    def _register_hotkeys(self):
        res = []
        for action, key in (("prev", self.s.hotkey_loot_prev),
                            ("next", self.s.hotkey_loot_next),
                            ("close", self.s.hotkey_loot_close),
                            ("sr_toggle", self.s.hotkey_speedrun_toggle),
                            ("sr_reset", self.s.hotkey_speedrun_reset)):
            ok = self.hotkeys.set_binding(action, key)
            res.append(f"{action}={key}" + ("" if ok else " (FAILED — combo taken?)"))
        self.log("Hotkeys: " + " · ".join(res))

    def _set_hotkey(self, attr, seq):
        self._set(attr, seq.toString())
        self._register_hotkeys()

    def _on_hotkey(self, action):
        if action in ("sr_toggle", "sr_reset"):
            ov = self.overlays.get("speedrun")
            if ov is None:
                self.log(f"Speedrun hotkey '{action}' — open the Speedrun overlay first.")
                return
            if action == "sr_toggle":
                ov.toggle()
                self.log("Speedrun: " + ("started" if ov.timer.state == "running"
                                         else f"stopped @ {ov.timer.elapsed():.2f}s"))
            else:
                ov.reset()
                self.log("Speedrun: reset")
            return
        ov = self.overlays.get("entity")
        if ov is None:
            self.log(f"Hotkey '{action}' — open the Entity overlay first.")
            return
        n = len(getattr(ov, "_enemies", [])) + len(getattr(ov, "_chests", []))
        self.log(f"Hotkey '{action}' ({n} targets)")
        if action == "prev":
            ov.select_prev()
        elif action == "next":
            ov.select_next()
        elif action == "close":
            ov.close_drops()

    def _set_entity_scale(self, v):
        sc = v / 100.0
        self._set("entity_scale", sc)
        ov = self.overlays.get("entity")
        if ov is not None and hasattr(ov, "set_scale"):
            ov.set_scale(sc)

    def _set_dps_scale(self, v):
        sc = v / 100.0
        self._set("dps_scale", sc)
        for key in ("dps", "skills"):      # the scale slider drives both DPS panels
            ov = self.overlays.get(key)
            if ov is not None and hasattr(ov, "apply_scale"):
                ov.apply_scale(sc)

    def _set_dps_mode(self, label):
        mode = {"Small": "small", "Medium": "medium", "Full": "default"}.get(label, "default")
        self._set("dps_mode", mode)
        ov = self.overlays.get("dps")
        if ov is not None and hasattr(ov, "apply_dps_mode"):
            ov.apply_dps_mode(mode)

    def _set_dps_columns(self):
        from .skill_table import COLUMN_ORDER
        keys = [k for k in COLUMN_ORDER
                if self._dps_col_toggles[k].isChecked()]
        self._set("dps_columns", keys)
        ov = self.overlays.get("skills")
        if ov is not None and hasattr(ov, "set_columns"):
            ov.set_columns(keys)

    def _set_overlay_cards_enabled(self, on: bool):
        """Overlay cards that need a live process (entity/dps/skills/map) are gated
        until attach+locate succeeds. Crosshair stays available (cosmetic)."""
        for key in ("entity", "dps", "skills", "map"):
            for card in self._overlay_cards.get(key, []):
                card.setEnabled(on)

    # ---- Combat / DPS ---------------------------------------------------
    def _page_combat(self):
        page, v = self._page_container()
        card = C.OverlayCard("swords", "Open DPS Meter",
                             "Big live DPS, sparkline, survivability, recent cycles.")
        card.toggled.connect(lambda on: self._request_overlay("dps", on))
        self._register_card("dps", card)
        v.addWidget(card)

        from ..config import experimental_enabled
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

        # --- per-skill table columns (Skill Breakdown panel) --- experimental only;
        # the columns configure the hidden Skill Breakdown panel, so skip the section
        # in release builds (keep the attribute so _set_dps_columns stays safe).
        self._dps_col_toggles = {}
        if experimental_enabled():
            from .skill_table import COLUMN_ORDER
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

    # ---- Loot -----------------------------------------------------------
    def _page_speedrun(self):
        page, v = self._page_container()

        # --- Timer ---
        v.addWidget(C.SectionHeader("Timer"))
        card = C.OverlayCard("timer", "Open Speedrun Timer",
                             "Stopwatch with auto-stop on the boss kill.")
        card.toggled.connect(lambda on: self._request_overlay("speedrun", on))
        self._register_card("speedrun", card)
        v.addWidget(card)
        sc = C.SliderRow("Overlay scale", 70, 200, int(self.s.speedrun_scale * 100),
                         lambda x: f"{x / 100:.2f}x")
        sc.valueChanged.connect(self._set_speedrun_scale)
        v.addWidget(sc)

        # --- Automation + upload ---
        v.addWidget(C.SectionHeader("Automation"))
        self._auto_toggle = C.LabeledToggle(
            "Auto Detect Boss Run", self.s.speedrun_auto)
        self._auto_toggle.toggled.connect(self._toggle_auto)
        v.addWidget(self._auto_toggle)

        self._auto_upload_toggle = C.LabeledToggle(
            "Auto-upload finished runs", self.s.speedrun_auto_upload)
        self._auto_upload_toggle.toggled.connect(self._toggle_auto_upload)
        v.addWidget(self._auto_upload_toggle)

        rearm = C.LabeledToggle(
            "Re-arm for back-to-back runs", self.s.speedrun_auto_rearm)
        rearm.toggled.connect(lambda on: self._set("speedrun_auto_rearm", on))
        v.addWidget(rearm)

        self._auto_upload_hint = QtWidgets.QLabel("")
        self._auto_upload_hint.setObjectName("Muted")
        self._auto_upload_hint.setWordWrap(True)
        v.addWidget(self._auto_upload_hint)
        self._refresh_speedrun_gating()

        mode_seg = C.SegmentedControl(
            ["Normal", "Hard"],
            "Hard" if self.s.speedrun_mode == "hard" else "Normal")
        mode_seg.currentChanged.connect(
            lambda t: self._set("speedrun_mode", "hard" if t == "Hard" else "normal"))
        f = C.Field("Difficulty fallback", mode_seg)
        f.setToolTip("Used when auto-detect is off or the boss level can't be read.")
        v.addWidget(f)

        # --- Build linked to uploaded runs ---
        v.addWidget(C.SectionHeader("Build"))
        self._build_profile_lbl = QtWidgets.QLabel("")
        self._build_profile_lbl.setObjectName("Muted")
        self._build_profile_lbl.setWordWrap(True)
        v.addWidget(self._build_profile_lbl)

        self._build_override_toggle = C.LabeledToggle(
            "Override build per run", self.s.speedrun_build_override_on)
        self._build_override_toggle.toggled.connect(self._toggle_build_override)
        v.addWidget(self._build_override_toggle)

        # Override input box (revealed only when the toggle is on).
        self._build_override_box = QtWidgets.QWidget()
        bob = QtWidgets.QVBoxLayout(self._build_override_box)
        bob.setContentsMargins(0, 0, 0, 0)
        bob.setSpacing(6)
        self._build_code_edit = QtWidgets.QLineEdit(self.s.speedrun_build_override or "")
        self._build_code_edit.setPlaceholderText("Build code, e.g. ABCD1234 (or paste a build link)")
        self._build_code_edit.setClearButtonEnabled(True)
        self._build_code_edit.textEdited.connect(self._on_build_code_edited)
        bob.addWidget(C.Field("Build code", self._build_code_edit))
        self._build_preview_lbl = QtWidgets.QLabel("")
        self._build_preview_lbl.setObjectName("Mono")
        self._build_preview_lbl.setWordWrap(True)
        bob.addWidget(self._build_preview_lbl)
        v.addWidget(self._build_override_box)
        self._build_override_box.setVisible(self.s.speedrun_build_override_on)

        self._build_lookup_worker: _ApiWorker | None = None
        self._refresh_profile_build()      # fetch + show the profile featured build
        if self.s.speedrun_build_override and self.s.speedrun_build_override_on:
            self._lookup_build(self.s.speedrun_build_override)

        # --- Co-runners (add friends to the run) ---
        self._corunner_head = C.SectionHeader("Co-runners")
        v.addWidget(self._corunner_head)
        co_intro = QtWidgets.QLabel(
            "Picked friends ride along on auto-uploaded runs with their own build.")
        co_intro.setObjectName("Muted")
        co_intro.setWordWrap(True)
        v.addWidget(co_intro)
        self._corunner_hint = QtWidgets.QLabel("")
        self._corunner_hint.setObjectName("Muted")
        self._corunner_hint.setWordWrap(True)
        v.addWidget(self._corunner_hint)
        self._corunner_host = QtWidgets.QWidget()
        self._corunner_box = QtWidgets.QVBoxLayout(self._corunner_host)
        self._corunner_box.setContentsMargins(0, 0, 0, 0)
        self._corunner_box.setSpacing(8)
        v.addWidget(self._corunner_host)
        self._render_corunners()

        # --- Hotkeys ---
        v.addWidget(C.SectionHeader("Hotkeys (global · work while in-game)"))
        hk = QtWidgets.QGridLayout()
        hk.setHorizontalSpacing(12)
        for i, (attr, label) in enumerate([
                ("hotkey_speedrun_toggle", "Start / Stop"),
                ("hotkey_speedrun_reset", "Reset")]):
            edit = QtWidgets.QKeySequenceEdit(QtGui.QKeySequence(getattr(self.s, attr)))
            try:
                edit.setMaximumSequenceLength(1)
            except (AttributeError, TypeError):
                pass
            edit.keySequenceChanged.connect(lambda seq, a=attr: self._set_hotkey(a, seq))
            hk.addWidget(C.Field(label, edit), 0, i)
        v.addLayout(hk)

        # --- Fair play ---
        fair = QtWidgets.QLabel(
            "Clean runs post instantly; suspicious times need a video. The "
            "companion is read-only.")
        fair.setObjectName("Muted")
        fair.setWordWrap(True)
        fair.setStyleSheet(f"color:{theme.GOLD};background:transparent;")
        v.addWidget(fair)

        clr = QtWidgets.QPushButton("Clear best times")
        clr.setObjectName("Outline")
        clr.clicked.connect(self._clear_speedrun_best)
        v.addWidget(clr)
        v.addStretch(1)
        return page

    def _corunner_row(self, f: dict) -> QtWidgets.QFrame:
        frame = QtWidgets.QFrame()
        frame.setObjectName("Cell")
        lay = QtWidgets.QHBoxLayout(frame)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)
        name = QtWidgets.QLabel(f.get("username", "?"))
        name.setStyleSheet(f"color:{theme.TEXT};background:transparent;")
        code = f.get("public_id", "")
        lay.addWidget(name, 1)
        lay.addWidget(self._presence_pill(f.get("presence", "offline")))
        tog = C.ToggleSwitch(code in (self.s.speedrun_corunners or []))
        tog.toggled.connect(lambda on, c=code: self._toggle_corunner(c, on))
        lay.addWidget(tog)
        return frame

    def _render_corunners(self):
        if not hasattr(self, "_corunner_box"):
            return
        while self._corunner_box.count():
            it = self._corunner_box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        signed = bool(self.s.account_token)
        if not signed:
            self._corunner_hint.setText("Sign in to pick friends as co-runners.")
            self._corunner_hint.setVisible(True)
            self._corunner_host.setVisible(False)
            return
        if not self._friends:
            self._corunner_hint.setText("No friends yet — add some on the Friends tab.")
            self._corunner_hint.setVisible(True)
            self._corunner_host.setVisible(False)
            return
        self._corunner_hint.setVisible(False)
        self._corunner_host.setVisible(True)
        for fr in self._friends:
            self._corunner_box.addWidget(self._corunner_row(fr))
        n = len(self.s.speedrun_corunners or [])
        self._corunner_head.set_tag(f"{n} PICKED" if n else "")

    def _toggle_corunner(self, code: str, on: bool):
        cur = list(self.s.speedrun_corunners or [])
        if on and code not in cur:
            cur.append(code)
        elif not on and code in cur:
            cur.remove(code)
        self._set("speedrun_corunners", cur)
        if hasattr(self, "_corunner_head"):
            n = len(cur)
            self._corunner_head.set_tag(f"{n} PICKED" if n else "")

    def _toggle_auto(self, on: bool):
        self._set("speedrun_auto", on)
        self._refresh_speedrun_gating()

    def _toggle_auto_upload(self, on: bool):
        self._set("speedrun_auto_upload", on)
        self._refresh_speedrun_gating()

    # --- build linked to runs -------------------------------------------
    def _toggle_build_override(self, on: bool):
        self._set("speedrun_build_override_on", on)
        self._build_override_box.setVisible(on)
        if on and self.s.speedrun_build_override:
            self._lookup_build(self.s.speedrun_build_override)

    @staticmethod
    def _build_code_from(text: str) -> str:
        """Accept a bare code or a pasted build link; pull out the code token."""
        s = (text or "").strip()
        if "/" in s:
            s = s.rstrip("/").split("/")[-1]
        if "=" in s:                       # ?b=CODE / ?code=CODE
            s = s.split("=")[-1]
        return s.strip().upper()

    def _on_build_code_edited(self, text: str):
        code = self._build_code_from(text)
        self._set("speedrun_build_override", code)
        if not code:
            self._build_preview_lbl.setText("")
            return
        self._build_preview_lbl.setText("resolving…")
        self._build_preview_lbl.setStyleSheet(f"color:{theme.MUTED};background:transparent;")
        self._lookup_build(code)

    def _lookup_build(self, code: str):
        if not self.s.account_token:
            self._build_preview_lbl.setText("sign in to resolve build codes")
            self._build_preview_lbl.setStyleSheet(f"color:{theme.MUTED};background:transparent;")
            return
        if self._build_lookup_worker is not None and self._build_lookup_worker.isRunning():
            return
        self._build_lookup_worker = _ApiWorker(
            self.s.api_base, self.s.account_token,
            lambda api, c=code: api.lookup_build(c), tag=code)
        self._build_lookup_worker.done.connect(self._on_build_lookup)
        self._build_lookup_worker.start()

    def _on_build_lookup(self, tag: str, res: dict):
        self._build_lookup_worker = None
        # Ignore a stale result if the field moved on since the request fired.
        if tag != (self.s.speedrun_build_override or "").strip().upper():
            return
        if res.get("ok"):
            cls = res.get("class")
            who = " · yours" if res.get("mine") else ""
            self._build_preview_lbl.setText(
                f"✓ {res.get('title') or res.get('code')}"
                + (f"  ({cls}{who})" if cls or who else ""))
            self._build_preview_lbl.setStyleSheet(f"color:{theme.GOOD};background:transparent;")
        else:
            self._build_preview_lbl.setText("✗ no build with that code")
            self._build_preview_lbl.setStyleSheet(f"color:{theme.GOLD};background:transparent;")

    def _refresh_profile_build(self):
        """Show the profile's featured build (the default linked to uploads)."""
        if not hasattr(self, "_build_profile_lbl"):
            return
        if not self.s.account_token:
            self._build_profile_lbl.setText("Sign in to link your profile build.")
            return
        self._build_profile_lbl.setText("Profile build: loading…")
        w = _ApiWorker(self.s.api_base, self.s.account_token, lambda api: api.me(), tag="me")
        w.done.connect(self._on_profile_build)
        w.start()
        self._me_worker = w   # keep a ref so it isn't GC'd mid-flight

    def _on_profile_build(self, tag: str, res: dict):
        if not hasattr(self, "_build_profile_lbl"):
            return
        fb = (res.get("user") or {}).get("featured_build") if res.get("ok") else None
        if fb:
            cls = fb.get("class")
            self._build_profile_lbl.setText(
                f"Profile build: {fb.get('title') or fb.get('code')}"
                + (f"  ({cls})" if cls else ""))
        else:
            self._build_profile_lbl.setText(
                "No featured build set. Override per run below, or set one on the site.")

    def _refresh_speedrun_gating(self):
        """Auto-upload is only usable when Auto Detect Boss Run is on AND signed in."""
        if not hasattr(self, "_auto_upload_toggle"):
            return
        logged_in = bool(self.s.account_token)
        self._auto_upload_toggle.setEnabled(bool(self.s.speedrun_auto) and logged_in)
        if not self.s.speedrun_auto:
            hint = "Turn on Auto Detect Boss Run to enable auto-upload."
        elif not logged_in:
            hint = "Sign in to auto-upload, or upload each run from the timer."
        else:
            hint = ""
        self._auto_upload_hint.setText(hint)
        self._auto_upload_hint.setVisible(bool(hint))

    def _set_speedrun_scale(self, vv):
        sc = vv / 100.0
        self._set("speedrun_scale", sc)
        ov = self.overlays.get("speedrun")
        if ov is not None and hasattr(ov, "apply_scale"):
            ov.apply_scale(sc)

    def _clear_speedrun_best(self):
        self.s.speedrun_best = {}
        self.s.save()
        ov = self.overlays.get("speedrun")
        if ov is not None:
            ov._render()
        self.log("Speedrun best times cleared.")

    # ---- Friends --------------------------------------------------------
    def _page_friends(self):
        page, v = self._page_container()
        v.addWidget(C.SectionHeader("Friends", tag="ONLINE STATUS"))

        intro = QtWidgets.QLabel(
            "Your friends and who's online. Add them on the website.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        v.addWidget(intro)

        # --- signed-out prompt ---
        self._friends_signedout = QtWidgets.QFrame()
        self._friends_signedout.setObjectName("Card")
        so = QtWidgets.QVBoxLayout(self._friends_signedout)
        so.setContentsMargins(16, 16, 16, 16)
        so.setSpacing(10)
        so_lbl = QtWidgets.QLabel("Sign in to your Farever Pal account to see your "
                                  "friends and share your online status.")
        so_lbl.setObjectName("Muted")
        so_lbl.setWordWrap(True)
        so_btn = QtWidgets.QPushButton("Sign in")
        so_btn.setObjectName("Accent")
        so_btn.setMinimumHeight(32)
        so_btn.setCursor(QtCore.Qt.PointingHandCursor)
        so_btn.clicked.connect(self._open_login)
        so.addWidget(so_lbl)
        so.addWidget(so_btn, 0, QtCore.Qt.AlignLeft)
        v.addWidget(self._friends_signedout)

        # --- signed-in content ---
        self._friends_main = QtWidgets.QWidget()
        fm = QtWidgets.QVBoxLayout(self._friends_main)
        fm.setContentsMargins(0, 0, 0, 0)
        fm.setSpacing(12)

        self._share_toggle = C.LabeledToggle(
            "Share my online status with friends", self.s.share_presence)
        self._share_toggle.toggled.connect(self._toggle_share_presence)
        fm.addWidget(self._share_toggle)

        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(8)
        self._friends_refresh_btn = QtWidgets.QPushButton("Refresh")
        self._friends_refresh_btn.setObjectName("Outline")
        self._friends_refresh_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._friends_refresh_btn.clicked.connect(lambda: self._friends_poll(force=True))
        manage = QtWidgets.QPushButton("Manage on web")
        manage.setObjectName("Outline")
        manage.setCursor(QtCore.Qt.PointingHandCursor)
        manage.clicked.connect(lambda: webbrowser.open(f"{self.s.api_base}/friends.php"))
        actions.addWidget(self._friends_refresh_btn)
        actions.addWidget(manage)
        actions.addStretch(1)
        self._friends_pending = QtWidgets.QLabel("")
        self._friends_pending.setObjectName("Mono")
        self._friends_pending.setStyleSheet(
            f"color:{theme.GOLD};background:transparent;")
        actions.addWidget(self._friends_pending)
        fm.addLayout(actions)

        fm.addWidget(C.SectionHeader("Friend list"))
        self._friends_list_host = QtWidgets.QWidget()
        self._friends_list_box = QtWidgets.QVBoxLayout(self._friends_list_host)
        self._friends_list_box.setContentsMargins(0, 0, 0, 0)
        self._friends_list_box.setSpacing(8)
        fm.addWidget(self._friends_list_host)

        self._friends_empty = QtWidgets.QLabel(
            "No friends yet — add some on the website with their friend code.")
        self._friends_empty.setObjectName("Muted")
        self._friends_empty.setWordWrap(True)
        fm.addWidget(self._friends_empty)

        v.addWidget(self._friends_main)
        v.addStretch(1)
        self._render_friends()
        return page

    def _presence_pill(self, state: str) -> QtWidgets.QWidget:
        color, label = {
            "companion": (theme.GOOD, "In-game"),
            "web": (theme.ACCENT, "Online (web)"),
        }.get(state, (theme.MUTED, "Offline"))
        w = QtWidgets.QWidget()
        w.setStyleSheet("background:transparent;")
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        dot = QtWidgets.QFrame()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet(f"background:{color};border:0;")
        lbl = QtWidgets.QLabel(label)
        lbl.setStyleSheet(
            f"color:{color};font-family:'{theme.MONO_FONT}','Consolas';"
            "font-size:11px;background:transparent;")
        lay.addWidget(dot)
        lay.addWidget(lbl)
        return w

    def _initial_tile(self, name: str, size: int) -> QtGui.QPixmap:
        pm = QtGui.QPixmap(size, size)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing, False)
        fill = QtGui.QColor(theme.ACCENT); fill.setAlpha(38)
        edge = QtGui.QColor(theme.ACCENT); edge.setAlpha(120)
        p.fillRect(0, 0, size, size, fill)
        p.setPen(edge)
        p.drawRect(0, 0, size - 1, size - 1)
        p.setPen(QtGui.QColor(theme.ACCENT))
        f = QtGui.QFont(theme.UI_FONT)
        f.setBold(True)
        f.setPointSize(max(8, int(size * 0.4)))
        p.setFont(f)
        p.drawText(pm.rect(), QtCore.Qt.AlignCenter, (name[:1] or "?").upper())
        p.end()
        return pm

    def _friend_row(self, f: dict) -> QtWidgets.QFrame:
        frame = QtWidgets.QFrame()
        frame.setObjectName("Cell")
        lay = QtWidgets.QHBoxLayout(frame)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)
        av = QtWidgets.QLabel()
        av.setFixedSize(34, 34)
        av.setPixmap(self._initial_tile(f.get("username", ""), 34))
        col = QtWidgets.QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        name = QtWidgets.QLabel(f.get("username", "?"))
        name.setStyleSheet(f"color:{theme.TEXT};font-weight:600;background:transparent;")
        code = QtWidgets.QLabel(f.get("public_id", ""))
        code.setObjectName("Mono")
        code.setStyleSheet(
            f"color:{theme.MUTED};font-family:'{theme.MONO_FONT}','Consolas';"
            "font-size:10px;background:transparent;")
        col.addWidget(name)
        col.addWidget(code)
        lay.addWidget(av)
        lay.addLayout(col, 1)
        lay.addWidget(self._presence_pill(f.get("presence", "offline")))
        view = QtWidgets.QPushButton("View")
        view.setObjectName("Outline")
        view.setCursor(QtCore.Qt.PointingHandCursor)
        code_str = f.get("public_id", "")
        view.clicked.connect(lambda _=False, c=code_str: webbrowser.open(f"{self.s.api_base}/u.php?c={c}"))
        lay.addWidget(view)
        return frame

    def _render_friends(self):
        if not hasattr(self, "_friends_main"):
            return
        signed = bool(self.s.account_token)
        self._friends_signedout.setVisible(not signed)
        self._friends_main.setVisible(signed)
        if not signed:
            return
        # rebuild the friend rows
        while self._friends_list_box.count():
            it = self._friends_list_box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        for fr in self._friends:
            self._friends_list_box.addWidget(self._friend_row(fr))
        self._friends_empty.setVisible(not self._friends)
        self._friends_list_host.setVisible(bool(self._friends))

    def _refresh_friends_gating(self):
        """Start/stop presence polling with sign-in state; refresh the page."""
        if not hasattr(self, "_friends_timer"):
            return
        if self.s.account_token:
            if not self._friends_timer.isActive():
                self._friends_timer.start()
            self._friends_poll(force=True)
        else:
            self._friends_timer.stop()
            self._friends = []
        self._render_friends()
        self._render_corunners()

    def _friends_poll(self, force: bool = False):
        if not self.s.account_token:
            return
        if self._friends_worker is not None and self._friends_worker.isRunning():
            return
        self._friends_worker = _FriendsWorker(
            self.s.api_base, self.s.account_token, bool(self.s.share_presence))
        self._friends_worker.done.connect(self._on_friends_done)
        self._friends_worker.start()

    def _on_friends_done(self, res: dict):
        self._friends_worker = None
        if not res.get("ok"):
            return
        self._friends = res.get("friends") or []
        n = int(res.get("incoming_count") or 0)
        if hasattr(self, "_friends_pending"):
            self._friends_pending.setText(
                f"{n} pending request{'s' if n != 1 else ''}" if n else "")
        self._render_friends()
        self._render_corunners()

    def _toggle_share_presence(self, on: bool):
        self._set("share_presence", on)
        self._friends_poll(force=True)   # push the new flag to the server now

    def _page_loot(self):
        page, v = self._page_container()
        head = QtWidgets.QHBoxLayout()
        h1 = QtWidgets.QLabel("DROP SIMULATION")
        h1.setObjectName("H1")
        tag = QtWidgets.QLabel("OFFLINE PREDICTOR")
        tag.setObjectName("Mono")
        tag.setStyleSheet(
            f"color:{theme.ACCENT};background:{theme.with_alpha(theme.ACCENT,30)};"
            f"border:1px solid {theme.with_alpha(theme.ACCENT,90)};border-radius:4px;"
            "padding:3px 8px;font-weight:700;")
        head.addWidget(h1)
        head.addStretch(1)
        head.addWidget(tag)
        v.addLayout(head)
        sub = QtWidgets.QLabel(
            "Deterministic expansion of any loot table to per-item drop probability.")
        sub.setObjectName("Muted")
        v.addWidget(sub)

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(12)
        self.table_combo = QtWidgets.QComboBox()
        try:
            ids = loot.table_ids()
        except Exception as e:
            ids = []
            self.log(f"loot data error: {e}")
        for tid in ids:
            self.table_combo.addItem(names.loot_table_label(tid), tid)
        default_ix = self.table_combo.findData("WorldCrate")
        if default_ix >= 0:
            self.table_combo.setCurrentIndex(default_ix)
        controls.addWidget(C.Field("Loot source", self.table_combo), 1)
        self.level_stepper = C.Stepper(self.s.level, 1, 60)
        controls.addWidget(C.Field("Level", self.level_stepper))
        btn = QtWidgets.QPushButton("Predict")
        btn.setObjectName("Accent")
        btn.clicked.connect(self.predict)
        controls.addWidget(C.Field(" ", btn))
        v.addLayout(controls)

        v.addWidget(C.SectionHeader("Drop Table Results"))
        self.loot_caption = QtWidgets.QLabel("")
        self.loot_caption.setObjectName("Mono")
        v.addWidget(self.loot_caption)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["PROB.", "ITEM", "RARITY", "TYPE"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(220)
        v.addWidget(self.table, 1)

        footer = QtWidgets.QHBoxLayout()
        footer.setSpacing(12)
        footer.addWidget(C.InfoCard("archive", "Data",
                                    f"CDB sheets (offline, {len(ids)} tables)"), 1)
        footer.addWidget(C.InfoCard("dice-5", "Predictor",
                                    "Deterministic recursive expansion", theme.ORANGE), 1)
        v.addLayout(footer)
        return page

    # ---- Crosshair ------------------------------------------------------
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

    # ---- Map ------------------------------------------------------------
    def _page_map(self):
        page, v = self._page_container()
        card = C.OverlayCard("map", "Open Minimap",
                             "Top-down POI radar centred on the player.")
        card.toggled.connect(lambda on: self._request_overlay("map", on))
        self._register_card("map", card)
        v.addWidget(card)

        v.addWidget(C.SectionHeader("Shape"))
        shape = C.SegmentedControl(["Circle", "Square"], self.s.minimap_shape)
        shape.currentChanged.connect(self._set_minimap_shape)
        v.addWidget(shape)
        rot = C.LabeledToggle("Rotate with player heading", self.s.minimap_rotate)
        rot.toggled.connect(self._set_minimap_rotate)
        v.addWidget(rot)
        tex = C.LabeledToggle("World map texture", self.s.minimap_texture)
        tex.toggled.connect(self._set_minimap_texture)
        v.addWidget(tex)
        bare = C.LabeledToggle("Bare map (no panel/titlebar)", self.s.minimap_bare)
        bare.toggled.connect(self._set_minimap_bare)
        v.addWidget(bare)
        ico = C.LabeledToggle("POI icons (else dots)", self.s.minimap_icons)
        ico.toggled.connect(lambda on: (self._set("minimap_icons", on), self._touch_minimap()))
        v.addWidget(ico)

        zoom = C.SliderRow("Zoom", 2, 60, int(self.s.minimap_zoom), str)
        zoom.valueChanged.connect(self._set_minimap_zoom)
        v.addWidget(zoom)
        szrow = QtWidgets.QGridLayout()
        szrow.setHorizontalSpacing(12)
        size = C.Stepper(self.s.minimap_size, 160, 640, 20)
        size.valueChanged.connect(self._set_minimap_size)
        isz = C.Stepper(self.s.minimap_icon_size, 8, 40, 2)
        isz.valueChanged.connect(lambda v_: (self._set("minimap_icon_size", v_), self._touch_minimap()))
        szrow.addWidget(C.Field("Size (px)", size), 0, 0)
        szrow.addWidget(C.Field("POI icon (px)", isz), 0, 1)
        v.addLayout(szrow)

        v.addWidget(C.SectionHeader("Layers"))
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(12)
        for i, (attr, label) in enumerate([
                ("minimap_enemies", "Enemies"),
                ("minimap_chests", "Chests / loot"),
                ("minimap_gatherables", "Gatherables"),
                ("minimap_obelisks", "Obelisks")]):
            t = C.LabeledToggle(label, getattr(self.s, attr))
            t.toggled.connect(lambda on, a=attr: self._set_minimap_layer(a, on))
            grid.addWidget(t, i // 2, i % 2)
        v.addLayout(grid)

        note = QtWidgets.QLabel("Right-click a marker to mark it done (persists).")
        note.setObjectName("Muted")
        v.addWidget(note)
        v.addStretch(1)
        return page

    # ---- Log ------------------------------------------------------------
    def _page_log(self):
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(22, 20, 22, 20)
        v.setSpacing(10)
        v.addWidget(C.SectionHeader("Activity Log"))
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        v.addWidget(self.log_view, 1)
        return page

    # ====================================================================
    #  Settings sync
    # ====================================================================
    def _set(self, attr, value):
        setattr(self.s, attr, value)
        self.s.save()

    def _set_accent(self, color):
        """Highlight color: re-tint the whole app live — the control panel and
        every open overlay. Updates the global accent + QSS, re-applies it, and
        refreshes the widgets that paint the accent directly (not via QSS)."""
        self.s.hud_accent = color
        self.s.save()
        theme.set_accent(color)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.QSS)        # control panel + inheriting overlays
        self._restyle_accent()                  # painted/captured-color widgets
        for ov in self.overlays.values():
            if ov is not None and hasattr(ov, "apply_accent"):
                ov.apply_accent(color)
        self._sync_accent_to_account()          # keep the web account's accent in sync

    def _restyle_accent(self):
        """Refresh control-panel widgets that hold the accent as a captured value
        or paint it directly (QSS re-apply alone doesn't repaint these)."""
        from .components import (SectionHeader, Stepper, NavItem,
                                 SegmentedControl, OverlayCard, SliderRow)
        for sh in self.findChildren(SectionHeader):
            sh.set_color(theme.ACCENT)          # control-panel headers are all accent
        # widgets that captured the accent at construction → re-tint each kind
        for cls in (Stepper, NavItem, SegmentedControl, OverlayCard, SliderRow):
            for w in self.findChildren(cls):
                w.restyle()
        if hasattr(self, "_logo"):
            self._logo.setPixmap(self._brand_pixmap(22))
        # the account button's avatar placeholder tile is accent-tinted too
        if self.s.account_token and getattr(self, "_avatar_pm", None) is None \
                and hasattr(self, "_acct_btn"):
            self._acct_btn.setIcon(QtGui.QIcon(self._account_avatar_pixmap(20)))
        for w in self.findChildren(QtWidgets.QWidget):
            w.update()                          # repaint live painters (toggles, etc.)

    def _set_opacity(self, v):
        self.s.opacity = v / 100.0
        self.s.save()
        for key in HUD_OVERLAYS:
            ov = self.overlays.get(key)
            if ov and ov.isVisible():
                ov.set_opacity(self.s.opacity)   # forwards to child windows (drop table)

    def _set_crosshair(self, attr, value):
        setattr(self.s, attr, value)
        self.s.save()
        if hasattr(self, "ch_preview"):
            self.ch_preview.update()
        ov = self.overlays.get("crosshair")
        if ov is not None:
            ov.apply_settings()

    def _set_lock(self, on):
        self.s.lock_overlays = on
        self.s.save()
        for key in HUD_OVERLAYS:
            ov = self.overlays.get(key)
            if ov is not None and hasattr(ov, "set_locked"):
                ov.set_locked(on)
        self.log("Overlays LOCKED (click-through)." if on
                 else "Overlays unlocked (interactive).")

    def _set_minimap_shape(self, shape):
        self._set("minimap_shape", shape)
        self._touch_minimap()

    def _set_minimap_rotate(self, on):
        self._set("minimap_rotate", on)
        self._touch_minimap()

    def _set_minimap_texture(self, on):
        self._set("minimap_texture", on)
        self._touch_minimap()

    def _set_minimap_bare(self, on):
        self._set("minimap_bare", on)
        ov = self.overlays.get("map")
        if ov is not None and hasattr(ov, "set_bare"):
            ov.set_bare(on)

    def _set_minimap_zoom(self, v):
        self._set("minimap_zoom", float(v))
        self._touch_minimap()

    def _set_minimap_size(self, v):
        self._set("minimap_size", v)
        ov = self.overlays.get("map")
        if ov is not None:
            ov.resize(v, v + 40)

    def _touch_minimap(self):
        ov = self.overlays.get("map")
        if ov is not None and hasattr(ov, "canvas"):
            ov.canvas.update()

    def _set_minimap_layer(self, attr, on):
        """Toggle a minimap POI layer + re-scan POIs now (don't wait for the
        slow tick) so the change is immediate."""
        self._set(attr, on)
        ov = self.overlays.get("map")
        if ov is not None and hasattr(ov, "canvas"):
            ov.canvas.refresh()

    # ====================================================================
    #  Process + overlays
    # ====================================================================
    def log(self, msg: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        if hasattr(self, "log_view"):
            self.log_view.appendPlainText(f"{ts}  {msg}")

    def _set_status(self, attached: bool, text: str):
        dot = theme.GOOD if attached else theme.NEUTRAL
        self.status.setText(f"●  {text}")
        self.status.setStyleSheet(
            f"color:{dot};font-family:'{theme.MONO_FONT}','Consolas';font-size:11px;")

    # --- locate progress feedback ---------------------------------------
    def _start_locate(self):
        """Show the busy bar + start the elapsed-time counter for a locate scan."""
        if self._locate_t0 is None:
            self._locate_t0 = time.monotonic()
        self._locate_bar.setVisible(True)
        if not self._locate_timer.isActive():
            self._locate_timer.start()
        self._tick_locate()

    def _stop_locate(self):
        """Hide the busy bar + stop the counter (locate finished or detached)."""
        self._locate_t0 = None
        self._locate_timer.stop()
        self._locate_bar.setVisible(False)

    def _tick_locate(self):
        if self._locate_t0 is None or self.proc is None:
            return
        el = time.monotonic() - self._locate_t0
        self._set_status(True, f"attached PID {self.proc.pid} · locating player… ({el:.0f}s)")

    def attach(self, auto: bool = False):
        self._attach_busy = True
        try:
            self.proc = Proc.attach()
        except ProcError as e:
            self.proc = None
            self._attach_busy = False
            if auto:
                # Don't yank the user to the Log tab or spam: one line, then back
                # off a few ticks (e.g. another memory tool conflicting).
                self._attach_cooldown = 5
                self.log(f"Auto-attach deferred: {e} (retrying shortly).")
                self._refresh_attach_status()
            else:
                self.log(f"ATTACH FAILED: {e}")
                self._select_nav("log")
            return
        self.model = LiveModel(self.proc)
        self._located_fail_logged = False
        self._set_status(True, f"attached PID {self.proc.pid} · locating player…")
        self._start_locate()
        self.log(f"Attached PID {self.proc.pid}. {len(self.model.chests)} chests. "
                 "Locating the player by scanning memory (read-only)…")
        self._worker = _LocateWorker(self.model)
        self._worker.done.connect(self._on_located)
        self._worker.start()

    def _on_located(self, result):
        self._attach_busy = False
        if isinstance(result, Exception):
            self._stop_locate()
            self.log(f"Locate failed: {result}")
            self._set_status(True, "attached · player NOT found")
            return
        if not result:
            # The watcher's RELOCATE retries each tick until the player loads in;
            # log once so menu time doesn't fill the log. The slow scan is done
            # (type is cached now), so hide the busy bar; retries are cheap.
            self._stop_locate()
            if not getattr(self, "_located_fail_logged", False):
                self.log("Player not found. Be fully loaded in-world (auto-retrying).")
                self._located_fail_logged = True
            self._set_status(True, "attached · waiting for world")
            return
        self._stop_locate()
        self._located_shown = True
        self._set_status(True, f"attached PID {self.proc.pid} · player @ {result:#x}")
        self._set_overlay_cards_enabled(True)
        self.log(f"Player located @ {result:#x}. Overlays unlocked.")

    # --- auto-attach watcher --------------------------------------------
    def _attach_tick(self):
        """2 s poll: attach/locate/detach with the game, via the manual paths."""
        if backend_name() == "none":
            return
        if self._attach_cooldown > 0:
            self._attach_cooldown -= 1
            return
        try:
            pid = find_pid()
        except Exception:
            return
        act = self._auto.decide(
            enabled=self.s.auto_attach,
            running_pid=pid,
            attached_pid=(self.proc.pid if self.proc else None),
            located=bool(self.model and self.model.player_addr is not None),
            busy=self._attach_busy,
        )
        if act is Act.ATTACH:
            self.attach(auto=True)
        elif act is Act.DETACH:
            self._auto_detach()
        elif act is Act.RELOCATE:
            self._relocate()
        if act is Act.NONE:
            self._refresh_attach_status()
        # `player_addr` can become set between locate workers, right as decide()
        # flips to NONE (it stops issuing RELOCATE once located). The
        # 'located' UI transition normally only fires from a worker's _on_located,
        # so without this reconciliation the top bar can stay stuck at "waiting for
        # world" while the player IS in fact located. Apply the transition once.
        if self.proc is not None and not self._attach_busy:
            pa = self.model.player_addr if self.model else None
            if pa is not None and not self._located_shown:
                self._on_located(pa)
            elif pa is None and self._located_shown:
                self._located_shown = False
                self._set_overlay_cards_enabled(False)

    def _relocate(self):
        """Quietly re-run the locate worker (attached but not yet in-world)."""
        if self.model is None or self._attach_busy:
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self._attach_busy = True
        self._worker = _LocateWorker(self.model)
        self._worker.done.connect(self._on_located)
        self._worker.start()

    def _auto_detach(self):
        """Detach because the game closed/restarted; the watcher re-attaches
        automatically when it relaunches."""
        self.detach()
        self.log("Game closed — detached. Watching for Farever…")
        self._refresh_attach_status()

    def _refresh_attach_status(self):
        """When detached, reflect the watcher state in the top-bar status line.

        Auto-attach is always on (no UI toggle); `auto_attach` survives as a
        hidden settings.json escape hatch — if a user sets it false there, the
        watcher stops attaching and we just read "detached"."""
        if self.proc is not None:
            return
        if self.s.auto_attach and backend_name() != "none":
            self._set_status(False, "watching for Farever…")
        else:
            self._set_status(False, "detached")

    # --- self-update ----------------------------------------------------
    def _on_update_found(self, info):
        if info is None:
            return
        self._update_info = info
        self._update_btn.setText(f"↑  Update to v{info.version}")
        self._update_btn.setEnabled(True)
        self._update_btn.setVisible(True)
        self.log(f"Update available: v{__version__} → v{info.version}. "
                 "Click the sidebar button to install.")

    def _on_update_clicked(self):
        info = self._update_info
        if info is None or self._updating:
            return
        # From source (dev) the self-swap can't work — just open the release page.
        if not updater.is_frozen():
            import webbrowser
            webbrowser.open(info.html_url or f"{self.s.api_base}/download.php")
            return
        notes = (info.notes or "").strip()
        if len(notes) > 600:
            notes = notes[:600] + "…"
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Update Farever Pal")
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setText(f"Update from v{__version__} to v{info.version}?\n\n"
                    "The app will download the new version, restart, and reconnect "
                    "automatically.")
        if notes:
            box.setInformativeText(notes)
        box.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        box.setDefaultButton(QtWidgets.QMessageBox.Yes)
        if box.exec() != QtWidgets.QMessageBox.Yes:
            return
        self._updating = True
        self._update_btn.setEnabled(False)
        self._update_btn.setText("Downloading…  0%")
        self.log(f"Downloading v{info.version}…")
        self._update_dl_worker = _UpdateDownloadWorker(info)
        self._update_dl_worker.progress.connect(self._on_update_progress)
        self._update_dl_worker.done.connect(self._on_update_downloaded)
        self._update_dl_worker.start()

    def _on_update_progress(self, pct: int):
        self._update_btn.setText(f"Downloading…  {pct}%")

    def _on_update_downloaded(self, result):
        if isinstance(result, Exception):
            self._updating = False
            self._update_btn.setEnabled(True)
            self._update_btn.setText(f"↑  Update to v{self._update_info.version} (retry)")
            self.log(f"Update failed: {result}")
            QtWidgets.QMessageBox.warning(
                self, "Update failed",
                f"Couldn't install the update:\n{result}\n\n"
                "You can retry, or download it from the website.")
            return
        # Staged successfully: swap the exe, relaunch, quit.
        self.log(f"Installing v{self._update_info.version} and restarting…")
        try:
            self.detach()                       # release the game cleanly first
            updater.apply_and_relaunch(result)
        except Exception as e:
            self._updating = False
            self._update_btn.setEnabled(True)
            self.log(f"Update install failed: {e}")
            QtWidgets.QMessageBox.warning(self, "Update failed", str(e))
            return
        QtWidgets.QApplication.quit()

    def _make_overlay(self, key: str):
        if key == "crosshair":
            return CrosshairOverlay(self.s)
        cls = {"entity": EntityOverlay, "dps": DpsOverlay, "skills": SkillOverlay,
               "map": MinimapOverlay, "speedrun": SpeedrunOverlay}[key]
        ov = cls(self.model, self.s)
        if hasattr(ov, "request_config"):
            ov.request_config.connect(self._raise_self)
        return ov

    def _raise_self(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _request_overlay(self, key: str, on: bool):
        if not on:
            ov = self.overlays.get(key)
            if ov is not None:
                ov.close()      # WA_DeleteOnClose -> _on_overlay_closed
            return
        if key != "crosshair" and (self.model is None or self.model.player_addr is None):
            self.log("Attach and locate the player first.")
            self._sync_cards(key)   # revert the toggle
            return
        ov = self.overlays.get(key)
        if ov is not None and ov.isVisible():
            return
        ov = self._make_overlay(key)
        ov.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        ov.destroyed.connect(lambda *_: self._on_overlay_closed(key))
        ov.show()
        if self.s.lock_overlays and hasattr(ov, "set_locked"):
            ov.set_locked(True)
        self.overlays[key] = ov
        self._sync_cards(key)
        self.log(f"Opened {key} overlay.")

    def _on_overlay_closed(self, key: str):
        self.overlays[key] = None
        self._sync_cards(key)

    def _sync_cards(self, key: str):
        ov = self.overlays.get(key)
        visible = bool(ov is not None and ov.isVisible())
        for card in self._overlay_cards.get(key, []):
            card.set_checked_silent(visible)

    def detach(self):
        """Tear down the live session (overlays, handle). Called only by the
        watcher (game closed) and on app close — there is no manual detach."""
        self._attach_busy = False
        self._located_shown = False
        self._stop_locate()
        for key, ov in list(self.overlays.items()):
            if ov is not None:
                ov.close()
            self.overlays[key] = None
        if self.model is not None:
            try:
                self.model.shutdown()      # stop background threads before detaching
            except Exception as e:
                self.log(f"shutdown warning: {e}")
        if self.proc:
            self.proc.close()
        self.proc = None
        self.model = None
        self._set_overlay_cards_enabled(False)
        self._set_status(False, "detached")    # caller refreshes the watch line

    # ====================================================================
    #  Loot prediction
    # ====================================================================
    def predict(self):
        self.table.setRowCount(0)
        tid = self.table_combo.currentData() or self.table_combo.currentText().strip()
        if not tid:
            return
        level = self.level_stepper.value()
        rows = loot.predict_sorted(tid, level)
        self.table.setRowCount(len(rows))
        for r, (item, prob, rar, typ) in enumerate(rows):
            # PROB
            prob_item = QtWidgets.QTableWidgetItem(fmt_pct(prob))
            prob_item.setForeground(QtGui.QColor(theme.ACCENT))
            f = prob_item.font(); f.setFamily(theme.MONO_FONT); prob_item.setFont(f)
            self.table.setItem(r, 0, prob_item)
            # ITEM = IconTile + name
            self.table.setCellWidget(r, 1, self._loot_item_cell(item, rar))
            # RARITY tag
            self.table.setCellWidget(r, 2, self._center(C.RarityTag(rar)))
            # TYPE humanized
            typ_item = QtWidgets.QTableWidgetItem(names.humanize(typ))
            typ_item.setForeground(QtGui.QColor(theme.MUTED))
            self.table.setItem(r, 3, typ_item)
        self.table.resizeRowsToContents()
        self.loot_caption.setText(f"TABLE: {tid}")
        self.log(f"Predicted '{tid}' @ L{level}: {len(rows)} drops.")

    def _loot_item_cell(self, item: str, rarity: str):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(4, 3, 4, 3)
        lay.setSpacing(8)
        tile = C.IconTile(24)
        tile.set("item", item, theme.rarity_color(rarity))
        name = QtWidgets.QLabel()
        if tokens.is_token(item):
            name.setText(f"🎲 random — {names.item_name(item)}")
            name.setStyleSheet(f"color:{theme.MUTED};font-style:italic;background:transparent;")
        else:
            name.setText(names.item_name(item) or item)
            name.setStyleSheet(
                f"color:{theme.rarity_color(rarity)};background:transparent;")
        lay.addWidget(tile)
        lay.addWidget(name, 1)
        return w

    def _center(self, widget):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.addWidget(widget)
        lay.addStretch(1)
        return w

    def closeEvent(self, e):
        try:
            self.hotkeys.clear()
        except Exception:
            pass
        try:
            self._attach_timer.stop()
        except Exception:
            pass
        try:
            self._friends_timer.stop()
            if self._friends_worker is not None and self._friends_worker.isRunning():
                self._friends_worker.wait(1500)
        except Exception:
            pass
        try:
            if self._update_check_worker is not None and self._update_check_worker.isRunning():
                self._update_check_worker.wait(1500)
            # the download worker is left to finish if a swap is mid-flight
        except Exception:
            pass
        self.detach()
        super().closeEvent(e)
