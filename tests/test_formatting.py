from datetime import datetime, timezone

import pytest

from tgmcp.formatting import ChatInfo, MsgView, format_message, looks_like_question, truncate
from tgmcp.telegram import parse_when

PUB = ChatInfo(id=-1001234567890, title="Py", username="python_ru", kind="supergroup")
PRIV = ChatInfo(id=-1001234567890, title="Priv", username=None, kind="supergroup", forum=True)


def test_links():
    assert PUB.link(42) == "https://t.me/python_ru/42"
    assert PRIV.link(42) == "https://t.me/c/1234567890/42"
    assert PRIV.link(42, topic_id=7) == "https://t.me/c/1234567890/7/42"
    assert ChatInfo(id=-55, title="g", username=None, kind="group").link(1) is None


def test_format_message():
    m = MsgView(chat=PUB, id=10, date=datetime(2025, 1, 2, 3, 4, tzinfo=timezone.utc), sender="Ann (@ann)",
                text="line1\nline2", reply_to_id=9, replies=3)
    out = format_message(m)
    assert "[Py] | #10 | 2025-01-02 03:04 UTC | Ann (@ann) (reply to #9, 3 replies)" in out
    assert "https://t.me/python_ru/10" in out
    assert "  line1\n  line2" in out


def test_truncate():
    assert truncate("abcdef", 3) == "abc …[+3 chars]"
    assert truncate("abc", 10) == "abc"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Как настроить nginx для websocket", True),
        ("подскажите, где взять ключ", True),
        ("Does anyone know a good ORM", True),
        ("работает ли это?", True),
        ("Спасибо, всё заработало", False),
        ("Что-то сломалось после обновления", False),
        ("ok", False),
        ("", False),
    ],
)
def test_question_heuristic(text, expected):
    assert looks_like_question(text) is expected


def test_parse_when():
    assert parse_when("2025-01-31") == datetime(2025, 1, 31, tzinfo=timezone.utc)
    end = parse_when("2025-01-31", end_of_day=True)
    assert (end.day, end.hour) == (31, 23)
    assert parse_when("7d") < datetime.now(timezone.utc)
    assert parse_when(None) is None
    with pytest.raises(ValueError):
        parse_when("yesterday")
