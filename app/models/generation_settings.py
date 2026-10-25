"""Resolved generation preferences shared by planning and video tasks."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResolvedGenerationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visual_style: Literal[
        "auto", "savage_animated_explainer", "relevant_stock", "asmr_cutting"
    ] = "auto"
    effective_visual_style: Literal[
        "savage_animated_explainer", "relevant_stock", "asmr_cutting"
    ] = "relevant_stock"
    visual_style_reason: str = Field(default="", max_length=500)
    style_preset_version: str = Field(default="1", max_length=32)
    min_duration_seconds: float | None = Field(default=None, gt=0, le=3600)
    max_duration_seconds: float | None = Field(default=None, gt=0, le=3600)
    duration_enforcement: Literal["flexible", "warn_only", "strict"] = "flexible"
    narrator_preset: Literal["standard", "desi_savage_aggressive"] = "standard"
    profanity_level: Literal[
        "mild", "savage_monetization_aware", "savage_unfiltered"
    ] = "savage_monetization_aware"
    tts_provider: str = Field(default="", max_length=100)
    tts_model: str = Field(default="", max_length=100)
    voice_name: str = Field(default="", max_length=200)
    voice_rate: float = Field(default=1.0, ge=0.1, le=4.0)
    voice_volume: float = Field(default=1.0, ge=0.0, le=2.0)
    caption_preset: str = Field(default="sentence", max_length=100)
    tts_delivery_mode: Literal[
        "native_metadata", "prompt_guided_legacy", "unsupported"
    ] = "unsupported"
