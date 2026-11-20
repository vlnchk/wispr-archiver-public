"""Interactive login (human only): loopback redirect on 127.0.0.1:<port>.

On a VPS: `ssh -L <port>:127.0.0.1:<port> vps` then `wispr-archiver login --no-browser`
and open the printed URL in the local browser. The flow must finish within 5 minutes.
"""

from __future__ import annotations

import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import anyio
from mcp.client.auth import AuthorizationCodeResult

from wispr_archiver.errors import ArchiverError

LOGIN_TIMEOUT = 300.0

_DONE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>wispr-archiver</title>"
    b"<p>Authorization received. You can close this tab.</p>"
)


class LoginError(ArchiverError):
    kind = "login"


class LoopbackCallback:
    """One-shot HTTP server on 127.0.0.1 that receives ?code=&state= on /callback."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._result: dict[str, str] = {}
        self._event = threading.Event()
        self._server: HTTPServer | None = None

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                url = urlparse(self.path)
                if url.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                outer._result = q
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_DONE_PAGE)
                outer._event.set()

            def log_message(self, format: str, *args: Any) -> None:
                return  # never echo the request line (it carries the code)

        try:
            self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        except OSError as exc:
            raise LoginError(f"port {self.port} on 127.0.0.1 is busy; use --port") from exc
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    async def wait(self) -> AuthorizationCodeResult:
        got = await anyio.to_thread.run_sync(self._event.wait, LOGIN_TIMEOUT)
        if not got:
            raise LoginError("no redirect received within 5 minutes")
        if "error" in self._result:
            raise LoginError(f"authorization denied: {self._result['error'][:60]}")
        if "code" not in self._result:
            raise LoginError("redirect without an authorization code")
        return AuthorizationCodeResult(
            code=self._result["code"], state=self._result.get("state"), iss=self._result.get("iss")
        )


def make_redirect_handler(*, open_browser: bool) -> Any:
    async def redirect(url: str) -> None:
        # The authorization URL carries no secret (PKCE challenge only); print it for ssh -L.
        print(
            "\nOpen this link in your browser and sign in to Wispr within five minutes.\n\n"
            + url
            + "\n",
            file=sys.stderr,
            flush=True,
        )
        if open_browser:
            webbrowser.open(url)

    return redirect
