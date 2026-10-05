"""Chat profiles: what each allowed chat is about, built once at startup so the model can pick the right chats.

The server has no language model of its own; it gathers signals (description, pinned message, forum topics, a sample
of recent messages) and condenses the sample into keywords and example questions. The model connected to the server
does the actual matching of a user's question against these profiles.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from .formatting import ChatInfo, MsgView, fmt_date, looks_like_question, truncate


@dataclass
class ChatProfile:
    pinned: str | None = None
    topics: list[str] = field(default_factory=list)  # forum topic titles
    keywords: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)  # recent questions people asked, first lines
    sampled: int = 0  # messages analysed
    span_days: float | None = None  # time covered by the sample
    last_message: datetime | None = None

    @property
    def per_day(self) -> float | None:
        if not self.sampled or self.span_days is None:
            return None
        return self.sampled / max(self.span_days, 1 / 24)


# --- keyword extraction --------------------------------------------------------

_STOP_RU = """
а без более бы был была были было быть в вам вас весь во вот все всё всего всех вы где да даже для до его ее её
если есть еще ещё же за здесь и из или им их к как ко когда кто ли либо мне может мы на над надо наш не него нее
неё нет ни них но ну о об однако он она они оно от очень по под при с со так также такой там те тем то того тоже
той только том ты у уже хотя чего чей чем что чтобы чье чья эта эти это я этот этого этой этом этих
будет будут было какой какая какие каких какое каким который которая которые которых котором которой
можно нужно надо есть нету просто тоже вообще сейчас потом тогда всегда никогда почему зачем сколько
очень больше меньше много мало сразу где-то кто-то что-то как-то когда-то какой-то какая-то какие-то
привет спасибо пожалуйста здравствуйте добрый доброе доброго день дня вечер утро всем ребята друзья коллеги
подскажите подскажи помогите помоги кто-нибудь кто-нибудь знает знаете вопрос вопросы ответ спасибочки
делать сделать знаю знать думаю думать хочу хотел хотела могу может можете нужен нужна нужны было будет
своей свой свои своих свою себя себе него неё нему ними этому этим тому такая такие такое такую таких
человек люди года году лет время раз два три сегодня вчера завтра недавно давно также кстати например
""".split()
_STOP_EN = """
about above after again also although always another anyone anything are because been before being
between both but can cannot could did does doing done down during each either else even ever every
from have having here hers herself himself however into itself just know like many might more most much
must need never only other others ought ours over please same should since some something still such than
thank thanks that their theirs them then there these they thing think this those though through thus
very want was were what whatever when where whether which while will with within without would your yours
hello guys anybody question questions help
""".split()
STOPWORDS = frozenset(_STOP_RU + _STOP_EN)

_URL_RE = re.compile(r"https?://\S+|t\.me/\S+|@\w+")
_WORD_RE = re.compile(r"[a-zа-яё]+(?:-[a-zа-яё]+)?")


def _stem(word: str) -> str:
    """Crude grouping key so визы/визу/визой count together (no morphology library on a small board)."""
    return word[:6] if len(word) > 6 else word


def extract_keywords(texts: list[str], top: int = 20, min_docs: int = 2) -> list[str]:
    """Most frequent meaningful words, counted once per message, shown in their most common form."""
    docs: Counter[str] = Counter()
    forms: dict[str, Counter[str]] = {}
    for text in texts:
        seen = set()
        for w in _WORD_RE.findall(_URL_RE.sub(" ", text.lower())):
            if len(w) < 4 or w in STOPWORDS:
                continue
            key = _stem(w)
            forms.setdefault(key, Counter())[w] += 1
            seen.add(key)
        docs.update(seen)
    ranked = [k for k, n in docs.most_common() if n >= min_docs]
    return [forms[k].most_common(1)[0][0] for k in ranked[:top]]


def sample_questions(msgs: list[MsgView], limit: int = 12) -> list[str]:
    """First lines of the most recent distinct questions (bots excluded)."""
    out: list[str] = []
    seen: set[str] = set()
    for m in sorted(msgs, key=lambda m: m.date, reverse=True):
        if m.sender.endswith("[bot]") or not looks_like_question(m.text):
            continue
        line = truncate(m.text.strip().splitlines()[0].strip(), 140)
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= limit:
            break
    return out


def profile_from_sample(profile: ChatProfile, msgs: list[MsgView]) -> None:
    """Fill keyword/question/activity fields of `profile` from a sample of recent messages."""
    msgs = [m for m in msgs if not m.service]
    if not msgs:
        return
    texts = [m.text for m in msgs if m.text and not m.sender.endswith("[bot]")]
    profile.keywords = extract_keywords(texts)
    profile.questions = sample_questions(msgs)
    profile.sampled = len(msgs)
    dates = sorted(m.date for m in msgs)
    profile.span_days = (dates[-1] - dates[0]).total_seconds() / 86400
    profile.last_message = dates[-1]


# --- rendering -------------------------------------------------------------------


def _header(c: ChatInfo) -> str:
    bits = [c.kind + (" (forum)" if c.forum else "")]
    if c.members:
        bits.append(f"{c.members} members")
    p: ChatProfile | None = c.profile
    if p and p.per_day is not None:
        rate = p.per_day
        bits.append(f"~{rate:.0f} msgs/day" if rate >= 1 else f"~{rate * 7:.0f} msgs/week")
    return f"{c.title} ({c.ref}) — " + ", ".join(bits)


def format_catalog_entry(c: ChatInfo, compact: bool = False) -> str:
    """A few lines per chat for the server instructions."""
    lines = [f"- {_header(c)}"]
    if c.about:
        lines.append("  about: " + truncate(c.about.replace("\n", " "), 120 if compact else 220))
    p: ChatProfile | None = c.profile
    if p:
        if p.topics:
            n = 8 if compact else 20
            lines.append("  forum topics: " + "; ".join(p.topics[:n]) + (" …" if len(p.topics) > n else ""))
        if p.keywords:
            lines.append("  frequent words: " + ", ".join(p.keywords[: 8 if compact else 15]))
    return "\n".join(lines)


def format_catalog(chats: list[ChatInfo], max_chars: int = 24_000) -> str:
    text = "\n".join(format_catalog_entry(c) for c in chats)
    if len(text) > max_chars:
        text = "\n".join(format_catalog_entry(c, compact=True) for c in chats)
    return truncate(text, max_chars)


def format_profile(c: ChatInfo) -> str:
    """Everything known about one chat, for list_chats."""
    lines = [f"## {_header(c)}", f"id: {c.id}"]
    if c.linked_chat_id:
        lines.append(f"comments live in linked discussion chat id {c.linked_chat_id}")
    if c.about:
        lines.append("about: " + truncate(c.about.replace("\n", " "), 600))
    p: ChatProfile | None = c.profile
    if p:
        if p.pinned:
            lines.append("pinned message: " + truncate(p.pinned.replace("\n", " "), 600))
        if p.topics:
            lines.append("forum topics: " + "; ".join(p.topics))
        if p.sampled:
            last = fmt_date(p.last_message) if p.last_message else "?"
            lines.append(f"recent activity: {p.sampled} messages over {p.span_days:.1f} days, last {last}")
        if p.keywords:
            lines.append("frequent words in recent messages: " + ", ".join(p.keywords))
        if p.questions:
            lines.append("recent questions asked here:")
            lines += [f"  - {q}" for q in p.questions]
    return "\n".join(lines)
