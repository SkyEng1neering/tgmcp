"""Read-only access to an allow-listed set of Telegram chats through a userbot (Telethon)."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, errors, functions, types, utils
from telethon.sessions import StringSession

from .config import ChatRef, Config, marked_channel_id
from .formatting import ChatInfo, MsgView, looks_like_question
from .limits import TTLCache
from .profiles import ChatProfile, profile_from_sample

log = logging.getLogger(__name__)

MEDIA_FILTERS = {
    "links": types.InputMessagesFilterUrl,
    "documents": types.InputMessagesFilterDocument,
    "photos": types.InputMessagesFilterPhotos,
    "videos": types.InputMessagesFilterVideo,
    "photos_videos": types.InputMessagesFilterPhotoVideo,
    "voice": types.InputMessagesFilterRoundVoice,
    "music": types.InputMessagesFilterMusic,
    "polls": types.InputMessagesFilterPoll,
    "pinned": types.InputMessagesFilterPinned,
}

_MAX_ANCESTOR_DEPTH = 15
_SCAN_LIMIT = 2000  # messages scanned to rebuild reply trees where Telegram has no threads
_MAX_SCAN = 3000  # upper bound on messages one request may page through (30 Telegram calls)


class AccessError(Exception):
    """Raised for chats outside the allow-list or messages that cannot be read."""


def parse_when(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    """Parse ISO dates (2025-01-31, 2025-01-31T10:00) or relative spans (7d, 12h, 2w, 3m, 1y)."""
    if value is None or not str(value).strip():
        return None
    s = str(value).strip().lower()
    if m := re.fullmatch(r"(\d+)\s*([hdwmy])", s):
        n, unit = int(m.group(1)), m.group(2)
        days = {"h": n / 24, "d": n, "w": 7 * n, "m": 30 * n, "y": 365 * n}[unit]
        # Rounded to the minute so repeated calls share cache entries.
        return (datetime.now(timezone.utc) - timedelta(days=days)).replace(second=0, microsecond=0)
    try:
        dt = datetime.fromisoformat(s.replace("z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"bad date {value!r}: use YYYY-MM-DD, ISO datetime, or relative like 7d / 12h / 3m") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end_of_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        dt += timedelta(days=1) - timedelta(microseconds=1)
    return dt


@dataclass
class Discussion:
    chat: ChatInfo
    root: MsgView
    replies: list[MsgView] = field(default_factory=list)  # linked to the root by reply chains
    follow_ups: list[MsgView] = field(default_factory=list)  # written after the root without a reply link
    preceding: list[MsgView] = field(default_factory=list)  # just before a root that isn't a question
    hit_ids: set[tuple[int, int]] = field(default_factory=set)  # MsgView.key: comments live in another chat
    total_replies: int | None = None

    def conversation(self) -> list[tuple[str, MsgView]]:
        """Replies and follow-ups merged chronologically, each tagged 'reply' or 'after'."""
        tagged = [("reply", m) for m in self.replies] + [("after", m) for m in self.follow_ups]
        return sorted(tagged, key=lambda t: t[1].id)

    def answers_by_others(self) -> list[MsgView]:
        return [m for _, m in self.conversation() if m.sender_id is None or m.sender_id != self.root.sender_id]


@dataclass(frozen=True)
class FollowUp:
    """How far to read on after a message, for answers posted without the reply button."""

    limit: int = 10
    window: timedelta = timedelta(hours=24)
    preceding: int = 3

    @classmethod
    def off(cls) -> FollowUp:
        return cls(limit=0, preceding=0)


def _keys(d: Discussion) -> set[tuple[int, int]]:
    return {d.root.key, *(m.key for m in d.replies), *(m.key for m in d.follow_ups)}


class TelegramService:
    def __init__(self, config: Config, client: TelegramClient | None = None):
        self.config = config
        self.client = client or TelegramClient(
            StringSession(config.session),
            config.api_id,
            config.api_hash,
            proxy=config.proxy,
            receive_updates=False,
            flood_sleep_threshold=60,
            device_model="tgmcp",
            app_version="tgmcp",
        )
        self.chats: dict[int, ChatInfo] = {}
        self._entities: dict[int, object] = {}
        self._foreign: dict[int, ChatInfo] = {}
        # Bounds concurrent Telegram requests across all clients (keeps us clear of flood limits).
        self._sem = asyncio.Semaphore(config.tg_parallel_requests)
        self.cache = TTLCache(config.cache_ttl)
        self.unresolved: list[tuple[str, str]] = []

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("TG_SESSION is not authorized: run `tgmcp-login` again to get a fresh session string")
        me = await self.client.get_me()
        log.info("Logged in as %s (id %s)", utils.get_display_name(me), me.id)
        await self._resolve_allowlist(self.config.chats)
        if not self.chats:
            raise RuntimeError("None of the chats in TG_CHATS could be resolved: " + repr(self.unresolved))
        for raw, err in self.unresolved:
            log.warning("Chat %r skipped: %s", raw, err)
        log.info("Allowed chats: %s", ", ".join(f"{c.title} ({c.ref})" for c in self.chats.values()))
        await asyncio.gather(*(self._profile(c) for c in self.chats.values()))

    async def _profile(self, chat: ChatInfo) -> None:
        """Learn what a chat is about: pinned message, forum topics and a sample of recent messages."""
        profile = ChatProfile()
        chat.profile = profile
        # Best-effort: Telethon can also raise ValueError/ConnectionError on flaky links, and a missing profile
        # must not stop the server from starting.
        try:
            pinned = await self.fetch(chat, media="pinned", limit=3)
            profile.pinned = "\n---\n".join(m.text for m in pinned if m.text) or None
        except Exception as e:  # noqa: BLE001
            log.warning("pinned messages of %s unavailable: %s", chat.title, e)
        if chat.forum:
            try:
                profile.topics = [t["title"] for t in await self.topics(chat, None, 100)]
            except Exception as e:  # noqa: BLE001
                log.warning("topics of %s unavailable: %s", chat.title, e)
        n = min(self.config.profile_sample, _MAX_SCAN)
        if n:
            try:
                profile_from_sample(profile, await self.fetch(chat, limit=n))
            except Exception as e:  # noqa: BLE001
                log.warning("message sample of %s unavailable: %s", chat.title, e)
        log.info(
            "Profiled %s: %d messages, %d topics, keywords: %s",
            chat.title, profile.sampled, len(profile.topics), ", ".join(profile.keywords[:8]) or "-",
        )

    async def stop(self) -> None:
        await self.client.disconnect()

    async def _resolve_allowlist(self, refs: list[ChatRef]) -> None:
        by_id: dict[int, ChatRef] = {}
        for ref in refs:
            try:
                if ref.kind == "username":
                    entity = await self.client.get_entity(ref.value)
                elif ref.kind == "invite":
                    invite = await self.client(functions.messages.CheckChatInviteRequest(ref.value))
                    if not isinstance(invite, types.ChatInviteAlready):
                        raise AccessError("the userbot is not a member of this private chat; join it first")
                    entity = invite.chat
                else:
                    by_id[int(ref.value)] = ref
                    continue
                await self._add_allowed(entity)
            except Exception as e:  # noqa: BLE001 - report and keep going with the rest
                self.unresolved.append((ref.raw, str(e)))

        if by_id:
            # Numeric ids are only resolvable for chats the account has in its dialog list.
            async for dialog in self.client.iter_dialogs():
                pid = utils.get_peer_id(dialog.entity)
                if pid in by_id:
                    await self._add_allowed(dialog.entity)
                    by_id.pop(pid)
                    if not by_id:
                        break
            for ref in by_id.values():
                self.unresolved.append((ref.raw, "not found among the userbot's dialogs (is it a member?)"))

    async def _add_allowed(self, entity) -> None:
        info = self._chat_info(entity)
        try:
            if isinstance(entity, types.Channel):
                full = await self.client(functions.channels.GetFullChannelRequest(entity))
                info.about = full.full_chat.about or None
                info.members = full.full_chat.participants_count
                if full.full_chat.linked_chat_id and not entity.megagroup:
                    info.linked_chat_id = marked_channel_id(full.full_chat.linked_chat_id)
            elif isinstance(entity, types.Chat):
                full = await self.client(functions.messages.GetFullChatRequest(entity.id))
                info.about = full.full_chat.about or None
                info.members = entity.participants_count
        except errors.RPCError as e:
            log.debug("full info for %s unavailable: %s", info.title, e)
        self.chats[info.id] = info
        self._entities[info.id] = entity

    @staticmethod
    def _chat_info(entity) -> ChatInfo:
        if isinstance(entity, types.Channel):
            kind = "supergroup" if entity.megagroup or entity.gigagroup else "channel"
        elif isinstance(entity, types.Chat):
            kind = "group"
        else:
            kind = "user"
        return ChatInfo(
            id=utils.get_peer_id(entity),
            title=utils.get_display_name(entity) or str(utils.get_peer_id(entity)),
            username=(getattr(entity, "username", None) or None),
            kind=kind,
            forum=bool(getattr(entity, "forum", False)),
        )

    # --- allow-list lookups ---------------------------------------------------

    def resolve(self, ref: str | int) -> ChatInfo:
        """Map a user-supplied chat reference (id, @username, link or title) to an allowed chat."""
        s = str(ref).strip()
        if not s:
            raise AccessError("empty chat reference")
        if re.fullmatch(r"-?\d+", s):
            n = int(s)
            for cand in (n, marked_channel_id(n) if n > 0 else n, -n):
                if cand in self.chats:
                    return self.chats[cand]
        m = re.match(r"(?:https?://)?(?:t\.me|telegram\.me)/(c/)?([\w]+)", s, re.I)
        if m:
            if m.group(1):
                return self.resolve(str(marked_channel_id(int(m.group(2)))))
            s = m.group(2)
        uname = s.lstrip("@").lower()
        for c in self.chats.values():
            if c.username and c.username.lower() == uname:
                return c
        low = s.lower()
        exact = [c for c in self.chats.values() if c.title.lower() == low]
        if len(exact) == 1:
            return exact[0]
        partial = [c for c in self.chats.values() if low in c.title.lower()]
        if len(partial) == 1:
            return partial[0]
        allowed = "; ".join(f"{c.title} ({c.ref})" for c in self.chats.values())
        if len(partial) > 1:
            raise AccessError(f"chat {ref!r} is ambiguous; use the id or @username. Allowed chats: {allowed}")
        raise AccessError(f"chat {ref!r} is not in the allowed list. Allowed chats: {allowed}")

    def resolve_many(self, refs: list[str] | None) -> list[ChatInfo]:
        if not refs:
            return list(self.chats.values())
        out: dict[int, ChatInfo] = {}
        for r in refs:
            c = self.resolve(r)
            out[c.id] = c
        return list(out.values())

    def entity(self, chat: ChatInfo):
        return self._entities[chat.id]

    # --- message conversion ---------------------------------------------------

    def _info_for_message(self, msg, default: ChatInfo) -> ChatInfo:
        pid = msg.chat_id
        if pid is None or pid == default.id:
            return default
        if pid in self.chats:
            return self.chats[pid]
        # e.g. comments of a channel post live in the channel's linked discussion group
        if pid not in self._foreign:
            ent = msg.chat
            self._foreign[pid] = (
                self._chat_info(ent) if ent else ChatInfo(id=pid, title=str(pid), username=None, kind="supergroup")
            )
        return self._foreign[pid]

    @staticmethod
    def _sender_name(msg) -> str:
        s = msg.sender
        post_author = getattr(msg, "post_author", None)
        if s is None:
            if post_author:
                return post_author
            return f"id:{msg.sender_id}" if msg.sender_id else "unknown"
        name = utils.get_display_name(s) or "deleted account"
        if getattr(s, "username", None):
            name += f" (@{s.username})"
        if getattr(s, "bot", False):
            name += " [bot]"
        if post_author and isinstance(s, types.Channel):
            name += f" / {post_author}"
        return name

    @staticmethod
    def _media_desc(msg) -> str | None:
        if msg.media is None or isinstance(msg.media, types.MessageMediaWebPage):
            return None
        if msg.photo:
            return "photo"
        if msg.voice:
            return "voice message"
        if msg.video_note:
            return "video note"
        if msg.video:
            return "video"
        if msg.gif:
            return "gif"
        if msg.sticker:
            alt = next((a.alt for a in msg.document.attributes if isinstance(a, types.DocumentAttributeSticker)), "")
            return f"sticker {alt}".strip()
        if msg.audio:
            return "audio"
        if msg.poll:
            poll = msg.poll.poll
            q = poll.question.text if hasattr(poll.question, "text") else str(poll.question)
            opts = []
            for a in poll.answers:
                opts.append(a.text.text if hasattr(a.text, "text") else str(a.text))
            return f"poll: {q} / options: " + " | ".join(opts)
        if msg.geo:
            return "location"
        if msg.contact:
            return "contact"
        if msg.document:
            name = msg.file.name if msg.file else None
            return f"document: {name}" if name else "document"
        return type(msg.media).__name__.replace("MessageMedia", "").lower()

    @staticmethod
    def _thread_ids(msg, forum: bool) -> tuple[int | None, int | None]:
        """Return (reply_to_id, topic_id) taking forum topic semantics into account."""
        rt = msg.reply_to
        if not isinstance(rt, types.MessageReplyHeader):
            return None, (1 if forum else None)
        if forum and rt.forum_topic:
            reply_to, topic = (rt.reply_to_msg_id, rt.reply_to_top_id) if rt.reply_to_top_id else (None, rt.reply_to_msg_id)
        elif forum:
            reply_to, topic = rt.reply_to_msg_id, 1
        else:
            reply_to, topic = rt.reply_to_msg_id, None
        if rt.reply_to_peer_id is not None and utils.get_peer_id(rt.reply_to_peer_id) != msg.chat_id:
            reply_to = None  # a quote of a message from another chat: its id means nothing here
        return reply_to, topic

    @staticmethod
    def _rpc_error(chat: ChatInfo, e: Exception) -> AccessError:
        return AccessError(f"{chat.title}: Telegram error {type(e).__name__}: {e}")

    def to_view(self, msg, chat: ChatInfo) -> MsgView:
        info = self._info_for_message(msg, chat)
        reply_to, topic = self._thread_ids(msg, info.forum)
        service = isinstance(msg, types.MessageService)
        fwd = None
        if getattr(msg, "fwd_from", None):
            fwd = msg.fwd_from.from_name
            fw = msg.forward
            if not fwd and fw is not None:
                src = fw.sender or fw.chat
                fwd = utils.get_display_name(src) if src else None
            fwd = fwd or "unknown"
        reactions = None
        rs = getattr(msg, "reactions", None)
        if rs and rs.results:
            reactions = sum(r.count for r in rs.results)
        text = getattr(msg, "message", None) or ""
        if service:
            text = f"[service: {type(msg.action).__name__.replace('MessageAction', '')}]"
        replies = getattr(msg, "replies", None)
        return MsgView(
            chat=info,
            id=msg.id,
            date=msg.date,
            sender=self._sender_name(msg),
            text=text,
            reply_to_id=reply_to,
            topic_id=topic,
            replies=(replies.replies if replies else None),
            media=None if service else self._media_desc(msg),
            forwarded_from=fwd,
            edited=bool(getattr(msg, "edit_date", None)) and not getattr(msg, "edit_hide", False),
            reactions=reactions,
            sender_id=msg.sender_id,
            service=service,
        )

    # --- primitive reads -----------------------------------------------------

    async def _sender_filter(self, sender: str | None):
        """Return (from_user entity or None, name substring for client-side filtering or None)."""
        if not sender:
            return None, None
        s = sender.strip()
        if s.startswith("@") or re.fullmatch(r"-?\d+", s):
            try:
                return await self.client.get_input_entity(int(s) if s.lstrip("-").isdigit() else s), None
            except (ValueError, errors.RPCError):
                pass
        return None, s.lstrip("@").lower()

    async def fetch(self, chat: ChatInfo, **kw) -> list[MsgView]:
        """Search or page through one chat (newest first unless reverse=True); results are cached briefly."""
        key = ("fetch", chat.id, tuple(sorted(kw.items())))
        return list(await self.cache.get(key, lambda: self._fetch(chat, **kw)))

    async def _fetch(
        self,
        chat: ChatInfo,
        *,
        query: str | None = None,
        limit: int = 50,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        sender: str | None = None,
        topic_id: int | None = None,
        media: str | None = None,
        offset_id: int = 0,
        min_id: int = 0,
        reverse: bool = False,
    ) -> list[MsgView]:
        from_user, name_filter = await self._sender_filter(sender)
        flt = None
        if media:
            if media not in MEDIA_FILTERS:
                raise ValueError(f"unknown media filter {media!r}; use one of {sorted(MEDIA_FILTERS)}")
            flt = MEDIA_FILTERS[media]
        topic_filter = None
        reply_to = None
        if topic_id and chat.forum:
            if query or flt or from_user or topic_id == 1:
                # Telethon switches to GetReplies for reply_to and would silently drop search/filter/from_user;
                # the General topic (id 1) has no thread to ask for. Read the chat and keep only this topic.
                topic_filter = topic_id
            else:
                reply_to = topic_id
        # Client-side filtering needs to look at more messages than it returns.
        # Kept <= 3000: above that Telethon sleeps 1s between pages while we hold the request semaphore.
        scan_cap = limit if name_filter is None and topic_filter is None else min(max(limit * 20, 1000), _MAX_SCAN)
        kwargs = dict(
            limit=scan_cap,
            wait_time=0,
            search=query or None,
            filter=flt,
            from_user=from_user,
            offset_id=offset_id,
            min_id=min_id,
            reverse=reverse,
            reply_to=reply_to,
        )
        if reverse and offset_id:
            # In reverse mode Telethon treats offset_id as a lower bound; "older than X" is max_id there.
            kwargs["offset_id"], kwargs["max_id"] = 0, offset_id
        out: list[MsgView] = []
        async with self._sem:
            try:
                if reverse and from_date:
                    # Telegram's search treats the date as an upper bound even when paging forward, so turn
                    # "after from_date" into an id bound that works for every request type.
                    first = [m async for m in self.client.iter_messages(
                        self.entity(chat), limit=1, offset_date=from_date, reverse=True, reply_to=reply_to)]
                    if not first:
                        return []
                    kwargs["min_id"] = max(min_id, first[0].id - 1)
                elif not reverse and to_date:
                    kwargs["offset_date"] = to_date
                async for msg in self.client.iter_messages(self.entity(chat), **kwargs):
                    if not reverse and from_date and msg.date < from_date:
                        break
                    if reverse and to_date and msg.date > to_date:
                        break
                    if isinstance(msg, types.MessageService) and query:
                        continue
                    view = self.to_view(msg, chat)
                    if (name_filter and name_filter not in view.sender.lower()) or (
                        topic_filter and view.topic_id != topic_filter
                    ):
                        continue
                    out.append(view)
                    if len(out) >= limit:
                        break
            except errors.RPCError as e:
                raise self._rpc_error(chat, e) from e
        return out

    async def get_by_ids(self, chat: ChatInfo, ids: list[int]) -> list[MsgView]:
        key = ("ids", chat.id, tuple(ids))
        return list(await self.cache.get(key, lambda: self._get_by_ids(chat, ids)))

    async def _get_by_ids(self, chat: ChatInfo, ids: list[int]) -> list[MsgView]:
        async with self._sem:
            try:
                msgs = await self.client.get_messages(self.entity(chat), ids=ids)
            except errors.RPCError as e:
                raise self._rpc_error(chat, e) from e
        return [self.to_view(m, chat) for m in msgs if m is not None and not isinstance(m, types.MessageEmpty)]

    async def context(self, chat: ChatInfo, msg_id: int, before: int, after: int) -> list[MsgView]:
        topic = None
        if chat.forum:
            # In a forum, neighbouring ids belong to unrelated topics; stay inside the message's topic.
            target = await self.get_by_ids(chat, [msg_id])
            if not target:
                return []
            topic = target[0].topic_id
        older = await self.fetch(chat, offset_id=msg_id + 1, limit=before + 1, topic_id=topic)
        newer = await self.fetch(chat, min_id=msg_id, limit=after, reverse=True, topic_id=topic) if after > 0 else []
        msgs = {m.id: m for m in older + newer}
        return [msgs[k] for k in sorted(msgs)]

    async def replies(self, chat: ChatInfo, msg_id: int, limit: int) -> tuple[list[MsgView], int | None]:
        """All messages in the reply thread of msg_id (oldest first) and the total count if known."""
        res, total = await self.cache.get(("replies", chat.id, msg_id, limit), lambda: self._replies(chat, msg_id, limit))
        return list(res), total

    async def _replies(self, chat: ChatInfo, msg_id: int, limit: int) -> tuple[list[MsgView], int | None]:
        ent = self.entity(chat)
        if isinstance(ent, types.Channel):
            try:
                async with self._sem:
                    # Oldest first: the first replies to a question are usually the answers.
                    it = self.client.iter_messages(ent, reply_to=msg_id, limit=limit, reverse=True)
                    res = [self.to_view(m, chat) async for m in it]
                    total = getattr(it, "total", None)
                return res, total
            except errors.MsgIdInvalidError as e:
                # The message has no reply thread (e.g. a post without comments).
                if chat.kind == "channel":
                    return [], 0
                log.debug("GetReplies failed for %s/%s (%s), falling back to scan", chat.title, msg_id, e)
            except errors.RPCError as e:
                raise self._rpc_error(chat, e) from e  # flood waits etc. must not be cached as "no replies"
        try:
            return await self._replies_by_scan(chat, msg_id, limit)
        except errors.RPCError as e:
            raise self._rpc_error(chat, e) from e

    async def _replies_by_scan(self, chat: ChatInfo, msg_id: int, limit: int) -> tuple[list[MsgView], int | None]:
        """Fallback for basic groups: scan forward from the message and collect the reply tree."""
        tree = {msg_id}
        out: list[MsgView] = []
        async with self._sem:
            async for m in self.client.iter_messages(self.entity(chat), min_id=msg_id, reverse=True, limit=_SCAN_LIMIT):
                rt = m.reply_to.reply_to_msg_id if isinstance(m.reply_to, types.MessageReplyHeader) else None
                if rt in tree:
                    tree.add(m.id)
                    out.append(self.to_view(m, chat))
                    if len(out) >= limit:
                        break
        return out, None

    async def ancestors(self, chat: ChatInfo, msg: MsgView) -> list[MsgView]:
        """Reply chain above msg, oldest (root) first."""
        chain: list[MsgView] = []
        cur = msg
        seen = {msg.id}
        while cur.reply_to_id and len(chain) < _MAX_ANCESTOR_DEPTH and cur.reply_to_id not in seen:
            got = await self.get_by_ids(chat, [cur.reply_to_id])
            if not got:
                break
            cur = got[0]
            seen.add(cur.id)
            chain.append(cur)
        chain.reverse()
        return chain

    # --- higher level ----------------------------------------------------------

    async def search(
        self,
        chats: list[ChatInfo],
        queries: list[str],
        *,
        limit: int,
        per_chat_limit: int,
        **filters,
    ) -> tuple[list[MsgView], list[str]]:
        """Run every query in every chat concurrently; return merged newest-first results and errors."""
        queries = [q.strip() for q in queries if q and q.strip()] or [""]
        errors_: list[str] = []

        async def one(c: ChatInfo, q: str):
            try:
                return await self.fetch(c, query=q or None, limit=per_chat_limit, **filters)
            except (AccessError, ValueError) as e:
                errors_.append(str(e))
                return []

        batches = await asyncio.gather(*(one(c, q) for c in chats for q in queries))
        merged: dict[tuple[int, int], MsgView] = {}
        hits: dict[tuple[int, int], int] = {}
        for batch in batches:
            for m in batch:
                merged[m.key] = m
                hits[m.key] = hits.get(m.key, 0) + 1
        # Messages matched by several query variants first, then newest.
        ordered = sorted(merged.values(), key=lambda m: (hits[m.key], m.date), reverse=True)
        return ordered[:limit], sorted(set(errors_))

    async def follow_ups(self, chat: ChatInfo, root: MsgView, thread: set[int], fu: FollowUp) -> list[MsgView]:
        """Messages written after `root` without a reply link — people often answer that way.

        Reads forward (inside the same forum topic) for up to `fu.window`, skipping messages that reply to something
        outside the conversation, since those belong to other discussions. Replies to accepted follow-ups are kept.
        `thread` holds the ids already in the conversation and is extended in place.
        """
        if fu.limit <= 0 or chat.kind == "channel":  # consecutive channel posts aren't answers
            return []
        msgs = await self.fetch(
            chat, min_id=root.id, reverse=True, limit=fu.limit * 3, to_date=root.date + fu.window,
            topic_id=root.topic_id if chat.forum else None,
        )
        out: list[MsgView] = []
        for m in msgs:
            if m.id in thread or m.service:
                continue
            if m.reply_to_id and m.reply_to_id not in thread:
                continue
            thread.add(m.id)
            out.append(m)
            if len(out) >= fu.limit:
                break
        return out

    async def preceding(self, chat: ChatInfo, msg: MsgView, fu: FollowUp) -> list[MsgView]:
        """A few messages right before `msg` (same topic), oldest first: the question a loose answer refers to."""
        if fu.preceding <= 0 or chat.kind == "channel":
            return []
        msgs = await self.fetch(
            chat, offset_id=msg.id, limit=fu.preceding, from_date=msg.date - fu.window,
            topic_id=msg.topic_id if chat.forum else None,
        )
        return [m for m in reversed(msgs) if not m.service]

    async def expand(
        self,
        chat: ChatInfo,
        root: MsgView,
        max_replies: int,
        fu: FollowUp,
        *,
        known_replies: int | None = None,
        linked: list[MsgView] = (),
    ) -> Discussion:
        """Root plus everything that answers it: reply-linked messages and loose follow-ups.

        `linked` are messages already known to belong to the thread (e.g. the chain down to a search hit).
        """
        if known_replies == 0:
            replies, total = [], 0  # Telegram already told us nobody replied
        else:
            replies, total = await self.replies(chat, root.id, max_replies)
        have = {m.key for m in replies}
        replies = sorted(replies + [m for m in linked if m.key not in have and m.id != root.id], key=lambda m: m.id)
        thread = {root.id, *(m.id for m in replies if m.chat.id == root.chat.id)}
        found = await self.follow_ups(chat, root, thread, fu)
        # Messages found by reading on that do carry a reply link (e.g. beyond a truncated replies listing)
        # are reply-linked, not loose.
        replies = sorted(replies + [m for m in found if m.reply_to_id], key=lambda m: m.id)
        follow = [m for m in found if not m.reply_to_id]
        return Discussion(chat=chat, root=root, replies=replies, follow_ups=follow, total_replies=total)

    async def discussion_for(self, chat: ChatInfo, hit: MsgView, max_replies: int, fu: FollowUp) -> Discussion:
        """Expand a single message into its full discussion: root, chain to the hit, replies and follow-ups."""
        chain = await self.ancestors(chat, hit)
        root = chain[0] if chain else hit
        d = await self.expand(chat, root, max_replies, fu, linked=chain[1:] + [hit])
        if not chain and not looks_like_question(root.text):
            # The root may itself be a loose answer; show what it was answering.
            d.preceding = await self.preceding(chat, root, fu)
        d.hit_ids = {hit.key}
        return d

    async def discussions(
        self, hits: list[MsgView], max_discussions: int, max_replies: int, fu: FollowUp
    ) -> list[Discussion]:
        out: dict[tuple[int, int], Discussion] = {}
        pending = [h for h in hits if h.chat.id in self.chats]

        async def expand_hit(hit: MsgView) -> Discussion | None:
            try:
                return await self.discussion_for(self.chats[hit.chat.id], hit, max_replies, fu)
            except AccessError as e:
                log.debug("discussion expansion failed: %s", e)
                return None

        # Expand in parallel batches (Telegram concurrency is still bounded by the semaphore), keeping hit order.
        while pending and len(out) < max_discussions:
            batch = []
            while pending and len(batch) < max_discussions - len(out):
                hit = pending.pop(0)
                # A hit already shown inside an earlier discussion just gets marked there.
                seen = next((d for d in out.values() if hit.key in _keys(d)), None)
                if seen:
                    seen.hit_ids.add(hit.key)
                else:
                    batch.append(hit)
            for hit, d in zip(batch, await asyncio.gather(*(expand_hit(h) for h in batch))):
                if d is None:
                    continue
                key = (d.chat.id, d.root.id)
                if key in out:
                    out[key].hit_ids.add(hit.key)
                elif len(out) < max_discussions:
                    out[key] = d
        return list(out.values())

    async def questions(
        self,
        chats: list[ChatInfo],
        queries: list[str],
        *,
        limit: int,
        scan_per_chat: int,
        include_answers: bool,
        max_answers: int,
        unanswered_only: bool,
        fu: FollowUp,
        **filters,
    ) -> list[Discussion]:
        """Questions (newest first) expanded with their reply-linked answers and the messages written after them."""
        candidates, _ = await self.search(chats, queries, limit=10_000, per_chat_limit=scan_per_chat, **filters)
        qs = [m for m in candidates if looks_like_question(m.text)]
        qs.sort(key=lambda m: m.date, reverse=True)
        qs = [q for q in qs if q.chat.id in self.chats]
        if not (include_answers or unanswered_only):
            return [Discussion(chat=self.chats[q.chat.id], root=q) for q in qs[:limit]]

        async def expand_q(q: MsgView) -> Discussion | None:
            try:
                return await self.expand(self.chats[q.chat.id], q, max_answers, fu, known_replies=q.replies)
            except AccessError as e:
                log.debug("answers lookup failed: %s", e)
                return None

        result: list[Discussion] = []
        checked = 0
        # Parallel batches, newest questions first; cap lookups so unanswered_only can't scan forever.
        while qs and len(result) < limit and checked < limit * 5:
            batch, qs = qs[: limit - len(result)], qs[limit - len(result):]
            checked += len(batch)
            for d in await asyncio.gather(*(expand_q(q) for q in batch)):
                if d is None:
                    continue
                # Messages from the asker themself are follow-ups, not answers.
                if unanswered_only and d.answers_by_others():
                    continue
                if not include_answers:
                    d.replies, d.follow_ups = [], []
                result.append(d)
        return result[:limit]

    async def topics(self, chat: ChatInfo, query: str | None, limit: int) -> list[dict]:
        if not chat.forum:
            raise AccessError(f"{chat.title} is not a forum (it has no topics)")
        return list(await self.cache.get(("topics", chat.id, query, limit), lambda: self._topics(chat, query, limit)))

    async def _topics(self, chat: ChatInfo, query: str | None, limit: int) -> list[dict]:
        async with self._sem:
            try:
                res = await self.client(
                    functions.messages.GetForumTopicsRequest(
                        peer=self.entity(chat), offset_date=None, offset_id=0, offset_topic=0, limit=limit,
                        q=query or None,
                    )
                )
            except errors.RPCError as e:
                raise self._rpc_error(chat, e) from e
        out = []
        for t in res.topics:
            if isinstance(t, types.ForumTopicDeleted):
                continue
            out.append(
                {
                    "id": t.id,
                    "title": t.title,
                    "date": t.date,
                    "closed": bool(t.closed),
                    "pinned": bool(t.pinned),
                    "top_message": t.top_message,
                    "link": chat.link(t.id),
                }
            )
        return out
