"""Explainable task-scoped visual-style choice and script search opportunities."""

from __future__ import annotations

from dataclasses import dataclass

AUTO = "auto"
ASMR_CUTTING = "asmr_cutting"
RELEVANT_STOCK = "relevant_stock"
SAVAGE_ANIMATED_EXPLAINER = "savage_animated_explainer"
EFFECTIVE_STYLES = {SAVAGE_ANIMATED_EXPLAINER, ASMR_CUTTING, RELEVANT_STOCK}


@dataclass(frozen=True)
class StylePreset:
    version: str
    defaults: dict[str, object]
    idea_guidance: str
    script_guidance: str
    visual_guidance: str


# Defaults and generation direction live here so UI, APIs, scheduling and
# generation use the same definition. Explicit values in higher-priority
# request/profile layers still act as overrides.
STYLE_PRESETS = {
    SAVAGE_ANIMATED_EXPLAINER: StylePreset(
        version="1",
        defaults={
            "narrator_preset": "desi_savage_aggressive",
            "profanity_level": "savage_monetization_aware",
            "caption_preset": "word_by_word",
            "subtitle_animation": "pop_spring",
            "text_fore_color": "#FFFF00",
            "tts_provider": "gemini-tts",
            "tts_model": "gemini-3.8-flash-tts",
            "voice_name": "gemini:Puck-Upbeat",
            "min_duration_seconds": 25.0,
            "max_duration_seconds": 30.0,
            "video_aspect": "9:16",
            "stock_video_style": "default",
        },
        idea_guidance=(
            "Suggest a specific, curiosity-driven misconception or established fact that is "
            "easy to compare with familiar objects in a sarcastic Hindi cartoon. Avoid obscure "
            "species, anonymous discoveries, unsupported claims, invented names and statistics. "
            "The narrator is on the viewer's side: aim jokes at confusing jargon, misconceptions, "
            "absurd situations, or overcomplicated systems, never at the viewer."
        ),
        script_guidance=(
            "Use a specific, memorable situational hook, not a generic 'क्या आप जानते हैं?' opener. "
            "Build curiosity, roast the confusing concept or absurdity, explain the supported fact "
            "simply, land the surprising reveal, and finish with a funny, topic-specific payoff in "
            "natural spoken Hindi/Hinglish. The narrator "
            "is the viewer's friend; never insult, belittle, or abuse the viewer. Direct any "
            "sarcasm or profanity at jargon, misconceptions, absurd situations, or system complexity. "
            "Make jokes support the explanation; do not stack unrelated insults."
        ),
        visual_guidance=(
            "Use a hybrid explainer: keep the recurring original stickman and animated captions; "
            "choose a relevant licensed stock clip for concrete people, places, objects, or everyday "
            "activities when approved local footage is available, and original diagrams for abstract "
            "processes, comparisons, and punchlines. Record each beat's visual choice and reason, "
            "avoid repeating generic diagrams, and use a useful illustrated fallback when stock is "
            "unavailable. Never invent names, studies, statistics, or evidence."
        ),
    ),
    RELEVANT_STOCK: StylePreset(
        version="1",
        defaults={
            "narrator_preset": "standard",
            "profanity_level": "mild",
            "caption_preset": "sentence",
            "subtitle_animation": "none",
            "text_fore_color": "#FFFFFF",
            "stock_video_style": "default",
        },
        idea_guidance=(
            "Suggest a specific topic whose real-world people, places, objects or events can be "
            "shown directly with relevant licensed footage."
        ),
        script_guidance="Use clear, engaging educational narration with a useful explanation and payoff.",
        visual_guidance=(
            "Choose a fact with recognizable real-world subjects shown in relevant licensed "
            "stock footage; use contextual searches and avoid unrelated filler."
        ),
    ),
    ASMR_CUTTING: StylePreset(
        version="1",
        defaults={
            "narrator_preset": "standard",
            "profanity_level": "mild",
            "caption_preset": "word_by_word",
            "subtitle_animation": "none",
            "text_fore_color": "#FFFFFF",
            "stock_video_style": "satisfying_asmr_cutting",
        },
        idea_guidance=(
            "Suggest an entertaining topic that can support concise narration over genuine, "
            "human-approved ASMR cutting clips."
        ),
        script_guidance="Keep narration concise and paced for satisfying ASMR cutting footage.",
        visual_guidance=(
            "Use only genuine footage from the approved ASMR library for cutting; never "
            "substitute unrelated stock or ordinary food preparation."
        ),
    ),
}


