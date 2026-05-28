"""Thin client for the Farever Pal web API (farever-pals.com).

Pure + blocking (stdlib urllib, no extra dependency) — callers run it off the Qt
thread. Every method returns the decoded JSON dict; on any failure it returns
{"ok": False, "error": ...} rather than raising, so the UI can stay simple.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


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

    def set_accent(self, hex_color: str) -> dict:
        return self._request("POST", "/api/profile.php", {"accent_color": hex_color}, auth=True)

    def categories(self) -> dict:
        return self._request("GET", "/api/categories.php")

    def submit_run(self, category: str, time_ms: int, mode: str = "normal",
                   video_url: str = "", title: str = "", notes: str = "", server: str = "") -> dict:
        """-> {ok, id, status, flagged, flag_reason}."""
        return self._request("POST", "/api/speedrun/submit.php", {
            "category": category,
            "time_ms": int(time_ms),
            "mode": mode,
            "video_url": video_url,
            "title": title,
            "notes": notes,
            "server": server,
        }, auth=True)

    def edit_run(self, run_id: int, **fields) -> dict:
        body = {"id": int(run_id)}
        body.update({k: v for k, v in fields.items() if k in ("video_url", "title", "notes")})
        return self._request("POST", "/api/speedrun/edit.php", body, auth=True)
