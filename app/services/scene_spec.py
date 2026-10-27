"""Validated, topic-independent visual plans for narration beats.

The planner uses relations expressed in the narration. It never claims that a
symbolic drawing depicts a particular animal, place, or historical event.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Primitive = Literal["label", "comparison", "flow", "transformation", "counter", "timeline"]


class SceneSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    primitive: Primitive
    heading: str = Field(min_length=1, max_length=48)
    labels: list[str] = Field(min_length=1, max_length=4)
    stock_query: str | None = Field(default=None, max_length=90)
    illustrative: bool = True

    @model_validator(mode="after")
    def layout_has_supported_parts(self):
        count = len(self.labels)
        if self.primitive == "label" and count != 1:
            raise ValueError("label scenes require one label")
        if self.primitive == "comparison" and count != 2:
            raise ValueError("comparison scenes require two labels")
        if self.primitive in {"flow", "transformation", "timeline"} and count < 2:
            raise ValueError("relational scenes require at least two stages")
        return self

    @field_validator("heading")
    @classmethod
    def heading_is_plain_text(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value or any(char in value for char in "<>{}"):
            raise ValueError("scene heading must be plain text")
        return value

    @field_validator("labels")
    @classmethod
    def labels_are_plain_text(cls, values: list[str]) -> list[str]:
        cleaned = [" ".join(value.split()) for value in values]
        if any(not value or len(value) > 64 or any(char in value for char in "<>{}")
               for value in cleaned):
            raise ValueError("scene labels must be short plain text")
        return cleaned

    @field_validator("stock_query")
    @classmethod
    def query_is_plain_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value or any(char in value for char in "<>{}") or "://" in value:
            raise ValueError("stock query must be plain search text")
        return value


_RELATIONS = {
    "transformation": re.compile(r"बदल|वापस|विकसित|रूपांतर|transform|revert|develop|evolv|turns? into", re.I),
    "comparison": re.compile(r"\bvs\.?\b|बनाम|तुलना|से अधिक|से कम|की अपेक्षा|compared? (?:to|with)|versus", re.I),
    "timeline": re.compile(r"रास्त|मार्ग|यात्रा|route|journey|centur|शताब्दी|सदी", re.I),
    "counter": re.compile(r"\d|प्रतिशत|percent|लाख|करोड़|million|billion", re.I),
    "flow": re.compile(r"फिर|जब|उसके बाद|तब|भेज|लेता|देता|request|response|through|then|after|because|इसलिए|कैसे", re.I),
}
_FILLER = frozenset({
    "अबे", "ये", "यह", "है", "या", "और", "एक", "तो", "जो", "जब", "उसका",
    "उसकी", "इसे", "अपने", "में", "का", "की", "को", "से", "लेकिन", "साला",
    "साली", "भाई", "the", "a", "an", "is", "are", "and", "or", "but", "of", "to",
})


def _tokens(text: str) -> list[str]:
    return [token.strip("।?!,.:;\"'()[]") for token in text.split()
            if token.strip("।?!,.:;\"'()[]")]


def _short_label(tokens: list[str], limit: int = 4) -> str:
    useful = [token for token in tokens if token.casefold() not in _FILLER]
    return " ".join((useful or tokens)[:limit])[:64]


def plan_scene(sentence: str, *, index: int = 0, total: int = 1,
               stock_query: str | None = None) -> SceneSpec:
    """Extract a conservative relation and labels from any Hindi/Hinglish beat."""
    text = " ".join(str(sentence or "").split())
    tokens = _tokens(text)
    if not tokens:
        raise ValueError("a scene needs spoken narration")
    primitive: Primitive = next(
        (kind for kind, pattern in _RELATIONS.items() if pattern.search(text)), "label")
    heading = _short_label(tokens, 4)[:48]
    # Keep labels anchored in spoken words. Unknown concepts remain explicitly
    # illustrative instead of acquiring an invented species, event, or number.
    midpoint = max(1, len(tokens) // 2)
    simple_comparison = re.split(r"\s+(?:बनाम|vs\.?|versus)\s+", text, maxsplit=1, flags=re.I)
    route = re.search(r"([^\s।?!,]+)\s+से\s+([^\s।?!,]+)\s+तक", text)
    if primitive == "timeline" and route:
        labels = [route.group(1), route.group(2)]
    elif primitive == "comparison" and len(simple_comparison) == 2:
        labels = [_short_label(_tokens(part), 3) for part in simple_comparison]
    elif primitive == "comparison" and "की तुलना" in text:
        before = _tokens(text.split("की तुलना", 1)[0])
        nouns = [token for token in before if token.casefold() not in _FILLER]
        labels = [nouns[0], nouns[-1]] if len(nouns) >= 2 else [_short_label(tokens, 4)]
    elif primitive in {"comparison", "flow", "transformation", "timeline"}:
        labels = [_short_label(tokens[:midpoint], 3),
                  _short_label(tokens[midpoint:], 3)]
    elif primitive == "counter":
        number = next((token for token in tokens if re.search(r"\d", token)), None)
        labels = [number or _short_label(tokens, 2), _short_label(tokens, 4)]
    else:
        labels = [_short_label(tokens, 4)]
    labels = [label for label in labels if label]
    # Stock is a separate media layer from the explanatory primitive. A relevant
    # clip may illustrate a process or comparison while the diagram remains the
    # fallback when no eligible asset exists.
    query = stock_query
    return SceneSpec(primitive=primitive, heading=heading,
                     labels=labels, stock_query=query)


def validate_scene_spec(value: dict | SceneSpec) -> SceneSpec:
    """Reject malformed or unsupported model/config output before rendering."""
    return SceneSpec.model_validate(value)
