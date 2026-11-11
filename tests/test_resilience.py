"""Behaviour under trouble: slow or unreachable Telegram, oversized results, request logging."""

import asyncio
import dataclasses
import logging

from mcp.client.client import Client

from fakes import FakeClient, make_service, msg
from tgmcp.httplog import RequestLogMiddleware, describe_rpc, mask_path
from tgmcp.main import telegram_watchdog
from tgmcp.server import build_server

MSGS = [msg(1, "nginx websocket?"), msg(2, "use proxy_set_header", reply_to=1)]


def _configure(svc, **overrides):
    svc.config = dataclasses.replace(svc.config, **overrides)
    return svc


def test_slow_call_is_cancelled_with_a_clear_error():
    class Slow(FakeClient):
        def iter_messages(self, *a, **kw):
            inner = super().iter_messages(*a, **kw)

            class It:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    await asyncio.sleep(2)
                    return await inner.__anext__()

            return It()

    async def go():
        svc = _configure(make_service(MSGS), call_timeout=5.0)
        svc.client = Slow(MSGS)
        svc.config = dataclasses.replace(svc.config, call_timeout=0.3)
        async with Client(build_server(svc)) as c:
            res = await c.call_tool("search_messages", {"queries": ["nginx"]})
            assert res.is_error and "took longer than 0s" in res.content[0].text

    asyncio.run(go())


def test_oversized_result_is_cut_with_a_hint(caplog):
    async def go():
        many = [msg(i, "nginx " + "подробности " * 30) for i in range(1, 25)]
        svc = _configure(make_service(many), max_result_chars=2000)
        with caplog.at_level(logging.INFO, logger="tgmcp.server"):
            async with Client(build_server(svc)) as c:
                res = await c.call_tool("search_messages", {"queries": ["nginx"], "limit": 24})
        text = res.content[0].text
        assert not res.is_error and len(text) < 2300 and "result cut at 2000 chars" in text
        assert "tool search_messages {queries=['nginx'], limit=24} -> ok, truncated from" in caplog.text

    asyncio.run(go())


def test_unreachable_telegram_gives_retry_hint(caplog):
    class Down(FakeClient):
        def iter_messages(self, *a, **kw):
            raise ConnectionError("Cannot send requests while disconnected")

    async def go():
        svc = make_service(MSGS)
        svc.client = Down(MSGS)
        with caplog.at_level(logging.INFO, logger="tgmcp.server"):
            async with Client(build_server(svc)) as c:
                res = await c.call_tool("search_messages", {"queries": ["nginx"]})
        assert res.is_error and "Telegram is unreachable" in res.content[0].text
        assert "telegram unreachable" in caplog.text

    asyncio.run(go())


def test_connect_retries_through_an_outage(caplog):
    class Flaky(FakeClient):
        attempts = 0

        async def connect(self):
            self.attempts += 1
            if self.attempts < 3:
                raise ConnectionError("Connection to Telegram failed 5 time(s)")

    async def go():
        svc = make_service(MSGS)
        svc.client = Flaky(MSGS)
        svc.reconnect_delays = (0.01,)
        with caplog.at_level(logging.WARNING, logger="tgmcp.telegram"):
            await svc.connect(retry_forever=True)
        assert svc.client.attempts == 3 and caplog.text.count("retrying in") == 2

        svc.client = Flaky(MSGS)
        try:
            await svc.connect(retry_forever=False)
        except ConnectionError:
            pass
        else:
            raise AssertionError("--check mode must fail fast")

    asyncio.run(go())


def test_watchdog_reconnects_and_rechecks_authorization():
    class Dropped(FakeClient):
        connects = 0

        def is_connected(self):
            return self.connects >= 2

        async def connect(self):
            self.connects += 1

        async def is_user_authorized(self):
            return False

    async def go():
        svc = make_service(MSGS)
        svc.client = Dropped(MSGS)
        task = asyncio.create_task(telegram_watchdog(svc, interval=0.01))
        await asyncio.sleep(0.2)
        task.cancel()
        assert svc.client.connects == 2 and svc.authorized is False

    asyncio.run(go())


def test_replies_fall_back_when_peer_is_invalid():
    from telethon import errors

    class NoThreads(FakeClient):
        def iter_messages(self, *a, reply_to=None, **kw):
            if reply_to is not None:
                raise errors.PeerIdInvalidError(request=None)
            return super().iter_messages(*a, **kw)

    svc = make_service(MSGS)
    svc.client = NoThreads(MSGS)
    chat = next(iter(svc.chats.values()))
    replies, _ = asyncio.run(svc.replies(chat, 1, 10))
    assert [r.id for r in replies] == [2]


def test_request_log_masks_token_and_names_the_tool(caplog):
    assert mask_path("/abcdefghijklmnopqrstuvwxyz0123/mcp") == "/<token>/mcp"
    assert mask_path("/mcp") == "/mcp" and mask_path("/health") == "/health"
    assert describe_rpc(b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"search_messages"}}') == (
        "tools/call search_messages"
    )
    assert describe_rpc(b'[{"method":"initialize"},{"result":{}}]') == "initialize, response"

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def go():
        sent = []
        body = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_thread"}}'
        scope = {"type": "http", "method": "POST", "path": "/abcdefghijklmnopqrstuvwxyz0123/mcp",
                 "headers": [(b"x-forwarded-for", b"203.0.113.9")], "client": ("127.0.0.1", 1)}

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            sent.append(message)

        with caplog.at_level(logging.INFO, logger="tgmcp.http"):
            await RequestLogMiddleware(app)(scope, receive, send)
        assert "POST /<token>/mcp [tools/call get_thread] -> 200" in caplog.text
        assert "from 203.0.113.9" in caplog.text and "abcdefghijklmnop" not in caplog.text

    asyncio.run(go())
