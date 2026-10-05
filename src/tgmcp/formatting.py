"""Turn Telegram messages into compact, citeable text for the model."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import unmarked_channel_id


@dataclass
class ChatInfo:
    id: int  # marked peer id (-100... for channels/supergroups)
    title: str
    username: str | None
    kind: str  # "channel" | "supergroup" | "group" | "user"
    forum: bool = False
    members: int | None = None
    about: str | None = None
    linked_chat_id: int | None = None
    profile: Any = None  # profiles.ChatProfile, filled at startup

    @property
    def ref(self) -> str:
        return f"@{self.username}" if self.username else str(self.id)

    def link(self, msg_id: int, topic_id: int | None = None) -> str | None:
        if self.username:
            base = f"https://t.me/{self.username}"
        elif self.kind in {"channel", "supergroup"}:
            base = f"https://t.me/c/{unmarked_channel_id(self.id)}"
        else:
            return None  # basic groups and private chats have no message links
        if topic_id and self.forum:
            return f"{base}/{topic_id}/{msg_id}"
        return f"{base}/{msg_id}"


@dataclass
class MsgView:
    chat: ChatInfo
    id: int
    date: datetime
    sender: str
    text: str
    reply_to_id: int | None = None
    topic_id: int | None = None
    replies: int | None = None
    media: str | None = None
    forwarded_from: str | None = None
    edited: bool = False
    reactions: int | None = None
    sender_id: int | None = None
    service: bool = False

    @property
    def key(self) -> tuple[int, int]:
        return (self.chat.id, self.id)


def fmt_date(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + f" …[+{len(text) - limit} chars]"


def format_message(
    m: MsgView, max_chars: int = 1500, show_chat: bool = True, indent: str = "", label: str | None = None
) -> str:
    head = [f"({label})"] if label else []
    if show_chat:
        head.append(f"[{m.chat.title}]")
    head.append(f"#{m.id}")
    head.append(fmt_date(m.date))
    head.append(m.sender)
    meta = []
    if m.reply_to_id:
        meta.append(f"reply to #{m.reply_to_id}")
    if m.topic_id and m.chat.forum:
        meta.append(f"topic #{m.topic_id}")
    if m.replies:
        meta.append(f"{m.replies} replies")
    if m.reactions:
        meta.append(f"{m.reactions} reactions")
    if m.forwarded_from:
        meta.append(f"fwd from {m.forwarded_from}")
    if m.edited:
        meta.append("edited")
    line = " | ".join(head)
    if meta:
        line += " (" + ", ".join(meta) + ")"
    body_parts = []
    if m.media:
        body_parts.append(f"[{m.media}]")
    if m.text:
        body_parts.append(truncate(m.text, max_chars))
    body = " ".join(body_parts) or "[empty]"
    out = [indent + line]
    link = m.chat.link(m.id, m.topic_id)
    if link:
        out.append(indent + link)
    out.extend(indent + "  " + ln for ln in body.splitlines() or [""])
    return "\n".join(out)


def format_messages(msgs: list[MsgView], max_chars: int = 1500, show_chat: bool = True) -> str:
    return "\n\n".join(format_message(m, max_chars, show_chat) for m in msgs)


# --- question detection -----------------------------------------------------

_QUESTION_WORDS = (
    # ru
    "как", "где", "когда", "почему", "зачем", "кто", "что", "чем", "какой", "какая", "какое", "какие",
    "сколько", "куда", "откуда", "можно ли", "есть ли", "подскажите", "подскажи", "посоветуйте",
    "посоветуй", "помогите", "помоги", "кто-нибудь", "кто нибудь", "ктонибудь", "не подскажете",
    "не знаете", "знает кто", "кто знает", "вопрос", "нужна помощь", "нужен совет", "в чем проблема",
    "в чём проблема", "реально ли", "стоит ли", "имеет смысл",
    # uk
    "чи", "підкажіть", "допоможіть",
    # en
    "how", "what", "why", "where", "when", "which", "who", "is there", "are there", "can i", "can you",
    "could", "does anyone", "anyone know", "any idea", "help", "should i", "is it possible",
)
_QUESTION_START_RE = re.compile(
    r"^\W*(" + "|".join(re.escape(w) for w in sorted(_QUESTION_WORDS, key=len, reverse=True)) + r")(?![\w-])",
    re.I,
)


def looks_like_question(text: str | None) -> bool:
    """Cheap heuristic: a question mark, or a sentence that starts with a question/help word."""
    if not text:
        return False
    t = text.strip()
    if len(t) < 8:
        return False
    if "?" in t:
        return True
    return any(_QUESTION_START_RE.match(s) for s in re.split(r"(?<=[.!\n])\s+", t)[:3])
