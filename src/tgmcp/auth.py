"""Shared-secret protection for the HTTP transport.

Two ways to present the token, both checked in constant time:
  * ``Authorization: Bearer <token>`` header (Claude Code, Claude Desktop, scripts);
  * a secret path prefix ``/<token>/mcp`` — for clients such as claude.ai custom connectors that only take a URL.
"""

from __future__ import annotations

import hmac
import json

from starlette.types import ASGIApp, Receive, Scope, Send

PUBLIC_PATHS = {"/health"}


class TokenAuthMiddleware:
    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token
        self._prefix = f"/{token}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        if self._path_ok(path):
            new_path = path[len(self._prefix):] or "/"
            scope = dict(scope, path=new_path, raw_path=new_path.encode())
            await self.app(scope, receive, send)
            return

        if self._header_ok(scope):
            await self.app(scope, receive, send)
            return

        body = json.dumps({"error": "unauthorized"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer realm="tgmcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _path_ok(self, path: str) -> bool:
        head = path[: len(self._prefix)]
        rest = path[len(self._prefix):]
        return hmac.compare_digest(head.encode(), self._prefix.encode()) and (rest == "" or rest.startswith("/"))

    def _header_ok(self, scope: Scope) -> bool:
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme, _, cred = value.decode("latin-1").partition(" ")
                if scheme.lower() == "bearer" and hmac.compare_digest(cred.strip().encode(), self.token.encode()):
                    return True
        return False
