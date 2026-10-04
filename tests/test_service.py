import asyncio

import pytest

from fakes import CHANNEL_ID, make_service, msg
from tgmcp.telegram import AccessError, FollowUp

def by(m, user_id):
    from telethon import types

    m.from_id = types.PeerUser(user_id)
    m._sender_id = user_id  # Telethon derives sender_id in the constructor
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
    d = run(svc.discussion_for(chat, hit, 50, FollowUp.off()))
    assert d.root.id == 2
    assert [r.id for r in d.replies] == [3, 4]
    assert d.hit_ids == {hit.key}
    assert d.follow_ups == [] and d.preceding == []


def test_replies_fallback_scan_for_groups_without_threads():
    svc = make_service(THREAD, supports_replies=False)
    chat = next(iter(svc.chats.values()))
    replies, _ = run(svc.replies(chat, 2, 10))
    assert [r.id for r in replies] == [3, 4]


def test_questions_with_answers_and_unanswered(svc):
    chats = list(svc.chats.values())
    found = run(svc.questions(chats, [], limit=10, scan_per_chat=100, include_answers=True, max_answers=10,
                              unanswered_only=False, fu=FollowUp.off()))
    assert {d.root.id: [a.id for a in d.replies] for d in found} == {2: [3, 4], 5: []}
    open_qs = run(svc.questions(chats, [], limit=10, scan_per_chat=100, include_answers=False, max_answers=10,
                                unanswered_only=True, fu=FollowUp.off()))
    assert [d.root.id for d in open_qs] == [5]


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


def test_oldest_first_from_date_uses_id_bound(svc):
    from datetime import timedelta

    from fakes import BASE

    chat = next(iter(svc.chats.values()))
    got = run(svc.fetch(chat, from_date=BASE + timedelta(minutes=3, seconds=30), reverse=True, limit=2))
    assert [m.id for m in got] == [4, 5]
    got = run(svc.fetch(chat, query="nginx", from_date=BASE + timedelta(minutes=3), reverse=True, limit=5))
    assert [m.id for m in got] == [4]


def test_cross_chat_reply_is_not_followed():
    from telethon import types

    quote = msg(20, "quoting another chat")
    quote.reply_to = types.MessageReplyHeader(reply_to_msg_id=2, reply_to_peer_id=types.PeerChannel(999))
    svc = make_service([msg(2, "unrelated"), quote])
    chat = next(iter(svc.chats.values()))
    assert svc.to_view(quote, chat).reply_to_id is None


def test_broadcast_post_without_comments_does_not_scan():
    svc = make_service(THREAD, supports_replies=False, kind="channel")
    chat = next(iter(svc.chats.values()))
    assert run(svc.replies(chat, 2, 10)) == ([], 0)


def test_forum_context_stays_in_topic():
    from telethon import types

    def in_topic(m, topic):
        m.reply_to = types.MessageReplyHeader(reply_to_msg_id=topic, forum_topic=True)
        return m

    svc = make_service([in_topic(msg(i, f"m{i}"), 7 if i % 2 else 8) for i in range(9, 20)], forum=True)
    chat = next(iter(svc.chats.values()))
    ids = [m.id for m in run(svc.context(chat, 13, before=2, after=2))]
    assert ids == [9, 11, 13, 15, 17]


# People often answer without the reply button: the conversation must include what was written after.
LOOSE = [
    by(msg(1, "Как настроить nginx для websocket?"), 100),
    by(msg(2, "Нужен proxy_set_header Upgrade"), 200),  # answer without reply
    by(msg(3, "а вот про докер", reply_to=99), 300),  # reply into another conversation
    by(msg(4, "спасибо, помогло", reply_to=2), 100),  # reply to the loose answer
    by(msg(5, "сильно позже", minutes=60 * 30), 200),  # outside the 24h window
]


def test_question_gets_loose_answers_after_it():
    svc = make_service(LOOSE)
    chats = list(svc.chats.values())
    [d] = run(svc.questions(chats, [], limit=10, scan_per_chat=100, include_answers=True, max_answers=10,
                            unanswered_only=False, fu=FollowUp()))
    assert d.root.id == 1
    # #2 was written after the question without a reply link; #4 replies to #2, so it is reply-linked
    assert [m.id for m in d.follow_ups] == [2]
    assert [(tag, m.id) for tag, m in d.conversation()] == [("after", 2), ("reply", 4)]


def test_loose_answer_hit_shows_the_question_before_it():
    svc = make_service(LOOSE)
    chat = next(iter(svc.chats.values()))
    hit = run(svc.get_by_ids(chat, [2]))[0]
    d = run(svc.discussion_for(chat, hit, 50, FollowUp()))
    assert [m.id for m in d.preceding] == [1]
    assert [(tag, m.id) for tag, m in d.conversation()] == [("reply", 4)]


def test_unanswered_ignores_askers_own_follow_ups():
    svc = make_service([by(msg(1, "Как починить сборку?"), 100), by(msg(2, "ап, всё ещё актуально"), 100)])
    chats = list(svc.chats.values())
    open_qs = run(svc.questions(chats, [], limit=10, scan_per_chat=100, include_answers=True, max_answers=10,
                                unanswered_only=True, fu=FollowUp()))
    assert [d.root.id for d in open_qs] == [1]
    svc = make_service(LOOSE)
    open_qs = run(svc.questions(list(svc.chats.values()), [], limit=10, scan_per_chat=100, include_answers=True,
                                max_answers=10, unanswered_only=True, fu=FollowUp()))
    assert open_qs == []


def test_reply_beyond_truncated_listing_is_labelled_reply():
    svc = make_service(THREAD)
    chat = next(iter(svc.chats.values()))
    q = run(svc.get_by_ids(chat, [2]))[0]
    d = run(svc.expand(chat, q, max_replies=1, fu=FollowUp()))
    tags = dict((m.id, tag) for tag, m in d.conversation())
    assert tags[3] == "reply" and tags[4] == "reply" and tags[5] == "after"


def test_oldest_first_before_message_id_is_upper_bound(svc):
    chat = next(iter(svc.chats.values()))
    got = run(svc.fetch(chat, offset_id=4, reverse=True, limit=10))
    assert [m.id for m in got] == [1, 2, 3]


def test_cross_chat_quote_keeps_forum_topic():
    from telethon import types

    svc = make_service([], forum=True)
    chat = next(iter(svc.chats.values()))
    m = msg(30, "quote in topic")
    m.reply_to = types.MessageReplyHeader(reply_to_msg_id=5, reply_to_top_id=7, forum_topic=True,
                                          reply_to_peer_id=types.PeerChannel(999))
    v = svc.to_view(m, chat)
    assert (v.reply_to_id, v.topic_id) == (None, 7)


def test_flood_wait_on_channel_post_is_not_cached_as_no_comments():
    from telethon import errors

    svc = make_service(THREAD, kind="channel")
    chat = next(iter(svc.chats.values()))

    def boom(*a, **kw):
        raise errors.FloodWaitError(request=None, capture=120)

    svc.client.iter_messages = boom
    with pytest.raises(AccessError, match="FloodWait"):
        run(svc.replies(chat, 2, 10))
    assert svc.cache._data == {}
