"""Entry point: `tgmcp` (uses MCP_TRANSPORT from .env) or `tgmcp --transport http|stdio`, `tgmcp --check`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import ConfigError, load_config
from .profiles import format_profile
from .server import build_server
from .telegram import TelegramService

log = logging.getLogger("tgmcp")


def build_http_app(mcp, config, tg: TelegramService):
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from mcp.server.transport_security import TransportSecuritySettings

    from .auth import TokenAuthMiddleware

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        ok = tg.client.is_connected()
        return JSONResponse(
            {
                "status": "ok" if ok else "telegram_disconnected",
                "chats": len(tg.chats),
                "calls": mcp.gate.stats(),
                "cache": {"hits": tg.cache.hits, "misses": tg.cache.misses},
            },
            status_code=200 if ok else 503,
        )

    # Stateless + JSON responses: survives restarts and plays well with reverse proxies.
    # The SDK enables DNS-rebinding protection for 127.0.0.1 and then rejects requests forwarded by a reverse proxy
    # (Host: your.domain). The endpoint is protected by MCP_AUTH_TOKEN instead, which rebinding can't obtain.
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host=config.host,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    if config.auth_token:
        app = TokenAuthMiddleware(app, config.auth_token)
    else:
        log.warning("MCP_ALLOW_NO_AUTH is set: the HTTP endpoint is open to anyone who can reach it")
    return app


async def telegram_watchdog(tg: TelegramService, on_fail, interval: float = 30, max_failures: int = 5) -> None:
    """Reconnect if Telethon's own auto-reconnect gave up; stop the server if Telegram stays unreachable so the
    supervisor (docker `restart: unless-stopped`) starts a fresh process."""
    failures = 0
    while True:
        await asyncio.sleep(interval)
        if tg.client.is_connected():
            failures = 0
            continue
        failures += 1
        log.warning("Telegram disconnected, reconnecting (attempt %d/%d)", failures, max_failures)
        try:
            await tg.client.connect()
        except Exception as e:  # noqa: BLE001
            log.warning("Reconnect failed: %s", e)
        if not tg.client.is_connected() and failures >= max_failures:
            log.error("Telegram unreachable, shutting down so the process gets restarted")
            on_fail()
            return


async def run(config, transport: str, check: bool) -> None:
    tg = TelegramService(config)
    try:
        await tg.start()
        if check:
            print("Telegram session OK. Chats and what the server learned about them:\n")
            for c in tg.chats.values():
                print(format_profile(c), end="\n\n")
            for raw, err in tg.unresolved:
                print(f" ! {raw}: {err}")
            return
        mcp = build_server(tg)
        if transport == "stdio":
            await mcp.run_stdio_async()
            return

        import uvicorn

        app = build_http_app(mcp, config, tg)
        log.info("MCP endpoint: http://%s:%s%s/mcp", config.host, config.port,
                 "/<MCP_AUTH_TOKEN>" if config.auth_token else "")
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.host,
                port=config.port,
                proxy_headers=True,
                forwarded_allow_ips="*",
                log_level=config.log_level.lower(),
                access_log=False,  # the access log would print the secret path
                limit_concurrency=config.max_connections,  # beyond this uvicorn answers 503 instead of falling over
                timeout_keep_alive=30,
                timeout_graceful_shutdown=20,
            )
        )

        def fail():
            server.should_exit = True

        watchdog = asyncio.create_task(telegram_watchdog(tg, fail))
        try:
            await server.serve()
        finally:
            watchdog.cancel()
    finally:
        await tg.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="tgmcp", description="Read-only Telegram search MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], help="override MCP_TRANSPORT")
    parser.add_argument("--check", action="store_true", help="verify the session and chat list, then exit")
    parser.add_argument("--env-file", help="path to the .env file (default: ./.env or $TGMCP_ENV_FILE)")
    args = parser.parse_args()

    if args.env_file:
        import os

        os.environ["TGMCP_ENV_FILE"] = args.env_file
    try:
        config = load_config()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(2)
    transport = args.transport or config.transport
    if transport == "http" and not args.check and not config.auth_token and not config.allow_no_auth:
        print("Configuration error: MCP_AUTH_TOKEN is required for the http transport", file=sys.stderr)
        sys.exit(2)

    # stdout belongs to the MCP protocol in stdio mode, so all logs go to stderr.
    logging.basicConfig(
        level=config.log_level, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("telethon").setLevel(max(logging.WARNING, logging.getLevelName(config.log_level)))
    try:
        asyncio.run(run(config, transport, args.check))
    except KeyboardInterrupt:
        pass
    except (RuntimeError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
