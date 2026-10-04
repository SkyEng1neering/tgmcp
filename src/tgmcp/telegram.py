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
    ancestors: list[MsgView] = field(default_factory=list)  # chain from root down to the hit's parent
    replies: list[MsgView] = field(default_factory=list)
    hit_ids: set[int] = field(default_factory=set)
    total_replies: int | None = None


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
            if rt.reply_to_top_id:
                return rt.reply_to_msg_id, rt.reply_to_top_id
            return None, rt.reply_to_msg_id
        if forum:
            return rt.reply_to_msg_id, 1
        return rt.reply_to_msg_id, None

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
        kwargs = dict(
            limit=None,
            search=query or None,
            filter=flt,
            from_user=from_user,
            offset_id=offset_id,
            min_id=min_id,
            reverse=reverse,
        )
        topic_filter = None
        if topic_id and chat.forum:
            if query or flt or from_user:
                # Telethon switches to GetReplies for reply_to and would silently drop search/filter/from_user,
                # so search the whole chat and keep only this topic.
                topic_filter = topic_id
            else:
                kwargs["reply_to"] = topic_id
        if reverse:
            if from_date:
                kwargs["offset_date"] = from_date
        elif to_date:
            kwargs["offset_date"] = to_date
        out: list[MsgView] = []
        scanned = 0
        # Client-side filtering needs to look at more messages than it returns.
        scan_cap = limit if name_filter is None and topic_filter is None else max(limit * 20, 1000)
        async with self._sem:
            try:
                async for msg in self.client.iter_messages(self.entity(chat), **kwargs):
                    scanned += 1
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
                        if scanned >= scan_cap:
                            break
                        continue
                    out.append(view)
                    if len(out) >= limit or scanned >= scan_cap:
                        break
            except errors.RPCError as e:
                raise AccessError(f"{chat.title}: Telegram error {type(e).__name__}: {e}") from e
        return out

    async def get_by_ids(self, chat: ChatInfo, ids: list[int]) -> list[MsgView]:
        key = ("ids", chat.id, tuple(ids))
        return list(await self.cache.get(key, lambda: self._get_by_ids(chat, ids)))

    async def _get_by_ids(self, chat: ChatInfo, ids: list[int]) -> list[MsgView]:
        async with self._sem:
            try:
                msgs = await self.client.get_messages(self.entity(chat), ids=ids)
            except errors.RPCError as e:
                raise AccessError(f"{chat.title}: Telegram error {type(e).__name__}: {e}") from e
        return [self.to_view(m, chat) for m in msgs if m is not None and not isinstance(m, types.MessageEmpty)]

    async def context(self, chat: ChatInfo, msg_id: int, before: int, after: int) -> list[MsgView]:
        older = await self.fetch(chat, offset_id=msg_id + 1, limit=before + 1) if before >= 0 else []
        newer = await self.fetch(chat, min_id=msg_id, limit=after, reverse=True) if after > 0 else []
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
                    it = self.client.iter_messages(ent, reply_to=msg_id, limit=limit)
                    res = [self.to_view(m, chat) async for m in it]
                    total = getattr(it, "total", None)
                res.reverse()
                return res, total
            except errors.RPCError as e:
                log.debug("GetReplies failed for %s/%s (%s), falling back to scan", chat.title, msg_id, e)
        return await self._replies_by_scan(chat, msg_id, limit)

    async def _replies_by_scan(self, chat: ChatInfo, msg_id: int, limit: int) -> tuple[list[MsgView], int | None]:
        """Fallback for basic groups: scan forward from the message and collect the reply tree."""
        tree = {msg_id}
        out: list[MsgView] = []
        scanned = 0
        async with self._sem:
            async for m in self.client.iter_messages(self.entity(chat), min_id=msg_id, reverse=True, limit=None):
                scanned += 1
                rt = m.reply_to.reply_to_msg_id if isinstance(m.reply_to, types.MessageReplyHeader) else None
                if rt in tree:
                    tree.add(m.id)
                    out.append(self.to_view(m, chat))
                    if len(out) >= limit:
                        break
                if scanned >= 2000:
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

    async def discussion_for(self, chat: ChatInfo, hit: MsgView, max_replies: int) -> Discussion:
        """Expand a single message into its full discussion: root, chain to the hit and the replies."""
        chain = await self.ancestors(chat, hit)
        root = chain[0] if chain else hit
        replies, total = await self.replies(chat, root.id, max_replies)
        known = {m.id for m in replies}
        # Make sure the chain and hit are present even if the replies listing was truncated.
        extra = [m for m in chain[1:] + ([hit] if hit.id != root.id else []) if m.id not in known]
        replies = sorted(replies + extra, key=lambda m: m.id)
        return Discussion(chat=chat, root=root, replies=replies, hit_ids={hit.id}, total_replies=total)

    async def discussions(self, hits: list[MsgView], max_discussions: int, max_replies: int) -> list[Discussion]:
        out: dict[tuple[int, int], Discussion] = {}
        for hit in hits:
            if len(out) >= max_discussions:
                break
            chat = self.chats.get(hit.chat.id)
            if chat is None:
                continue
            try:
                d = await self.discussion_for(chat, hit, max_replies)
            except AccessError as e:
                log.debug("discussion expansion failed: %s", e)
                continue
            key = (chat.id, d.root.id)
            if key in out:
                out[key].hit_ids.add(hit.id)
            else:
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
        **filters,
    ) -> list[tuple[MsgView, list[MsgView] | None]]:
        candidates, _ = await self.search(chats, queries, limit=10_000, per_chat_limit=scan_per_chat, **filters)
        qs = [m for m in candidates if looks_like_question(m.text)]
        qs.sort(key=lambda m: m.date, reverse=True)
        result: list[tuple[MsgView, list[MsgView] | None]] = []
        for q in qs:
            if len(result) >= limit:
                break
            answers = None
            if include_answers or unanswered_only:
                chat = self.chats[q.chat.id]
                answers, _ = await self.replies(chat, q.id, max_answers)
                # Replies from the asker themself are follow-ups, not answers.
                answers_by_others = [a for a in answers if a.sender_id is None or a.sender_id != q.sender_id]
                if unanswered_only and answers_by_others:
                    continue
            result.append((q, answers if include_answers else None))
        return result

    async def topics(self, chat: ChatInfo, query: str | None, limit: int) -> list[dict]:
        if not chat.forum:
            raise AccessError(f"{chat.title} is not a forum (it has no topics)")
        return list(await self.cache.get(("topics", chat.id, query, limit), lambda: self._topics(chat, query, limit)))

    async def _topics(self, chat: ChatInfo, query: str | None, limit: int) -> list[dict]:
        async with self._sem:
            res = await self.client(
                functions.messages.GetForumTopicsRequest(
                    peer=self.entity(chat), offset_date=None, offset_id=0, offset_topic=0, limit=limit, q=query or None
                )
            )
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
