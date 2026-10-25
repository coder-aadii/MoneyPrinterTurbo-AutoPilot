"""Stage-specific, deterministic checks for one-sentence video ideas."""

from __future__ import annotations

import re


_GENERIC = (
    "interesting facts", "amazing facts", "विज्ञान के तथ्य", "रोचक तथ्य",
    "कुछ रोचक बातें", "दुनिया के रहस्य",
)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_SPECIFICITY = re.compile(
    r"(?i)\b(?:why|how|when|what|does|do|can|weighs?|weight|difference|temperature|"
    r"cause|causes|changes?|affect|floats?|contains?|forms?|works?)\b|"
    r"(?:क्यों|कैसे|कब|क्या|कितना|कितनी|कितने|वजन|भार|तापमान|असर|कारण|बनता|बनती|बदलता|"
    r"तीन|दो|पहला|पहली|अंतर|किस तरह)"
)


def topic_issues(topic: str) -> list[str]:
    """Check topic specificity and basic coherence without grading its style."""
    text = re.sub(r"\s+", " ", str(topic or "")).strip()
    folded = text.casefold()
    if not text:
        return ["empty topic"]
    words = _WORD.findall(text)
    issues = []
    if len(words) < 4:
        issues.append("topic is too broad or underspecified")
    if any(value in folded for value in _GENERIC):
        issues.append("topic is generic rather than a specific idea")
    if len(words) > 28:
        issues.append("topic is too long; return one concise topic sentence")
    if len(words) >= 4 and not _SPECIFICITY.search(text) and not re.search(r"\d", text):
        issues.append("topic lacks a concrete relationship, question, comparison, quantity, or mechanism")
    if re.search(r"(.)\1{4,}", text, re.IGNORECASE):
        issues.append("topic contains repeated-character gibberish")
    # A short idea may be a question or a statement. Do not require any Savage
    # hook, punchline, or profanity at the topic-planning stage.
    if len(words) >= 5 and len(set(word.casefold() for word in words)) <= 2:
        issues.append("topic has too little distinct content to be coherent")
    if re.search(r"(?:कीड़े में चिल्लाता विज्ञान|कार के कीड़े में)", folded):
        issues.append("topic phrase is syntactically incoherent")
    return issues
