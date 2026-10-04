"""`tgmcp-login`: interactive login that prints a Telethon StringSession for TG_SESSION."""

from __future__ import annotations

import asyncio
import getpass
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession


def _ask(prompt: str, env: str) -> str:
    value = os.environ.get(env, "").strip()
    if value:
        print(f"{prompt}: (from {env})")
        return value
    return input(f"{prompt}: ").strip()


async def _login() -> None:
    print("Get api_id / api_hash at https://my.telegram.org -> API development tools.\n")
    api_id = int(_ask("api_id", "TG_API_ID"))
    api_hash = _ask("api_hash", "TG_API_HASH")
    client = TelegramClient(StringSession(), api_id, api_hash, receive_updates=False, device_model="tgmcp")
    await client.start(
        phone=lambda: input("Phone number (international format, e.g. +79991234567): ").strip(),
        code_callback=lambda: input("Login code from Telegram: ").strip(),
        password=lambda: getpass.getpass("2FA password (if enabled): "),
    )
    me = await client.get_me()
    session = client.session.save()
    await client.disconnect()
    print(f"\nLogged in as {me.first_name or ''} {me.last_name or ''} (@{me.username or '-'}, id {me.id}).")
    print("\nPut this line into your .env. It gives FULL access to the account — keep it secret:\n")
    print(f"TG_SESSION={session}\n")


def main() -> None:
    load_dotenv(os.environ.get("TGMCP_ENV_FILE") or ".env", override=False)
    try:
        asyncio.run(_login())
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
