"""Thin client for the Farever Pal web API (farever-pals.com).

Pure + blocking (stdlib urllib, no extra dependency), callers run it off the Qt
thread. Every method returns the decoded JSON dict; on any failure it returns
{"ok": False, "error": ...} rather than raising, so the UI can stay simple.
"""
from __future__ import annotations

import http.server
import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


class FareverAPI:
    def __init__(self, base_url: str, token: str = "") -> None:
        self.base = (base_url or "https://farever-pals.com").rstrip("/")
        self.token = token or ""

    # ---- low level -----------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None, auth: bool = False) -> dict:
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "FareverPal-Companion")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth and self.token:
            req.add_header("Authorization", "Bearer " + self.token)
            req.add_header("X-Api-Key", self.token)  # shared-host fallback
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8") or "{}")
            except Exception:
                return {"ok": False, "error": f"http_{e.code}"}
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return {"ok": False, "error": "network"}

    # ---- endpoints -----------------------------------------------------
    def login(self, username: str, password: str) -> dict:
        """-> {ok, token, user} | {ok:False, error}."""
        res = self._request("POST", "/api/auth/token.php", {"username": username, "password": password})
        if res.get("ok") and res.get("token"):
            self.token = res["token"]
        return res

    def me(self) -> dict:
        return self._request("GET", "/api/me.php", auth=True)

    def latest_release(self) -> dict:
        """Latest desktop-app release for the in-app updater (public, no auth).
        -> {ok, version, url, asset, size, sha256, notes, html_url} | {ok:False}."""
        return self._request("GET", "/api/release.php")

    def set_accent(self, hex_color: str) -> dict:
        return self._request("POST", "/api/profile.php", {"accent_color": hex_color}, auth=True)

    def categories(self) -> dict:
        return self._request("GET", "/api/categories.php")

    def submit_run(self, category: str, time_ms: int, mode: str = "hard",
                   run_type: str = "full", video_url: str = "", title: str = "",
                   notes: str = "", server: str = "", co_runners: list | None = None,
                   build_code: str = "", companion_version: str = "",
                   client_run_id: str = "") -> dict:
        """-> {ok, id, status, flagged, flag_reason, on_board, board_reason}.
        `run_type` is the board ('full' dungeon | 'boss' only). `client_run_id` ties
        the two splits the timer emits from one run. `companion_version` lets the
        season gate stale builds (board_reason 'companion_outdated' => profile-only).
        `co_runners` = friend codes (FRVR-XXXX-XXXX); the server links each
        co-runner's featured build. `build_code` overrides the build linked to *your*
        run; empty = the server falls back to your profile's featured build."""
        body = {
            "category": category,
            "time_ms": int(time_ms),
            "mode": mode,
            "run_type": run_type,
            "video_url": video_url,
            "title": title,
            "notes": notes,
            "server": server,
            "co_runners": list(co_runners or []),
        }
        if build_code:
            body["build_code"] = build_code
        if companion_version:
            body["companion_version"] = companion_version
        if client_run_id:
            body["client_run_id"] = client_run_id
        return self._request("POST", "/api/speedrun/submit.php", body, auth=True)

    def lookup_build(self, code: str) -> dict:
        """Resolve a build code to its display chip (for the Speedrun-tab preview).
        -> {ok, code, title, class, icon, mine} | {ok:False}. Requires auth."""
        code = urllib.parse.quote(str(code).strip())
        return self._request("GET", f"/api/build/lookup.php?code={code}", auth=True)

    def edit_run(self, run_id: int, **fields) -> dict:
        body = {"id": int(run_id)}
        body.update({k: v for k, v in fields.items() if k in ("video_url", "title", "notes")})
        return self._request("POST", "/api/speedrun/edit.php", body, auth=True)

    # ---- friends + presence -------------------------------------------
    def friends_list(self) -> dict:
        """-> {ok, friends:[{public_id, username, avatar_url, presence, ...}], incoming_count}."""
        return self._request("GET", "/api/friends/list.php", auth=True)

    def presence_ping(self, share: bool | None = None) -> dict:
        """Heartbeat so friends see us 'in-game'. `share` (optional) sets the
        hideable 'share my online status' flag server-side. -> {ok, share_presence}."""
        body: dict = {}
        if share is not None:
            body["share"] = bool(share)
        return self._request("POST", "/api/presence/ping.php", body, auth=True)


