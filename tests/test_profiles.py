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


def test_stemming_groups_short_russian_forms_and_other_scripts():
    from tgmcp.profiles import _stem

    assert {_stem(w) for w in ["виза", "визы", "визу", "визой", "визами"]} == {"виз"}
    assert {_stem(w) for w in ["компания", "компании", "компанию", "компаний"]} == {"компан"}
    assert _stem("visas") == "visa" and _stem("nginx") == "nginx"
    kws = extract_keywords(["підкажіть де взяти візу", "підкажіть будь ласка", "ipv6 ipv6 2024"], min_docs=1)
    assert "підкажіть" in kws and "ipv6" in kws and "2024" not in kws and "дкаж" not in kws


def test_keyword_order_is_deterministic_on_ties():
    texts = ["кипр налоги виза", "виза кипр", "налоги виза"]
    # виза: 3 messages; кипр/налоги tie on 2 messages and 2 mentions -> alphabetical
    assert extract_keywords(texts) == extract_keywords(list(reversed(texts))) == ["виза", "кипр", "налоги"]


def test_descriptive_about_alone_is_enough():
    from tgmcp.profiles import is_unclear

    svc = make_service([msg(1, "post")])
    chat = next(iter(svc.chats.values()))
    chat.about = "Новости и анонсы IT-сообщества Кипра: митапы, вакансии, события."
    chat.profile = ChatProfile()
    assert not is_unclear(chat)
    chat.about = "Чат"
    assert is_unclear(chat)


def test_catalog_never_cuts_an_entry_in_half():
    chats = []
    for i in range(40):
        svc = make_service([])
        chat = next(iter(svc.chats.values()))
        chat.title, chat.about = f"Chat {i}", "описание " * 30
        chats.append(chat)
    text = format_catalog(chats, max_chars=2000)
    assert len(text) <= 2000 and text.endswith("call list_chats to see all of them)")
    # every listed chat is complete: its header line and its about line are both present
    assert text.count("- Chat ") == text.count("  about:")


def test_startup_survives_transient_errors_while_profiling(caplog):
    svc = make_service(SAMPLE)
    chat = next(iter(svc.chats.values()))
    real = svc.client.iter_messages
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("Request was unsuccessful 5 time(s)")
        return real(*a, **kw)

    svc.client.iter_messages = flaky
    asyncio.run(svc._profile(chat))  # must not raise
    assert chat.profile.sampled == 6 and "unavailable" in caplog.text


def test_tiny_sample_has_no_activity_rate():
    p = ChatProfile()
    svc = make_service([msg(1, "одно сообщение в чате")])
    profile_from_sample(p, asyncio.run(svc.fetch(next(iter(svc.chats.values())), limit=10)))
    assert p.sampled == 1 and p.per_day is None


def test_question_line_skips_greeting_and_channels_get_headlines():
    from tgmcp.profiles import _question_line

    assert _question_line("Добрый день!\nПодскажите, где купить марки?\nСпасибо") == "Подскажите, где купить марки?"
    assert _question_line("Всем привет! Кто знает хорошего врача?") == "Всем привет! Кто знает хорошего врача?"
    chan = make_service([msg(1, "💳 Revolut: как это работает?\nДлинный текст"), msg(2, "Кипр переходит на зимнее время")],
                        kind="channel")
    chat = next(iter(chan.chats.values()))
    p = ChatProfile()
    profile_from_sample(p, asyncio.run(chan.fetch(chat, limit=10)))
    assert p.questions == [] and p.headlines == ["Кипр переходит на зимнее время", "💳 Revolut: как это работает?"]


def test_noise_words_and_months_are_not_keywords():
    texts = ["меня через вроде пока точно чтоб лучше сентября октября сообщения пишите"] * 3 + ["виза виза", "визы"]
    assert extract_keywords(texts) == ["виза"]


def test_keywords_show_the_shortest_form():
    texts = ["на кипре хорошо", "в кипре тепло", "кипр остров", "лимассоле жарко", "лимассоле дорого", "лимассол"]
    assert extract_keywords(texts) == ["кипр", "лимассол"]
