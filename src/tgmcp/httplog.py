"""One log line per HTTP request, so that client-side failures can be matched against what the server saw.

Logged: method, path (secrets masked), JSON-RPC method and tool name for MCP calls, status, duration and the
client address. Sits outside the auth middleware so that rejected requests (401) are logged as well.
"""

from __future__ import annotations

import json
import logging
import re
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("tgmcp.http")

_SECRET_SEGMENT = re.compile(r"^/[^/]{16,}(?=/|$)")  # the token (or an attempt at it) in front of /mcp
_MAX_BODY_INSPECT = 64 * 1024


def mask_path(path: str) -> str:
    return _SECRET_SEGMENT.sub("/<token>", path)


def describe_rpc(body: bytes) -> str:
    """'tools/call search_messages' for a JSON-RPC request body, '' when it isn't one."""
    if not body:
        return ""
    try:
        data = json.loads(body)
    except ValueError:
        return "non-json"
    items = data if isinstance(data, list) else [data]
    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        method = item.get("method")
        if not method:
            parts.append("response" if "result" in item or "error" in item else "?")
            continue
        if method == "tools/call":
            name = (item.get("params") or {}).get("name")
            parts.append(f"tools/call {name}" if name else method)
        else:
            parts.append(method)
    return ", ".join(parts)


class RequestLogMiddleware:
    def __init__(self, app: ASGIApp, skip_paths: tuple[str, ...] = ("/health",)):
        self.app = app
        self.skip_paths = skip_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.skip_paths:
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        status = {"code": None}
        body = bytearray()

        async def receive_logged() -> Message:
            message = await receive()
            if message["type"] == "http.request" and len(body) < _MAX_BODY_INSPECT:
                body.extend(message.get("body", b""))
            return message

        async def send_logged(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive_logged, send_logged)
        finally:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            client = headers.get("x-forwarded-for") or (scope.get("client") or ("?",))[0]
            rpc = describe_rpc(bytes(body)) if scope.get("method") == "POST" else ""
            log.info(
                "%s %s%s -> %s %.0fms from %s",
                scope.get("method"),
                mask_path(scope.get("path", "")),
                f" [{rpc}]" if rpc else "",
                status["code"] if status["code"] is not None else "aborted",
                (time.monotonic() - started) * 1000,
                client,
            )
