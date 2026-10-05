import asyncio

from mcp.client.client import Client

from fakes import make_service, msg
from tgmcp.profiles import ChatProfile, extract_keywords, format_catalog, profile_from_sample, sample_questions
from tgmcp.server import build_server

SAMPLE = [
    msg(1, "Подскажите, как продлить визу на Кипре? Визы у всех заканчиваются"),
    msg(2, "Продление визы делается через миграционную службу, визу продлевают за месяц"),
    msg(3, "А налоги? Какие налоги для фрилансера на Кипре"),
    msg(4, "Налоги платятся раз в год, налог на доход"),
    msg(5, "Привет всем! Спасибо за чат"),
    msg(6, "https://t.me/cylaw/2 @someone смотри тут"),
]


def test_keywords_skip_stopwords_links_and_group_word_forms():
    kws = extract_keywords([m.message for m in SAMPLE])
    assert kws[0] in {"визу", "визы", "виза"}  # 3 messages mention visas
    assert "налоги" in kws or "налог" in kws
    assert "привет" not in kws and "спасибо" not in kws and "someone" not in kws


def test_profile_from_sample_and_questions():
    svc = make_service(SAMPLE)
    chat = next(iter(svc.chats.values()))
    views = asyncio.run(svc.fetch(chat, limit=100))
    p = ChatProfile()
    profile_from_sample(p, views)
    assert p.sampled == 6 and p.last_message is not None
    assert sample_questions(views)[0].startswith("А налоги?")  # newest question first
    assert all("Привет" not in q for q in p.questions)


def test_startup_profiles_chat_and_instructions_carry_catalog():
    async def go():
        svc = make_service(SAMPLE)
        chat = next(iter(svc.chats.values()))
        await svc._profile(chat)
        assert chat.profile.sampled == 6
        text = format_catalog([chat])
        assert "Dev Chat (@devchat)" in text and "frequent words:" in text
        async with Client(build_server(svc)) as c:
            assert "CHATS AVAILABLE" in c.instructions and "Dev Chat (@devchat)" in c.instructions
            res = await c.call_tool("list_chats", {})
            out = res.content[0].text
            assert "recent questions asked here:" in out and "визу" in out

    asyncio.run(go())


def test_catalog_falls_back_to_compact_when_too_long():
    chats = []
    for i in range(30):
        svc = make_service(SAMPLE)
        chat = next(iter(svc.chats.values()))
        chat.title = f"Chat {i}"
        chat.about = "x" * 500
        chats.append(chat)
    assert len(format_catalog(chats, max_chars=3000)) <= 3000 + 20


def test_unclear_chats_are_flagged_for_reading_history():
    from tgmcp.profiles import UNCLEAR_HINT, format_profile, is_unclear

    svc = make_service([msg(1, "hi"), msg(2, "ok")])
    chat = next(iter(svc.chats.values()))
    asyncio.run(svc._profile(chat))
    assert is_unclear(chat)
    assert UNCLEAR_HINT in format_catalog([chat]) and UNCLEAR_HINT in format_profile(chat)

    rich = make_service(SAMPLE * 3)
    rchat = next(iter(rich.chats.values()))
    rchat.about = "Чат про визы, ВНЖ, налоги и страховки на Кипре. Задавайте вопросы."
    rchat.profile = ChatProfile(pinned="Правила чата и FAQ: t.me/cylaw/2")
    assert not is_unclear(rchat)  # description + pinned are enough even without a sample
