"""Tiny shared character checks for the app preflight and WhisperX worker."""

from __future__ import annotations

import unicodedata


def missing_alignment_characters(script: str, dictionary: dict) -> list[str]:
    return sorted({
        char for char in script.lower()
        if unicodedata.category(char)[0] in {"L", "M", "N"}
        and char not in dictionary
    })
