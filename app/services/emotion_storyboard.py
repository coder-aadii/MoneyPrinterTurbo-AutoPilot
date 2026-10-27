"""Deterministic narration beats shared by expressive TTS and animated scenes."""

from __future__ import annotations

import re
import math

from app.services import scene_spec


_WORD = re.compile(r"\S+")
_SENTENCE = re.compile(r"[^।?!\n]+[।?!]*|\n+")


def _classify(sentence: str, index: int, total: int,
              narrator_preset: str = "standard") -> dict:
    """Choose delivery and character reaction from narration role, not subject."""
    text = sentence.casefold()
    savage = narrator_preset == "desi_savage_aggressive"
    hook = index == 0 and any(mark in sentence for mark in ("?", "क्या", "क्यों"))
    payoff = index == total - 1 and total > 1
    correction = any(term in text for term in ("गलत", "नहीं", "मिथक", "कौन बोला"))
    reveal = any(term in text for term in ("असल", "दरअसल", "यानी", "लेकिन", "पता चला"))
    if not savage:
        return {"emotion": "neutral explanation", "intensity": .32,
                "delivery": "clear, measured explanation; natural pace",
                "pose": "explaining", "gesture": "point to the explanation",
                "sound_effect": None}
    if hook:
        return {"emotion": "mocking disbelief", "intensity": .78,
                "delivery": "assertive, sarcastic question; punchy, not shouted",
                "pose": "skeptical", "gesture": "head tilt then palm-up challenge",
                "sound_effect": "pop"}
    if payoff:
        return {"emotion": "comedic payoff", "intensity": .82,
                "delivery": "playful, confident punchline; lift final words without shouting",
                "pose": "celebrating", "gesture": "brief celebratory bounce",
                "sound_effect": "comic_surprise"}
    if correction:
        return {"emotion": "frustrated emphasis", "intensity": .74,
                "delivery": "mock the misconception with controlled emphasis",
                "pose": "frustrated_burst", "gesture": "point to the correction",
                "sound_effect": "cartoon_impact"}
    if reveal:
        return {"emotion": "surprised reveal", "intensity": .72,
                "delivery": "build curiosity then land the factual reveal",
                "pose": "idea_reveal", "gesture": "point to the explanation",
                "sound_effect": "reveal"}
    return {"emotion": "assertive explanation", "intensity": .46,
            "delivery": "confident, conversational Hindi/Hinglish",
            "pose": "serious_fact", "gesture": "point to the explanation",
            "sound_effect": None}


_PROFANITY = re.compile(r"(?i)\b(?:saala|saale|chutiya|chod|bhenchod|madarchod|gaand|fuck|shit)\b|साला|चूतिया|भेंचोद|मादरचोद")


def build(
    script: str,
    *,
    narrator_preset: str = "standard",
    profanity_level: str = "savage_monetization_aware",
) -> list[dict]:
    """Split final narration into word-indexed beats with validated scene data."""
    sentences = [match.group(0).strip() for match in _SENTENCE.finditer(script or "")
                 if match.group(0).strip()]
    beats = []
    word_cursor = 0
    for index, sentence in enumerate(sentences):
        word_count = len(_WORD.findall(sentence))
        if not word_count:
            continue
        mood = _classify(sentence, index, len(sentences), narrator_preset)
        scene = scene_spec.plan_scene(sentence, index=index, total=len(sentences))
        contains_profanity = bool(_PROFANITY.search(sentence))
        delivery = mood["delivery"]
        intensity = mood["intensity"]
        if contains_profanity and profanity_level != "mild":
            delivery += "; direct comic emphasis at the idea, never at the viewer"
            intensity = min(1.0, intensity + .04)
        beats.append({
            "beat_id": f"beat_{index + 1:02d}",
            "start_word": word_cursor, "end_word": word_cursor + word_count,
            "text": sentence, "emotion": mood["emotion"], "intensity": intensity,
            "delivery": delivery, "pose": mood["pose"], "gesture": mood["gesture"],
            "visual_mode": "stock_video" if scene.stock_query else "illustration",
            "top_visual": "scene_spec_v1", "scene_spec": scene.model_dump(),
            "stock_query": scene.stock_query, "short_visual_title": scene.heading,
            "visual_rationale": "Validated illustrative scene derived from this narration beat.",
            "humor_target": "jargon_or_misconception" if narrator_preset == "desi_savage_aggressive" else None,
            "caption_emphasis": [], "sound_effect": mood["sound_effect"],
            "profanity_level": profanity_level,
            "contains_profanity": contains_profanity,
        })
        word_cursor += word_count
    return beats


def resolve(beats: list[dict], cues: list[tuple[float, float, str]]) -> list[dict]:
    """Resolve word-index ranges against actual Vakyansh/WhisperX aligned cues."""
    if not beats or not cues:
        return []
    resolved = []
    for beat in beats:
        start_word, end_word = int(beat["start_word"]), int(beat["end_word"])
        if start_word < 0 or end_word > len(cues) or end_word <= start_word:
            raise ValueError(f"emotion beat {beat.get('beat_id')} is outside aligned narration")
        resolved.append({
            **beat,
            "start": float(cues[start_word][0]),
            "end": float(cues[end_word - 1][1]),
            "start_word_text": cues[start_word][2],
            "end_word_text": cues[end_word - 1][2],
        })
    return resolved


def schedule_visuals_from_duration(
    beats: list[dict], narration_duration: float, *, offset: float = 0.0,
) -> list[dict]:
    """Schedule visual changes when measured word cues are unavailable.

    These are approximate scene windows, never subtitle or word-alignment cues.
    The measured audio duration bounds the windows and the saved word ranges
    preserve narration order.
    """
    if (not beats or not math.isfinite(narration_duration) or narration_duration <= 0
            or not math.isfinite(offset) or offset < 0):
        return []
    total_words = max(int(beat.get("end_word", 0)) for beat in beats)
    if total_words <= 0:
        return []
    scheduled = []
    for beat in beats:
        start_word = int(beat.get("start_word", 0))
        end_word = int(beat.get("end_word", 0))
        if start_word < 0 or end_word <= start_word or end_word > total_words:
            continue
        scheduled.append({
            **beat,
            "start": offset + narration_duration * start_word / total_words,
            "end": offset + narration_duration * end_word / total_words,
            "timing_source": "estimated_visual_schedule",
        })
    return scheduled


def gemini_segments(beats: list[dict]) -> list[dict]:
    """Keep delivery instructions outside the transcript sent for exact recitation."""
    return [{
        "type": "text", "text": beat["text"],
        "annotations": [{"type": "speech_metadata", "style": beat["delivery"]}],
    } for beat in beats if beat.get("text", "").strip()]


def legacy_gemini_prompt(script: str, beats: list[dict]) -> str:
    """Build the documented prompt-guided format for pre-3.8 Gemini TTS."""
    profile = (
        "Audio Profile: Young adult Indian Hindi/Hinglish narrator; confident, clear, "
        "conversational, and expressive. Preserve natural pronunciation."
    )
    instructions = []
    for beat in beats:
        spoken = str(beat.get("text", "")).strip()
        delivery = str(beat.get("delivery", "")).strip()
        if spoken and delivery:
            instructions.append(f'- For the transcript words "{spoken}": {delivery}')
    director = "\n".join(instructions) or "- Speak naturally and clearly."
    return (
        f"{profile}\n\nScene: Short Hindi factual narration.\n\n"
        "Director's Notes: Apply the following performance directions without speaking them. "
        "Speak only the exact text in the Transcript section.\n"
        f"{director}\n\nTranscript:\n{script.strip()}"
    )
