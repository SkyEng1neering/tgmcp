"""MCP tool definitions. Every tool is read-only and limited to the allow-listed chats."""

from __future__ import annotations

import functools
from datetime import timedelta
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .formatting import fmt_date, format_message, format_messages
from .limits import BusyError, CallGate
from .profiles import format_catalog, format_profile
from .telegram import AccessError, Discussion, FollowUp, TelegramService, parse_when

INSTRUCTIONS = """\
This server is a guide to a fixed set of Telegram chats (communities, channels, forums): it finds what people
asked, discussed and answered there. Access is read-only; nothing can be sent.

CHATS AVAILABLE (profiled at startup from the description, pinned message, forum topics and recent messages):
{catalog}

Choosing where to search: match the user's question against the chats above and pass the relevant ones in
`chats` (several if more than one fits). If the question is outside what these chats cover, say so and tell
the user which subjects the chats do cover, instead of searching; only search everything if the user asks
for that explicitly. `list_chats` gives fuller profiles (pinned message, all topics, recent questions).

Suggested workflow:
1. Pick chats as described above.
2. `search_discussions` for "how did people solve X / what do people think about Y" questions — it
   returns whole conversations: the question, reply-linked answers AND the messages written right after it
   without the reply button (tagged "after"; many people answer that way, but some of those are unrelated
   chatter — judge by content). When a hit is a loose answer, the messages just before it are shown too. Use `search_messages` for quick fact lookups and
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
FollowUpsArg = Annotated[
    int,
    Field(
        description="Also read this many messages written after the question/root WITHOUT a reply link (people "
        "often answer that way); 0 disables",
        ge=0, le=50,
    ),
]
FollowWindowArg = Annotated[
    float, Field(description="How many hours after the question to look for such follow-up messages", gt=0, le=720)
]
MaxCharsArg = Annotated[
    int | None, Field(description="Truncate each message text to this many characters (default from server config)", ge=50)
]


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _render_discussion(title: str, d: Discussion, max_chars: int) -> str:
    """Root, then replies and follow-ups in chronological order.

    (reply) = linked to the conversation by Telegram's reply button; (after) = written after it without a reply link.
    » marks messages that matched the search.
    """
    head = f"=== {title}: [{d.chat.title}] {fmt_date(d.root.date)}"
    if d.total_replies:
        head += f", {d.total_replies} reply-linked messages in thread"
    lines = [head]
    if d.preceding:
        lines.append("BEFORE (what the root may be answering):")
        lines += [format_message(m, max_chars, show_chat=False, indent="  ") for m in d.preceding]
        lines.append("ROOT:")
    mark = "» " if d.root.key in d.hit_ids else ""
    lines.append(format_message(d.root, max_chars, show_chat=False, indent=mark))
    conv = d.conversation()
    if conv:
        n_after = len(d.follow_ups)
        lines.append(
            f"CONVERSATION ({len(conv) - n_after} reply-linked, {n_after} written after without a reply link):"
        )
        for tag, m in conv:
            ind = "» " if m.key in d.hit_ids else "  "
            lines.append(format_message(m, max_chars, show_chat=False, indent=ind, label=tag))
    else:
        lines.append("(no replies and no follow-up messages found — widen follow_window_hours or check "
                     "get_message_context)")
    return "\n".join(lines)


def _follow(follow_ups: int, hours: float) -> FollowUp:
    return FollowUp(limit=follow_ups, window=timedelta(hours=hours))


def build_server(tg: TelegramService, gate: CallGate | None = None) -> MCPServer:
    cfg = tg.config
    default_chars = cfg.max_message_chars
    gate = gate or CallGate(cfg.max_concurrent_calls, cfg.queue_timeout, cfg.max_queue)
    mcp = MCPServer(
        "tgmcp",
        title="Telegram chat search",
        instructions=INSTRUCTIONS.format(catalog=format_catalog(list(tg.chats.values()))),
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

    def check_topic(cs, topic_id):
        """Topic ids only mean something inside one forum chat."""
        if topic_id is None:
            return
        if len(cs) != 1:
            raise ToolError("topic_id refers to a topic of one forum chat: pass exactly that chat in `chats`")
        if not cs[0].forum:
            raise ToolError(f"{cs[0].title} is not a forum, so it has no topics; drop topic_id")

    def chat_or_error(ref):
        try:
            return tg.resolve(ref)
        except AccessError as e:
            raise ToolError(str(e)) from e

    @tool
    async def list_chats() -> str:
        """Full profiles of the chats this server covers: description, pinned message, forum topics, activity,
        frequent words and recent questions. Use it to decide which chats fit a question and what they can answer."""
        out = [f"{len(tg.chats)} chats available:", ""]
        out += [format_profile(c) + "\n" for c in tg.chats.values()]
        if tg.unresolved:
            out.append("Configured but unavailable: " + "; ".join(f"{r} ({e})" for r, e in tg.unresolved))
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
        check_topic(cs, topic_id)
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
        max_replies: Annotated[int, Field(description="Max reply-linked messages per thread", ge=1, le=200)] = 40,
        follow_ups: FollowUpsArg = 10,
        follow_window_hours: FollowWindowArg = 24,
        max_chars: MaxCharsArg = None,
    ) -> str:
        """Find discussions about a topic: searches the keywords, then expands each hit into its whole conversation —
        the original question/post, reply-linked answers and the messages written after it without the reply button
        (and, if the hit is a loose answer, the messages just before it). Best tool for "what did people say / how
        was X solved" questions."""
        cs = chats_or_error(chats)
        check_topic(cs, topic_id)
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
        ds = await tg.discussions(hits, max_discussions, max_replies, _follow(follow_ups, follow_window_hours))
        mc = chars(max_chars) if max_chars else min(default_chars, 800)
        parts = [f"{len(ds)} discussion(s) for {queries!r} ({len(hits)} matching messages)."]
        if errs:
            parts.append("Errors: " + "; ".join(errs))
        parts += [_render_discussion(f"Discussion {i}", d, mc) for i, d in enumerate(ds, 1)]
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
        include_answers: Annotated[
            bool, Field(description="Attach the replies and the messages written after each question")
        ] = True,
        unanswered_only: Annotated[
            bool,
            Field(description="Only questions with no reply and no follow-up message from anyone else within "
                  "follow_window_hours (in busy chats unrelated chatter also counts, so this is conservative)"),
        ] = False,
        limit: Annotated[int, Field(description="Max questions to return", ge=1, le=50)] = 15,
        max_answers: Annotated[int, Field(description="Max reply-linked answers per question", ge=1, le=100)] = 15,
        follow_ups: FollowUpsArg = 8,
        follow_window_hours: FollowWindowArg = 24,
        scan_per_chat: Annotated[
            int, Field(description="How many matching/recent messages to scan per chat and query", ge=20, le=1000)
        ] = 200,
        max_chars: MaxCharsArg = None,
    ) -> str:
        """Find questions people asked in the chats (messages with '?' or starting with how/why/подскажите/etc.),
        optionally about given keywords, with the answers they got — both reply-linked and messages written right
        after the question. Good for FAQs, common problems and open issues."""
        cs = chats_or_error(chats)
        check_topic(cs, topic_id)
        fd, td = dates(from_date, to_date)
        try:
            found = await tg.questions(
                cs, queries or [], limit=limit, scan_per_chat=scan_per_chat,
                include_answers=include_answers, max_answers=max_answers, unanswered_only=unanswered_only,
                fu=_follow(follow_ups, follow_window_hours), from_date=fd, to_date=td, topic_id=topic_id,
            )
        except ValueError as e:
            raise ToolError(str(e)) from e
        if not found:
            return "No questions found. Try other keywords, a wider date range or a larger scan_per_chat."
        mc = chars(max_chars) if max_chars else min(default_chars, 800)
        parts = [f"{len(found)} question(s):"]
        for i, d in enumerate(found, 1):
            if include_answers:
                parts.append(_render_discussion(f"Q{i}", d, mc))
            else:
                parts.append(f"=== Q{i}\n" + format_message(d.root, mc))
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
        check_topic([c], topic_id)
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
        follow_ups: FollowUpsArg = 15,
        follow_window_hours: FollowWindowArg = 24,
        max_chars: MaxCharsArg = None,
    ) -> str:
        """Get the whole conversation a message belongs to: the chain of messages it replies to up to the root, all
        replies to the root, and the messages written after the root without a reply link. For channel posts this
        returns the comments."""
        c = chat_or_error(chat)
        try:
            got = await tg.get_by_ids(c, [message_id])
            if not got:
                raise ToolError(f"Message #{message_id} not found in {c.title}.")
            d = await tg.discussion_for(c, got[0], max_replies, _follow(follow_ups, follow_window_hours))
        except AccessError as e:
            raise ToolError(str(e)) from e
        return _render_discussion("Thread", d, chars(max_chars))

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

