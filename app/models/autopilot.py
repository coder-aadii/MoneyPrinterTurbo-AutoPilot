from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.channel_profile import ChannelProfilePreview
from app.models.schema import VisualStyle


class ContentPlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=2, max_length=500)
    script: str = Field(default="", max_length=8000)
    intended_publication_slot: datetime | None = None
    check_cross_channel_duplicates: bool | None = None
    min_duration_seconds: float | None = Field(default=None, gt=0, le=3600)
    max_duration_seconds: float | None = Field(default=None, gt=0, le=3600)
    duration_enforcement: Literal["flexible", "warn_only", "strict"] | None = None
    visual_style_override: VisualStyle | None = None

    @field_validator("intended_publication_slot")
    @classmethod
    def require_slot_timezone(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("intended_publication_slot must include a timezone")
        return value


class ContentPlan(BaseModel):
    plan_id: str
    channel_id: str
    profile_version: int
    topic: str
    script: str
    visual_style_override: VisualStyle | None = None
    effective_visual_style: Literal["savage_animated_explainer", "asmr_cutting", "relevant_stock"] | None = None
    visual_style_reason: str = ""
    duration_estimate_seconds: float
    min_duration_seconds: float
    max_duration_seconds: float
    duration_enforcement: Literal["flexible", "warn_only", "strict"] = "flexible"
    intended_publication_slot: datetime | None = None
    state: Literal["planned", "ready", "job_created", "cancelled"] = "planned"
    created_at: datetime
    updated_at: datetime

    @field_validator("intended_publication_slot")
    @classmethod
    def require_slot_timezone(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("intended_publication_slot must include a timezone")
        return value


class ContentPlanReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accept_duration_estimate: bool = False


class AssetRightsEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_key: str = Field(min_length=1, max_length=500)
    evidence: str = Field(min_length=1, max_length=2000)


class ContentQualityReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    originality_status: Literal["original", "needs_revision", "needs_review"]
    rights_status: Literal["documented", "missing_evidence", "needs_review"]
    notes: str = Field(min_length=10, max_length=2000)
    rights_evidence: list[AssetRightsEvidence] = Field(default_factory=list, max_length=30)


class ProductionJob(BaseModel):
    job_id: str
    plan_id: str
    channel_id: str
    profile_version: int
    profile_snapshot: dict
    params_snapshot: dict
    visual_style: VisualStyle = "savage_animated_explainer"
    effective_visual_style: Literal["savage_animated_explainer", "asmr_cutting", "relevant_stock"] | None = None
    visual_style_reason: str = ""
    topic: str
    script: str
    fact_verification_state: str = "unverified"
    intended_publication_slot: datetime | None = None
    target_duration_seconds: int
    min_duration_seconds: float
    max_duration_seconds: float
    duration_enforcement: Literal["flexible", "warn_only", "strict"] = "flexible"
    duration_estimate_seconds: float
    actual_duration_seconds: float | None = None
    duration_outside_requested_range: bool | None = None
    state: Literal[
        "planned", "ready", "queued", "generating", "generated", "failed", "cancelled"
    ]
    failure_details: str | None = None
    attempt_count: int = 0
    mpt_task_id: str | None = None
    worker_owner: str | None = Field(default=None, exclude=True)
    output_artifacts: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class JobCreated(BaseModel):
    job: ProductionJob
    preview: ChannelProfilePreview
