"""End-to-end: real uvicorn + MCP client over streamable HTTP, auth, and many concurrent clients."""

import asyncio
import dataclasses
import socket

import httpx
import uvicorn
from mcp.client.client import Client

from fakes import make_service, msg
from tgmcp.limits import CallGate
from tgmcp.main import build_http_app
from tgmcp.server import build_server

TOKEN = "t" * 32


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _serve(port: int, gate: CallGate):
    svc = make_service([msg(1, "nginx websocket?"), msg(2, "use proxy_set_header", reply_to=1)])
    svc.config = dataclasses.replace(svc.config, auth_token=TOKEN, host="127.0.0.1", port=port)
    app = build_http_app(build_server(svc, gate), svc.config, svc)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    return server, task


def test_http_auth_and_concurrent_clients():
    async def go():
        port = _free_port()
        gate = CallGate(limit=2, timeout=30, max_queue=100)
        server, task = await _serve(port, gate)
        base = f"http://127.0.0.1:{port}"
        try:
            async with httpx.AsyncClient() as h:
                assert (await h.get(f"{base}/health")).json()["status"] == "ok"
                assert (await h.post(f"{base}/mcp", json={})).status_code == 401
                assert (await h.post(f"{base}/wrong-token/mcp", json={})).status_code == 401
                r = await h.post(f"{base}/mcp", json={}, headers={"Authorization": f"Bearer {TOKEN}"})
                assert r.status_code != 401

            async def one_client(i):
                async with Client(f"{base}/{TOKEN}/mcp") as c:
                    res = await c.call_tool("search_discussions", {"queries": ["nginx"]})
                    assert not res.is_error, res
                    return res.content[0].text

            texts = await asyncio.gather(*(one_client(i) for i in range(20)))
            assert all("ROOT:" in t and "proxy_set_header" in t for t in texts)
            assert gate.running == 0 and gate.waiting == 0
        finally:
            server.should_exit = True
            await task

    asyncio.run(go())
