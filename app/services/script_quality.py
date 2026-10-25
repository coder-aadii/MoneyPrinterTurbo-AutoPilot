"""Small deterministic checks for Savage narration style, separate from fact checking."""

from __future__ import annotations

import re


_GENERIC_HOOKS = (
    "क्या आप जानते हैं",
    "क्या तुम्हें पता है",
    "क्या तुम्हें मालूम है",
    "क्या आपने कभी सोचा है",
    "did you know",
)
_VIEWER_ABUSE = re.compile(
    r"(?i)(?:तुझे|तुम्हें|तुम|आप)\s+(?:गधे|बेवकूफ|मूर्ख|चूतिये|चूतिया)|"
    r"(?:अबे|ओ|अरे)\s+(?:गधे|बेवकूफ|मूर्ख|चूतिये|चूतिया)|"
    r"\b(?:you idiot|you moron|stupid viewer)\b"
)
_FRAGMENT_START = re.compile(r"(?i)^(?:और|लेकिन|क्योंकि|तो|फिर|ये|यह)\b")
_FLAT_ENDINGS = (
    "ज़रूरी है", "जरूरी है", "होता है", "होती है", "होते हैं", "करता है", "करती है",
    "करते हैं", "है", "हैं", "था", "थी", "थे",
)
_HOOK_CUE = re.compile(
    r"(?i)(?<![\w])(?:सोचो|मान लो|ज़रा|जरा|मगर|लेकिन|नहीं|दिखते|दिखता|निकला|निकले|"
    r"कौन|क्यों|कैसे|अचानक|देखो|why|how|imagine|but|yet|turns out|looks)(?![\w])"
)
_PAYOFF_CUE = re.compile(
    r"(?i)(?:ओवरटाइम|शर्मिंदा|कर्मचारी|नाम भारी|काम सीधा|और अब|तो फिर|यही ट्विस्ट|"
    r"फेल|लटका|निकला|निकली|निकलता|पलट गया|बूम|कहानी खत्म|बस यही|यानी|फँस गया|"
    r"mic drop|gotcha|punchline|plot twist)"
)


def savage_script_warnings(script: str) -> list[str]:
    """Return creative-style warnings; none of these make narration unusable."""
    raw = re.sub(r"\s+", " ", str(script or "")).strip()
    text = raw.casefold()
    if not text:
        return []
    issues = []
    if any(text.startswith(hook) for hook in _GENERIC_HOOKS):
        issues.append("generic curiosity-question hook")
    opening = text.split(maxsplit=18)
    opening_text = " ".join(opening)
    if not _HOOK_CUE.search(opening_text) and not re.search(r"[!?]", opening_text):
        issues.append("missing a specific, memorable hook")
    if _VIEWER_ABUSE.search(text):
        issues.append("viewer-directed insult or abuse")
    if re.search(r"(?i)(?<![A-Za-z0-9_])bc(?![A-Za-z0-9_])", raw):
        issues.append("ambiguous profanity abbreviation; write the exact unambiguous spoken expression")
    if len(text.split()) >= 8 and not re.search(r"[.!?।॥]$", raw):
        issues.append("missing sentence punctuation; narration reads as a fragment")
    if _FRAGMENT_START.search(text):
        issues.append("narration starts with a dependent or fragmented phrase")
    words = text.split()
    ending = " ".join(words[-max(6, len(words) // 3):])
    final_words = text.rstrip(".!?।॥ ")
    has_payoff_cue = bool(_PAYOFF_CUE.search(ending))
    if len(words) >= 16 and not has_payoff_cue:
        issues.append("no discernible topic-specific payoff ending")
    elif len(words) >= 18 and not has_payoff_cue and final_words.endswith(_FLAT_ENDINGS):
        issues.append("no discernible payoff ending")
    return issues


def savage_script_hard_errors(script: str) -> list[str]:
    """Reject only drafts that are empty or clearly unusable as narration.

    Language, pronunciation, factual support and creative style are checked by
    their own preflights and policies; they are deliberately not hard errors here.
    """
    raw = re.sub(r"\s+", " ", str(script or "")).strip()
    if not raw:
        return ["empty narration"]
    words = raw.split()
    if len(words) < 4:
        return ["narration is too short to form a usable explanation"]
    normalized_words = [re.sub(r"[^\w]", "", word.casefold()) for word in words]
    if len(normalized_words) >= 4 and len(set(normalized_words)) == 1:
        return ["repetitive or gibberish wording"]
    if re.search(r"\b(\w+)(?:\s+\1){3,}\b", raw.casefold()):
        return ["repetitive or gibberish wording"]
    return []


def savage_script_issues(script: str) -> list[str]:
    """Compatibility helper returning hard errors followed by style warnings."""
    return savage_script_hard_errors(script) + savage_script_warnings(script)


FAILED_DRAFT_PREFIX = "Error: Savage script rejected after one bounded rewrite."


def failed_draft_result(draft: str, issues: list[str]) -> str:
    issue_text = "; ".join(issues) or "script quality requirements were not met"
    return f"{FAILED_DRAFT_PREFIX} Issues: {issue_text}\nRejected draft for editing:\n{draft.strip()}"


def parse_failed_draft(result: str) -> tuple[str, list[str]] | None:
    if not isinstance(result, str) or not result.startswith(FAILED_DRAFT_PREFIX):
        return None
    header, marker, draft = result.partition("\nRejected draft for editing:\n")
    if not marker:
        return "", [header.removeprefix(FAILED_DRAFT_PREFIX).strip()]
    issue_text = header.partition("Issues:")[2].strip()
    return draft.strip(), [item.strip() for item in issue_text.split(";") if item.strip()]