def preset_for(style: str) -> StylePreset:
    """Return a concrete preset; AUTO is resolved to stock before generation."""
    if style == AUTO:
        style = RELEVANT_STOCK
    try:
        return STYLE_PRESETS[style]
    except KeyError as exc:
        raise ValueError(f"unsupported visual style: {style}") from exc

def initial_selection(requested: str, script: str, topic: str = "") -> tuple[str, str]:
    """Resolve AUTO to one coherent style before script/TTS generation."""
    if requested == SAVAGE_ANIMATED_EXPLAINER:
        return SAVAGE_ANIMATED_EXPLAINER, (
            "Savage Animated Explainer was explicitly requested; original diagrams and "
            "the approved character pack provide a local no-stock fallback."
        )
    if requested == ASMR_CUTTING:
        return ASMR_CUTTING, "ASMR Cutting was explicitly requested; only quality-approved ASMR is eligible."
    if requested == RELEVANT_STOCK:
        return RELEVANT_STOCK, "Relevant Stock was explicitly requested; ASMR footage is ineligible."
    if requested != AUTO:
        raise ValueError(f"unsupported visual style: {requested}")
    text = f"{topic}\n{script}".casefold()
    if any(term in text for term in ("cutting", "satisfying", "slicing", "asmr", "काट")):
        return ASMR_CUTTING, "AUTO selected ASMR Cutting for an explicitly satisfying-cutting topic."
    return SAVAGE_ANIMATED_EXPLAINER, (
        "AUTO selected Savage Animated Explainer because the available scene planner provides "
        "no semantic stock-fit evidence; local characters and diagrams remain the safe fallback."
    )


def decide_after_preflight(
    requested: str,
    preferred: str,
    *,
    relevant_coverage: float,
    asmr_coverage: float,
    required_coverage: float,
    preferred_failure: str = "",
) -> tuple[str, str]:
    """Choose one coherent mode after both candidates have been quality-screened."""
    stock_ok = relevant_coverage >= required_coverage
    asmr_ok = asmr_coverage >= required_coverage
    if requested == RELEVANT_STOCK:
        if not stock_ok:
            raise ValueError(
                f"Relevant Stock coverage is insufficient: quality-approved "
                f"{relevant_coverage:.1f}s, required {required_coverage:.1f}s."
            )
        return RELEVANT_STOCK, "Explicit Relevant Stock request passed relevance and coverage preflight."
    if requested == ASMR_CUTTING:
        if not asmr_ok:
            raise ValueError(
                f"ASMR Cutting coverage is insufficient: quality-approved "
                f"{asmr_coverage:.1f}s, required {required_coverage:.1f}s."
            )
        return ASMR_CUTTING, "Explicit ASMR Cutting request passed human quality and coverage preflight."
    if preferred == RELEVANT_STOCK and stock_ok:
        return RELEVANT_STOCK, "AUTO selected Relevant Stock after quality-approved relevant footage passed coverage preflight."
    if asmr_ok:
        return ASMR_CUTTING, (
            "AUTO fell back to quality-approved ASMR because relevant stock did not pass "
            f"preflight ({preferred_failure or f'{relevant_coverage:.1f}s coverage'})."
        )
    if stock_ok:
        return RELEVANT_STOCK, (
            "AUTO used relevant stock after the preferred choice failed preflight; "
            "approved ASMR did not have sufficient coverage."
        )
    raise ValueError(
        "No suitable footage passed quality and coverage preflight: "
        f"Relevant Stock {relevant_coverage:.1f}s, approved ASMR {asmr_coverage:.1f}s, "
        f"required {required_coverage:.1f}s."
    )
