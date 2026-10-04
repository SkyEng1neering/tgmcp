import pytest

from tgmcp.config import ConfigError, load_config, marked_channel_id, parse_chat_list, parse_chat_ref

from telethon.crypto import AuthKey
from telethon.sessions import StringSession


def _dummy_session() -> str:
    s = StringSession()
    s.set_dc(2, "149.154.167.51", 443)
    s.auth_key = AuthKey(b"\0" * 256)
    return s.save()


BASE_ENV = {"TG_API_ID": "123", "TG_API_HASH": "abc", "TG_SESSION": _dummy_session(), "TG_CHATS": "@python_ru"}


@pytest.mark.parametrize(
    "raw,kind,value",
    [
        ("@Python_RU", "username", "python_ru"),
        ("python_ru", "username", "python_ru"),
        ("https://t.me/python_ru", "username", "python_ru"),
        ("t.me/python_ru/12345", "username", "python_ru"),
        ("-1001234567890", "id", -1001234567890),
        ("https://t.me/c/1234567890/55", "id", -1001234567890),
        ("https://t.me/+AbCdEf_123", "invite", "AbCdEf_123"),
        ("https://t.me/joinchat/AbCdEf", "invite", "AbCdEf"),
    ],
)
def test_parse_chat_ref(raw, kind, value):
    ref = parse_chat_ref(raw)
    assert (ref.kind, ref.value) == (kind, value)


def test_marked_id_matches_bot_api_convention():
    assert marked_channel_id(1234567890) == -1001234567890
    assert marked_channel_id(111) == -1000000000111


def test_chat_list_separators_and_comments():
    refs = parse_chat_list("@one_chat, @two_chat;\n# comment\n-1001234567890\n")
    assert [r.value for r in refs] == ["one_chat", "two_chat", -1001234567890]


def test_bad_ref():
    with pytest.raises(ConfigError):
        parse_chat_ref("ab")


def test_load_config_defaults():
    cfg = load_config(dict(BASE_ENV))
    assert cfg.transport == "stdio" and cfg.api_id == 123 and cfg.port == 8000


def test_http_requires_token():
    with pytest.raises(ConfigError, match="MCP_AUTH_TOKEN"):
        load_config({**BASE_ENV, "MCP_TRANSPORT": "http"})
    cfg = load_config({**BASE_ENV, "MCP_TRANSPORT": "http", "MCP_AUTH_TOKEN": "x" * 32})
    assert cfg.transport == "http"
    with pytest.raises(ConfigError, match="too short"):
        load_config({**BASE_ENV, "MCP_TRANSPORT": "http", "MCP_AUTH_TOKEN": "short"})


def test_missing_session_message():
    with pytest.raises(ConfigError, match="tgmcp-login"):
        load_config({**BASE_ENV, "TG_SESSION": ""})


def test_invalid_session_message():
    with pytest.raises(ConfigError, match="not a valid session"):
        load_config({**BASE_ENV, "TG_SESSION": "garbage"})


def test_proxy():
    cfg = load_config({**BASE_ENV, "TG_PROXY": "socks5://u:p@127.0.0.1:1080"})
    assert cfg.proxy == ("socks5", "127.0.0.1", 1080, True, "u", "p")
