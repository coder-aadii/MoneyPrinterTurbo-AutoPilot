"""Canonical Hindi narration text and stable display/alignment token mapping."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass

from app.services import hindi_numbers
from app.utils import utils


# Deliberately curated: unknown Latin words stay intact and are rejected by
# the Hindi alignment worker with a useful unsupported-character diagnostic.
PRONUNCIATIONS = {
    "ai": "एआई", "api": "एपीआई", "browser": "ब्राउज़र",
    "chatgpt": "चैटजीपीटी", "client": "क्लाइंट",
    "cpu": "सीपीयू", "database": "डेटाबेस", "html": "एचटीएमएल",
    "http": "एचटीटीपी", "https": "एचटीटीपीएस", "iss": "आईएसएस", "javascript": "जावास्क्रिप्ट",
    "process": "प्रोसेस", "request": "रिक्वेस्ट", "response": "रिस्पॉन्स",
    "server": "सर्वर", "url": "यूआरएल", "wifi": "वाई-फाई",
    "wi-fi": "वाई-फाई", "youtube": "यूट्यूब", "video": "वीडियो",
}

_EDGE_PUNCTUATION = "\"'“”‘’([{<,.;:!?)]}>—–"


@dataclass(frozen=True)
class DisplayWord:
    text: str
    spoken_start: int
    spoken_end: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ScriptRepresentations:
    editable_script: str
    spoken_script: str
    display_script: str
    display_words: tuple[DisplayWord, ...]
    alignment_words: tuple[str, ...]

    def display_map(self) -> list[dict]:
        return [word.to_dict() for word in self.display_words]


def _plain_markdown(text: str) -> str:
    text = utils.remove_pause_tags(text or "")
    text = re.sub(
        r"(?<!\w)(\*{1,3}|_{1,3}|~~)(.+?)\1(?!\w)", r"\2", text
    )
    text = re.sub(r"(`{1,3})(.+?)\1", r"\2", text)
    text = re.sub(r"(?m)^\s*[-*_]{3,}\s*$", "", text)
    return text


def source_symbol_issues(text: str) -> list[dict]:
    """Find non-punctuation symbols the spoken normalizer cannot safely omit."""
    plain = _plain_markdown(text or "")
    issues = []
    for char in sorted(set(plain)):
        if char.isspace():
            continue
        category = unicodedata.category(char)
        if category[0] in {"S", "C"} and not (
            char == "°" and re.search(r"\d\s*°\s*[CF]\b", plain, re.IGNORECASE)
        ):
            issues.append({
                "code": "unsupported_source_symbol", "character": char,
                "message": f"Unsupported symbol {char!r}; remove it or spell out what should be spoken.",
            })
    return issues


def _pronounce_token(token: str, pronunciation_overrides: dict[str, str] | None = None) -> str:
    # Match a curated term while retaining attached punctuation and casing is
    # immaterial for the spoken form. Hyphens inside terms are significant.
    start, end = 0, len(token)
    while start < end and token[start] in _EDGE_PUNCTUATION:
        start += 1
    while end > start and token[end - 1] in _EDGE_PUNCTUATION:
        end -= 1
    core = token[start:end]
    overrides = {
        str(key).strip().casefold(): str(value).strip()
        for key, value in (pronunciation_overrides or {}).items()
        if str(key).strip() and str(value).strip()
    }
    replacement = overrides.get(core.casefold()) or PRONUNCIATIONS.get(core.casefold())
    if replacement:
        return token[:start] + replacement + token[end:]
    # Expand supported number/percent forms one display token at a time.
    return hindi_numbers.normalize_hindi_numbers(token, "hi")


def build_representations(
    script: str,
    language: str = "hi",
    pronunciation_overrides: dict[str, str] | None = None,
) -> ScriptRepresentations:
    """Build editable, TTS/alignment and display forms with token spans."""
    if not (str(language or "hi").casefold().startswith(("hi", "hindi"))):
        plain = _plain_markdown(script)
        words = tuple(plain.split())
        spans, cursor = [], 0
        for word in words:
            spans.append(DisplayWord(word, cursor, cursor + 1))
            cursor += 1
        return ScriptRepresentations(script, " ".join(words), " ".join(words), tuple(spans), words)

    display_tokens = _plain_markdown(script).split()
    spoken_tokens: list[str] = []
    mappings: list[DisplayWord] = []
    for display in display_tokens:
        spoken = _pronounce_token(display, pronunciation_overrides)
        token_words = [word for word in spoken.split()
                       if any(unicodedata.category(char)[0] in {"L", "N"} for char in word)]
        if not token_words:
            continue
        first = len(spoken_tokens)
        spoken_tokens.extend(token_words)
        mappings.append(DisplayWord(display, first, len(spoken_tokens)))
    spoken_script = " ".join(spoken_tokens)
    display_script = " ".join(item.text for item in mappings)
    return ScriptRepresentations(
        script, spoken_script, display_script, tuple(mappings), tuple(spoken_tokens)
    )
