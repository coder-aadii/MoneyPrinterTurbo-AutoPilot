"""Persistent, versioned channel configuration and Upload-Post binding checks."""

import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from pydantic import BaseModel

from app.config import config
from app.models.channel_profile import ChannelProfile, ChannelProfilePreview
from app.models.schema import TaskVideoRequest
from app.services.upload_post import UploadPostService
from app.services import visual_style
from app.services.generation_settings import resolve_generation_settings
from app.services.youtube_direct import YouTubeConnectionStore
from app.utils import utils


class ProfileNotFound(LookupError):
    pass


class ProfileVersionConflict(ValueError):
    pass


class UploadProfileError(ValueError):
    pass


class UploadProfileUnavailable(UploadProfileError):
    pass


def _json_value(model: BaseModel) -> str:
    return model.model_dump_json()


def _profile_from_json(value: str) -> ChannelProfile:
    data = json.loads(value)
    # Rows created before backend selection always used Upload-Post.
    data.setdefault("publishing_backend", "upload_post")
    return ChannelProfile.model_validate(data)


class ChannelProfileStore:
    def __init__(self, database_path: str | Path | None = None):
        self.database_path = Path(database_path or (Path(utils.storage_dir("autopilot", create=True)) / "profiles.sqlite3"))
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(str(self.database_path), timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self):
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS channel_profiles (
                    channel_id TEXT PRIMARY KEY,
                    current_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_profile_versions (
                    channel_id TEXT NOT NULL REFERENCES channel_profiles(channel_id),
                    version INTEGER NOT NULL,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(channel_id, version)
                );
            """)

    def create(self, profile: ChannelProfile) -> ChannelProfile:
        now = datetime.now(timezone.utc)
        profile = profile.model_copy(update={"version": 1, "created_at": now, "updated_at": now})
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("INSERT INTO channel_profiles VALUES (?, ?, ?, ?)",
                             (profile.channel_id, 1, now.isoformat(), now.isoformat()))
                conn.execute("INSERT INTO channel_profile_versions VALUES (?, ?, ?, ?)",
                             (profile.channel_id, 1, _json_value(profile), now.isoformat()))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return profile

    def list(self, include_disabled: bool = True) -> list[ChannelProfile]:
        with self._connection() as conn:
            rows = conn.execute("""SELECT v.config_json FROM channel_profiles p
                JOIN channel_profile_versions v ON v.channel_id=p.channel_id AND v.version=p.current_version
                ORDER BY p.created_at, p.channel_id""").fetchall()
        profiles = [_profile_from_json(row[0]) for row in rows]
        return profiles if include_disabled else [p for p in profiles if p.enabled]

    def get(self, channel_id: str, version: int | None = None) -> ChannelProfile:
        with self._connection() as conn:
            if version is None:
                row = conn.execute("""SELECT v.config_json FROM channel_profiles p
                    JOIN channel_profile_versions v ON v.channel_id=p.channel_id AND v.version=p.current_version
                    WHERE p.channel_id=?""", (channel_id,)).fetchone()
            else:
                row = conn.execute("SELECT config_json FROM channel_profile_versions WHERE channel_id=? AND version=?",
                                    (channel_id, version)).fetchone()
        if row is None:
            raise ProfileNotFound(channel_id)
        return _profile_from_json(row[0])

    def update(self, channel_id: str, profile: ChannelProfile, expected_version: int) -> ChannelProfile:
        now = datetime.now(timezone.utc)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT current_version, created_at FROM channel_profiles WHERE channel_id=?",
                                   (channel_id,)).fetchone()
                if row is None:
                    raise ProfileNotFound(channel_id)
                if row["current_version"] != expected_version:
                    raise ProfileVersionConflict(f"expected version {expected_version}, found {row['current_version']}")
                next_version = expected_version + 1
                profile = profile.model_copy(update={"channel_id": channel_id, "version": next_version,
                                                       "created_at": datetime.fromisoformat(row["created_at"]),
                                                       "updated_at": now})
                conn.execute("UPDATE channel_profiles SET current_version=?, updated_at=? WHERE channel_id=?",
                             (next_version, now.isoformat(), channel_id))
                conn.execute("INSERT INTO channel_profile_versions VALUES (?, ?, ?, ?)",
                             (channel_id, next_version, _json_value(profile), now.isoformat()))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return profile


def build_preview(profile: ChannelProfile, job_values: dict[str, Any] | None = None) -> ChannelProfilePreview:
    """Resolve profile defaults with explicit per-job topic/script taking precedence."""
    values = dict(job_values or {})
    profile_fields = {
        "channel_id": profile.channel_id,
        "channel_branding": {
            name: {
                "enabled": getattr(profile, f"{name}_enabled"),
                "text": getattr(profile, f"{name}_text"),
                "audio": getattr(profile, f"{name}_audio"),
            }
            for name in ("intro", "outro")
        },
        "video_aspect": profile.video_aspect.value,
        "stock_video_style": profile.stock_video_style,
        "visual_style": profile.visual_style,
        "asmr_categories": profile.asmr_categories,
        "video_source": profile.video_source,
        "video_language": profile.language,
        "voice_name": resolve_profile_voice(profile),
        "tts_provider": profile.generation_settings.get("tts_provider"),
        "tts_model": profile.tts_model,
        "narrator_preset": profile.narrator_preset,
        "profanity_level": profile.profanity_level,
        "fact_verification_policy": profile.fact_verification_policy,
        "allow_unverified_publication": profile.allow_unverified_publication,
        "caption_preset": profile.subtitle_display_mode,
        "voice_rate": profile.voice_rate,
        "voice_volume": profile.voice_volume,
        "hindi_alignment_policy": profile.hindi_alignment_policy,
        "subtitle_enabled": profile.subtitle_enabled,
        "subtitle_position": profile.subtitle_position,
        "subtitle_display_mode": profile.subtitle_display_mode,
        "subtitle_animation": profile.subtitle_animation,
        "duration_enforcement": profile.duration_enforcement,
        "font_name": profile.font_name,
        "bgm_type": profile.bgm_type,
        "bgm_file": profile.bgm_file,
        "bgm_volume": profile.bgm_volume,
    }
    # Resolve settings before constructing the immutable task request snapshot.
    explicit_task_values = dict(values)
    values = {**values, **profile_fields}
    for key in (
        "fact_verification_policy", "fact_research_timeout_seconds",
        "allow_unverified_publication", "hindi_alignment_policy",
    ):
        if key in explicit_task_values and explicit_task_values[key] is not None:
            values[key] = explicit_task_values[key]
    resolved = resolve_generation_settings(
        task_override=explicit_task_values,
        channel_profile=profile_fields,
        topic=str(values.get("video_subject") or ""),
    )
    values.update(resolved.model_dump(mode="python"))
    values["resolved_generation_settings"] = resolved
    if resolved.caption_preset in {"sentence", "word_by_word"}:
        values["subtitle_display_mode"] = resolved.caption_preset
    # A per-task mode is the one channel setting callers may override.
    values["visual_style"] = resolved.visual_style
    values.setdefault("video_subject", "")
    values.setdefault("video_script", "")
    style_bits = [profile.niche, profile.script_style,
                  f"Target duration: {profile.target_duration_seconds} seconds"]
    profile_prompt = "\n".join(bit for bit in style_bits if bit)
    if profile_prompt:
        values["video_script_prompt"] = profile_prompt
    effective, reason = visual_style.initial_selection(
        resolved.visual_style,
        values.get("video_script", ""),
        values.get("video_subject", ""),
    )
    effective = resolved.effective_visual_style
    values["effective_visual_style"] = values.get("effective_visual_style") or effective
    values["visual_style_reason"] = values.get("visual_style_reason") or reason
    values["stock_video_style"] = (
        "satisfying_asmr_cutting" if values["effective_visual_style"] == visual_style.ASMR_CUTTING
        else "default"
    )
    params = TaskVideoRequest.model_validate(values)
    return ChannelProfilePreview(channel_id=profile.channel_id, profile_version=profile.version,
                                 target_duration_seconds=profile.target_duration_seconds, params=params)


def _provider_profiles() -> list[dict[str, Any]]:
    key = config.app.get("upload_post_api_key", "")
    if not isinstance(key, str) or not key.strip():
        raise UploadProfileUnavailable("Upload-Post API key is not configured")
    try:
        response = requests.get(
            f"{UploadPostService.API_BASE}/api/uploadposts/users",
            headers={"Authorization": f"Apikey {key}"}, timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise UploadProfileUnavailable("Upload-Post profile verification is unavailable") from exc
    # API docs return a list, sometimes wrapped in a data/users object.
    if isinstance(payload, dict):
        payload = payload.get("users", payload.get("data", []))
    if not isinstance(payload, list):
        raise UploadProfileUnavailable("Upload-Post returned an unrecognized profile response")
    return [entry for entry in payload if isinstance(entry, dict)]


def upload_post_profiles() -> list[dict[str, Any]]:
    """Return public connection state only; never return credentials or raw provider payload."""
    sanitized = []
    for entry in _provider_profiles():
        username = entry.get("username")
        social = entry.get("social_accounts") or {}
        youtube = social.get("youtube") if isinstance(social, dict) else None
        connected = isinstance(youtube, dict) and bool(youtube.get("username") or youtube.get("channel_id"))
        sanitized.append({
            "username": username,
            "display_name": entry.get("display_name") or username,
            "youtube_connected": connected,
            "reauth_required": bool(youtube.get("reauth_required")) if isinstance(youtube, dict) else False,
            "youtube_handle": (youtube.get("handle") or youtube.get("username")) if isinstance(youtube, dict) else None,
        })
    return sanitized


def verify_youtube_binding(username: str | None) -> dict[str, Any] | None:
    if username is None:
        return None
    for profile in upload_post_profiles():
        if profile.get("username") == username:
            if not profile["youtube_connected"]:
                raise UploadProfileError("Upload-Post profile is not connected to YouTube")
            if profile["reauth_required"]:
                raise UploadProfileError("Upload-Post YouTube connection requires reauthorization")
            return profile
    raise UploadProfileError("Upload-Post profile was not found")


def verify_publisher_binding(profile: ChannelProfile):
    if profile.publishing_backend == "upload_post":
        verify_youtube_binding(profile.upload_post_username)
        return
    if not profile.youtube_connection_id and not profile.youtube_channel_id:
        return  # Allow saving a profile draft before its first OAuth connection.
    if not profile.youtube_connection_id or not profile.youtube_channel_id:
        raise UploadProfileError("direct YouTube destination requires both connection and verified channel IDs")
    connection = YouTubeConnectionStore(store.database_path).get_connection(profile.youtube_connection_id)
    if (connection["local_channel_id"] != profile.channel_id
            or connection["youtube_channel_id"] != profile.youtube_channel_id):
        raise UploadProfileError("direct YouTube connection does not match this channel profile")


def resolve_profile_voice(profile: ChannelProfile) -> str:
    """Use the Video Generator voice as the default when a channel has no override."""
    return profile.voice_name or str(config.ui.get("voice_name", "") or "")


class _LazyChannelProfileStore:
    """Avoid creating storage files just because MPT imports its API modules."""

    def __init__(self):
        self._store: ChannelProfileStore | None = None

    def __getattr__(self, name):
        if self._store is None:
            self._store = ChannelProfileStore()
        return getattr(self._store, name)


store = _LazyChannelProfileStore()
