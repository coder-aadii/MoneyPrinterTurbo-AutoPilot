from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.schema import AsmrCategory, StockVideoStyle, TaskVideoRequest, VideoAspect, VisualStyle


class ChannelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(default_factory=lambda: str(uuid4()))
    display_name: str = Field(min_length=1, max_length=120)
    channel_hashtag: str = Field(default="", max_length=80)
    niche: str = Field(default="", max_length=500)
    topic_preferences: list[str] = Field(default_factory=list, max_length=100)
    language: str = Field(default="", max_length=64)
    script_style: str = Field(default="", max_length=2000)
    target_duration_seconds: int = Field(default=30, ge=5, le=3600)
    duration_enforcement: Literal["flexible", "warn_only", "strict"] = "flexible"
    video_aspect: VideoAspect = VideoAspect.portrait
    stock_video_style: StockVideoStyle = "default"
    visual_style: VisualStyle = "savage_animated_explainer"
    visual_style_rotation: list[VisualStyle] = Field(default_factory=list, max_length=3)
    fact_verification_policy: Literal["off", "advisory", "strict"] = "advisory"
    asmr_categories: list[AsmrCategory] = Field(
        default_factory=lambda: ["kinetic_sand", "soap"], min_length=1, max_length=6
    )
    video_source: Literal[
        "pexels", "pixabay", "coverr", "local", "wavespeed", "volcengine_seedance",
        "ofox", "metaso_minimax", "muapi", "loomloom", "openai_image",
    ] = "pexels"
    voice_name: str = Field(default="", max_length=200)
    tts_model: str = Field(default="gemini-3.8-flash-tts", max_length=100)
    narrator_preset: Literal["standard", "desi_savage_aggressive"] = "desi_savage_aggressive"
    profanity_level: Literal["mild", "savage_monetization_aware", "savage_unfiltered"] = "savage_monetization_aware"
    savage_quality_policy: Literal["advisory", "strict"] = "advisory"
    voice_rate: float = Field(default=1.0, ge=0.5, le=2.0)
    voice_volume: float = Field(default=1.0, ge=0, le=2.0)
    hindi_alignment_policy: Literal["balanced", "strict"] = "balanced"
    subtitle_enabled: bool = True
    subtitle_position: str = Field(default="bottom", max_length=32)
    subtitle_display_mode: Literal["sentence", "word_by_word"] = "word_by_word"
    subtitle_animation: Literal["none", "pop_spring"] = "pop_spring"
    font_name: str = Field(default="NotoSansDevanagari-VF.ttf", max_length=200)
    bgm_type: str = Field(default="random", max_length=64)
    bgm_file: str = Field(default="", max_length=1000)
    bgm_volume: float = Field(default=0.2, ge=0, le=1)
    intro_enabled: bool = False
    intro_text: str = Field(default="", max_length=1000)
    intro_audio: dict[str, str | float | int | None] | None = None
    outro_enabled: bool = False
    outro_text: str = Field(default="", max_length=1000)
    outro_audio: dict[str, str | float | int | None] | None = None
    generation_provider: str = Field(default="global", max_length=100)
    generation_settings: dict[str, str | int | float | bool] = Field(default_factory=dict)
    timezone: str = "UTC"
    publications_per_week: int = Field(default=0, ge=0, le=100)
    schedule_times_local: list[str] = Field(default_factory=lambda: ["09:00"], max_length=24)
    schedule_weekdays: list[int] = Field(default_factory=lambda: list(range(7)), max_length=7)
    ready_buffer_target: int = Field(default=6, ge=1, le=24)
    generation_lead_hours: int = Field(default=48, ge=1, le=336)
    provider_retry_limit: int = Field(default=2, ge=0, le=5)
    check_cross_channel_duplicates: bool = False
    publishing_policy: Literal["manual", "private", "unlisted", "scheduled_public", "automatic_public"] = "manual"
    publishing_backend: Literal["youtube_direct", "upload_post"] = "youtube_direct"
    youtube_connection_id: str | None = Field(default=None, max_length=64)
    youtube_channel_id: str | None = Field(default=None, max_length=128)
    auto_public_enabled: bool = False
    allow_unverified_publication: bool = False
    youtube_made_for_kids: bool = False
    contains_synthetic_media: bool = False
    upload_post_username: str | None = Field(default=None, max_length=200)
    enabled: bool = True
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_visual_style(cls, values):
        if isinstance(values, dict) and "visual_style" not in values:
            legacy = values.get("stock_video_style")
            if legacy == "satisfying_asmr_cutting":
                values = {**values, "visual_style": "asmr_cutting"}
            elif legacy == "cinematic_stock":
                values = {**values, "visual_style": "relevant_stock"}
        return values

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("upload_post_username")
    @classmethod
    def clean_upload_profile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("schedule_times_local")
    @classmethod
    def validate_schedule_times(cls, values: list[str]) -> list[str]:
        import re
        if not values or any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) for value in values):
            raise ValueError("schedule_times_local must contain HH:MM times")
        return sorted(set(values))

    @field_validator("schedule_weekdays")
    @classmethod
    def validate_schedule_weekdays(cls, values: list[int]) -> list[int]:
        if not values or any(day < 0 or day > 6 for day in values):
            raise ValueError("schedule_weekdays must contain weekdays from 0 (Monday) to 6 (Sunday)")
        return sorted(set(values))

    @field_validator("visual_style_rotation")
    @classmethod
    def validate_visual_style_rotation(cls, values: list[VisualStyle]) -> list[VisualStyle]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_schedule_capacity(self):
        if self.publications_per_week > len(self.schedule_times_local) * len(self.schedule_weekdays):
            raise ValueError("schedule times and weekdays must provide at least publications_per_week slots")
        return self

    @field_validator("generation_settings")
    @classmethod
    def reject_credential_fields(cls, value: dict[str, str | int | float | bool]):
        allowed = {"model", "quality", "resolution", "duration", "seed", "style", "temperature"}
        if any(key not in allowed for key in value):
            raise ValueError("generation_settings only accepts non-secret generation options")
        return value


class ChannelProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    profile: ChannelProfile


class ChannelProfilePreview(BaseModel):
    channel_id: str
    profile_version: int
    target_duration_seconds: int
    params: TaskVideoRequest
