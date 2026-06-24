"""Code-watch dashboard HTTP server — served by gateway (no separate process)."""

from __future__ import annotations

import json
import hashlib
import hmac
import mimetypes
import secrets
import sys
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from nanobot.runtime.chat_hub import ChatHub

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WATCH_DIR = _REPO_ROOT / "scripts" / "code-watch"
_STATIC = _WATCH_DIR / "static"
_SESSION_COOKIE = "cw_session"
_SESSION_TTL_S = 30 * 24 * 3600


class _ReuseHTTPServer(ThreadingHTTPServer):
    """Allow quick rebinding after restart (avoids TIME_WAIT death-spiral)."""
    allow_reuse_address = True


def _ensure_watch_imports() -> None:
    path = str(_WATCH_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)


def watch_static_dir() -> Path:
    return _STATIC


def resolve_repo(repo: Path | None) -> Path:
    _ensure_watch_imports()
    from git_snapshot import repo_root  # noqa: WPS433

    return (repo or repo_root(_REPO_ROOT)).resolve()


class DashboardHandler(BaseHTTPRequestHandler):
    repo: Path = _REPO_ROOT
    refresh_hint_s: int = 5
    auth_password: str | None = None
    auth_token: str | None = None
    sessions: dict[str, float] = {}
    sessions_lock: threading.Lock = threading.Lock()
    chat_hub: ChatHub | None = None
    gateway_port: int = 18790

    def log_message(self, fmt: str, *args) -> None:
        return

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _session_id(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        jar = cookies.SimpleCookie()
        jar.load(raw)
        morsel = jar.get(_SESSION_COOKIE)
        return morsel.value if morsel else None

    def _session_valid(self, sid: str | None) -> bool:
        if not sid:
            return False
        now = time.time()
        with self.sessions_lock:
            exp = self.sessions.get(sid)
            if exp and exp >= now:
                return True
            if exp:
                self.sessions.pop(sid, None)
        return self._signed_session_valid(sid)

    def _issue_session(self) -> str:
        if self.auth_password:
            exp = int(time.time() + _SESSION_TTL_S)
            nonce = secrets.token_urlsafe(16)
            body = f"{exp}.{nonce}"
            sig = hmac.new(
                self.auth_password.encode("utf-8"),
                body.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            return f"v1.{body}.{sig}"
        sid = secrets.token_urlsafe(32)
        with self.sessions_lock:
            self.sessions[sid] = time.time() + _SESSION_TTL_S
        return sid

    def _signed_session_valid(self, sid: str) -> bool:
        if not self.auth_password or not sid.startswith("v1."):
            return False
        parts = sid.split(".", 3)
        if len(parts) != 4:
            return False
        _, exp_s, nonce, sig = parts
        try:
            exp = int(exp_s)
        except ValueError:
            return False
        if exp < time.time() or not nonce:
            return False
        body = f"{exp_s}.{nonce}"
        expected = hmac.new(
            self.auth_password.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    def _clear_session(self) -> None:
        sid = self._session_id()
        if sid:
            with self.sessions_lock:
                self.sessions.pop(sid, None)

    def _auth_required(self) -> bool:
        return bool(self.auth_password or self.auth_token)

    def _authorized(self, parsed) -> bool:
        if not self._auth_required():
            return True
        if self.auth_password and self._session_valid(self._session_id()):
            return True
        if self.auth_token:
            qs = parse_qs(parsed.query)
            token = qs.get("token", [None])[0]
            if token == self.auth_token:
                return True
            header = self.headers.get("X-Code-Watch-Token", "")
            if header == self.auth_token:
                return True
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer ") and auth[7:] == self.auth_token:
                return True
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            if parsed.path == "/api/meta":
                self._api(parsed)
                return
            if not self._authorized(parsed):
                self._error(401, "unauthorized")
                return
            self._api(parsed)
            return
        if parsed.path in ("/", "/index.html"):
            self._file(_STATIC / "index.html", "text/html; charset=utf-8")
            return
        rel = parsed.path.lstrip("/")
        target = (_STATIC / rel).resolve()
        if not str(target).startswith(str(_STATIC.resolve())):
            self._error(403, "forbidden")
            return
        if target.is_file():
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._file(target, ctype)
            return
        self._error(404, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/login":
            self._login()
            return
        if parsed.path == "/api/logout":
            self._logout()
            return
        if parsed.path.startswith("/api/"):
            if not self._authorized(parsed):
                self._error(401, "unauthorized")
                return
            if parsed.path == "/api/chat/send":
                self._chat_send()
                return
            if parsed.path == "/api/control/action":
                self._control_action()
                return
        self._error(404, "not found")

    def _login(self) -> None:
        if not self.auth_password:
            self._error(400, "password auth disabled")
            return
        body = self._read_json_body()
        password = str(body.get("password", ""))
        if not secrets.compare_digest(password, self.auth_password):
            self._error(401, "wrong password")
            return
        sid = self._issue_session()
        payload = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Set-Cookie",
            f"{_SESSION_COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age={_SESSION_TTL_S}",
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _chat_send(self) -> None:
        hub = self.chat_hub
        if not hub:
            self._error(503, "chat hub not enabled")
            return
        body = self._read_json_body()
        content = str(body.get("content", "")).strip()
        if not content:
            self._error(400, "content required")
            return
        ok = hub.send(content, echo=bool(body.get("echo", True)))
        if not ok:
            self._error(503, hub.last_error or "chat not ready")
            return
        self._json({"ok": True})

    def _control_action(self) -> None:
        body = self._read_json_body()
        action = str(body.get("action", ""))
        if action in {"command", "command_template"}:
            hub = self.chat_hub
            if not hub:
                self._error(503, "chat hub not enabled")
                return
            content = str(body.get("value", "")).strip()
            if action == "command_template":
                template = str(body.get("template", ""))
                content = template.replace("{value}", content)
            if not content:
                self._error(400, "content required")
                return
            ok = hub.send(content, echo=False)
            if not ok:
                self._error(503, hub.last_error or "chat not ready")
                return
            self._json({"ok": True, "message": "command dispatched"})
            return
        try:
            from nanobot.runtime.chat_controls import apply_control_action, runtime_control_commands

            commands = runtime_control_commands(body)
            if commands:
                hub = self.chat_hub
                if not hub:
                    self._error(503, "chat hub not enabled")
                    return
                for command in commands:
                    ok = hub.send(command, echo=False)
                    if not ok:
                        self._error(503, hub.last_error or "chat not ready")
                        return
                self._json({"ok": True, "message": "runtime controls dispatched", "reload": True})
                return
            self._json(apply_control_action(body))
        except ValueError as exc:
            self._error(400, str(exc))

    def _logout(self) -> None:
        self._clear_session()
        payload = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Set-Cookie",
            f"{_SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
        )
        self.end_headers()
        self.wfile.write(payload)

    def _api(self, parsed) -> None:
        _ensure_watch_imports()
        from agent_insights import (  # noqa: WPS433
            agent_dashboard,
            architecture,
            chat_status,
            list_agents,
            nanobot_home,
            prompt_stack,
            read_prompt_file,
            recent_activity,
            runtime_snapshot,
        )
        from git_snapshot import (  # noqa: WPS433
            changed_files,
            diff,
            log,
            show_commit,
            snapshot,
            summary,
        )

        qs = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/snapshot":
                self._json(snapshot(self.repo))
            elif parsed.path == "/api/summary":
                self._json(summary(self.repo))
            elif parsed.path == "/api/files":
                self._json({"files": changed_files(self.repo)})
            elif parsed.path == "/api/diff":
                path = qs.get("path", [None])[0]
                staged = qs.get("staged", ["0"])[0] in ("1", "true", "yes")
                self._text(diff(self.repo, path=path, staged=staged))
            elif parsed.path == "/api/log":
                limit = int(qs.get("limit", ["30"])[0])
                self._json({"log": log(self.repo, limit=limit)})
            elif parsed.path == "/api/commit":
                ref = qs.get("ref", [None])[0]
                if not ref:
                    self._error(400, "ref required")
                    return
                self._json(show_commit(self.repo, ref))
            elif parsed.path == "/api/architecture":
                files = changed_files(self.repo)
                self._json(architecture(self.repo, files))
            elif parsed.path == "/api/runtime":
                self._json(runtime_snapshot())
            elif parsed.path == "/api/agents":
                rt = runtime_snapshot()
                self._json({
                    "runtime": rt,
                    "agents": list_agents(active=rt.get("active_agents")),
                    "prompt_stack": prompt_stack(mode=rt.get("mode", "idle")),
                })
            elif parsed.path == "/api/activity":
                limit = int(qs.get("limit", ["40"])[0])
                self._json(recent_activity(limit=limit))
            elif parsed.path == "/api/prompt":
                rel = qs.get("path", [None])[0]
                if not rel:
                    self._error(400, "path required")
                    return
                content = read_prompt_file(nanobot_home(), rel)
                if content is None:
                    self._error(404, "prompt not found")
                    return
                self._text(content)
            elif parsed.path == "/api/agent-dashboard":
                self._json(agent_dashboard())
            elif parsed.path == "/api/chat/status":
                payload = chat_status()
                hub = self.chat_hub
                if hub:
                    payload["hub"] = hub.status()
                    payload["ready"] = hub.connected
                    payload["upstream_reachable"] = True
                    payload["mode"] = "gateway"
                self._json(payload)
            elif parsed.path == "/api/chat/commands":
                from nanobot.runtime.chat_controls import command_catalog
                self._json(command_catalog())
            elif parsed.path == "/api/control/schema":
                from nanobot.runtime.chat_controls import control_schema
                self._json(control_schema())
            elif parsed.path == "/api/control/providers":
                from nanobot.runtime.chat_controls import providers_panel
                self._json(providers_panel())
            elif parsed.path == "/api/chat/events":
                after = int(qs.get("after", ["0"])[0])
                hub = self.chat_hub
                if not hub:
                    self._json({"events": [], "latest_id": 0, "connected": False})
                    return
                st = hub.status()
                self._json({
                    "events": hub.events_after(after),
                    "latest_id": st["latest_id"],
                    "connected": st["connected"],
                })
            elif parsed.path == "/api/meta":
                mode = "none"
                if self.auth_password:
                    mode = "password"
                elif self.auth_token:
                    mode = "token"
                cs = chat_status()
                hub = self.chat_hub
                if hub:
                    cs["hub"] = hub.status()
                    cs["ready"] = hub.connected
                self._json({
                    "repo": str(self.repo),
                    "refresh_hint_s": self.refresh_hint_s,
                    "auth": mode,
                    "logged_in": self._authorized(parsed),
                    "chat": cs,
                    "gateway_port": self.gateway_port,
                    "integrated": True,
                })
            else:
                self._error(404, "unknown api")
        except RuntimeError as exc:
            self._error(500, str(exc))

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str) -> None:
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DashboardServer:
    """Background HTTP server for the code-watch UI."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        repo: Path,
        chat_hub: ChatHub | None,
        password: str | None = None,
        token: str | None = None,
        refresh_s: int = 5,
    ) -> None:
        self.host = host
        self.port = port
        self.repo = repo
        self.chat_hub = chat_hub
        self.password = password or ""
        self.token = token or ""
        self.refresh_s = max(2, refresh_s)
        self._http: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        """Start the server in a daemon thread. Returns the dashboard URL."""
        if self._thread and self._thread.is_alive():
            return self.url

        password = self.password
        if self.host in ("0.0.0.0", "::") and not password and not self.token:
            password = secrets.token_urlsafe(16)
            from loguru import logger

            logger.warning("Dashboard public bind without password — generated: {}", password)

        handler = DashboardHandler
        handler.repo = self.repo
        handler.refresh_hint_s = self.refresh_s
        handler.auth_password = password or None
        handler.auth_token = self.token or None
        handler.sessions = {}
        handler.sessions_lock = threading.Lock()
        handler.chat_hub = self.chat_hub
        handler.gateway_port = self.port

        from loguru import logger

        try:
            self._http = _ReuseHTTPServer((self.host, self.port), handler)
        except OSError as e:
            if getattr(e, "errno", None) == 98:  # EADDRINUSE
                logger.error(
                    "Dashboard port {} already in use — skipping dashboard "
                    "(kill duplicate `nanobot gateway` or free the port; Telegram/Web still work)",
                    self.port,
                )
                return ""
            raise

        self._thread = threading.Thread(
            target=self._http.serve_forever,
            name="nanobot-dashboard",
            daemon=True,
        )
        self._thread.start()

        if password:
            creds = Path.home() / ".nanobot" / "code-watch.password"
            creds.parent.mkdir(parents=True, exist_ok=True)
            creds.write_text(f"PASSWORD={password}\nURL={self.url}\n", encoding="utf-8")

        logger.info("Dashboard listening on {} (repo={})", self.url, self.repo)
        if password:
            logger.info("Dashboard password saved to ~/.nanobot/code-watch.password")
        return self.url

    def stop(self) -> None:
        if self._http:
            self._http.shutdown()
            self._http = None
        self._thread = None

    @property
    def url(self) -> str:
        display = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{display}:{self.port}/"
