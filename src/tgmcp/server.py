"""MCP tool definitions. Every tool is read-only and limited to the allow-listed chats."""

from __future__ import annotations

import functools
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .formatting import fmt_date, format_chat, format_message, format_messages
from .limits import BusyError, CallGate
from .telegram import AccessError, Discussion, TelegramService, parse_when

INSTRUCTIONS = """\
This server gives read-only access to a fixed list of Telegram chats (communities, channels, forums)
so you can find what people asked, discussed and answered there. You cannot send anything.

Suggested workflow:
1. `list_chats` once to see which chats exist and what they are about.
2. `search_discussions` for "how did people solve X / what do people think about Y" questions — it
   returns whole threads (question + answers). Use `search_messages` for quick fact lookups and
   `find_questions` to see what people ask about a topic (optionally with answers / only unanswered).
3. Drill down with `get_thread`, `get_message_context` (messages around a hit — many chats reply
   without the reply button, so context matters) and `get_chat_history` (recent messages / a date range).

Search tips: Telegram search is keyword-based (no semantics, weak morphology). Pass several `queries`
at once: synonyms, both languages (e.g. Russian and English), short word stems ("настро" for
настройка/настроить), product names and common misspellings. Prefer 1–2 word queries over phrases.
If nothing is found, broaden: fewer words, other stems, a wider date range, or browse history.
Always cite findings with the message links returned by the tools.
"""

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

ChatsArg = Annotated[
    list[str] | None,
    Field(description="Chats to search (id, @username, link or title from list_chats). Omit to use all allowed chats."),
]
ChatArg = Annotated[str, Field(description="One chat: id, @username, t.me link or title from list_chats")]
QueriesArg = Annotated[
    list[str],
    Field(
        description="One or more keyword queries; variants/synonyms/translations are searched in parallel and merged",
        min_length=1,
    ),
]
FromArg = Annotated[
    str | None, Field(description="Only messages after this: YYYY-MM-DD, ISO datetime, or relative (12h, 7d, 2w, 3m = 3 months, 1y)")
]
ToArg = Annotated[str | None, Field(description="Only messages before this: YYYY-MM-DD (inclusive) or ISO datetime")]
SenderArg = Annotated[
    str | None, Field(description="Only messages from this sender: @username, user id, or part of the display name")
]
TopicArg = Annotated[int | None, Field(description="Forum chats only: restrict to this topic id (see list_topics)")]
MediaArg = Annotated[
    Literal["links", "documents", "photos", "videos", "photos_videos", "voice", "music", "polls", "pinned"] | None,
    Field(description="Only messages of this kind, e.g. 'links' or 'documents' for shared resources, 'pinned' for FAQs"),
]
MaxCharsArg = Annotated[
    int | None, Field(description="Truncate each message text to this many characters (default from server config)", ge=50)
]


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _render_discussion(i: int, d: Discussion, max_chars: int) -> str:
    head = f"=== Discussion {i}: [{d.chat.title}] started {fmt_date(d.root.date)}"
    if d.total_replies is not None:
        head += f", {d.total_replies} replies in thread"
    lines = [head, ("» " if d.root.id in d.hit_ids else "") + "ROOT:"]
    lines.append(format_message(d.root, max_chars, show_chat=False, indent="  "))
    if d.replies:
        lines.append(f"REPLIES ({len(d.replies)} shown, » = matched your query):")
        for r in d.replies:
            mark = "» " if r.id in d.hit_ids else "  "
            lines.append(format_message(r, max_chars, show_chat=False, indent=mark))
    else:
        lines.append("(no replies found via reply links — try get_message_context for loose follow-ups)")
    return "\n".join(lines)