# --- OAuth (Google / Discord) sign-in, loopback flow ----------------------
# The native-app pattern (RFC 8252): open the system browser to the web's OAuth
# bridge, which runs the normal provider consent against farever-pals.com's
# registered redirect_uri, then hands a companion API token back to a one-shot
# HTTP server we run on 127.0.0.1. The provider never sees localhost; only the
# final web -> app hop is local. `state` ties the browser flow to this server so
# a stray local process can't inject a token.

_OAUTH_OK_HTML = (
    b"<!doctype html><meta charset=utf-8><title>Farever Pal</title>"
    b"<style>html{background:#0e0f13;color:#e5e7eb;font:15px/1.5 system-ui,sans-serif}"
    b"div{max-width:420px;margin:18vh auto;text-align:center;padding:0 24px}"
    b"b{color:#38bdf8}</style>"
    b"<div><h2><b>Signed in.</b></h2><p>You can close this tab and return to "
    b"Farever Pal.</p></div>"
)
_OAUTH_ERR_HTML = (
    b"<!doctype html><meta charset=utf-8><title>Farever Pal</title>"
    b"<style>html{background:#0e0f13;color:#e5e7eb;font:15px/1.5 system-ui,sans-serif}"
    b"div{max-width:420px;margin:18vh auto;text-align:center;padding:0 24px}"
    b"b{color:#f0556b}</style>"
    b"<div><h2><b>Sign-in failed.</b></h2><p>Close this tab and try again from "
    b"Farever Pal.</p></div>"
)


def oauth_login(base_url: str, provider: str, timeout: float = 180.0) -> dict:
    """Run the browser OAuth flow for `provider` ('google'|'discord').

    Returns {ok: True, token} on success, else {ok: False, error}. Blocking -
    call off the UI thread.
    """
    base = (base_url or "https://farever-pals.com").rstrip("/")
    state = secrets.token_urlsafe(16)
    result: dict = {}
    done = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib name)
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if (params.get("state") or [""])[0] != state:
                # favicon / stray hit - ignore, keep waiting for the real callback
                self.send_response(204)
                self.end_headers()
                return
            token = (params.get("token") or [""])[0]
            if token:
                result["token"] = token
            else:
                result["error"] = (params.get("error") or ["oauth"])[0]
            body = _OAUTH_OK_HTML if token else _OAUTH_ERR_HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass
            done.set()

        def log_message(self, *_a):  # silence the default stderr logging
            pass

    try:
        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    except OSError:
        return {"ok": False, "error": "loopback"}
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = (f"{base}/api/oauth/companion.php?provider={urllib.parse.quote(provider)}"
               f"&port={port}&state={urllib.parse.quote(state)}")
        if not webbrowser.open(url):
            return {"ok": False, "error": "browser"}
        if not done.wait(timeout):
            return {"ok": False, "error": "timeout"}
    finally:
        server.shutdown()
    if result.get("token"):
        return {"ok": True, "token": result["token"]}
    return {"ok": False, "error": result.get("error", "oauth")}


def login_with_profile(base_url: str, username: str, password: str) -> dict:
    api = FareverAPI(base_url)
    res = api.login(username, password)
    if res.get("ok") and res.get("token"):
        me = FareverAPI(base_url, res["token"]).me()
        if me.get("ok") and isinstance(me.get("user"), dict):
            res["user"] = {**res.get("user", {}), **me["user"]}
    return res


def oauth_with_profile(base_url: str, provider: str) -> dict:
    res = oauth_login(base_url, provider)
    if res.get("ok") and res.get("token"):
        me = FareverAPI(base_url, res["token"]).me()
        if me.get("ok") and isinstance(me.get("user"), dict):
            res["user"] = me["user"]
    return res


def presence_and_friends(base_url: str, token: str, share: bool) -> dict:
    api = FareverAPI(base_url, token)
    api.presence_ping(share)
    res = api.friends_list()
    return res if isinstance(res, dict) else {"ok": False}
