from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.channel_profile import ChannelProfile, ChannelProfilePreview, ChannelProfileUpdate
from app.services import channel_profiles as profiles

router = new_router(dependencies=[Depends(base.verify_token)])


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel_id: str
    video_subject: str = Field(default="", max_length=500)
    video_script: str = Field(default="", max_length=8000)


def _http_error(exc):
    if isinstance(exc, profiles.ProfileNotFound):
        return HTTPException(status_code=404, detail="channel profile not found")
    if isinstance(exc, profiles.ProfileVersionConflict):
        return HTTPException(status_code=409, detail="channel profile version conflict")
    if isinstance(exc, profiles.UploadProfileUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, profiles.UploadProfileError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail="invalid channel profile")


@router.get("/channels", response_model=list[ChannelProfile])
def list_channels():
    return profiles.store.list()


@router.post("/channels", response_model=ChannelProfile, status_code=201)
def create_channel(profile: ChannelProfile):
    try:
        profiles.verify_publisher_binding(profile)
        return profiles.store.create(profile)
    except (profiles.ProfileNotFound, profiles.ProfileVersionConflict, profiles.UploadProfileError) as exc:
        raise _http_error(exc) from exc


@router.get("/channels/upload-post/profiles")
def list_upload_post_profiles():
    try:
        return profiles.upload_post_profiles()
    except profiles.UploadProfileUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/channels/{channel_id}", response_model=ChannelProfile)
def get_channel(channel_id: str, version: int | None = None):
    try:
        return profiles.store.get(channel_id, version)
    except profiles.ProfileNotFound as exc:
        raise _http_error(exc) from exc


@router.put("/channels/{channel_id}", response_model=ChannelProfile)
def update_channel(channel_id: str, body: ChannelProfileUpdate):
    try:
        profiles.verify_publisher_binding(body.profile)
        return profiles.store.update(channel_id, body.profile, body.expected_version)
    except (profiles.ProfileNotFound, profiles.ProfileVersionConflict, profiles.UploadProfileError) as exc:
        raise _http_error(exc) from exc


@router.post("/channels/{channel_id}/disable", response_model=ChannelProfile)
def disable_channel(channel_id: str, expected_version: int = Query(ge=1)):
    try:
        current = profiles.store.get(channel_id)
        if current.version != expected_version:
            raise profiles.ProfileVersionConflict("stale profile version")
        disabled = current.model_copy(update={"enabled": False})
        return profiles.store.update(channel_id, disabled, expected_version)
    except (profiles.ProfileNotFound, profiles.ProfileVersionConflict) as exc:
        raise _http_error(exc) from exc


@router.post("/channels/preview", response_model=ChannelProfilePreview)
def preview_channel(body: PreviewRequest):
    try:
        profile = profiles.store.get(body.channel_id)
        return profiles.build_preview(profile, {
            "video_subject": body.video_subject,
            "video_script": body.video_script,
        })
    except profiles.ProfileNotFound as exc:
        raise _http_error(exc) from exc