def build_server(tg: TelegramService, gate: CallGate | None = None) -> MCPServer:
    cfg = tg.config
    default_chars = cfg.max_message_chars
    gate = gate or CallGate(cfg.max_concurrent_calls, cfg.queue_timeout, cfg.max_queue)
    mcp = MCPServer(
        "tgmcp",
        title="Telegram chat search",
        instructions=INSTRUCTIONS,
        version=__version__,
    )
    mcp.gate = gate  # exposed for the /health endpoint

    def tool(fn):
        """Register a read-only tool whose executions are queued through the shared CallGate."""

        @functools.wraps(fn)
        async def gated(*args, **kwargs):
            try:
                async with gate.slot():
                    return await fn(*args, **kwargs)
            except BusyError as e:
                raise ToolError(str(e)) from e

        return mcp.tool(annotations=RO)(gated)

    def chars(v: int | None) -> int:
        return v or default_chars

    def dates(from_date: str | None, to_date: str | None):
        try:
            return parse_when(from_date), parse_when(to_date, end_of_day=True)
        except ValueError as e:
            raise ToolError(str(e)) from e

    def chats_or_error(refs):
        try:
            return tg.resolve_many(refs)
        except AccessError as e:
            raise ToolError(str(e)) from e

    def chat_or_error(ref):
        try:
            return tg.resolve(ref)
        except AccessError as e:
            raise ToolError(str(e)) from e

    @tool
    async def list_chats() -> str:
        """List the Telegram chats this server may read: titles, ids, type, whether it is a forum, description."""
        out = [f"{len(tg.chats)} allowed chats:"]
        out += [f"- {format_chat(c)}" for c in tg.chats.values()]
        if tg.unresolved:
            out.append("\nConfigured but unavailable: " + "; ".join(f"{r} ({e})" for r, e in tg.unresolved))
        return "\n".join(out)

    @tool
    async def search_messages(
        queries: QueriesArg,
        chats: ChatsArg = None,
        from_date: FromArg = None,
        to_date: ToArg = None,
        sender: SenderArg = None,
        topic_id: TopicArg = None,
        media: MediaArg = None,
        limit: Annotated[int, Field(description="Max messages to return overall", ge=1, le=200)] = 30,
        max_chars: MaxCharsArg = None,
    ) -> str:
        """Keyword search across the allowed chats. Returns matching messages (most query variants matched, then newest
        first) with sender, date, reply info and t.me links. Use search_discussions to get the surrounding threads."""
        cs = chats_or_error(chats)
        fd, td = dates(from_date, to_date)
        per_chat = _clamp(limit, 10, 100)
        try:
            msgs, errs = await tg.search(
                cs, queries, limit=limit, per_chat_limit=per_chat,
                from_date=fd, to_date=td, sender=sender, topic_id=topic_id, media=media,
            )
        except ValueError as e:
            raise ToolError(str(e)) from e
        head = f"Found {len(msgs)} messages for {queries!r} in {len(cs)} chat(s)."
        if not msgs:
            head += " Try other keywords/stems/synonyms, another language, or a wider date range."
        if errs:
            head += "\nErrors: " + "; ".join(errs)
        return head + ("\n\n" + format_messages(msgs, chars(max_chars)) if msgs else "")

    @tool
    async def search_discussions(
        queries: QueriesArg,
        chats: ChatsArg = None,
        from_date: FromArg = None,
        to_date: ToArg = None,
        sender: SenderArg = None,
        topic_id: TopicArg = None,
        max_discussions: Annotated[int, Field(description="How many threads to expand", ge=1, le=20)] = 6,
        max_replies: Annotated[int, Field(description="Max replies per thread", ge=1, le=200)] = 40,
        max_chars: MaxCharsArg = None,
    ) -> str:
        """Find discussions about a topic: searches the keywords, then expands each hit into its whole thread
        (the original question/post, the reply chain and the answers). Best tool for "what did people say / how was
        X solved" questions."""
        cs = chats_or_error(chats)
        fd, td = dates(from_date, to_date)
        try:
            hits, errs = await tg.search(
                cs, queries, limit=max_discussions * 4, per_chat_limit=_clamp(max_discussions * 4, 10, 60),
                from_date=fd, to_date=td, sender=sender, topic_id=topic_id,
            )
        except ValueError as e:
            raise ToolError(str(e)) from e
        if not hits:
            msg = f"No messages found for {queries!r}. Try other keywords/stems/synonyms or a wider date range."
            return msg + ("\nErrors: " + "; ".join(errs) if errs else "")
        ds = await tg.discussions(hits, max_discussions, max_replies)
        mc = chars(max_chars) if max_chars else min(default_chars, 800)
        parts = [f"{len(ds)} discussion(s) for {queries!r} ({len(hits)} matching messages)."]
        if errs:
            parts.append("Errors: " + "; ".join(errs))
        parts += [_render_discussion(i, d, mc) for i, d in enumerate(ds, 1)]
        return "\n\n".join(parts)

    @tool
    async def find_questions(
        queries: Annotated[
            list[str] | None,
            Field(description="Keywords the questions should be about; omit to scan recent messages for any questions"),
        ] = None,
        chats: ChatsArg = None,
        from_date: FromArg = None,
        to_date: ToArg = None,
        topic_id: TopicArg = None,
        include_answers: Annotated[bool, Field(description="Attach the replies each question received")] = True,
        unanswered_only: Annotated[bool, Field(description="Only questions nobody else replied to")] = False,
        limit: Annotated[int, Field(description="Max questions to return", ge=1, le=50)] = 15,
        max_answers: Annotated[int, Field(description="Max replies per question", ge=1, le=100)] = 15,
        scan_per_chat: Annotated[
            int, Field(description="How many matching/recent messages to scan per chat and query", ge=20, le=1000)
        ] = 200,
        max_chars: MaxCharsArg = None,
    ) -> str:
        """Find questions people asked in the chats (messages with '?' or starting with how/why/подскажите/etc.),
        optionally about given keywords, with the answers they got. Good for FAQs, common problems and open issues."""
        cs = chats_or_error(chats)
        fd, td = dates(from_date, to_date)
        try:
            found = await tg.questions(
                cs, queries or [], limit=limit, scan_per_chat=scan_per_chat,
                include_answers=include_answers, max_answers=max_answers, unanswered_only=unanswered_only,
                from_date=fd, to_date=td, topic_id=topic_id,
            )
        except ValueError as e:
            raise ToolError(str(e)) from e
        if not found:
            return "No questions found. Try other keywords, a wider date range or a larger scan_per_chat."
        mc = chars(max_chars) if max_chars else min(default_chars, 800)
        parts = [f"{len(found)} question(s):"]
        for i, (q, answers) in enumerate(found, 1):
            block = [f"=== Q{i}", format_message(q, mc)]
            if answers is not None:
                if answers:
                    block.append(f"ANSWERS ({len(answers)}):")
                    block += [format_message(a, mc, show_chat=False, indent="  ") for a in answers]
                else:
                    block.append("(no replies via reply links — check get_message_context for loose answers)")
            parts.append("\n".join(block))
        return "\n\n".join(parts)

    @tool
    async def get_chat_history(
        chat: ChatArg,
        from_date: FromArg = None,
        to_date: ToArg = None,
        sender: SenderArg = None,
        topic_id: TopicArg = None,
        media: MediaArg = None,
        oldest_first: Annotated[bool, Field(description="Return in chronological order starting at from_date")] = False,
        before_message_id: Annotated[
            int | None, Field(description="Paging: only messages older than this id (newest-first mode)")
        ] = None,
        after_message_id: Annotated[
            int | None, Field(description="Paging: only messages newer than this id (use with oldest_first)")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=300)] = 50,
        max_chars: MaxCharsArg = None,
    ) -> str:
        """Read messages of one chat without a search query: the latest messages, a date range, one forum topic or
        one sender. Use it for "what's new / what was discussed last week" and to page through history."""
        c = chat_or_error(chat)
        fd, td = dates(from_date, to_date)
        try:
            msgs = await tg.fetch(
                c, limit=limit, from_date=fd, to_date=td, sender=sender, topic_id=topic_id, media=media,
                offset_id=before_message_id or 0, min_id=after_message_id or 0, reverse=oldest_first,
            )
        except (AccessError, ValueError) as e:
            raise ToolError(str(e)) from e
        if not oldest_first:
            msgs.reverse()  # present chronologically
        if not msgs:
            return f"No messages in {c.title} for these filters."
        tail = ""
        if len(msgs) >= limit:
            if oldest_first:
                tail = f"\n\n(limit reached; for newer messages call again with after_message_id={msgs[-1].id})"
            else:
                tail = f"\n\n(limit reached; for older messages call again with before_message_id={msgs[0].id})"
        return f"{len(msgs)} messages from {c.title}, oldest first:\n\n" + format_messages(
            msgs, chars(max_chars), show_chat=False
        ) + tail

    @tool
    async def get_message_context(
        chat: ChatArg,
        message_id: int,
        before: Annotated[int, Field(description="Messages before", ge=0, le=100)] = 10,
        after: Annotated[int, Field(description="Messages after", ge=0, le=100)] = 15,
        max_chars: MaxCharsArg = None,
    ) -> str:
        """Show the messages around one message in chronological order — useful because people often answer
        without using the reply button."""
        c = chat_or_error(chat)
        try:
            msgs = await tg.context(c, message_id, before, after)
        except AccessError as e:
            raise ToolError(str(e)) from e
        if not msgs:
            return f"Message #{message_id} not found in {c.title}."
        lines = [f"Context of #{message_id} in {c.title} (» = the message):"]
        for m in msgs:
            lines.append(format_message(m, chars(max_chars), show_chat=False, indent="» " if m.id == message_id else ""))
        return "\n\n".join(lines)

    @tool
    async def get_thread(
        chat: ChatArg,
        message_id: int,
        max_replies: Annotated[int, Field(ge=1, le=300)] = 100,
        max_chars: MaxCharsArg = None,
    ) -> str:
        """Get the full reply thread a message belongs to: the chain of messages it replies to up to the root, and
        all replies to the root. For channel posts this returns the comments."""
        c = chat_or_error(chat)
        try:
            got = await tg.get_by_ids(c, [message_id])
            if not got:
                raise ToolError(f"Message #{message_id} not found in {c.title}.")
            d = await tg.discussion_for(c, got[0], max_replies)
        except AccessError as e:
            raise ToolError(str(e)) from e
        return _render_discussion(1, d, chars(max_chars)).replace("=== Discussion 1:", "=== Thread:", 1)

    @tool
    async def get_messages(
        chat: ChatArg,
        message_ids: Annotated[list[int], Field(min_length=1, max_length=100)],
        max_chars: MaxCharsArg = None,
    ) -> str:
        """Fetch specific messages by id (e.g. ids seen in 'reply to #123' or taken from a t.me link)."""
        c = chat_or_error(chat)
        try:
            msgs = await tg.get_by_ids(c, message_ids)
        except AccessError as e:
            raise ToolError(str(e)) from e
        if not msgs:
            return "None of these messages exist (deleted?)."
        return format_messages(msgs, chars(max_chars), show_chat=False)

    @tool
    async def list_topics(
        chat: Annotated[str | None, Field(description="Forum chat; omit to list topics of every forum chat")] = None,
        query: Annotated[str | None, Field(description="Filter topics by title")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> str:
        """List forum topics (sub-chats) of forum-type chats, optionally filtered by title. Use the topic id as
        topic_id in other tools to search or read inside one topic."""
        cs = [chat_or_error(chat)] if chat else [c for c in tg.chats.values() if c.forum]
        if not cs:
            return "None of the allowed chats is a forum."
        out = []
        for c in cs:
            try:
                ts = await tg.topics(c, query, limit)
            except AccessError as e:
                out.append(f"{c.title}: {e}")
                continue
            out.append(f"{c.title} ({c.ref}): {len(ts)} topic(s)")
            for t in ts:
                flags = ", ".join(f for f, on in (("pinned", t["pinned"]), ("closed", t["closed"])) if on)
                out.append(
                    f"  - topic_id {t['id']}: {t['title']} (created {fmt_date(t['date'])}"
                    + (f", {flags}" if flags else "")
                    + (f") {t['link']}" if t["link"] else ")")
                )
        return "\n".join(out)

    return mcp

