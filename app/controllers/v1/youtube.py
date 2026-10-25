"""OAuth lifecycle routes for direct YouTube publishing."""

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse

from app.controllers import base
from app.controllers.v1.base import new_router
from app.services import autopilot
from app.services.youtube_direct import YouTubePublisherError

router = new_router()


@router.post("/channels/{channel_id}/youtube/oauth", dependencies=[Depends(base.verify_token)])
def begin_youtube_oauth(channel_id: str):
    try:
        instance = autopilot.get_service()
        instance.profile_store.get(channel_id)
        _, authorization_url = instance.youtube_connections.begin_oauth(channel_id)
        return {"authorization_url": authorization_url,
                "instructions": "Open this URL, authorize the intended YouTube channel identity, and return to the configured MPT callback."}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="channel profile not found") from exc
    except YouTubePublisherError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/youtube/oauth/callback")
def youtube_oauth_callback(code: str | None = None, state: str | None = None,
                           error: str | None = None):
    # This callback is intentionally public: Google's redirect is authenticated by
    # the expiring, single-use random state stored by the authenticated start route.
    if error:
        return JSONResponse({"status": "authorization_denied", "message": "Google authorization was not completed."}, status_code=400)
    if not code or not state:
        raise HTTPException(status_code=400, detail="OAuth code and state are required")
    instance = None
    result = None
    try:
        instance = autopilot.get_service()
        result = instance.youtube_publisher.oauth_callback(state, code)
        current = instance.profile_store.get(result["local_channel_id"])
        bound = current.model_copy(update={
            "publishing_backend": "youtube_direct",
            "youtube_connection_id": result["connection_id"],
            "youtube_channel_id": result["youtube_channel_id"],
            "enabled": current.enabled,
        })
        instance.profile_store.update(current.channel_id, bound, current.version)
        return result
    except LookupError as exc:
        _discard_unbound_connection(instance, result)
        raise HTTPException(status_code=404, detail="channel profile for OAuth transaction no longer exists") from exc
    except YouTubePublisherError as exc:
        _discard_unbound_connection(instance, result)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        _discard_unbound_connection(instance, result)
        raise HTTPException(status_code=409, detail="OAuth completed but channel profile changed; inspect the connection and retry binding") from exc


def _discard_unbound_connection(instance, result):
    """Revoke a grant if OAuth succeeded but the profile binding did not commit."""
    connection_id = result.get("connection_id") if isinstance(result, dict) else None
    if not instance or not connection_id:
        return
    try:
        instance.youtube_publisher.revoke(connection_id)
    except Exception:
        instance.youtube_connections.disconnect(connection_id)


@router.get("/channels/{channel_id}/youtube/connection", dependencies=[Depends(base.verify_token)])
def youtube_connection_status(channel_id: str):
    try:
        instance = autopilot.get_service()
        instance.profile_store.get(channel_id)
        return instance.youtube_connections.connection_for_channel(channel_id) or {"state": "not_connected"}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="channel profile not found") from exc


@router.post("/channels/{channel_id}/youtube/disconnect", dependencies=[Depends(base.verify_token)])
def disconnect_youtube(channel_id: str):
    try:
        instance = autopilot.get_service()
        current = instance.profile_store.get(channel_id)
        connection_id = current.youtube_connection_id
        paused = current.model_copy(update={"enabled": False, "youtube_connection_id": None,
                                           "youtube_channel_id": None})
        instance.profile_store.update(channel_id, paused, current.version)
        try:
            if connection_id:
                instance.youtube_publisher.revoke(connection_id)
        finally:
            instance.youtube_connections.disconnect_channel(channel_id)
        return {"channel_id": channel_id, "state": "disconnected", "profile_paused": True}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="channel profile not found") from exc
    except YouTubePublisherError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
