"""Authenticated loopback HTTP endpoint for local notification scripts."""

from __future__ import annotations

import hmac
import http.server
import json
import logging
import os
import secrets
import stat
import tempfile
import threading
from collections.abc import Callable

from gi.repository import GLib

DEFAULT_PORT = 17334
MAX_BODY_BYTES = 4096
MAX_TITLE_CHARS = 160
MAX_BODY_CHARS = 1200
MAX_APP_CHARS = 80


def _default_token_path() -> str:
    data_home = os.environ.get(
        "XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share")
    )
    return os.path.join(data_home, "ledge-pill", "webhook-token")


class _LoopbackServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class WebhookService:
    """Serve POST /notify and /agent on IPv4 loopback and forward to GTK."""

    def __init__(
        self,
        on_message: Callable[[str, str, str], None],
        port: int = DEFAULT_PORT,
        token_path: str | None = None,
        on_agent_event: Callable[[str, str, str], None] | None = None,
    ):
        self.on_message = on_message
        self.on_agent_event = on_agent_event
        self.port = max(1024, min(65535, int(port)))
        self.token_path = token_path or _default_token_path()
        self.token = ""
        self._server: _LoopbackServer | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False
        try:
            self.token = self._load_or_create_token()
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Webhook disabled because its token could not be stored: %s", exc
            )

    def _load_or_create_token(self) -> str:
        parent = os.path.dirname(self.token_path) or "."
        os.makedirs(parent, mode=0o700, exist_ok=True)
        try:
            info = os.stat(self.token_path, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("webhook token path is not a regular file")
            with open(self.token_path, encoding="ascii") as f:
                raw_token = f.read(257)
            token = raw_token.strip()
            if (
                len(raw_token) <= 256
                and 32 <= len(token) <= 128
                and all(char.isalnum() or char in "-_" for char in token)
            ):
                os.chmod(self.token_path, 0o600, follow_symlinks=False)
                return token
        except FileNotFoundError:
            pass

        token = secrets.token_urlsafe(32)
        fd, tmp_path = tempfile.mkstemp(prefix=".webhook-token-", dir=parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(token + "\n")
            os.replace(tmp_path, self.token_path)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return token

    def start(self) -> bool:
        if self._server is not None:
            return True
        if not self.token or self._stopped:
            return False
        service = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "LedgePillWebhook/1"
            sys_version = ""

            def log_message(self, fmt, *args):
                logging.getLogger(__name__).debug("Webhook request: " + fmt, *args)

            def _respond(self, status: int, message: str):
                payload = json.dumps({"ok": status < 400, "message": message}).encode(
                    "utf-8"
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    self._respond(403, "loopback clients only")
                    return
                if self.path not in ("/notify", "/agent"):
                    self._respond(404, "unknown endpoint")
                    return
                auth = self.headers.get("Authorization", "")
                scheme, _, supplied = auth.partition(" ")
                try:
                    authorized = scheme.casefold() == "bearer" and hmac.compare_digest(
                        supplied.strip().encode("ascii"), service.token.encode("ascii")
                    )
                except UnicodeEncodeError:
                    authorized = False
                if not authorized:
                    self._respond(401, "unauthorized")
                    return
                content_type = self.headers.get_content_type()
                if content_type != "application/json":
                    self._respond(415, "application/json required")
                    return
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    self._respond(411, "content length required")
                    return
                if length < 1 or length > MAX_BODY_BYTES:
                    self._respond(413, "request body must be between 1 and 4096 bytes")
                    return
                try:
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._respond(400, "invalid JSON")
                    return
                if not isinstance(data, dict):
                    self._respond(400, "JSON object required")
                    return
                if service._stopped:
                    self._respond(503, "LedgePill is stopping")
                    return
                if self.path == "/agent":
                    agent = data.get("agent", "Agent")
                    state = data.get("state", "active")
                    message = data.get("message", "")
                    if (
                        not isinstance(agent, str)
                        or not isinstance(state, str)
                        or not isinstance(message, str)
                    ):
                        self._respond(400, "agent, state, and message must be strings")
                        return
                    clean_state = state.strip().lower()
                    if clean_state not in (
                        "idle",
                        "active",
                        "working",
                        "attention",
                        "done",
                        "error",
                    ):
                        self._respond(400, "invalid agent state")
                        return
                    clean_agent = agent.strip()[:MAX_APP_CHARS] or "Agent"
                    clean_msg = message.strip()[:MAX_BODY_CHARS]
                    GLib.idle_add(
                        service._deliver_agent, clean_agent, clean_state, clean_msg
                    )
                    self._respond(202, "agent event accepted")
                    return
                title = data.get("title")
                body = data.get("body", "")
                app = data.get("app", "Local script")
                if not isinstance(title, str) or not title.strip():
                    self._respond(400, "title must be a non-empty string")
                    return
                if not isinstance(body, str) or not isinstance(app, str):
                    self._respond(400, "app and body must be strings")
                    return
                title = title.strip()[:MAX_TITLE_CHARS]
                body = body.strip()[:MAX_BODY_CHARS]
                app = app.strip()[:MAX_APP_CHARS] or "Local script"
                GLib.idle_add(service._deliver, app, title, body)
                self._respond(202, "notification accepted")

            def do_GET(self):
                self._respond(405, "POST required")

            def do_PUT(self):
                self._respond(405, "POST required")

            def do_DELETE(self):
                self._respond(405, "POST required")

        try:
            self._server = _LoopbackServer(("127.0.0.1", self.port), Handler)
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Webhook endpoint could not bind to 127.0.0.1:%s: %s",
                self.port,
                exc,
            )
            return False
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="ledge-pill-webhook",
            kwargs={"poll_interval": 0.25},
            daemon=True,
        )
        self._thread.start()
        return True

    def _deliver(self, app: str, title: str, body: str):
        if not self._stopped:
            self.on_message(app, title, body)
        return GLib.SOURCE_REMOVE

    def _deliver_agent(self, agent: str, state: str, message: str):
        if not self._stopped and self.on_agent_event is not None:
            self.on_agent_event(agent, state, message)
        return GLib.SOURCE_REMOVE

    def stop(self):
        self._stopped = True
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
