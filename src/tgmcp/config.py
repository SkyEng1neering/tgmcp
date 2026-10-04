"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def marked_channel_id(channel_id: int) -> int:
    """Bot-API style id of a channel/supergroup: -(10**12 + id), i.e. -100<id> for 10-digit ids."""
    return -(10**12 + channel_id)


def unmarked_channel_id(marked: int) -> int:
    return -marked - 10**12


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


@dataclass(frozen=True)
class ChatRef:
    """One entry of TG_CHATS, normalised to something Telethon can resolve."""

    raw: str
    kind: str  # "username" | "id" | "invite"
    value: str | int


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session: str
    chats: list[ChatRef]
    transport: str = "stdio"
    host: str = "0.0.0.0"
    port: int = 8000
    auth_token: str | None = None
    allow_no_auth: bool = False
    proxy: tuple | None = None
    max_message_chars: int = 1500
    log_level: str = "INFO"
    # Load management
    max_concurrent_calls: int = 8  # tool calls executing at once; the rest wait in a queue
    queue_timeout: float = 120.0  # seconds a call may wait in the queue before "server busy"
    max_queue: int = 200  # calls allowed to wait at once; beyond that callers get "busy" immediately
    tg_parallel_requests: int = 4  # concurrent requests to Telegram
    cache_ttl: float = 120.0  # seconds to reuse identical Telegram reads; 0 disables
    max_connections: int = 256  # HTTP connections handled at once (uvicorn limit_concurrency)


_INVITE_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([\w-]+)", re.I)
_PRIVATE_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/c/(\d+)", re.I)
_PUBLIC_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][\w]{3,})", re.I)
_USERNAME_RE = re.compile(r"@?([A-Za-z][\w]{3,})")


def parse_chat_ref(raw: str) -> ChatRef:
    """Parse one chat reference: @username, username, numeric id, t.me link or invite link."""
    s = raw.strip()
    if not s:
        raise ConfigError("empty chat reference")
    if m := _INVITE_RE.fullmatch(s.rstrip("/")):
        return ChatRef(raw=s, kind="invite", value=m.group(1))
    if m := _PRIVATE_LINK_RE.match(s):
        # t.me/c/<channel_id>/... -> marked id
        return ChatRef(raw=s, kind="id", value=marked_channel_id(int(m.group(1))))
    if re.fullmatch(r"-?\d+", s):
        return ChatRef(raw=s, kind="id", value=int(s))
    if m := _PUBLIC_LINK_RE.match(s):
        return ChatRef(raw=s, kind="username", value=m.group(1).lower())
    if m := _USERNAME_RE.fullmatch(s):
        return ChatRef(raw=s, kind="username", value=m.group(1).lower())
    raise ConfigError(f"cannot parse chat reference: {raw!r}")


def parse_chat_list(value: str) -> list[ChatRef]:
    """Split TG_CHATS on commas, semicolons and newlines."""
    parts = [p for p in re.split(r"[,;\n]+", value) if p.strip() and not p.strip().startswith("#")]
    refs = [parse_chat_ref(p) for p in parts]
    if not refs:
        raise ConfigError("TG_CHATS is empty: list at least one chat")
    return refs


def _parse_proxy(value: str | None) -> tuple | None:
    """Parse TG_PROXY like socks5://user:pass@host:port into a Telethon proxy tuple."""
    if not value:
        return None
    m = re.fullmatch(r"(socks5|socks4|http)://(?:([^:@]+)(?::([^@]*))?@)?([^:]+):(\d+)", value.strip())
    if not m:
        raise ConfigError(f"TG_PROXY must look like socks5://[user:pass@]host:port, got {value!r}")
    scheme, user, password, host, port = m.groups()
    return (scheme, host, int(port), True, user, password)


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _require(env: dict, name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is not set (see .env.example)")
    return value


def load_config(env: dict | None = None, env_file: str | Path | None = None) -> Config:
    """Build a Config from a mapping (defaults to os.environ after loading .env)."""
    if env is None:
        load_dotenv(env_file or os.environ.get("TGMCP_ENV_FILE") or ".env", override=False)
        env = dict(os.environ)

    try:
        api_id = int(_require(env, "TG_API_ID"))
    except ValueError as e:
        raise ConfigError("TG_API_ID must be an integer") from e

    session = (env.get("TG_SESSION") or "").strip()
    if not session:
        raise ConfigError("TG_SESSION is not set: run `tgmcp-login` to create a session string")
    try:
        from telethon.sessions import StringSession

        StringSession(session)
    except ValueError as e:
        raise ConfigError("TG_SESSION is not a valid session string: run `tgmcp-login` again") from e

    transport = (env.get("MCP_TRANSPORT") or "stdio").strip().lower()
    if transport in {"http", "streamable-http", "streamable_http"}:
        transport = "http"
    if transport not in {"stdio", "http"}:
        raise ConfigError("MCP_TRANSPORT must be 'stdio' or 'http'")

    auth_token = (env.get("MCP_AUTH_TOKEN") or "").strip() or None
    allow_no_auth = _bool(env.get("MCP_ALLOW_NO_AUTH"))
    if transport == "http" and not auth_token and not allow_no_auth:
        raise ConfigError(
            "MCP_AUTH_TOKEN is required for MCP_TRANSPORT=http "
            "(generate one with: python -c 'import secrets; print(secrets.token_urlsafe(32))')"
        )
    if auth_token and len(auth_token) < 16:
        raise ConfigError("MCP_AUTH_TOKEN is too short: use at least 16 random characters")

    def num(name: str, default, cast=int, minimum=0):
        raw = (env.get(name) or "").strip()
        try:
            value = cast(raw) if raw else default
        except ValueError as e:
            raise ConfigError(f"{name} must be a number, got {raw!r}") from e
        if value < minimum:
            raise ConfigError(f"{name} must be >= {minimum}")
        return value

    port = num("MCP_PORT", num("PORT", 8000, minimum=1), minimum=1)
    log_level = (env.get("LOG_LEVEL") or "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")

    return Config(
        api_id=api_id,
        api_hash=_require(env, "TG_API_HASH"),
        session=session,
        chats=parse_chat_list(_require(env, "TG_CHATS")),
        transport=transport,
        host=(env.get("MCP_HOST") or "0.0.0.0").strip(),
        port=port,
        auth_token=auth_token,
        allow_no_auth=allow_no_auth,
        proxy=_parse_proxy(env.get("TG_PROXY")),
        max_message_chars=num("TG_MAX_MESSAGE_CHARS", 1500, minimum=0),
        log_level=log_level,
        max_concurrent_calls=num("MCP_MAX_CONCURRENT_CALLS", 8, minimum=1),
        queue_timeout=num("MCP_QUEUE_TIMEOUT", 120.0, float, minimum=1),
        max_queue=num("MCP_MAX_QUEUE", 200, minimum=0),
        tg_parallel_requests=num("TG_PARALLEL_REQUESTS", 4, minimum=1),
        cache_ttl=num("TG_CACHE_TTL", 120.0, float, minimum=0),
        max_connections=num("MCP_MAX_CONNECTIONS", 256, minimum=1),
    )
