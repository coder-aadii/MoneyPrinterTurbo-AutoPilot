"""One resolver for visual, narration, TTS and caption settings."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.config import config
from app.models.generation_settings import ResolvedGenerationSettings
from app.services import visual_style as visual_style_service


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python", exclude_unset=True)
    return dict(value) if isinstance(value, dict) else {}


def global_generation_defaults() -> dict[str, Any]:
    """Return nonsecret process defaults used when task/profile values are absent."""
    visual_style = config.ui.get(
        "visual_style", visual_style_service.SAVAGE_ANIMATED_EXPLAINER
    )
    if visual_style not in {
        "auto", "savage_animated_explainer", "relevant_stock", "asmr_cutting"
    }:
        visual_style = visual_style_service.SAVAGE_ANIMATED_EXPLAINER
    narrator = config.ui.get("narrator_preset")
    if narrator not in {"standard", "desi_savage_aggressive"}:
        narrator = None
    profanity = config.ui.get("profanity_level", "savage_monetization_aware")
    if profanity not in {"mild", "savage_monetization_aware", "savage_unfiltered"}:
        profanity = "savage_monetization_aware"
    return {
        "visual_style": visual_style,
        "narrator_preset": narrator,
        "profanity_level": profanity,
        # A selected voice prefix is more reliable than the current UI backend
        # when resolving saved jobs. Use the configured backend only as fallback.
        "tts_server": config.ui.get("tts_server", "azure-tts-v1"),
        "tts_model": "gemini-3.8-flash-tts",
        "voice_name": config.ui.get("voice_name", ""),
        "voice_rate": config.ui.get("voice_rate", 1.0),
        "voice_volume": config.ui.get("voice_volume", 1.0),
        "caption_preset": config.ui.get("subtitle_display_mode", "sentence"),
        "subtitle_display_mode": config.ui.get("subtitle_display_mode", "sentence"),
        "duration_enforcement": config.ui.get("duration_enforcement", "flexible"),
    }


def _voice_provider(voice_name: str) -> str:
    prefix = (voice_name or "").split(":", 1)[0].strip().lower()
    return {
        "gemini": "gemini-tts",
        "mimo": "mimo-tts",
        "siliconflow": "siliconflow",
        "minimax": "minimax-tts",
        "fish_audio": "fish_audio",
        "elevenlabs": "elevenlabs",
    }.get(prefix, "")


def _auto_materials_available(style: str, task_values: dict[str, Any], layers: list[dict[str, Any]]) -> bool:
    """Check local/configured prerequisites for AUTO before prompts or synthesis."""
    if style == visual_style_service.SAVAGE_ANIMATED_EXPLAINER:
        return True
    source = next((layer.get("video_source") for layer in layers if layer.get("video_source")), None)
    source = str(source or config.app.get("video_source", "pexels")).lower()
    if style == visual_style_service.RELEVANT_STOCK:
        if source == "local":
            return bool(task_values.get("video_materials"))
        key_name = {
            "pexels": "pexels_api_keys", "pixabay": "pixabay_api_keys",
            "coverr": "coverr_api_keys",
        }.get(source)
        keys = config.app.get(key_name, []) if key_name else []
        return bool(isinstance(keys, (list, tuple)) and any(str(key).strip() for key in keys))
    if style == visual_style_service.ASMR_CUTTING:
        categories = task_values.get("asmr_categories") or ["kinetic_sand", "soap"]
        try:
            from app.services import asmr_library
            clips = asmr_library.load_clips(list(categories), task_values.get("video_aspect", "9:16"))
        except (OSError, TypeError, ValueError):
            return False
        available = sum(max(float(item.duration), 0) for _, item in clips)
        enforcement = next(
            (layer.get("duration_enforcement") for layer in layers
             if layer.get("duration_enforcement") in {"flexible", "warn_only", "strict"}),
            "flexible",
        )
        if enforcement != "strict":
            return available > 0
        requested = float(task_values.get("min_duration_seconds") or 25)
        return available >= requested
    return False


def resolve_generation_settings(
    *,
    task_override: Any = None,
    plan_override: Any = None,
    channel_profile: Any = None,
    global_defaults: Any = None,
    topic: str = "",
) -> ResolvedGenerationSettings:
    """Resolve each supported field in task > plan > profile > global order."""
    layers = [
        _mapping(task_override),
        _mapping(plan_override),
        _mapping(channel_profile),
        _mapping(global_defaults) or global_generation_defaults(),
    ]

    requested_style = next(
        (layer.get("visual_style") for layer in layers
         if layer.get("visual_style") in {
             visual_style_service.AUTO, visual_style_service.SAVAGE_ANIMATED_EXPLAINER,
             visual_style_service.RELEVANT_STOCK, visual_style_service.ASMR_CUTTING,
         }),
        visual_style_service.SAVAGE_ANIMATED_EXPLAINER,
    )
    task_values = _mapping(task_override)
    topic = str(topic or task_values.get("video_subject") or "")
    script = str(task_values.get("video_script") or "")
    effective_style, style_reason = visual_style_service.initial_selection(
        requested_style, script, topic
    )
    if requested_style == visual_style_service.AUTO and not _auto_materials_available(
        effective_style, task_values, layers
    ):
        effective_style = visual_style_service.SAVAGE_ANIMATED_EXPLAINER
        style_reason = (
            "AUTO selected the local Savage Animated Explainer because the initially "
            "suitable footage style has no configured, quality-approved material source."
        )
    style_origin = next(
        (index for index, layer in enumerate(layers)
         if layer.get("visual_style") == requested_style),
        len(layers) - 1,
    )
    # If a higher-priority request repeats the profile's existing style, the
    # profile's saved dependent settings remain explicit overrides. A genuine
    # style transition (for example stock profile → Savage plan) inserts the
    # new preset before lower layers so their stale values cannot leak through.
    for index in range(style_origin + 1, len(layers)):
        lower_style = next(
            (layer.get("visual_style") for layer in layers[index:]
             if layer.get("visual_style") in {
                 visual_style_service.AUTO,
                 visual_style_service.SAVAGE_ANIMATED_EXPLAINER,
                 visual_style_service.RELEVANT_STOCK,
                 visual_style_service.ASMR_CUTTING,
             }),
            None,
        )
        if lower_style != requested_style:
            break
        style_origin = index
    preset = visual_style_service.preset_for(effective_style)
    preset_values = dict(preset.defaults)
    # Use Gemini's expressive per-beat delivery only when the application has
    # credentials configured. Otherwise retain the configured provider/voice
    # and let the compatibility summary report whether it can honor emotions.
    if not config.app.get("gemini_api_key"):
        for key in ("tts_provider", "tts_model", "voice_name"):
            preset_values.pop(key, None)
    layers.insert(style_origin + 1, preset_values)

    def first(*names: str, default=None):
        for layer in layers:
            for name in names:
                value = layer.get(name)
                if value is not None and not (isinstance(value, str) and not value.strip()):
                    return value
        return default

    visual_style = first("visual_style", default="auto")
    narrator_preset = first("narrator_preset")
    if narrator_preset is None:
        narrator_preset = (
            "desi_savage_aggressive"
            if visual_style == "savage_animated_explainer"
            else "standard"
        )
    voice_name = str(first("voice_name", default="") or "")
    provider = first("tts_provider") or _voice_provider(voice_name) or first("tts_server", default="")
    provider_key = str(provider or "").strip().lower()
    model = str(first("tts_model", default="gemini-3.8-flash-tts") or "")
    if provider_key in {"gemini", "gemini-tts", "gemini_tts"}:
        delivery_mode = (
            "native_metadata"
            if model in {"gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"}
            else "prompt_guided_legacy"
        )
    else:
        delivery_mode = "unsupported"
    caption_preset = first(
        "caption_preset", "subtitle_display_mode", default="sentence"
    )
    return ResolvedGenerationSettings(
        visual_style=visual_style,
        effective_visual_style=effective_style,
        visual_style_reason=style_reason,
        style_preset_version=preset.version,
        min_duration_seconds=first("min_duration_seconds"),
        max_duration_seconds=first("max_duration_seconds"),
        narrator_preset=narrator_preset,
        profanity_level=first(
            "profanity_level", default="savage_monetization_aware"
        ),
        tts_provider=str(provider or ""),
        tts_model=model,
        voice_name=voice_name,
        voice_rate=float(first("voice_rate", default=1.0)),
        voice_volume=float(first("voice_volume", default=1.0)),
        caption_preset=str(caption_preset or "sentence"),
        duration_enforcement=(
            first("duration_enforcement", default="flexible")
            if first("duration_enforcement", default="flexible")
            in {"flexible", "warn_only", "strict"}
            else "flexible"
        ),
        tts_delivery_mode=delivery_mode,
    )


def resolve_task_settings(params: Any, *, topic: str = "") -> ResolvedGenerationSettings:
    """Resolve and pin settings on a task model once, preserving its snapshot."""
    existing = getattr(params, "resolved_generation_settings", None)
    settings = (
        existing if isinstance(existing, ResolvedGenerationSettings)
        else resolve_generation_settings(task_override=params, topic=topic)
    )
    try:
        params.resolved_generation_settings = settings
        for field in (
            "visual_style", "narrator_preset", "profanity_level", "tts_provider",
            "tts_model", "voice_name", "voice_rate", "voice_volume", "caption_preset",
            "duration_enforcement",
        ):
            if hasattr(params, field):
                setattr(params, field, getattr(settings, field))
        if hasattr(params, "effective_visual_style"):
            params.effective_visual_style = settings.effective_visual_style
        if hasattr(params, "visual_style_reason"):
            params.visual_style_reason = settings.visual_style_reason
        defaults = visual_style_service.preset_for(
            settings.effective_visual_style
        ).defaults
        explicit_fields = getattr(params, "model_fields_set", set())
        for field in (
            "subtitle_animation", "stock_video_style", "video_aspect",
        ):
            if hasattr(params, field) and field not in explicit_fields:
                value = defaults.get(field)
                if value is not None:
                    setattr(params, field, value)
        if hasattr(params, "min_duration_seconds") and params.min_duration_seconds is None:
            params.min_duration_seconds = settings.min_duration_seconds
        if hasattr(params, "max_duration_seconds") and params.max_duration_seconds is None:
            params.max_duration_seconds = settings.max_duration_seconds
        if hasattr(params, "subtitle_display_mode") and settings.caption_preset in {
            "sentence", "word_by_word"
        }:
            params.subtitle_display_mode = settings.caption_preset
    except (AttributeError, TypeError, ValueError):
        # Immutable request-like values are still usable through the return value.
        pass
    return settings


def tts_compatibility(provider: str, model: str) -> dict[str, str | bool]:
    """Describe whether the selected synthesis path can apply emotion directions."""
    provider_key = str(provider or "").lower()
    if provider_key in {"gemini", "gemini-tts", "gemini_tts"}:
        if model in {"gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"}:
            return {
                "status": "supported_native_metadata",
                "supported": True,
                "detail": "Per-beat speech metadata is sent with each transcript segment.",
            }
        return {
            "status": "supported_prompt_guided_legacy",
            "supported": True,
            "detail": "Legacy Gemini receives documented prose delivery guidance; per-beat controls are best-effort.",
        }
    return {
        "status": "unsupported_delivery_controls",
        "supported": False,
        "detail": f"{provider or 'Selected TTS provider'} does not consume the requested emotion plan in this MPT adapter.",
    }


def synthesis_fingerprint(
    spoken_script: str,
    settings: ResolvedGenerationSettings,
    delivery_plan: list[dict] | None,
) -> str:
    """Identify audio inputs; visual-only settings intentionally do not participate."""
    payload = {
        "spoken_script": str(spoken_script or "").strip(),
        "tts_provider": settings.tts_provider,
        "tts_model": settings.tts_model,
        "voice_name": settings.voice_name,
        "voice_rate": settings.voice_rate,
        "voice_volume": settings.voice_volume,
        "narrator_preset": settings.narrator_preset,
        "profanity_level": settings.profanity_level,
        "tts_delivery_mode": settings.tts_delivery_mode,
        "delivery_plan": delivery_plan or [],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
