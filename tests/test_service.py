import asyncio

import pytest

from fakes import CHANNEL_ID, make_service, msg
from tgmcp.telegram import AccessError

def by(m, user_id):
    from telethon import types

    m.from_id = types.PeerUser(user_id)
    return m


THREAD = [
    msg(1, "Привет всем"),
    by(msg(2, "Как настроить nginx для websocket?"), 100),
    by(msg(3, "Нужен proxy_set_header Upgrade", reply_to=2), 200),
    by(msg(4, "Спасибо, nginx заработал", reply_to=3, top=2), 100),
    msg(5, "Кто идёт на митап?"),
    msg(6, "оффтоп"),
]


@pytest.fixture
def svc():
    return make_service(THREAD)


def run(coro):
    return asyncio.run(coro)


def test_resolve_by_username_title_id_and_link(svc):
    chat = next(iter(svc.chats.values()))
    for ref in ("@devchat", "DEVCHAT", "Dev Chat", "dev", str(chat.id), str(CHANNEL_ID), "https://t.me/devchat/5",
                f"https://t.me/c/{CHANNEL_ID}/5"):
        assert svc.resolve(ref) is chat
    with pytest.raises(AccessError, match="not in the allowed list"):
        svc.resolve("@other")


def test_search_merges_variants_and_ranks_multi_hits(svc):
    msgs, errs = run(svc.search(list(svc.chats.values()), ["nginx", "websocket"], limit=10, per_chat_limit=10))
    assert not errs
    assert [m.id for m in msgs] == [2, 4]  # #2 matches both variants, then newest


def test_context_is_chronological(svc):
    chat = next(iter(svc.chats.values()))
    ids = [m.id for m in run(svc.context(chat, 3, before=1, after=2))]
    assert ids == [2, 3, 4, 5]


def test_discussion_expands_hit_to_root_and_replies(svc):
    chat = next(iter(svc.chats.values()))
    hit = run(svc.get_by_ids(chat, [4]))[0]
    d = run(svc.discussion_for(chat, hit, 50))
    assert d.root.id == 2
    assert [r.id for r in d.replies] == [3, 4]
    assert d.hit_ids == {4}


def test_replies_fallback_scan_for_groups_without_threads():
    svc = make_service(THREAD, supports_replies=False)
    chat = next(iter(svc.chats.values()))
    replies, _ = run(svc.replies(chat, 2, 10))
    assert [r.id for r in replies] == [3, 4]


def test_questions_with_answers_and_unanswered(svc):
    chats = list(svc.chats.values())
    found = run(svc.questions(chats, [], limit=10, scan_per_chat=100, include_answers=True, max_answers=10,
                              unanswered_only=False))
    assert {q.id: [a.id for a in ans] for q, ans in found} == {2: [3, 4], 5: []}
    open_qs = run(svc.questions(chats, [], limit=10, scan_per_chat=100, include_answers=False, max_answers=10,
                                unanswered_only=True))
    assert [q.id for q, _ in open_qs] == [5]


def test_cache_reuses_identical_reads(svc):
    chat = next(iter(svc.chats.values()))

    async def go():
        a, b = await asyncio.gather(svc.fetch(chat, query="nginx", limit=5), svc.fetch(chat, query="nginx", limit=5))
        await svc.fetch(chat, query="nginx", limit=5)
        return a, b

    a, b = run(go())
    assert [m.id for m in a] == [m.id for m in b]
    assert svc.cache.misses == 1 and svc.cache.hits == 2


def test_forum_topic_semantics():
    from telethon import types

    svc = make_service([], forum=True)
    chat = next(iter(svc.chats.values()))
    plain = msg(10, "in topic")
    plain.reply_to = types.MessageReplyHeader(reply_to_msg_id=7, forum_topic=True)
    reply = msg(11, "reply in topic")
    reply.reply_to = types.MessageReplyHeader(reply_to_msg_id=10, reply_to_top_id=7, forum_topic=True)
    general = msg(12, "general topic")
    assert (svc.to_view(plain, chat).reply_to_id, svc.to_view(plain, chat).topic_id) == (None, 7)
    assert (svc.to_view(reply, chat).reply_to_id, svc.to_view(reply, chat).topic_id) == (10, 7)
    assert svc.to_view(general, chat).topic_id == 1


def test_search_inside_forum_topic_keeps_query():
    from telethon import types

    def in_topic(m, topic):
        m.reply_to = types.MessageReplyHeader(reply_to_msg_id=topic, forum_topic=True)
        return m

    svc = make_service(
        [in_topic(msg(10, "nginx in topic 7"), 7), in_topic(msg(11, "other in topic 7"), 7),
         in_topic(msg(12, "nginx in topic 8"), 8)],
        forum=True,
    )
    chat = next(iter(svc.chats.values()))
    found = run(svc.fetch(chat, query="nginx", topic_id=7, limit=10))
    assert [m.id for m in found] == [10]
    browsed = run(svc.fetch(chat, topic_id=7, limit=10))
    assert {m.id for m in browsed} == {10, 11}
