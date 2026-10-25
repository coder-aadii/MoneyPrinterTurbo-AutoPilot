from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.autopilot import (ContentPlan, ContentPlanCreate, ContentPlanReview,
                                  ContentQualityReview, JobCreated, ProductionJob)
from app.models.channel_profile import ChannelProfilePreview
from app.services import autopilot as service

router = new_router(dependencies=[Depends(base.verify_token)])


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str


class JobActionResponse(BaseModel):
    job: ProductionJob
    message: str


class ChannelPauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool
    reason: str = Field(default="", max_length=500)


class JobReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str
    reviewer: str = "operator"
    notes: str = Field(default="", max_length=2000)


def _http_error(exc):
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, service.DuplicateContent):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, service.InvalidTransition):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, service.DurationPlanningError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, service.ProductionQueueFull):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, service.AutopilotError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail="invalid Autopilot request")


@router.post("/channels/{channel_id}/content-plans", response_model=ContentPlan, status_code=201)
def create_content_plan(channel_id: str, body: ContentPlanCreate):
    try:
        return service.get_service().plan(channel_id, body)
    except (LookupError, service.AutopilotError, service.DuplicateContent, ValueError) as exc:
        raise _http_error(exc) from exc


@router.get("/channels/{channel_id}/content-plans", response_model=list[ContentPlan])
def list_content_plans(channel_id: str, limit: int = Query(default=100, ge=1, le=500)):
    try:
        service.get_service().profile_store.get(channel_id)
        return service.get_service().store.list_plans(channel_id, limit)
    except LookupError as exc:
        raise _http_error(exc) from exc


@router.post("/channels/{channel_id}/content-plans/{plan_id}/ready", response_model=ContentPlan)
def ready_content_plan(channel_id: str, plan_id: str, body: ContentPlanReview):
    try:
        return service.get_service().ready(
            channel_id, plan_id, body.accept_duration_estimate
        )
    except (LookupError, service.AutopilotError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.get("/channels/{channel_id}/monetization-readiness")
def channel_monetization_readiness(channel_id: str):
    try:
        return service.get_service().readiness_report(channel_id)
    except LookupError as exc:
        raise _http_error(exc) from exc


@router.post("/channels/{channel_id}/production-jobs", response_model=JobCreated, status_code=201)
def create_production_job(channel_id: str, body: JobCreateRequest):
    try:
        instance = service.get_service()
        job = instance.create_job(channel_id, body.plan_id)
        params = instance.store.get_job(job.job_id).params_snapshot
        return JobCreated(
            job=job,
            preview=ChannelProfilePreview(
                channel_id=job.channel_id, profile_version=job.profile_version,
                target_duration_seconds=job.target_duration_seconds, params=params,
            ),
        )
    except (LookupError, service.AutopilotError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.get("/production-jobs", response_model=list[ProductionJob])
def list_production_jobs(
    channel_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    return service.get_service().store.list_jobs(channel_id, limit)


@router.get("/production-jobs/{job_id}", response_model=ProductionJob)
def get_production_job(job_id: str):
    try:
        return service.get_service().store.get_job(job_id)
    except LookupError as exc:
        raise _http_error(exc) from exc


@router.post("/production-jobs/{job_id}/start", response_model=JobActionResponse)
def start_production_job(job_id: str):
    try:
        job = service.get_service().start_job(job_id)
        return JobActionResponse(job=job, message="manual generation submitted")
    except (LookupError, service.AutopilotError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.post("/production-jobs/{job_id}/retry", response_model=JobActionResponse)
def retry_production_job(job_id: str):
    try:
        job = service.get_service().retry_job(job_id)
        return JobActionResponse(job=job, message="retry submitted using the pinned job snapshot")
    except (LookupError, service.AutopilotError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.post("/production-jobs/{job_id}/cancel", response_model=ProductionJob)
def cancel_production_job(job_id: str):
    try:
        return service.get_service().cancel_job(job_id)
    except (LookupError, service.AutopilotError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.post("/production-jobs/{job_id}/recover", response_model=ProductionJob)
def recover_production_job(job_id: str):
    try:
        return service.get_service().store.recover_job(job_id)
    except (LookupError, service.AutopilotError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.get("/channels/{channel_id}/schedule")
def get_channel_schedule(channel_id: str):
    try:
        instance = service.get_service()
        return {**instance.schedule_state(channel_id),
                "slots": instance.store.list_slots(channel_id)}
    except LookupError as exc:
        raise _http_error(exc) from exc


@router.post("/channels/{channel_id}/schedule/pause")
def pause_channel_schedule(channel_id: str, body: ChannelPauseRequest):
    try:
        return service.get_service().set_channel_paused(channel_id, body.paused, body.reason)
    except LookupError as exc:
        raise _http_error(exc) from exc


@router.post("/scheduler/tick")
def run_scheduler_tick():
    return {"created": service.get_service().tick_scheduler()}


@router.get("/production-jobs/{job_id}/qa")
def get_job_qa(job_id: str):
    try:
        instance = service.get_service()
        with instance.store._connection() as conn:
            row = conn.execute("SELECT state,report_json,reviewed_by,reviewed_at FROM job_qa WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return instance.qa_report(job_id)
        return {"state": row["state"], "report": __import__("json").loads(row["report_json"]),
                "reviewed_by": row["reviewed_by"], "reviewed_at": row["reviewed_at"]}
    except (LookupError, service.AutopilotError, service.InvalidTransition) as exc:
        raise _http_error(exc) from exc


@router.post("/production-jobs/{job_id}/review")
def review_production_job(job_id: str, body: JobReviewRequest):
    try:
        return service.get_service().review_job(job_id, body.decision, body.reviewer, body.notes)
    except (LookupError, service.AutopilotError, service.InvalidTransition, ValueError) as exc:
        raise _http_error(exc) from exc


@router.post("/production-jobs/{job_id}/publish")
def publish_production_job(job_id: str):
    try:
        return service.get_service().publish_job(job_id)
    except (LookupError, service.AutopilotError, service.InvalidTransition, ValueError) as exc:
        raise _http_error(exc) from exc


@router.post("/production-jobs/{job_id}/retry-publication")
def retry_production_publication(job_id: str):
    try:
        return service.get_service().publish_job(job_id)
    except (LookupError, service.AutopilotError, service.InvalidTransition, ValueError) as exc:
        raise _http_error(exc) from exc


@router.post("/production-jobs/{job_id}/content-quality-review")
def review_content_quality(job_id: str, body: ContentQualityReview, reviewer: str = "operator"):
    try:
        return service.get_service().review_content_quality(job_id, reviewer, body)
    except (LookupError, service.AutopilotError, service.InvalidTransition, ValueError) as exc:
        raise _http_error(exc) from exc


@router.get("/channels/{channel_id}/calendar")
def channel_calendar(channel_id: str):
    try:
        instance = service.get_service()
        instance.profile_store.get(channel_id)
        return {"slots": instance.store.list_slots(channel_id),
                "jobs": [job.model_dump(mode="json") for job in instance.store.list_jobs(channel_id)],
                "publications": instance.store.list_publications(channel_id)}
    except LookupError as exc:
        raise _http_error(exc) from exc
