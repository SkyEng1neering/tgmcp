"""A tiny in-memory stand-in for the parts of TelegramClient that TelegramService uses."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from telethon import errors, types

from tgmcp.config import Config, marked_channel_id, parse_chat_list
from tgmcp.formatting import ChatInfo
from tgmcp.telegram import TelegramService

BASE = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
CHANNEL_ID = 111


def msg(mid: int, text: str, reply_to: int | None = None, top: int | None = None, minutes: int | None = None):
    rt = types.MessageReplyHeader(reply_to_msg_id=reply_to, reply_to_top_id=top) if reply_to else None
    return types.Message(
        id=mid,
        peer_id=types.PeerChannel(CHANNEL_ID),
        date=BASE + timedelta(minutes=mid if minutes is None else minutes),
        message=text,
        reply_to=rt,
    )


class _Iter:
    def __init__(self, items):
        self._items = items
        self.total = len(items)

    def __aiter__(self):
        self._it = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class FakeClient:
    def __init__(self, messages, supports_replies: bool = True):
        self.messages = sorted(messages, key=lambda m: m.id)
        self.supports_replies = supports_replies

    def _thread_root(self, m):
        rt = m.reply_to
        if rt is None:
            return None
        return rt.reply_to_top_id or rt.reply_to_msg_id

    def iter_messages(self, entity, limit=None, *, search=None, offset_id=0, min_id=0, reverse=False,
                      reply_to=None, offset_date=None, from_user=None, filter=None, max_id=0, **_):
        items = list(self.messages)
        if reply_to is not None:
            if not self.supports_replies:
                raise errors.MsgIdInvalidError(request=None)
            items = [m for m in items if self._thread_root(m) == reply_to]
        if search:
            items = [m for m in items if search.lower() in (m.message or "").lower()]
        if offset_id:
            items = [m for m in items if m.id < offset_id]
        if min_id:
            items = [m for m in items if m.id > min_id]
        if max_id:
            items = [m for m in items if m.id < max_id]
        if offset_date and not reverse:
            items = [m for m in items if m.date < offset_date]
        if offset_date and reverse:
            items = [m for m in items if m.date > offset_date]
        items = items if reverse else items[::-1]
        if limit is not None:
            items = items[:limit]
        return _Iter(items)

    def is_connected(self):
        return True

    async def get_messages(self, entity, ids):
        by_id = {m.id: m for m in self.messages}
        return [by_id.get(i) for i in ids]


def make_service(messages, supports_replies: bool = True, forum: bool = False, kind: str = "supergroup") -> TelegramService:
    cfg = Config(api_id=1, api_hash="x", session="s", chats=parse_chat_list("@devchat"))
    svc = TelegramService(cfg, client=FakeClient(messages, supports_replies))
    chat = ChatInfo(id=marked_channel_id(CHANNEL_ID), title="Dev Chat", username="devchat", kind=kind, forum=forum)
    svc.chats = {chat.id: chat}
    svc._entities = {chat.id: types.Channel(id=CHANNEL_ID, title="Dev Chat", photo=types.ChatPhotoEmpty(),
                                            date=BASE, megagroup=kind == "supergroup")}
    return svc
