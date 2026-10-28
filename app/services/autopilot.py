"""Persistent channel content plans and manually started production jobs."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import socket
import subprocess
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from app.config import config
from app.models import const
from app.models.autopilot import ContentPlan, ContentPlanCreate, ProductionJob
from app.models.channel_profile import ChannelProfile
from app.models.schema import TaskVideoRequest
from app.services import llm, state as sm, task as task_service, video
from app.services import script_quality
from app.services import youtube_direct
from app.services import visual_style
from app.services import hindi_numbers
from app.services.upload_post import UploadPostService
from app.services.channel_profiles import ChannelProfileStore
from app.services.generation_settings import resolve_generation_settings
from app.utils import utils


class AutopilotError(ValueError):
    pass


class DuplicateContent(AutopilotError):
    pass


class InvalidTransition(AutopilotError):
    pass


class DurationPlanningError(AutopilotError):
    pass


class ProductionQueueFull(AutopilotError):
    pass


_ALLOWED_TRANSITIONS = {
    "planned": {"ready", "cancelled"},
    "ready": {"queued", "cancelled"},
    "queued": {"generating", "failed", "cancelled"},
    "generating": {"generated", "failed"},
    "failed": {"ready", "cancelled"},
    "generated": {"failed"},
    "cancelled": set(),
}
_NEAR_DUPLICATE_TOPIC = 0.86
_NEAR_DUPLICATE_SCRIPT = 0.78
_HISTORY_SCAN_LIMIT = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_content(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    # Python's \w excludes combining marks. Removing Hindi vowel signs would
    # turn distinct Devanagari words into damaged topic/script fingerprints.
    value = "".join(char if char == "_" or char.isspace()
                    or unicodedata.category(char)[0] in {"L", "N", "M"} else " "
                    for char in value)
    return " ".join(value.split())


def fingerprint(value: str) -> str:
    return hashlib.sha256(normalize_content(value).encode("utf-8")).hexdigest()


def _safe_failure(value: object) -> str:
    message = llm._sanitize_error_message(value)
    secret_markers = ("api_key", "api_keys", "token", "password", "secret", "credential")
    for section_name in ("app", "azure", "chatterbox", "elevenlabs", "fish_audio",
                         "kokoro", "minimax_tts", "proxy", "siliconflow", "voxcpm"):
        section = getattr(config, section_name, {})
        if not isinstance(section, dict):
            continue
        for key, secret_values in section.items():
            if not any(marker in str(key).lower() for marker in secret_markers):
                continue
            values = secret_values if isinstance(secret_values, (list, tuple)) else [secret_values]
            for secret in values:
                if isinstance(secret, str) and len(secret) >= 5:
                    message = message.replace(secret, "[redacted]")
    return message[:2000]


def _transient_generation_failure(value: str | None) -> bool:
    message = (value or "").casefold()
    return any(marker in message for marker in (
        "connectionerror", "connection error", "timeout", "temporarily unavailable",
        "temporary failure", "rate limit", "too many requests", "http 429", "http 500",
        "http 502", "http 503", "http 504", "service unavailable", "name resolution",
    ))


def _safe_receipt(value: Any) -> str:
    """Serialize provider receipts while excluding credentials and credential-like fields."""
    secret_markers = ("api_key", "token", "password", "secret", "credential", "authorization")

    def clean(item):
        if isinstance(item, dict):
            return {str(key): ("[redacted]" if any(marker in str(key).lower() for marker in secret_markers)
                               else clean(child)) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(child) for child in item]
        if isinstance(item, str):
            cleaned = item
            for section_name in ("app", "azure", "chatterbox", "elevenlabs", "fish_audio", "kokoro",
                                 "minimax_tts", "proxy", "siliconflow", "voxcpm"):
                section = getattr(config, section_name, {})
                if not isinstance(section, dict):
                    continue
                for key, secret in section.items():
                    if any(marker in str(key).lower() for marker in secret_markers) and isinstance(secret, str) and len(secret) >= 5:
                        cleaned = cleaned.replace(secret, "[redacted]")
            return cleaned
        return item

    return json.dumps(clean(value), ensure_ascii=False, default=str)


def _youtube_receipt_result(results: Any) -> dict:
    if isinstance(results, dict):
        entry = results.get("youtube") or results.get("youtube_shorts") or {}
        return entry if isinstance(entry, dict) else {}
    if isinstance(results, list):
        return next((entry for entry in results if isinstance(entry, dict)
                     and str(entry.get("platform", "")).startswith("youtube")), {})
    return {}


def _platform_receipt_result(results: Any, platform: str) -> dict:
    if isinstance(results, dict):
        entry = results.get(platform) or {}
        return entry if isinstance(entry, dict) else {}
    if isinstance(results, list):
        return next((entry for entry in results if isinstance(entry, dict)
                     and entry.get("platform") == platform), {})
    return {}


def _script_similarity(left: str, right: str) -> float:
    left_words, right_words = normalize_content(left).split(), normalize_content(right).split()
    if not left_words or not right_words:
        return 0.0
    if max(len(left_words), len(right_words)) < 8:
        return SequenceMatcher(None, " ".join(left_words), " ".join(right_words)).ratio()
    left_grams = set(zip(left_words, left_words[1:], left_words[2:]))
    right_grams = set(zip(right_words, right_words[1:], right_words[2:]))
    union = left_grams | right_grams
    return len(left_grams & right_grams) / len(union) if union else 0.0


def estimate_narration_duration(script: str, language: str, voice_rate: float) -> float:
    """Coarse word-rate estimate used for planning only; rendered media is measured later."""
    words = len(re.findall(r"\S+", script.strip()))
    words_per_second = 2.35 if (language or "").lower().startswith("hi") else 2.7
    rate = max(0.5, min(float(voice_rate or 1.0), 2.0))
    return round(words / (words_per_second * rate), 2)


def _duration_bounds(profile: ChannelProfile, request: ContentPlanCreate) -> tuple[float, float]:
    target = float(profile.target_duration_seconds)
    minimum = request.min_duration_seconds if request.min_duration_seconds is not None else max(1.0, target - 5.0)
    maximum = request.max_duration_seconds if request.max_duration_seconds is not None else target
    if minimum >= maximum:
        raise ValueError("min_duration_seconds must be less than max_duration_seconds")
    return float(minimum), float(maximum)


def _script_prompt(profile: ChannelProfile, topic: str, style: str | None = None) -> str:
    effective_style = style or profile.visual_style
    parts = [profile.niche, profile.script_style]
    if profile.topic_preferences:
        parts.append("Channel topic preferences: " + ", ".join(profile.topic_preferences))
    if (profile.language or "").lower().startswith("hi") and effective_style == "asmr_cutting":
        parts.append("Structure the narration as Hook → Interesting Fact → Explanation → Payoff, in concise natural Hindi.")
    parts.append(
        f"Draft narration for the topic: {topic}. Aim for about {profile.target_duration_seconds} seconds. "
        + ("Duration is guidance; keep the explanation complete and natural. "
           if profile.duration_enforcement != "strict" else
           "Keep the narration within the requested duration range. ")
        + "Write original narration and explanation in fresh wording; do not copy or minimally rewrite source text. "
        "Open with a specific hook, explain one distinct subject in a useful or entertaining way, "
        "and end with a clear payoff."
    )
    # build_script_prompt adds the selected preset's visual and narration guidance
    # itself. Copying those same long blocks here can push a valid profile over
    # MAX_SCRIPT_PROMPT_LENGTH before the provider request is made.
    return "\n".join(part for part in parts if part)


class AutopilotStore:
    def __init__(self, database_path: str | Path | None = None):
        if database_path is None:
            database_path = Path(utils.storage_dir("autopilot", create=True)) / "profiles.sqlite3"
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(str(self.database_path), timeout=20, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
        conn.execute("PRAGMA foreign_keys=ON")
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
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS content_plans (
                    plan_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    profile_version INTEGER NOT NULL,
                    profile_json TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    topic_fingerprint TEXT NOT NULL,
                    script TEXT NOT NULL,
                    script_fingerprint TEXT NOT NULL,
                    source_references_json TEXT NOT NULL,
                    research_report_json TEXT NOT NULL DEFAULT '{}',
                    visual_style_override TEXT NOT NULL DEFAULT '',
                    effective_visual_style TEXT,
                    visual_style_reason TEXT NOT NULL DEFAULT '',
                    fact_verification_state TEXT NOT NULL,
                    fact_review_notes TEXT NOT NULL DEFAULT '',
                    duration_estimate_seconds REAL NOT NULL,
                    min_duration_seconds REAL NOT NULL,
                    max_duration_seconds REAL NOT NULL,
                    intended_publication_slot TEXT,
                    state TEXT NOT NULL,
                    cross_channel_check INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(channel_id, topic_fingerprint),
                    UNIQUE(channel_id, script_fingerprint)
                );
                CREATE INDEX IF NOT EXISTS ix_content_plans_channel_created
                    ON content_plans(channel_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS production_jobs (
                    job_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL UNIQUE REFERENCES content_plans(plan_id),
                    parent_job_id TEXT,
                    channel_id TEXT NOT NULL,
                    profile_version INTEGER NOT NULL,
                    profile_snapshot_json TEXT NOT NULL,
                    params_snapshot_json TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    script TEXT NOT NULL,
                    source_references_json TEXT NOT NULL,
                    research_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    visual_style TEXT NOT NULL DEFAULT 'auto',
                    effective_visual_style TEXT,
                    visual_style_reason TEXT NOT NULL DEFAULT '',
                    fact_review_notes TEXT NOT NULL DEFAULT '',
                    fact_verification_state TEXT NOT NULL,
                    intended_publication_slot TEXT,
                    target_duration_seconds INTEGER NOT NULL,
                    min_duration_seconds REAL NOT NULL,
                    max_duration_seconds REAL NOT NULL,
                    duration_estimate_seconds REAL NOT NULL,
                    actual_duration_seconds REAL,
                    duration_outside_requested_range INTEGER,
                    state TEXT NOT NULL,
                    failure_details TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    mpt_task_id TEXT,
                    worker_owner TEXT,
                    output_artifacts_json TEXT NOT NULL DEFAULT '[]',
                    youtube_metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_production_jobs_channel_created
                    ON production_jobs(channel_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS ix_production_jobs_state
                    ON production_jobs(state, created_at);
                CREATE TABLE IF NOT EXISTS channel_runtime (
                    channel_id TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
                    reason TEXT, updated_at TEXT NOT NULL,
                    last_scheduler_tick_at TEXT
                );
                CREATE TABLE IF NOT EXISTS autopilot_slots (
                    slot_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, slot_utc TEXT NOT NULL,
                    state TEXT NOT NULL, job_id TEXT, details TEXT, created_at TEXT NOT NULL,
                    lease_owner TEXT, lease_expires_at TEXT, visual_style TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0, recovery_of_slot_id TEXT,
                    UNIQUE(channel_id, slot_utc)
                );
                CREATE TABLE IF NOT EXISTS job_qa (
                    job_id TEXT PRIMARY KEY REFERENCES production_jobs(job_id),
                    state TEXT NOT NULL, report_json TEXT NOT NULL, reviewed_by TEXT,
                    reviewed_at TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_fact_reviews (
                    job_id TEXT PRIMARY KEY REFERENCES production_jobs(job_id),
                    channel_id TEXT NOT NULL, source_references_json TEXT NOT NULL,
                    reviewer TEXT NOT NULL, notes TEXT NOT NULL, reviewed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publications (
                    publication_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES production_jobs(job_id),
                    channel_id TEXT NOT NULL, profile_version INTEGER NOT NULL, account_username TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE, provider_request_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL, provider_video_id TEXT, provider_url TEXT,
                    receipt_json TEXT, failure_details TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    publisher_backend TEXT NOT NULL DEFAULT 'upload_post', youtube_connection_id TEXT,
                    youtube_channel_id TEXT, resumable_session_url TEXT, retry_after TEXT,
                    platform TEXT NOT NULL DEFAULT 'youtube', destination_account_id TEXT NOT NULL DEFAULT '',
                    provider_schedule_id TEXT, next_check_at TEXT,
                    UNIQUE(job_id, platform, destination_account_id)
                );
                CREATE TABLE IF NOT EXISTS job_content_reviews (
                    job_id TEXT PRIMARY KEY REFERENCES production_jobs(job_id),
                    originality_status TEXT NOT NULL, rights_status TEXT NOT NULL,
                    notes TEXT NOT NULL, rights_evidence_json TEXT NOT NULL,
                    reviewer TEXT NOT NULL, reviewed_at TEXT NOT NULL
                );
            """)
            publication_columns = {row["name"] for row in conn.execute("PRAGMA table_info(publications)")}
            if "provider_schedule_id" not in publication_columns:
                conn.execute("ALTER TABLE publications ADD COLUMN provider_schedule_id TEXT")
            if "next_check_at" not in publication_columns:
                conn.execute("ALTER TABLE publications ADD COLUMN next_check_at TEXT")
            for column, definition in (
                ("publisher_backend", "TEXT NOT NULL DEFAULT 'upload_post'"),
                ("youtube_connection_id", "TEXT"), ("youtube_channel_id", "TEXT"),
                ("resumable_session_url", "TEXT"), ("retry_after", "TEXT"),
                ("platform", "TEXT NOT NULL DEFAULT 'youtube'"),
                ("destination_account_id", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in publication_columns:
                    conn.execute(f"ALTER TABLE publications ADD COLUMN {column} {definition}")
            # Old installations allowed only one publication per job. Rebuild the
            # same table with a destination-scoped key while preserving all legacy
            # YouTube receipts and resumable session state.
            unique_job_index = False
            for index in conn.execute("PRAGMA index_list(publications)").fetchall():
                if not index["unique"]:
                    continue
                indexed_columns = [row["name"] for row in conn.execute(
                    f"PRAGMA index_info({index['name']})"
                ).fetchall()]
                if indexed_columns == ["job_id"]:
                    unique_job_index = True
                    break
            if unique_job_index:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("""CREATE TABLE publications_multi (
                    publication_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES production_jobs(job_id),
                    channel_id TEXT NOT NULL, profile_version INTEGER NOT NULL, account_username TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE, provider_request_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL, provider_video_id TEXT, provider_url TEXT,
                    receipt_json TEXT, failure_details TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    publisher_backend TEXT NOT NULL DEFAULT 'upload_post', youtube_connection_id TEXT,
                    youtube_channel_id TEXT, resumable_session_url TEXT, retry_after TEXT,
                    platform TEXT NOT NULL DEFAULT 'youtube', destination_account_id TEXT NOT NULL DEFAULT '',
                    provider_schedule_id TEXT, next_check_at TEXT,
                    UNIQUE(job_id, platform, destination_account_id)
                )""")
                conn.execute("""INSERT INTO publications_multi (
                    publication_id,job_id,channel_id,profile_version,account_username,idempotency_key,
                    provider_request_id,state,provider_video_id,provider_url,receipt_json,failure_details,
                    attempt_count,created_at,updated_at,publisher_backend,youtube_connection_id,
                    youtube_channel_id,resumable_session_url,retry_after,platform,destination_account_id,
                    provider_schedule_id,next_check_at)
                    SELECT publication_id,job_id,channel_id,profile_version,account_username,idempotency_key,
                    provider_request_id,state,provider_video_id,provider_url,receipt_json,failure_details,
                    attempt_count,created_at,updated_at,publisher_backend,youtube_connection_id,
                    youtube_channel_id,resumable_session_url,retry_after,COALESCE(platform,'youtube'),
                    COALESCE(NULLIF(destination_account_id,''),NULLIF(youtube_channel_id,''),
                             NULLIF(account_username,''),''),provider_schedule_id,next_check_at
                    FROM publications""")
                conn.execute("DROP TABLE publications")
                conn.execute("ALTER TABLE publications_multi RENAME TO publications")
                conn.commit()
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_publications_destination "
                         "ON publications(job_id, platform, destination_account_id)")
            job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(production_jobs)")}
            if "parent_job_id" not in job_columns:
                conn.execute("ALTER TABLE production_jobs ADD COLUMN parent_job_id TEXT")
            slot_columns = {row["name"] for row in conn.execute("PRAGMA table_info(autopilot_slots)")}
            if "lease_owner" not in slot_columns:
                conn.execute("ALTER TABLE autopilot_slots ADD COLUMN lease_owner TEXT")
            if "lease_expires_at" not in slot_columns:
                conn.execute("ALTER TABLE autopilot_slots ADD COLUMN lease_expires_at TEXT")
            if "visual_style" not in slot_columns:
                conn.execute("ALTER TABLE autopilot_slots ADD COLUMN visual_style TEXT")
            if "attempt_count" not in slot_columns:
                conn.execute("ALTER TABLE autopilot_slots ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
            if "recovery_of_slot_id" not in slot_columns:
                conn.execute("ALTER TABLE autopilot_slots ADD COLUMN recovery_of_slot_id TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_autopilot_slot_recovery_origin "
                         "ON autopilot_slots(recovery_of_slot_id) WHERE recovery_of_slot_id IS NOT NULL")
            runtime_columns = {row["name"] for row in conn.execute("PRAGMA table_info(channel_runtime)")}
            if "last_scheduler_tick_at" not in runtime_columns:
                conn.execute("ALTER TABLE channel_runtime ADD COLUMN last_scheduler_tick_at TEXT")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(production_jobs)")}
            if "youtube_metadata_json" not in columns:
                conn.execute(
                    "ALTER TABLE production_jobs ADD COLUMN youtube_metadata_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "worker_owner" not in columns:
                conn.execute("ALTER TABLE production_jobs ADD COLUMN worker_owner TEXT")
            if "fact_review_notes" not in columns:
                conn.execute("ALTER TABLE production_jobs ADD COLUMN fact_review_notes TEXT NOT NULL DEFAULT ''")
            if "duration_outside_requested_range" not in columns:
                conn.execute("ALTER TABLE production_jobs ADD COLUMN duration_outside_requested_range INTEGER")
            plan_columns = {row["name"] for row in conn.execute("PRAGMA table_info(content_plans)")}
            if "fact_review_notes" not in plan_columns:
                conn.execute("ALTER TABLE content_plans ADD COLUMN fact_review_notes TEXT NOT NULL DEFAULT ''")
            if "research_report_json" not in plan_columns:
                conn.execute("ALTER TABLE content_plans ADD COLUMN research_report_json TEXT NOT NULL DEFAULT '{}' ")
            if "research_snapshot_json" not in columns:
                conn.execute("ALTER TABLE production_jobs ADD COLUMN research_snapshot_json TEXT NOT NULL DEFAULT '{}' ")
            for column, definition in (
                ("visual_style", "TEXT NOT NULL DEFAULT 'auto'"),
                ("effective_visual_style", "TEXT"),
                ("visual_style_reason", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in columns:
                    conn.execute(f"ALTER TABLE production_jobs ADD COLUMN {column} {definition}")
            for column, definition in (
                ("visual_style_override", "TEXT NOT NULL DEFAULT ''"),
                ("effective_visual_style", "TEXT"),
                ("visual_style_reason", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in plan_columns:
                    conn.execute(f"ALTER TABLE content_plans ADD COLUMN {column} {definition}")

    @staticmethod
    def _plan_from_row(row: sqlite3.Row) -> ContentPlan:
        saved_profile = json.loads(row["profile_json"])
        return ContentPlan(
            plan_id=row["plan_id"], channel_id=row["channel_id"], profile_version=row["profile_version"],
            topic=row["topic"], script=row["script"],
            visual_style_override=(row["visual_style_override"] or None) if "visual_style_override" in row.keys() else None,
            effective_visual_style=row["effective_visual_style"] if "effective_visual_style" in row.keys() else None,
            visual_style_reason=row["visual_style_reason"] if "visual_style_reason" in row.keys() else "",
            duration_estimate_seconds=row["duration_estimate_seconds"],
            min_duration_seconds=row["min_duration_seconds"], max_duration_seconds=row["max_duration_seconds"],
            duration_enforcement=saved_profile.get("duration_enforcement", "flexible"),
            intended_publication_slot=row["intended_publication_slot"], state=row["state"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> ProductionJob:
        params_snapshot = json.loads(row["params_snapshot_json"])
        return ProductionJob(
            job_id=row["job_id"], plan_id=row["plan_id"], channel_id=row["channel_id"],
            parent_job_id=row["parent_job_id"] if "parent_job_id" in row.keys() else None,
            profile_version=row["profile_version"], profile_snapshot=json.loads(row["profile_snapshot_json"]),
            params_snapshot=params_snapshot, topic=row["topic"], script=row["script"],
            fact_verification_state=row["fact_verification_state"],
            visual_style=row["visual_style"] if "visual_style" in row.keys() else "auto",
            effective_visual_style=row["effective_visual_style"] if "effective_visual_style" in row.keys() else None,
            visual_style_reason=row["visual_style_reason"] if "visual_style_reason" in row.keys() else "",
            intended_publication_slot=row["intended_publication_slot"],
            target_duration_seconds=row["target_duration_seconds"],
            min_duration_seconds=row["min_duration_seconds"], max_duration_seconds=row["max_duration_seconds"],
            duration_enforcement=params_snapshot.get("duration_enforcement", "flexible"),
            duration_estimate_seconds=row["duration_estimate_seconds"],
            actual_duration_seconds=row["actual_duration_seconds"], state=row["state"],
            duration_outside_requested_range=(
                bool(row["duration_outside_requested_range"])
                if "duration_outside_requested_range" in row.keys()
                and row["duration_outside_requested_range"] is not None else None
            ),
            failure_details=row["failure_details"], attempt_count=row["attempt_count"],
            mpt_task_id=row["mpt_task_id"], worker_owner=row["worker_owner"],
            output_artifacts=json.loads(row["output_artifacts_json"]),
            youtube_metadata=(json.loads(row["youtube_metadata_json"])
                              if "youtube_metadata_json" in row.keys() else {}),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def _assert_unique_content(self, conn, channel_id: str, topic: str, script: str,
                               cross_channel: bool, ignore_plan_id: str | None = None):
        if cross_channel:
            rows = conn.execute(
                "SELECT plan_id, channel_id, topic, script FROM content_plans ORDER BY created_at DESC LIMIT ?",
                (_HISTORY_SCAN_LIMIT * 2,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT plan_id, channel_id, topic, script FROM content_plans WHERE channel_id=? "
                "ORDER BY created_at DESC LIMIT ?", (channel_id, _HISTORY_SCAN_LIMIT),
            ).fetchall()
        topic_norm, script_norm = normalize_content(topic), normalize_content(script)
        for row in rows:
            if row["plan_id"] == ignore_plan_id:
                continue
            same_channel = row["channel_id"] == channel_id
            if not same_channel and not cross_channel:
                continue
            old_topic = normalize_content(row["topic"])
            old_script = normalize_content(row["script"])
            if old_topic == topic_norm or (topic_norm and SequenceMatcher(None, topic_norm, old_topic).ratio() >= _NEAR_DUPLICATE_TOPIC):
                raise DuplicateContent("topic duplicates or closely matches existing channel history")
            if old_script == script_norm or (script_norm and _script_similarity(script, row["script"]) >= _NEAR_DUPLICATE_SCRIPT):
                raise DuplicateContent("script duplicates or closely matches existing channel history")

    def assert_topic_available(self, channel_id: str, topic: str, cross_channel: bool,
                               ignore_plan_id: str | None = None):
        with self._connection() as conn:
            if cross_channel:
                rows = conn.execute(
                    "SELECT plan_id, channel_id, topic FROM content_plans ORDER BY created_at DESC LIMIT ?",
                    (_HISTORY_SCAN_LIMIT * 2,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT plan_id, channel_id, topic FROM content_plans WHERE channel_id=? "
                    "ORDER BY created_at DESC LIMIT ?", (channel_id, _HISTORY_SCAN_LIMIT),
                ).fetchall()
        normalized = normalize_content(topic)
        for row in rows:
            if row["plan_id"] == ignore_plan_id:
                continue
            if row["channel_id"] != channel_id and not cross_channel:
                continue
            old = normalize_content(row["topic"])
            if old == normalized or (normalized and SequenceMatcher(None, normalized, old).ratio() >= _NEAR_DUPLICATE_TOPIC):
                raise DuplicateContent("topic duplicates or closely matches existing channel history")

    def create_plan(self, plan: ContentPlanCreate, profile: ChannelProfile, script: str,
                    source_job_id: str | None = None) -> ContentPlan:
        now = _now()
        minimum, maximum = _duration_bounds(profile, plan)
        estimate = estimate_narration_duration(script, profile.language, profile.voice_rate)
        duration_enforcement = plan.duration_enforcement or profile.duration_enforcement
        plan_profile = profile.model_copy(update={"duration_enforcement": duration_enforcement})
        requested_style = plan.visual_style_override or profile.visual_style
        resolved_style = resolve_generation_settings(
            task_override={"visual_style": requested_style, "video_subject": plan.topic,
                           "video_script": script, "video_source": profile.video_source,
                           "asmr_categories": profile.asmr_categories},
            channel_profile=plan_profile, topic=plan.topic,
        )
        effective_style = resolved_style.effective_visual_style
        style_reason = resolved_style.visual_style_reason
        plan_id = str(uuid4())
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                source = None
                if source_job_id:
                    source = conn.execute(
                        "SELECT plan_id,channel_id FROM production_jobs WHERE job_id=?",
                        (source_job_id,),
                    ).fetchone()
                    if source is None or source["channel_id"] != profile.channel_id:
                        raise LookupError("source video not found for this channel")
                self._assert_unique_content(conn, profile.channel_id, plan.topic, script,
                                            plan.check_cross_channel_duplicates,
                                            ignore_plan_id=source["plan_id"] if source else None)
                topic_fingerprint = fingerprint(plan.topic)
                script_fingerprint = fingerprint(script)
                if source:
                    # The database's per-channel uniqueness indexes still protect normal plans;
                    # an explicit version gets its own immutable fingerprints while preserving
                    # the visible topic and narration text.
                    topic_fingerprint = fingerprint(f"{plan.topic} version {plan_id}")
                    script_fingerprint = fingerprint(f"{script} version {plan_id}")
                conn.execute("""INSERT INTO content_plans (
                    plan_id,channel_id,profile_version,profile_json,topic,topic_fingerprint,script,script_fingerprint,
                    source_references_json,research_report_json,fact_verification_state,duration_estimate_seconds,min_duration_seconds,
                    max_duration_seconds,intended_publication_slot,state,cross_channel_check,visual_style_override,
                    effective_visual_style,visual_style_reason,created_at,updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?)""",
                    (plan_id, profile.channel_id, profile.version, plan_profile.model_dump_json(), plan.topic,
                     topic_fingerprint, script, script_fingerprint, "[]",
                     "unverified", estimate, minimum, maximum,
                     plan.intended_publication_slot.isoformat() if plan.intended_publication_slot else None,
                     int(plan.check_cross_channel_duplicates), plan.visual_style_override or "",
                     effective_style, style_reason, now, now))
                row = conn.execute("SELECT * FROM content_plans WHERE plan_id=?", (plan_id,)).fetchone()
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise DuplicateContent("topic or script already exists for this channel") from exc
            except Exception:
                conn.rollback()
                raise
        return self._plan_from_row(row)

    def create_manual_settings_retry_plan(self, source_job_id: str,
                                          profile: ChannelProfile) -> ContentPlan:
        """Create a new immutable manual attempt for a failed, unpublished video."""
        now = _now()
        plan_id = str(uuid4())
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                job = conn.execute("SELECT * FROM production_jobs WHERE job_id=?", (source_job_id,)).fetchone()
                if job is None:
                    raise LookupError("production job not found")
                if job["state"] != "failed":
                    raise InvalidTransition("only a failed job can create a new settings-based attempt")
                old_profile = json.loads(job["profile_snapshot_json"])
                artifacts = json.loads(job["output_artifacts_json"] or "[]")
                if (job["channel_id"] != profile.channel_id or old_profile.get("publishing_policy") != "manual"
                        or job["intended_publication_slot"] or artifacts):
                    raise AutopilotError("new-settings retry is limited to failed, unpublished manual videos on the same channel")
                if conn.execute("SELECT 1 FROM publications WHERE job_id=?", (source_job_id,)).fetchone():
                    raise AutopilotError("this video has a publication record and cannot be recreated from this action")
                if (profile.voice_name == old_profile.get("voice_name")
                        and profile.stock_video_style == old_profile.get("stock_video_style")):
                    raise AutopilotError("save a different channel voice or choose a different visual style before creating a new attempt")
                source_plan = conn.execute("SELECT * FROM content_plans WHERE plan_id=? AND channel_id=?",
                                           (job["plan_id"], job["channel_id"])).fetchone()
                if source_plan is None:
                    raise LookupError("source content plan not found")
                retry_fingerprint = fingerprint(f"{job['topic']} manual settings retry {source_job_id}")
                retry_script_fingerprint = fingerprint(f"{job['script']} manual settings retry {source_job_id}")
                conn.execute("""INSERT INTO content_plans (
                    plan_id,channel_id,profile_version,profile_json,topic,topic_fingerprint,script,script_fingerprint,
                    source_references_json,research_report_json,fact_verification_state,fact_review_notes,
                    duration_estimate_seconds,min_duration_seconds,max_duration_seconds,
                    intended_publication_slot,state,cross_channel_check,created_at,updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'ready', 0, ?, ?)""",
                    (plan_id, profile.channel_id, profile.version,
                     profile.model_copy(update={"publishing_policy": "manual", "auto_public_enabled": False}).model_dump_json(),
                     job["topic"], retry_fingerprint, job["script"], retry_script_fingerprint,
                     job["source_references_json"], source_plan["research_report_json"],
                     job["fact_verification_state"], job["fact_review_notes"],
                     estimate_narration_duration(job["script"], profile.language, profile.voice_rate),
                     job["min_duration_seconds"], job["max_duration_seconds"], now, now))
                row = conn.execute("SELECT * FROM content_plans WHERE plan_id=?", (plan_id,)).fetchone()
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise DuplicateContent("a new-settings attempt was already created for this failed job") from exc
            except Exception:
                conn.rollback()
                raise
        return self._plan_from_row(row)

    def list_plans(self, channel_id: str | None = None, limit: int = 100) -> list[ContentPlan]:
        with self._connection() as conn:
            if channel_id:
                rows = conn.execute("SELECT * FROM content_plans WHERE channel_id=? ORDER BY created_at DESC LIMIT ?",
                                    (channel_id, min(max(limit, 1), 500))).fetchall()
            else:
                rows = conn.execute("SELECT * FROM content_plans ORDER BY created_at DESC LIMIT ?",
                                    (min(max(limit, 1), 500),)).fetchall()
        return [self._plan_from_row(row) for row in rows]

    def get_plan(self, plan_id: str, channel_id: str | None = None) -> ContentPlan:
        with self._connection() as conn:
            if channel_id:
                row = conn.execute("SELECT * FROM content_plans WHERE plan_id=? AND channel_id=?",
                                   (plan_id, channel_id)).fetchone()
            else:
                row = conn.execute("SELECT * FROM content_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise LookupError("content plan not found")
        return self._plan_from_row(row)

    def mark_plan_ready(self, plan_id: str, channel_id: str,
                        accept_duration_estimate: bool = False) -> ContentPlan:
        now = _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM content_plans WHERE plan_id=? AND channel_id=?",
                                   (plan_id, channel_id)).fetchone()
                if row is None:
                    raise LookupError("content plan not found")
                if row["state"] != "planned":
                    raise InvalidTransition(f"cannot ready a plan in state {row['state']}")
                estimate = row["duration_estimate_seconds"]
                enforcement = json.loads(row["profile_json"]).get("duration_enforcement", "flexible")
                outside = not row["min_duration_seconds"] <= estimate <= row["max_duration_seconds"]
                if outside and enforcement == "warn_only":
                    logger.warning(
                        "Autopilot narration estimate differs from requested duration: "
                        "plan_id={}, requested={}–{}s, estimate={:.1f}s",
                        plan_id, row["min_duration_seconds"], row["max_duration_seconds"], estimate,
                    )
                if outside and enforcement == "strict" and not accept_duration_estimate:
                    raise DurationPlanningError(
                        f"estimated narration is {estimate:.1f}s; requested range is "
                        f"{row['min_duration_seconds']:.1f}–{row['max_duration_seconds']:.1f}s; "
                        "edit the script or explicitly accept the estimate"
                    )
                conn.execute("UPDATE content_plans SET state='ready', updated_at=? WHERE plan_id=?",
                             (now, plan_id))
                row = conn.execute("SELECT * FROM content_plans WHERE plan_id=?", (plan_id,)).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._plan_from_row(row)

    def record_content_review(self, job_id: str, reviewer: str, review) -> dict:
        job = self.get_job(job_id)
        if job.state != "generated":
            raise InvalidTransition("content review is available only after video generation")
        if review.rights_status == "documented" and not review.rights_evidence:
            raise AutopilotError("rights approval requires links or records supporting every material's usage rights")
        with self._connection() as conn:
            conn.execute("INSERT INTO job_content_reviews VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET originality_status=excluded.originality_status,rights_status=excluded.rights_status,notes=excluded.notes,rights_evidence_json=excluded.rights_evidence_json,reviewer=excluded.reviewer,reviewed_at=excluded.reviewed_at",
                         (job_id, review.originality_status, review.rights_status,
                          review.notes[:2000], json.dumps([item.model_dump() for item in review.rights_evidence]),
                          reviewer[:200], _now()))
        return self.content_review(job_id)

    def content_review(self, job_id: str) -> dict:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM job_content_reviews WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return {"originality_status": "human_review_required", "rights_status": "unknown",
                    "notes": "", "rights_evidence": []}
        result = dict(row)
        result["rights_evidence"] = json.loads(result.pop("rights_evidence_json"))
        return result

    def save_visual_selection(self, job_id: str, selection: dict) -> None:
        mode = selection.get("effective_mode")
        reason = str(selection.get("reason") or "")[:2000]
        if mode not in {"savage_animated_explainer", "asmr_cutting", "relevant_stock"}:
            return
        with self._connection() as conn:
            row = conn.execute(
                "SELECT plan_id,params_snapshot_json FROM production_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                return
            params = json.loads(row["params_snapshot_json"] or "{}")
            params["effective_visual_style"] = mode
            params["visual_style_reason"] = reason
            conn.execute(
                "UPDATE production_jobs SET effective_visual_style=?,visual_style_reason=?,params_snapshot_json=?,updated_at=? WHERE job_id=?",
                (mode, reason, json.dumps(params), _now(), job_id),
            )
            conn.execute(
                "UPDATE content_plans SET effective_visual_style=?,visual_style_reason=?,updated_at=? WHERE plan_id=?",
                (mode, reason, _now(), row["plan_id"]),
            )

    def save_fact_verification(self, job_id: str, task_state: dict) -> None:
        state = str(task_state.get("fact_verification_state") or "unverified")
        allowed = {"unverified", "source_verified", "unsupported", "contradicted",
                   "unavailable", "verification_error"}
        if state not in allowed:
            state = "unavailable"
        report = task_state.get("fact_verification_report") or {}
        script_hash = str(task_state.get("fact_verification_script_hash") or "")
        display_status = {
            "source_verified": "verified", "unsupported": "unverified",
            "contradicted": "contradicted", "unavailable": "unavailable",
            "verification_error": "unavailable", "unverified": "unverified",
        }.get(state, "unavailable")
        with self._connection() as conn:
            row = conn.execute(
                "SELECT params_snapshot_json FROM production_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                return
            params = json.loads(row["params_snapshot_json"] or "{}")
            policy = task_state.get("fact_verification_policy")
            params.update({
                "fact_verification_state": state,
                "fact_verification_status": display_status,
                "fact_verification_script_hash": script_hash,
                "fact_verification_report": report,
                "fact_research_timeout_seconds": params.get("fact_research_timeout_seconds")
                or config.app.get("fact_research_timeout_seconds", 8),
            })
            if policy in {"off", "advisory", "strict"}:
                params["fact_verification_policy"] = policy
            conn.execute(
                "UPDATE production_jobs SET fact_verification_state=?,research_snapshot_json=?,params_snapshot_json=?,updated_at=? WHERE job_id=?",
                (state, json.dumps({"state": state, "status": display_status,
                                    "script_hash": script_hash,
                                    "report": report}, ensure_ascii=False),
                 json.dumps(params, ensure_ascii=False), _now(), job_id),
            )

    def create_job(self, plan_id: str, channel_id: str, profile_store: ChannelProfileStore,
                   parent_job_id: str | None = None,
                   video_terms: str | list[str] | None = None) -> ProductionJob:
        now = _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                plan = conn.execute("SELECT * FROM content_plans WHERE plan_id=? AND channel_id=?",
                                    (plan_id, channel_id)).fetchone()
                if plan is None:
                    raise LookupError("content plan not found")
                if parent_job_id:
                    parent = conn.execute(
                        "SELECT channel_id FROM production_jobs WHERE job_id=?", (parent_job_id,)
                    ).fetchone()
                    if parent is None or parent["channel_id"] != channel_id:
                        raise LookupError("source video not found for this channel")
                if plan["state"] != "ready":
                    raise InvalidTransition("content plan must be ready before a production job is created")
                # A manual video's choices are captured with its plan. Scheduled plans
                # contain the ordinary channel profile at the same version.
                profile = ChannelProfile.model_validate_json(plan["profile_json"])
                if profile.channel_id != channel_id or profile.version != plan["profile_version"]:
                    raise AutopilotError("content plan profile does not match its channel")
                from app.services.channel_profiles import build_preview, resolve_profile_voice
                profile = profile.model_copy(update={"voice_name": resolve_profile_voice(profile)})
                resolved = build_preview(profile, {
                    "video_subject": plan["topic"], "video_script": plan["script"],
                    "visual_style": plan["visual_style_override"] or profile.visual_style,
                    "min_duration_seconds": plan["min_duration_seconds"],
                    "max_duration_seconds": plan["max_duration_seconds"],
                    "effective_visual_style": plan["effective_visual_style"],
                    "visual_style_reason": plan["visual_style_reason"],
                    **({"video_terms": video_terms} if video_terms else {}),
                }).params
                job_id = str(uuid4())
                conn.execute("""INSERT INTO production_jobs (
                    job_id, plan_id, parent_job_id, channel_id, profile_version, profile_snapshot_json, params_snapshot_json,
                    topic, script, source_references_json, research_snapshot_json, fact_verification_state, intended_publication_slot,
                    fact_review_notes, visual_style, effective_visual_style, visual_style_reason,
                    target_duration_seconds, min_duration_seconds, max_duration_seconds,
                    duration_estimate_seconds, state, attempt_count, output_artifacts_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', 0, '[]', ?, ?)""",
                    (job_id, plan_id, parent_job_id, channel_id, plan["profile_version"], profile.model_dump_json(),
                     json.dumps(resolved.model_dump(mode="json", warnings=False)),
                     plan["topic"], plan["script"], plan["source_references_json"], plan["research_report_json"],
                     plan["fact_verification_state"], plan["intended_publication_slot"], plan["fact_review_notes"],
                     plan["visual_style_override"] or profile.visual_style,
                     resolved.effective_visual_style, resolved.visual_style_reason,
                     profile.target_duration_seconds, plan["min_duration_seconds"], plan["max_duration_seconds"],
                     plan["duration_estimate_seconds"], now, now))
                conn.execute("UPDATE content_plans SET state='job_created', updated_at=? WHERE plan_id=?",
                             (now, plan_id))
                row = conn.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise InvalidTransition("a production job already exists for this plan") from exc
            except Exception:
                conn.rollback()
                raise
        return self._job_from_row(row)

    def list_jobs(self, channel_id: str | None = None, limit: int = 100) -> list[ProductionJob]:
        with self._connection() as conn:
            if channel_id:
                rows = conn.execute("SELECT * FROM production_jobs WHERE channel_id=? ORDER BY created_at DESC LIMIT ?",
                                    (channel_id, min(max(limit, 1), 500))).fetchall()
            else:
                rows = conn.execute("SELECT * FROM production_jobs ORDER BY created_at DESC LIMIT ?",
                                    (min(max(limit, 1), 500),)).fetchall()
        return [self._job_from_row(row) for row in rows]

    def get_job(self, job_id: str) -> ProductionJob:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise LookupError("production job not found")
        return self._job_from_row(row)

    def save_youtube_metadata(self, job_id: str, metadata: dict) -> None:
        serialized = json.dumps(metadata, ensure_ascii=False)
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE production_jobs SET youtube_metadata_json=? WHERE job_id=?",
                (serialized, job_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("production job not found")

    def delete_job(self, job_id: str, channel_id: str) -> dict:
        """Permanently remove one terminal local job and its task-owned artifacts."""
        task_id = None
        shared_task_id = False
        shared_task_files = False
        safe_task_id = False
        task_dir = None
        shared_artifacts: set[str] = set()
        artifact_paths: list[Path] = []
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
                if row is None:
                    raise LookupError("production job not found")
                if row["channel_id"] != channel_id:
                    raise AutopilotError("job does not belong to the selected channel")
                if row["state"] not in {"failed", "generated", "cancelled"}:
                    raise InvalidTransition("only completed, failed, or cancelled jobs can be permanently deleted")
                publications = conn.execute(
                    "SELECT state FROM publications WHERE job_id=?", (job_id,)
                ).fetchall()
                if any(item["state"] in {"uploading", "unknown"} for item in publications):
                    raise AutopilotError(
                        "an upload is still in progress or needs reconciliation; resolve it before deleting local tracking"
                    )
                job = self._job_from_row(row)
                task_id = job.mpt_task_id
                other_jobs = conn.execute(
                    "SELECT mpt_task_id,output_artifacts_json FROM production_jobs WHERE job_id<>?",
                    (job_id,),
                ).fetchall()
                shared_task_id = bool(task_id and any(
                    other["mpt_task_id"] == task_id for other in other_jobs
                ))
                other_artifacts = set()
                for other in other_jobs:
                    try:
                        other_artifacts.update(
                            str(Path(item).resolve(strict=False))
                            for item in json.loads(other["output_artifacts_json"] or "[]")
                        )
                    except (OSError, ValueError, TypeError):
                        continue
                artifact_paths = [Path(item) for item in job.output_artifacts]
                shared_artifacts = {
                    str(item.resolve(strict=False)) for item in artifact_paths
                    if str(item.resolve(strict=False)) in other_artifacts
                }
                if task_id and not shared_task_id:
                    tasks_root = Path(utils.storage_dir("tasks")).resolve()
                    candidate = tasks_root / task_id
                    if (re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id)
                            and not candidate.is_symlink()
                            and candidate.resolve(strict=False).parent == tasks_root):
                        safe_task_id = True
                        candidate_resolved = candidate.resolve(strict=False)
                        shared_task_files = any(
                            Path(path).resolve(strict=False).is_relative_to(candidate_resolved)
                            for path in other_artifacts
                        )
                        if not shared_task_files:
                            task_dir = candidate

                # Keep scheduler slot uniqueness/audit history, but detach its
                # local job reference so deleting the job cannot resubmit it.
                conn.execute(
                    "UPDATE autopilot_slots SET job_id=NULL,details=? WHERE job_id=?",
                    ("Local video deleted by user; remote publication is unchanged.", job_id),
                )
                conn.execute("UPDATE production_jobs SET parent_job_id=NULL WHERE parent_job_id=?", (job_id,))
                # These are local receipts only; no remote delete request is made.
                conn.execute("DELETE FROM publications WHERE job_id=?", (job_id,))
                for table in ("job_qa", "job_fact_reviews", "job_content_reviews"):
                    conn.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
                conn.execute("DELETE FROM production_jobs WHERE job_id=? AND channel_id=?", (job_id, channel_id))
                conn.execute("DELETE FROM content_plans WHERE plan_id=?", (row["plan_id"],))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        cleanup_errors = []
        if task_id and not shared_task_id:
            try:
                if task_dir and task_dir.exists():
                    shutil.rmtree(task_dir)
                elif not safe_task_id:
                    cleanup_errors.append(f"task {task_id}: unsafe local path; files retained")
                if safe_task_id and hasattr(sm.state, "delete_task"):
                    sm.state.delete_task(task_id)
            except OSError as exc:
                cleanup_errors.append(f"task {task_id}: {type(exc).__name__}")
                logger.warning("local task cleanup failed after permanent deletion: task_id={}, error={}",
                               task_id, type(exc).__name__)
        elif task_dir is None and (shared_task_id or shared_task_files):
            cleanup_errors.append("task files are shared with another video version and were retained")
        storage_root = Path(utils.storage_dir()).resolve()
        for artifact in artifact_paths:
            resolved = artifact.resolve(strict=False)
            if str(resolved) in shared_artifacts or (task_dir and resolved.is_relative_to(task_dir.resolve())):
                continue
            if artifact.is_symlink() or not resolved.is_relative_to(storage_root) or not artifact.is_file():
                continue
            try:
                artifact.unlink()
            except OSError as exc:
                cleanup_errors.append(f"artifact {artifact.name}: {type(exc).__name__}")
                logger.warning("local artifact cleanup failed after permanent deletion: path={}, error={}",
                               artifact.name, type(exc).__name__)
        return {"job_id": job_id, "deleted": True, "cleanup_errors": cleanup_errors}

    def list_slots(self, channel_id: str, limit: int = 200) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM autopilot_slots WHERE channel_id=? ORDER BY slot_utc DESC LIMIT ?",
                                 (channel_id, min(max(limit, 1), 1000))).fetchall()
        return [dict(row) for row in rows]

    def last_scheduler_tick(self, channel_id: str) -> datetime | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT last_scheduler_tick_at FROM channel_runtime WHERE channel_id=?",
                (channel_id,),
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def set_scheduler_tick(self, channel_id: str, now: datetime) -> None:
        timestamp = now.astimezone(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO channel_runtime (channel_id,paused,reason,updated_at,last_scheduler_tick_at) "
                "VALUES (?,0,NULL,?,?) ON CONFLICT(channel_id) DO UPDATE SET "
                "last_scheduler_tick_at=CASE WHEN channel_runtime.last_scheduler_tick_at IS NULL "
                "OR channel_runtime.last_scheduler_tick_at<excluded.last_scheduler_tick_at "
                "THEN excluded.last_scheduler_tick_at ELSE channel_runtime.last_scheduler_tick_at END, "
                "updated_at=excluded.updated_at",
                (channel_id, timestamp, timestamp),
            )

    def pending_missed_slots(self, channel_id: str) -> list[dict]:
        """Return missed publication slots that have not been durably rescheduled."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT missed.* FROM autopilot_slots AS missed "
                "WHERE missed.channel_id=? AND missed.state='skipped' "
                "AND missed.details LIKE 'missed during downtime%' "
                "AND NOT EXISTS (SELECT 1 FROM autopilot_slots AS recovery "
                "WHERE recovery.recovery_of_slot_id=missed.slot_id) "
                "ORDER BY missed.slot_utc",
                (channel_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recovery_origin(self, recovery_slot_id: str) -> str | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT recovery_of_slot_id FROM autopilot_slots WHERE slot_id=?",
                (recovery_slot_id,),
            ).fetchone()
        return row[0] if row else None

    def missed_slot_has_publication_attempt(self, missed_slot_id: str, channel_id: str) -> bool:
        """Do not create replacement content while the original upload may exist."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM content_plans p JOIN production_jobs j ON j.plan_id=p.plan_id "
                "JOIN publications pub ON pub.job_id=j.job_id "
                "WHERE p.channel_id=? AND p.intended_publication_slot=("
                "SELECT slot_utc FROM autopilot_slots WHERE slot_id=? AND channel_id=?) LIMIT 1",
                (channel_id, missed_slot_id, channel_id),
            ).fetchone()
        return row is not None

    def claim_recovery_slot(self, missed_slot_id: str, channel_id: str,
                            recovery_slot_id: str, recovery_slot_utc: str,
                            owner: str, style_rotation: list[str] | None = None) -> bool:
        """Atomically reserve a future schedule slot for one missed slot."""
        now = datetime.now(timezone.utc)
        expiry = (now + timedelta(hours=1)).isoformat()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                missed = conn.execute(
                    "SELECT state,slot_utc FROM autopilot_slots WHERE slot_id=? AND channel_id=?",
                    (missed_slot_id, channel_id),
                ).fetchone()
                linked = conn.execute(
                    "SELECT 1 FROM autopilot_slots WHERE recovery_of_slot_id=?",
                    (missed_slot_id,),
                ).fetchone()
                occupied = conn.execute(
                    "SELECT 1 FROM autopilot_slots WHERE channel_id=? AND slot_utc=?",
                    (channel_id, recovery_slot_utc),
                ).fetchone()
                planned = conn.execute(
                    "SELECT 1 FROM content_plans WHERE channel_id=? AND intended_publication_slot=? "
                    "UNION ALL SELECT 1 FROM production_jobs WHERE channel_id=? AND intended_publication_slot=? LIMIT 1",
                    (channel_id, recovery_slot_utc, channel_id, recovery_slot_utc),
                ).fetchone()
                if not missed or missed["state"] != "skipped" or linked or occupied or planned:
                    conn.rollback()
                    return False
                old_plan = conn.execute(
                    "SELECT plan_id FROM content_plans WHERE channel_id=? AND intended_publication_slot=? LIMIT 1",
                    (channel_id, missed["slot_utc"]),
                ).fetchone()
                if old_plan:
                    old_job = conn.execute(
                        "SELECT job_id,state,output_artifacts_json FROM production_jobs WHERE plan_id=?",
                        (old_plan["plan_id"],),
                    ).fetchone()
                    if old_job:
                        if old_job["state"] == "generated":
                            qa = conn.execute(
                                "SELECT state FROM job_qa WHERE job_id=?", (old_job["job_id"],),
                            ).fetchone()
                            try:
                                artifacts = json.loads(old_job["output_artifacts_json"])
                            except (TypeError, json.JSONDecodeError):
                                artifacts = []
                            if (not qa or qa["state"] != "qa_passed" or not artifacts
                                    or not all(isinstance(path, str) and os.path.isfile(path) for path in artifacts)):
                                conn.rollback()
                                return False
                        elif old_job["state"] != "ready":
                            # Let queued/active or failed work finish its own safe
                            # recovery before allocating replacement production.
                            conn.rollback()
                            return False
                        if conn.execute(
                            "SELECT 1 FROM publications WHERE job_id=?", (old_job["job_id"],),
                        ).fetchone():
                            conn.rollback()
                            return False
                        conn.execute(
                            "UPDATE production_jobs SET intended_publication_slot=?,updated_at=? WHERE job_id=?",
                            (recovery_slot_utc, now.isoformat(), old_job["job_id"]),
                        )
                    conn.execute(
                        "UPDATE content_plans SET intended_publication_slot=?,updated_at=? WHERE plan_id=?",
                        (recovery_slot_utc, now.isoformat(), old_plan["plan_id"]),
                    )
                rotation = list(dict.fromkeys(style_rotation or []))
                assigned_style = None
                if rotation:
                    assigned = conn.execute(
                        "SELECT COUNT(*) FROM autopilot_slots WHERE channel_id=? AND visual_style IS NOT NULL",
                        (channel_id,),
                    ).fetchone()[0]
                    assigned_style = rotation[assigned % len(rotation)]
                conn.execute(
                    "INSERT INTO autopilot_slots (slot_id,channel_id,slot_utc,state,job_id,details,created_at,"
                    "lease_owner,lease_expires_at,visual_style,attempt_count,recovery_of_slot_id) "
                    "VALUES (?, ?, ?, 'planning', NULL, NULL, ?, ?, ?, ?, 1, ?)",
                    (recovery_slot_id, channel_id, recovery_slot_utc, now.isoformat(), owner,
                     expiry, assigned_style, missed_slot_id),
                )
                conn.execute(
                    "UPDATE autopilot_slots SET details=? WHERE slot_id=? AND state='skipped'",
                    (f"missed during downtime; rescheduled to {recovery_slot_utc}", missed_slot_id),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
            except Exception:
                conn.rollback()
                raise

    def reuse_completed_missed_job(self, missed_slot_id: str, channel_id: str,
                                   recovery_slot_utc: str) -> str | None:
        """Move a completed, QA-passed job only when no publication was attempted."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                missed = conn.execute(
                    "SELECT slot_utc FROM autopilot_slots WHERE slot_id=? AND channel_id=? AND state='skipped'",
                    (missed_slot_id, channel_id),
                ).fetchone()
                if not missed:
                    conn.rollback()
                    return None
                row = conn.execute(
                    "SELECT j.job_id,j.state,j.output_artifacts_json,q.state AS qa_state "
                    "FROM content_plans p JOIN production_jobs j ON j.plan_id=p.plan_id "
                    "JOIN job_qa q ON q.job_id=j.job_id "
                    "WHERE p.channel_id=? AND p.intended_publication_slot IN (?,?) LIMIT 1",
                    (channel_id, missed["slot_utc"], recovery_slot_utc),
                ).fetchone()
                if (not row or row["state"] != "generated" or row["qa_state"] != "qa_passed"
                        or conn.execute("SELECT 1 FROM publications WHERE job_id=?", (row["job_id"],)).fetchone()):
                    conn.rollback()
                    return None
                try:
                    artifacts = json.loads(row["output_artifacts_json"])
                except (TypeError, json.JSONDecodeError):
                    artifacts = []
                if not artifacts or not all(isinstance(path, str) and os.path.isfile(path) for path in artifacts):
                    conn.rollback()
                    return None
                conn.execute(
                    "UPDATE content_plans SET intended_publication_slot=?,updated_at=? "
                    "WHERE plan_id=(SELECT plan_id FROM production_jobs WHERE job_id=?)",
                    (recovery_slot_utc, _now(), row["job_id"]),
                )
                conn.execute(
                    "UPDATE production_jobs SET intended_publication_slot=?,updated_at=? "
                    "WHERE job_id=? AND state='generated'",
                    (recovery_slot_utc, _now(), row["job_id"]),
                )
                conn.commit()
                return row["job_id"]
            except Exception:
                conn.rollback()
                raise

    def list_publications(self, channel_id: str, limit: int = 200) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM publications WHERE channel_id=? ORDER BY created_at DESC LIMIT ?",
                                 (channel_id, min(max(limit, 1), 1000))).fetchall()
        return [dict(row) for row in rows]

    def claim_slot(self, slot_id: str, channel_id: str, slot_utc: str, owner: str,
                   style_rotation: list[str] | None = None, retry_limit: int = 2) -> bool:
        now = datetime.now(timezone.utc)
        expiry = (now + timedelta(hours=1)).isoformat()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT state,lease_expires_at,details,attempt_count FROM autopilot_slots WHERE slot_id=?", (slot_id,)).fetchone()
                if row is None:
                    rotation = list(dict.fromkeys(style_rotation or []))
                    assigned_style = None
                    if rotation:
                        assigned = conn.execute(
                            "SELECT COUNT(*) FROM autopilot_slots WHERE channel_id=? AND visual_style IS NOT NULL",
                            (channel_id,),
                        ).fetchone()[0]
                        assigned_style = rotation[assigned % len(rotation)]
                    conn.execute("INSERT INTO autopilot_slots (slot_id,channel_id,slot_utc,state,job_id,details,created_at,lease_owner,lease_expires_at,visual_style,attempt_count) VALUES (?, ?, ?, 'planning', NULL, NULL, ?, ?, ?, ?, 1)",
                                 (slot_id, channel_id, slot_utc, now.isoformat(), owner, expiry, assigned_style))
                    conn.commit()
                    return True
                retrying_failure = row["state"] == "failed"
                if retrying_failure and (
                    row["attempt_count"] > retry_limit
                    or not _transient_generation_failure(row["details"])
                    or (row["lease_expires_at"] and row["lease_expires_at"] > now.isoformat())
                ):
                    conn.commit()
                    return False
                if row["state"] not in {"planning", "failed"}:
                    conn.commit()
                    return False
                if (not retrying_failure and row["lease_expires_at"]
                        and row["lease_expires_at"] > now.isoformat()):
                    conn.commit()
                    return False
                if not retrying_failure and row["attempt_count"] > retry_limit:
                    conn.commit()
                    return False
                conn.execute("UPDATE autopilot_slots SET state='planning',lease_owner=?,lease_expires_at=?,details=NULL,attempt_count=attempt_count+1 WHERE slot_id=?",
                             (owner, expiry, slot_id))
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def slot_visual_style(self, slot_id: str) -> str | None:
        with self._connection() as conn:
            row = conn.execute("SELECT visual_style FROM autopilot_slots WHERE slot_id=?", (slot_id,)).fetchone()
        return row[0] if row else None

    def buffer_counts(self, channel_id: str, now: datetime | None = None,
                      target: int = 6) -> dict:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._connection() as conn:
            counts = conn.execute("""SELECT
                SUM(CASE WHEN j.state IN ('ready','queued','generating') THEN 1 ELSE 0 END) AS in_progress,
                SUM(CASE WHEN j.state='generated' AND q.state='qa_passed' THEN 1 ELSE 0 END) AS validated
                FROM production_jobs j LEFT JOIN job_qa q ON q.job_id=j.job_id
                WHERE j.channel_id=? AND j.intended_publication_slot>?""",
                (channel_id, now.isoformat())).fetchone()
        return {"in_progress": int(counts["in_progress"] or 0),
                "validated": int(counts["validated"] or 0),
                "target": target,
                "remaining": max(0, target
                                 - int(counts["in_progress"] or 0)
                                 - int(counts["validated"] or 0))}

    def finish_slot(self, slot_id: str, state: str, job_id: str | None = None,
                    details: str | None = None, retry_delay_seconds: int | None = None):
        retry_at = ((datetime.now(timezone.utc) + timedelta(seconds=retry_delay_seconds)).isoformat()
                    if state == "failed" and retry_delay_seconds is not None else None)
        with self._connection() as conn:
            conn.execute("UPDATE autopilot_slots SET state=?,job_id=?,details=?,lease_owner=NULL,lease_expires_at=? WHERE slot_id=?",
                         (state, job_id, details, retry_at, slot_id))

    def slot_attempt_count(self, slot_id: str) -> int:
        with self._connection() as conn:
            row = conn.execute("SELECT attempt_count FROM autopilot_slots WHERE slot_id=?", (slot_id,)).fetchone()
        return int(row[0]) if row else 0

    def transition(self, job_id: str, to_state: str, **updates) -> ProductionJob:
        now = _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
                if row is None:
                    raise LookupError("production job not found")
                old_state = row["state"]
                if to_state not in _ALLOWED_TRANSITIONS.get(old_state, set()):
                    raise InvalidTransition(f"cannot transition production job from {old_state} to {to_state}")
                assignments = ["state=?", "updated_at=?"]
                values: list[Any] = [to_state, now]
                allowed = {
                    "failure_details", "mpt_task_id", "attempt_count", "actual_duration_seconds",
                    "output_artifacts_json", "worker_owner", "duration_outside_requested_range",
                }
                if set(updates) - allowed:
                    raise ValueError("unsupported production job update")
                for key, value in updates.items():
                    assignments.append(f"{key}=?")
                    values.append(value)
                values.append(job_id)
                if old_state == "generating" and to_state in {"generated", "failed"} and "worker_owner" not in updates:
                    assignments.append("worker_owner=NULL")
                conn.execute(f"UPDATE production_jobs SET {', '.join(assignments)} WHERE job_id=?", values)
                row = conn.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._job_from_row(row)

    def queue(self, job_id: str) -> ProductionJob:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
                if row is None:
                    raise LookupError("production job not found")
                if row["state"] == "queued":
                    conn.commit()
                    return self._job_from_row(row)
                if row["state"] != "ready":
                    raise InvalidTransition(f"cannot start production job in state {row['state']}")
                limit = max(0, int(config.app.get("max_queued_tasks", 100)))
                queued = conn.execute("SELECT COUNT(*) FROM production_jobs WHERE state='queued'").fetchone()[0]
                if queued >= limit:
                    raise ProductionQueueFull("Autopilot production queue is full")
                conn.execute("UPDATE production_jobs SET state='queued', failure_details=NULL, updated_at=? WHERE job_id=?",
                             (_now(), job_id))
                row = conn.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._job_from_row(row)

    def claim(self, job_id: str, task_id: str, worker_owner: str) -> ProductionJob | None:
        try:
            job = self.get_job(job_id)
            return self.transition(job_id, "generating", mpt_task_id=task_id,
                                   worker_owner=worker_owner, attempt_count=job.attempt_count + 1,
                                   failure_details=None) if job.state == "queued" else None
        except InvalidTransition:
            return None

    @staticmethod
    def _worker_alive(owner: str | None) -> bool | None:
        if not owner:
            return False
        host, separator, raw_pid = owner.rpartition(":")
        if not separator or host != socket.gethostname():
            return None
        try:
            os.kill(int(raw_pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except (OSError, ValueError):
            return False
        return True

    def recover_interrupted_jobs(self) -> list[str]:
        """Mark dead local workers failed; never silently repeat an external generation call."""
        with self._connection() as conn:
            rows = conn.execute("SELECT job_id, worker_owner FROM production_jobs WHERE state='generating'").fetchall()
        recovered = []
        for row in rows:
            if self._worker_alive(row["worker_owner"]) is False:
                try:
                    self.transition(
                        row["job_id"], "failed",
                        failure_details="generation worker stopped before completion; inspect task artifacts before retry",
                    )
                    recovered.append(row["job_id"])
                except InvalidTransition:
                    pass
        return recovered

    def recover_job(self, job_id: str) -> ProductionJob:
        job = self.get_job(job_id)
        if job.state != "generating":
            raise InvalidTransition("only a generating job can be checked for interruption")
        alive = self._worker_alive(job.worker_owner)
        if alive is not False:
            raise InvalidTransition("worker is active or its host cannot be checked")
        return self.transition(
            job_id, "failed",
            failure_details="generation worker stopped before completion; inspect task artifacts before retry",
        )


def probe_video_duration(path: str) -> float:
    clip = video._open_video_clip_quietly(path, audio=False)
    try:
        return float(clip.duration)
    finally:
        clip.close()


class AutopilotService:
    def __init__(self, database_path: str | Path | None = None, runner=None):
        self.store = AutopilotStore(database_path)
        self.store.recover_interrupted_jobs()
        self.profile_store = ChannelProfileStore(database_path or self.store.database_path)
        self.youtube_connections = youtube_direct.YouTubeConnectionStore(self.store.database_path)
        self.youtube_publisher = youtube_direct.YouTubeDirectPublisher(self.youtube_connections)
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(config.app.get("max_concurrent_tasks", 2))),
            thread_name_prefix="mpt-autopilot",
        )
        self._runner = runner or self._run_job
        self._scheduler_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4()}"
        self._submit_lock = threading.Lock()
        self._active_submissions: set[str] = set()
        self.reconcile_publications()
        self._scheduler_stop = threading.Event()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, name="mpt-autopilot-scheduler", daemon=True)
        self._scheduler_thread.start()
        for pending in self.store.list_jobs(limit=500):
            if pending.state == "queued":
                with self._submit_lock:
                    if pending.job_id not in self._active_submissions:
                        self._active_submissions.add(pending.job_id)
                        self._executor.submit(self._run_scheduled_job, pending.job_id)

    def close(self, wait: bool = True) -> None:
        """Stop the background scheduler and drain worker threads on shutdown."""
        self._scheduler_stop.set()
        if self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=5)
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def reconcile_publications(self):
        """Status-only startup recovery; uncertain uploads are never resubmitted here."""
        self._reconcile_direct_publications()
        api_key = config.app.get("upload_post_api_key", "")
        if not api_key:
            return
        with self.store._connection() as conn:
            rows = conn.execute("SELECT * FROM publications WHERE (state='uploading' AND (next_check_at IS NULL OR next_check_at<=?)) OR (state IN ('unknown','scheduled') AND next_check_at<=?)",
                                (_now(), _now())).fetchall()
        for row in rows:
            if not row["account_username"]:
                continue
            publisher = UploadPostService({"upload_post_api_key": api_key,
                                           "upload_post_username": row["account_username"],
                                           "upload_post_enabled": True})
            platform = row["platform"] or "youtube"
            if row["state"] == "scheduled":
                history = publisher.check_history(row["job_id"], row["account_username"], platform)
                records = history.get("history") or []
                match = next((item for item in records if isinstance(item, dict)
                              and item.get("external_id") == row["job_id"]
                              and item.get("platform") == platform), None)
                if match and match.get("success") is True:
                    video_id = match.get("platform_post_id")
                    if isinstance(video_id, list):
                        video_id = json.dumps(video_id)
                    with self.store._connection() as conn:
                        conn.execute("UPDATE publications SET state='published',provider_video_id=?,provider_url=?,receipt_json=?,failure_details=NULL,next_check_at=NULL,updated_at=? WHERE publication_id=?",
                                     (video_id, match.get("post_url"), _safe_receipt(match), _now(), row["publication_id"]))
                elif match and match.get("success") is False:
                    with self.store._connection() as conn:
                        conn.execute("UPDATE publications SET state='failed',receipt_json=?,failure_details=?,next_check_at=NULL,updated_at=? WHERE publication_id=?",
                                     (_safe_receipt(match), _safe_failure(match.get("error_message") or "Upload-Post history reports failure"), _now(), row["publication_id"]))
                else:
                    with self.store._connection() as conn:
                        conn.execute("UPDATE publications SET next_check_at=?,updated_at=? WHERE publication_id=?",
                                     ((datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), _now(), row["publication_id"]))
                continue
            status = publisher.check_status(row["provider_request_id"])
            remote_status = status.get("status")
            if remote_status == "completed":
                job = self.store.get_job(row["job_id"])
                state = "scheduled" if job.profile_snapshot.get("publishing_policy") == "scheduled_public" else "published"
                next_check = (job.intended_publication_slot.astimezone(timezone.utc) + timedelta(minutes=5)).isoformat() if state == "scheduled" and job.intended_publication_slot else None
                platform_result = _platform_receipt_result(status.get("results"), platform)
                if platform_result.get("success") is True:
                    remote_id = (platform_result.get("video_id") or platform_result.get("video_reel_id")
                                 or platform_result.get("publish_id") or platform_result.get("post_id")
                                 or platform_result.get("id"))
                    remote_url = platform_result.get("url") or platform_result.get("post_url")
                    with self.store._connection() as conn:
                        conn.execute("UPDATE publications SET state=?,provider_schedule_id=?,provider_video_id=?,provider_url=?,receipt_json=?,failure_details=NULL,next_check_at=?,updated_at=? WHERE publication_id=?",
                                     (state, status.get("job_id") or status.get("schedule_id"),
                                      remote_id, remote_url, _safe_receipt(status), next_check, _now(), row["publication_id"]))
                else:
                    with self.store._connection() as conn:
                        conn.execute("UPDATE publications SET state='failed',receipt_json=?,failure_details=?,next_check_at=NULL,updated_at=? WHERE publication_id=?",
                                     (_safe_receipt(status), _safe_failure(platform_result.get("error") or "Upload-Post completed without platform confirmation"), _now(), row["publication_id"]))
            elif remote_status == "failed":
                with self.store._connection() as conn:
                    conn.execute("UPDATE publications SET state='failed',receipt_json=?,failure_details=?,updated_at=? WHERE publication_id=?",
                                 (_safe_receipt(status), _safe_failure(status.get("message") or "Upload-Post reports failure"), _now(), row["publication_id"]))
            elif row["state"] == "uploading":
                with self.store._connection() as conn:
                    conn.execute("UPDATE publications SET state='unknown',receipt_json=?,next_check_at=?,updated_at=? WHERE publication_id=?",
                                 (_safe_receipt(status), (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), _now(), row["publication_id"]))
            else:
                with self.store._connection() as conn:
                    conn.execute("UPDATE publications SET next_check_at=?,updated_at=? WHERE publication_id=?",
                                 ((datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), _now(), row["publication_id"]))

    def _reconcile_direct_publications(self):
        with self.store._connection() as conn:
            rows = conn.execute("SELECT * FROM publications WHERE publisher_backend='youtube_direct' AND state IN ('uploading','unknown','scheduled') AND (next_check_at IS NULL OR next_check_at<=?)",
                                (_now(),)).fetchall()
        for row in rows:
            try:
                job = self.store.get_job(row["job_id"])
                connection_id = row["youtube_connection_id"] or job.profile_snapshot.get("youtube_connection_id")
                channel_id = row["youtube_channel_id"] or job.profile_snapshot.get("youtube_channel_id")
                if not connection_id or not channel_id or row["channel_id"] != job.channel_id:
                    continue
                connection = self.youtube_connections.get_connection(connection_id)
                if connection["local_channel_id"] != job.channel_id or connection["youtube_channel_id"] != channel_id:
                    continue
                if row["state"] in {"uploading", "unknown"}:
                    if not row["resumable_session_url"] or not job.output_artifacts or not os.path.isfile(job.output_artifacts[0]):
                        continue  # no safe remote lookup key; retain unknown for operator reconciliation
                    context = self._direct_session_context(job, connection_id, channel_id, job.output_artifacts[0])
                    session, legacy_unverified = self._load_direct_session(
                        row, job, connection_id, channel_id, job.output_artifacts[0], context)
                    if not session:
                        continue
                    _, access = self.youtube_publisher.access_token(connection_id)
                    result = self.youtube_publisher.resume(session, job.output_artifacts[0], access, channel_id,
                                                           session_context=context,
                                                           legacy_unverified=legacy_unverified,
                                                           on_session=lambda url: self._save_direct_session(job.job_id, url, context))
                    self._record_direct_receipt(job, result)
                    continue
                if not row["provider_video_id"]:
                    continue
                remote = self.youtube_publisher.videos_list(connection_id, row["provider_video_id"])
                if remote and (remote.get("id") != row["provider_video_id"]
                               or (remote.get("snippet") or {}).get("channelId") != channel_id):
                    raise AutopilotError("YouTube status lookup returned a different video or channel")
                status = (remote.get("status") or {}) if remote else {}
                if status.get("privacyStatus") == "public":
                    new_state, next_check = "published", None
                elif status.get("uploadStatus") == "rejected":
                    new_state, next_check = "failed", None
                else:
                    new_state = "scheduled"
                    next_check = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
                with self.store._connection() as conn:
                    conn.execute("UPDATE publications SET state=?,receipt_json=?,next_check_at=?,updated_at=? WHERE publication_id=?",
                                 (new_state, _safe_receipt(remote), next_check, _now(), row["publication_id"]))
            except Exception as exc:
                retry_after = getattr(exc, "retry_after", None)
                with self.store._connection() as conn:
                    conn.execute("UPDATE publications SET state='unknown',failure_details=?,retry_after=?,next_check_at=?,updated_at=? WHERE publication_id=?",
                                 (_safe_failure(f"{type(exc).__name__}: {exc}"),
                                  (datetime.now(timezone.utc) + timedelta(seconds=retry_after)).isoformat() if retry_after else None,
                                  (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), _now(), row["publication_id"]))

    def _scheduler_loop(self):
        while not self._scheduler_stop.wait(30):
            try:
                self.tick_scheduler()
            except Exception as exc:
                logger.error(f"autopilot scheduler tick failed, error_type={type(exc).__name__}")

    def _is_paused(self, channel_id: str) -> bool:
        with self.store._connection() as conn:
            row = conn.execute("SELECT paused FROM channel_runtime WHERE channel_id=?", (channel_id,)).fetchone()
        return bool(row and row[0])

    def _channel_runnable(self, channel_id: str) -> bool:
        try:
            return self.profile_store.get(channel_id).enabled and not self._is_paused(channel_id)
        except LookupError:
            return False

    def _job_runnable(self, job: ProductionJob) -> bool:
        # Pausing a recurring schedule must not disable an operator's one-off video.
        if job.profile_snapshot.get("publishing_policy") == "manual" and not job.intended_publication_slot:
            try:
                return self.profile_store.get(job.channel_id).enabled
            except LookupError:
                return False
        return self._channel_runnable(job.channel_id)

    def set_channel_paused(self, channel_id: str, paused: bool, reason: str = "") -> dict:
        self.profile_store.get(channel_id)
        with self.store._connection() as conn:
            conn.execute("INSERT INTO channel_runtime (channel_id,paused,reason,updated_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(channel_id) DO UPDATE SET paused=excluded.paused, reason=excluded.reason, updated_at=excluded.updated_at",
                         (channel_id, int(paused), reason[:500] or None, _now()))
        return {"channel_id": channel_id, "paused": paused, "reason": reason}

    def schedule_state(self, channel_id: str) -> dict:
        profile = self.profile_store.get(channel_id)
        with self.store._connection() as conn:
            row = conn.execute("SELECT paused,reason FROM channel_runtime WHERE channel_id=?", (channel_id,)).fetchone()
        buffer = self.store.buffer_counts(channel_id, target=profile.ready_buffer_target)
        return {"channel_id": channel_id, "enabled": profile.enabled, "paused": bool(row and row["paused"]),
                "reason": (row["reason"] if row else None), "timezone": profile.timezone,
                "publications_per_week": profile.publications_per_week,
                "weekdays": profile.schedule_weekdays, "times_local": profile.schedule_times_local,
                "visual_style_rotation": profile.visual_style_rotation or [profile.visual_style],
                "buffer": buffer}

    def _topic_for_slot(self, profile: ChannelProfile, slot: datetime,
                        visual_style_override: str | None = None) -> str:
        if not profile.topic_preferences:
            raise AutopilotError("channel has no topic preferences; add niche topics before enabling its schedule")
        settings = resolve_generation_settings(
            channel_profile=profile,
            task_override={"visual_style": visual_style_override} if visual_style_override else None,
            topic=f"{profile.niche} {', '.join(profile.topic_preferences)}",
        )
        preset = visual_style.preset_for(settings.effective_visual_style)
        with config.runtime_config_lock():
            topic = llm.generate_topic(
                niche=profile.niche,
                preferences=profile.topic_preferences,
                language=profile.language,
                style_guidance=f"{preset.idea_guidance} {preset.visual_guidance}",
                slot_date=slot.date().isoformat(),
            )
        if not topic or topic.startswith("Error: "):
            reason = str(topic or "empty topic").removeprefix("Error: ").strip()
            raise AutopilotError(f"topic planning failed: {reason}")
        return topic

    def _record_overdue_completed_slots(self, now: datetime) -> None:
        """Keep completed buffer jobs attached to missed slots for safe reuse."""
        now_utc = now.astimezone(timezone.utc)
        for profile in self.profile_store.list(include_disabled=False):
            previous_tick = self.store.last_scheduler_tick(profile.channel_id)
            missed_since = (previous_tick or (now_utc - timedelta(hours=3))).astimezone(timezone.utc)
            with self.store._connection() as conn:
                rows = conn.execute(
                    "SELECT s.slot_id,s.job_id FROM autopilot_slots s "
                    "JOIN production_jobs j ON j.job_id=s.job_id "
                    "JOIN job_qa q ON q.job_id=j.job_id "
                    "LEFT JOIN publications p ON p.job_id=j.job_id "
                    "WHERE s.channel_id=? AND s.state='queued' AND s.slot_utc>? AND s.slot_utc<=? "
                    "AND j.state='generated' AND q.state='qa_passed' "
                    "AND (p.job_id IS NULL OR p.state NOT IN ('published','scheduled'))",
                    (profile.channel_id, missed_since.isoformat(), now_utc.isoformat()),
                ).fetchall()
            for row in rows:
                self.store.finish_slot(
                    row["slot_id"], "skipped", row["job_id"],
                    details="missed during downtime; recovery pending",
                )

    def tick_scheduler(self, now: datetime | None = None) -> list[dict]:
        """Fill the ready buffer and reschedule missed publications without exceeding cadence."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("scheduler time must include timezone")
        self.reconcile_publications()
        self._record_overdue_completed_slots(now)
        created = []
        # Retry only scheduled jobs whose saved failure is clearly transient.
        # The task id is retained so validated scripts/audio/render artifacts can
        # be reused by MPT's task-local artifact cache.
        for failed in self.store.list_jobs(limit=500):
            if failed.state != "failed" or not failed.intended_publication_slot:
                continue
            try:
                profile = self.profile_store.get(failed.channel_id)
            except LookupError:
                continue
            if (failed.attempt_count > profile.provider_retry_limit
                    or not _transient_generation_failure(failed.failure_details)
                    or not self._channel_runnable(failed.channel_id)):
                continue
            delay = min(300, 30 * (2 ** max(0, failed.attempt_count - 1)))
            failed_at = (failed.updated_at if isinstance(failed.updated_at, datetime)
                         else datetime.fromisoformat(failed.updated_at))
            if failed_at.tzinfo is None:
                failed_at = failed_at.replace(tzinfo=timezone.utc)
            if now.astimezone(timezone.utc) < failed_at.astimezone(timezone.utc) + timedelta(seconds=delay):
                continue
            try:
                self.store.transition(failed.job_id, "ready", failure_details=None)
                self.start_job(failed.job_id)
                created.append({"job_id": failed.job_id, "retry": failed.attempt_count})
            except (AutopilotError, InvalidTransition):
                continue
        # Durable queued jobs are resumed after restarts and by any active API/UI process.
        for queued in self.store.list_jobs(limit=500):
            if queued.state == "queued" and self._job_runnable(queued):
                with self._submit_lock:
                    if queued.job_id not in self._active_submissions:
                        self._active_submissions.add(queued.job_id)
                        self._executor.submit(self._run_scheduled_job, queued.job_id)
        with self.store._connection() as conn:
            stranded_slots = conn.execute("SELECT job_id FROM autopilot_slots WHERE state='queued' AND job_id IS NOT NULL").fetchall()
        for slot in stranded_slots:
            try:
                job = self.store.get_job(slot["job_id"])
                if job.state == "ready" and self._channel_runnable(job.channel_id):
                    self.start_job(job.job_id)
            except (LookupError, InvalidTransition, AutopilotError):
                continue
        with self.store._connection() as conn:
            pending_releases = conn.execute("""SELECT j.job_id,j.channel_id,j.profile_snapshot_json
                FROM production_jobs j JOIN job_qa q ON q.job_id=j.job_id
                LEFT JOIN publications p ON p.job_id=j.job_id
                WHERE j.state='generated' AND q.state='qa_passed' AND p.job_id IS NULL
                AND (j.intended_publication_slot IS NULL OR j.intended_publication_slot>?)""",
                (now.astimezone(timezone.utc).isoformat(),)).fetchall()
        for row in pending_releases:
            policy = json.loads(row["profile_snapshot_json"]).get("publishing_policy")
            public_ok = policy in {"automatic_public", "scheduled_public"}
            if policy in {"private", "unlisted"} or public_ok:
                if not self._channel_runnable(row["channel_id"]):
                    continue
                with self.store._connection() as conn:
                    conn.execute("UPDATE job_qa SET state='approved',reviewed_by='automatic-policy-recovery',reviewed_at=?,updated_at=? WHERE job_id=? AND state='qa_passed'",
                                 (_now(), _now(), row["job_id"]))
                try:
                    self.publish_job(row["job_id"])
                except AutopilotError:
                    logger.warning(f"autopilot publication remains pending, job_id={row['job_id']}")
        for profile in self.profile_store.list(include_disabled=True):
            if (not profile.enabled or profile.publications_per_week <= 0
                    or self._is_paused(profile.channel_id)):
                self.store.set_scheduler_tick(profile.channel_id, now)
                continue
            previous_tick = self.store.last_scheduler_tick(profile.channel_id)
            # Before the first persistent tick exists, retain the former short
            # startup catch-up window; later restarts use the exact durable cursor.
            missed_since = previous_tick or (now - timedelta(hours=3))
            missed_since = missed_since.astimezone(timezone.utc)
            local_now = now.astimezone(ZoneInfo(profile.timezone))
            channel_tz = ZoneInfo(profile.timezone)
            # Frequency is distributed over the configured weekdays and local times.
            choices = [(day, hour, minute) for day in profile.schedule_weekdays
                       for hour, minute in (tuple(map(int, value.split(":"))) for value in profile.schedule_times_local)]
            choices.sort()
            if not choices:
                continue
            selected = [choices[min(len(choices)-1, int(i * len(choices) / profile.publications_per_week))]
                        for i in range(profile.publications_per_week)]
            due_slots = []
            recovery_due_slots = []
            lead_hours = profile.generation_lead_hours
            # Keep a second week available for recovery placement when the normal
            # buffer already occupies every slot in the generation lead window.
            days_ahead = max(14, (lead_hours + 23) // 24)
            for days_ago in range(7, -days_ahead - 1, -1):
                local_date = local_now.date() - timedelta(days=days_ago)
                for day, hour, minute in sorted(set(selected)):
                    if day != local_date.weekday():
                        continue
                    naive_slot = datetime(local_date.year, local_date.month, local_date.day, hour, minute)
                    local_slot = naive_slot.replace(tzinfo=channel_tz, fold=0)
                    # Skip nonexistent wall times at spring-forward; fold=0 chooses
                    # the earlier occurrence when a fall-back time occurs twice.
                    if local_slot.astimezone(timezone.utc).astimezone(channel_tz).replace(tzinfo=None) != naive_slot:
                        continue
                    age = (local_now - local_slot).total_seconds()
                    slot_utc = local_slot.astimezone(timezone.utc)
                    slot_id = f"{profile.channel_id}:{slot_utc.isoformat()}"
                    if age < -(lead_hours * 3600):
                        recovery_due_slots.append((slot_id, slot_utc, age))
                        continue
                    due_slots.append((slot_id, slot_utc, age))
            with self.store._connection() as conn:
                existing = {row["slot_id"]: dict(row) for row in conn.execute(
                    "SELECT slot_id,state,details,attempt_count,lease_expires_at FROM autopilot_slots WHERE channel_id=?",
                    (profile.channel_id,),
                )}
            retry_cutoff = now.astimezone(timezone.utc).isoformat()
            candidates = [item for item in due_slots
                          if ((item[0] not in existing
                               and not (item[2] > 0 and item[1] <= missed_since))
                              or (item[0] in existing
                                  and existing[item[0]]["state"] == "planning")
                              or (item[0] in existing
                                  and existing[item[0]]["state"] == "failed"
                                  and existing[item[0]]["attempt_count"] <= profile.provider_retry_limit
                                  and _transient_generation_failure(existing[item[0]]["details"])
                                  and (not existing[item[0]]["lease_expires_at"]
                                       or existing[item[0]]["lease_expires_at"] <= retry_cutoff)))]
            candidates.sort(key=lambda item: item[1])
            buffer = self.store.buffer_counts(profile.channel_id, now, profile.ready_buffer_target)
            stale = [item for item in candidates if item[2] > 3 * 3600]
            overdue = [item for item in candidates if 0 <= item[2] <= 3 * 3600]
            future = [item for item in candidates if item[2] < 0]
            selected_slots = stale + overdue + future[:max(0, buffer["remaining"] - len(overdue))]
            for slot_id, slot_utc, age in selected_slots:
                if not self.store.claim_slot(
                    slot_id, profile.channel_id, slot_utc.isoformat(), self._scheduler_owner,
                    profile.visual_style_rotation if age <= 3 * 3600 else [],
                    retry_limit=profile.provider_retry_limit,
                ):
                    continue
                if age > 0:
                    # Keep the original slot as the audit record. Recovery assigns
                    # its work to a later configured slot with a durable link.
                    self.store.finish_slot(slot_id, "skipped", details="missed during downtime; recovery pending")
                    continue
                try:
                    recovery_origin = self.store.recovery_origin(slot_id)
                    if recovery_origin:
                        reused_job_id = self.store.reuse_completed_missed_job(
                            recovery_origin, profile.channel_id, slot_utc.isoformat(),
                        )
                        if reused_job_id:
                            self.store.finish_slot(slot_id, "queued", reused_job_id)
                            created.append({"slot_id": slot_id, "job_id": reused_job_id,
                                            "recovery_of": recovery_origin, "reused": True})
                            continue
                    with self.store._connection() as conn:
                        existing_plan = conn.execute("SELECT plan_id,state FROM content_plans WHERE channel_id=? AND intended_publication_slot=?",
                                                     (profile.channel_id, slot_utc.isoformat())).fetchone()
                    if existing_plan:
                        plan = self.store.get_plan(existing_plan["plan_id"], profile.channel_id)
                    else:
                        slot_style = self.store.slot_visual_style(slot_id)
                        topic = self._topic_for_slot(profile, slot_utc, slot_style)
                        plan = self.plan(profile.channel_id, ContentPlanCreate(
                            topic=topic, intended_publication_slot=slot_utc,
                            min_duration_seconds=profile.target_duration_seconds * 0.9,
                            max_duration_seconds=profile.target_duration_seconds * 1.1,
                            visual_style_override=slot_style,
                        ))
                    if plan.state == "planned":
                        plan = self.store.mark_plan_ready(
                            plan.plan_id, profile.channel_id,
                            accept_duration_estimate=(plan.duration_enforcement != "strict"),
                        )
                    job = next((item for item in self.store.list_jobs(profile.channel_id) if item.plan_id == plan.plan_id), None)
                    if job is None:
                        job = self.create_job(profile.channel_id, plan.plan_id)
                    self.store.finish_slot(slot_id, "queued", job.job_id)
                    self.start_job(job.job_id)
                    created.append({"slot_id": slot_id, "job_id": job.job_id})
                except Exception as exc:
                    details = _safe_failure(f"{type(exc).__name__}: {exc}")
                    attempts = self.store.slot_attempt_count(slot_id)
                    retry_delay = (
                        min(300, 30 * (2 ** max(0, attempts - 1)))
                        if _transient_generation_failure(details)
                        and attempts <= profile.provider_retry_limit else None
                    )
                    self.store.finish_slot(
                        slot_id, "failed", details=details,
                        retry_delay_seconds=retry_delay,
                    )
            # Recover both the just-missed slots and records left pending if the
            # previous process stopped after recording the miss. Candidate times
            # are drawn from the same configured weekly cadence; existing plans,
            # jobs, publications, and slot rows make a time unavailable.
            recovery_times = [item for item in due_slots + recovery_due_slots if item[2] < 0]
            for missed in self.store.pending_missed_slots(profile.channel_id):
                # A persisted publication means an earlier attempt may already
                # have reached YouTube. Let the publisher reconcile that record;
                # never create replacement content while its outcome is unclear.
                if self.store.missed_slot_has_publication_attempt(
                    missed["slot_id"], profile.channel_id,
                ):
                    continue
                for recovery_id, recovery_utc, _age in recovery_times:
                    if not self.store.claim_recovery_slot(
                        missed["slot_id"], profile.channel_id, recovery_id,
                        recovery_utc.isoformat(), self._scheduler_owner,
                        profile.visual_style_rotation,
                    ):
                        continue
                    try:
                        reused_job_id = self.store.reuse_completed_missed_job(
                            missed["slot_id"], profile.channel_id, recovery_utc.isoformat(),
                        )
                        if reused_job_id:
                            self.store.finish_slot(recovery_id, "queued", reused_job_id)
                            created.append({"slot_id": recovery_id, "job_id": reused_job_id,
                                            "recovery_of": missed["slot_id"], "reused": True})
                            break
                        with self.store._connection() as conn:
                            existing_plan = conn.execute(
                                "SELECT plan_id FROM content_plans WHERE channel_id=? AND intended_publication_slot=?",
                                (profile.channel_id, recovery_utc.isoformat()),
                            ).fetchone()
                        if existing_plan:
                            plan = self.store.get_plan(existing_plan["plan_id"], profile.channel_id)
                        else:
                            slot_style = self.store.slot_visual_style(recovery_id)
                            topic = self._topic_for_slot(profile, recovery_utc, slot_style)
                            plan = self.plan(profile.channel_id, ContentPlanCreate(
                                topic=topic, intended_publication_slot=recovery_utc,
                                min_duration_seconds=profile.target_duration_seconds * 0.9,
                                max_duration_seconds=profile.target_duration_seconds * 1.1,
                                visual_style_override=slot_style,
                            ))
                        if plan.state == "planned":
                            plan = self.store.mark_plan_ready(
                                plan.plan_id, profile.channel_id,
                                accept_duration_estimate=(plan.duration_enforcement != "strict"),
                            )
                        job = next((item for item in self.store.list_jobs(profile.channel_id)
                                    if item.plan_id == plan.plan_id), None)
                        if job is None:
                            job = self.create_job(profile.channel_id, plan.plan_id)
                        self.store.finish_slot(recovery_id, "queued", job.job_id)
                        self.start_job(job.job_id)
                        created.append({"slot_id": recovery_id, "job_id": job.job_id,
                                        "recovery_of": missed["slot_id"], "reused": False})
                        break
                    except Exception as exc:
                        details = _safe_failure(f"{type(exc).__name__}: {exc}")
                        self.store.finish_slot(recovery_id, "failed", details=details)
                        logger.warning(f"missed autopilot slot recovery failed, slot_id={missed['slot_id']}, details={details}")
            self.store.set_scheduler_tick(profile.channel_id, now)
        return created

    def qa_report(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if job.state != "generated":
            raise InvalidTransition("QA is available only for generated jobs")
        task_state = sm.state.get_task(job.mpt_task_id) or {}
        verification = self._fact_verification_for_job(job, task_state)
        checks = {"artifacts": bool(job.output_artifacts) and all(os.path.isfile(p) for p in job.output_artifacts),
                  "duration": (job.duration_enforcement != "strict" or
                      (job.actual_duration_seconds is not None and
                       job.min_duration_seconds <= job.actual_duration_seconds <= job.max_duration_seconds)),
                  "subtitles": not job.params_snapshot.get("subtitle_enabled") or bool(task_state.get("subtitle_path") and os.path.isfile(task_state.get("subtitle_path"))),
                  "audio": bool(task_state.get("audio_file") and os.path.isfile(task_state.get("audio_file"))),
                  "material_references": bool(task_state.get("materials"))}
        audio_artifacts = [str(item) for item in (task_state.get("videos") or []) if item]
        if not audio_artifacts:
            audio_artifacts = [
                item for item in job.output_artifacts
                if Path(item).name.startswith("final-")
            ] or list(job.output_artifacts)
        # Check every render variant's orientation; only final videos carry audio.
        try:
            orientations, audio_tracks = [], []
            for artifact in job.output_artifacts:
                clip = video._open_video_clip_quietly(artifact, audio=True)
                try:
                    aspect = job.params_snapshot.get("video_aspect")
                    orientations.append((aspect == "9:16" and clip.h >= clip.w)
                        or (aspect == "16:9" and clip.w >= clip.h)
                        or (aspect == "1:1" and abs(clip.w - clip.h) / max(clip.w, clip.h) <= 0.03))
                    # combined-*.mp4 is the intentionally silent visual intermediate;
                    # final-*.mp4 is rendered with narration and is the deliverable.
                    if artifact in audio_artifacts:
                        audio_tracks.append(clip.audio is not None)
                finally:
                    clip.close()
            checks["orientation"] = bool(orientations) and all(orientations)
            checks["audio_track"] = bool(audio_tracks) and all(audio_tracks)
        except Exception:
            checks["orientation"] = checks["audio_track"] = False
        provenance = self._material_provenance(job, task_state)
        repetition = self.repetition_report(job)
        content_review = self.store.content_review(job_id)
        return {"passed": all(checks.values()), "technical_checks_passed": all(checks.values()), "checks": checks,
                "originality_state": content_review["originality_status"],
                "rights_state": content_review["rights_status"],
                "repetition_warnings": repetition["warnings"],
                "fact_verification": verification,
                "channel_branding": {
                    "applied": task_state.get("channel_branding_applied", []),
                    "fallbacks": task_state.get("channel_branding_fallbacks", []),
                },
                "automatic_publication_allowed": (all(checks.values())
                    and bool(job.profile_snapshot.get("auto_public_enabled", False))
                    and (verification["status"] == "verified"
                         or bool(job.profile_snapshot.get("allow_unverified_publication", False)))
                    and content_review["originality_status"] == "original"
                    and content_review["rights_status"] == "documented" and not repetition["blocking_warnings"]),
                "provenance_metadata": provenance,
                "copyright_claims": {"status": "unknown", "details": None,
                    "note": "YouTube copyright claims are not reliably exposed by the Data API integration."}}

    @staticmethod
    def _fact_verification_for_job(job: ProductionJob, task_state: dict | None = None) -> dict:
        task_state = task_state or (sm.state.get_task(job.mpt_task_id) if job.mpt_task_id else None) or {}
        snapshot_state = {
            "fact_verification_state": job.params_snapshot.get("fact_verification_state", "unverified"),
            "fact_verification_script_hash": job.params_snapshot.get("fact_verification_script_hash", ""),
        }
        verification_state = {**snapshot_state, **task_state}
        spoken_script = hindi_numbers.normalize_hindi_numbers(
            str(job.script or ""), job.params_snapshot.get("video_language", "")
        )
        expected_hash = hashlib.sha256(spoken_script.encode("utf-8")).hexdigest()
        raw_state = verification_state.get("fact_verification_state", "unverified")
        exact = (raw_state == "source_verified"
                 and verification_state.get("fact_verification_script_hash") == expected_hash)
        status = {
            "source_verified": "verified" if exact else "unverified",
            "contradicted": "contradicted",
            "unavailable": "unavailable",
            "verification_error": "unavailable",
            "unsupported": "unverified",
            "unverified": "unverified",
        }.get(raw_state, "unverified")
        return {"status": status, "mode": job.params_snapshot.get(
            "fact_verification_policy", job.profile_snapshot.get("fact_verification_policy", "advisory")
        ), "script_hash_matches": exact}

    def _publication_metadata(self, job: ProductionJob) -> tuple[str, str]:
        title = str(job.topic or "")
        description = str(job.script or "")
        if (job.profile_snapshot.get("publishing_policy") in {"automatic_public", "scheduled_public"}
                and self._fact_verification_for_job(job)["status"] != "verified"):
            title = ("[UNVERIFIED FACTS] " + title)[:100]
            description = (
                "Disclosure: MPT did not verify the factual claims in this video against sources.\n\n"
                + description
            )[:5000]
        return title, description

    @staticmethod
    def _youtube_caption_context(job: ProductionJob) -> tuple[str, str, str]:
        profile = job.profile_snapshot or {}
        params = job.params_snapshot or {}
        language_value = str(
            params.get("youtube_caption_language")
            or profile.get("youtube_caption_language")
            or "English"
        ).casefold()
        language = "Hindi" if language_value in {"hi", "hindi"} else "English"
        context = {
            "channel": profile.get("display_name", ""),
            "channel_hashtag": AutopilotService._youtube_channel_hashtag(job),
            "caption_language": language,
            "niche": profile.get("niche", ""),
            "topic_preferences": profile.get("topic_preferences", []),
            "storytelling_style": profile.get("script_style", ""),
            "visual_style": (params.get("effective_visual_style")
                             or params.get("visual_style") or job.effective_visual_style
                             or job.visual_style),
            "narrator_preset": params.get("narrator_preset") or profile.get("narrator_preset", ""),
            "profanity_level": params.get("profanity_level") or profile.get("profanity_level", ""),
            "voice_name": params.get("voice_name") or profile.get("voice_name", ""),
            "voice_rate": params.get("voice_rate", profile.get("voice_rate", 1.0)),
            "intro": {
                "enabled": bool(profile.get("intro_enabled")),
                "text": str(profile.get("intro_text") or "")[:240],
                "audio_present": bool(profile.get("intro_audio")),
            },
            "outro": {
                "enabled": bool(profile.get("outro_enabled")),
                "text": str(profile.get("outro_text") or "")[:240],
                "audio_present": bool(profile.get("outro_audio")),
            },
        }
        context_text = json.dumps(context, ensure_ascii=False, sort_keys=True)
        context_fingerprint = hashlib.sha256(context_text.encode("utf-8")).hexdigest()
        return language, context_text[:1500], context_fingerprint

    @staticmethod
    def _youtube_caption_source(job: ProductionJob, narration: str) -> str:
        profile = job.profile_snapshot or {}
        segments = []
        intro = str(profile.get("intro_text") or "").strip()
        outro = str(profile.get("outro_text") or "").strip()
        if profile.get("intro_enabled") and intro:
            segments.append(f"Channel intro: {intro[:500]}")
        segments.append(f"Final narration: {str(narration or '').strip()}")
        if profile.get("outro_enabled") and outro:
            segments.append(f"Channel outro: {outro[:500]}")
        return "\n".join(segments)

    @staticmethod
    def _youtube_channel_hashtag(job: ProductionJob) -> str:
        profile = job.profile_snapshot or {}
        value = profile.get("channel_hashtag") or profile.get("display_name") or ""
        return next(iter(llm._normalize_hashtags([str(value)], 1)), "")

    @staticmethod
    def _youtube_caption_body_errors(body: str, language: str = "English") -> list[str]:
        errors = []
        body = " ".join(str(body or "").split())
        if not body:
            return ["caption body is empty"]
        if not AutopilotService._youtube_text_matches_language(body, language):
            errors.append(f"caption must be written in {language}")
        if AutopilotService._youtube_word_count(body) > 10:
            errors.append("each caption sentence must contain at most 10 words")
        if "#" in body:
            errors.append("hashtags must be separate trailing tokens")
        for opening, closing in (("(", ")"), ("[", "]"), ("“", "”"), ('"', '"')):
            if body.count(opening) != body.count(closing):
                errors.append("caption has unbalanced punctuation")
                break

        ending = body.rstrip()
        while ending:
            category = unicodedata.category(ending[-1])
            if ending[-1] in "'\"’”)]}" or category in {"So", "Sk", "Cf", "Mn", "Me"}:
                ending = ending[:-1].rstrip()
            else:
                break
        if not ending or ending[-1] not in ".!?।॥":
            errors.append("caption ends without a complete sentence mark")
            return errors
        if re.search(r"[.!?।॥]", ending[:-1]):
            errors.append("each caption line must contain exactly one sentence")

        terminal_mark = ending[-1]
        final_text = re.sub(r"[.!?।॥]+$", "", ending).rstrip()
        final_word_match = re.search(r"([^\s,;:!?।॥]+)$", final_text, flags=re.UNICODE)
        if final_word_match:
            final_word = final_word_match.group(1).casefold()
            dangling = {
                "a", "an", "and", "are", "because", "but", "for", "how", "if", "of",
                "or", "the", "to", "when", "where", "which", "who", "why", "with",
                "अगर", "और", "कहाँ", "कब", "क्यों", "क्योंकि", "कैसे", "क्या", "के",
                "की", "का", "कौन", "लेकिन", "जिसमें", "जिससे", "तक", "तो", "पर", "में", "से", "या",
            }
            if final_word in dangling and terminal_mark not in "?？":
                errors.append("caption ends with an incomplete phrase or question")
        return errors

    @staticmethod
    def _youtube_emoji_count(text: str) -> int:
        return len(re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text))

    @staticmethod
    def _youtube_word_count(text: str) -> int:
        return sum(
            any(unicodedata.category(char)[0] in {"L", "N"} for char in token)
            for token in str(text or "").split()
        )

    @classmethod
    def _youtube_caption_validation_errors(
        cls, caption: str, required_hashtag: str = "", language: str = "English",
    ) -> list[str]:
        lines = [" ".join(line.split()) for line in str(caption or "").splitlines()]
        lines = [line for line in lines if line]
        tokens = str(caption or "").split()
        errors = []
        if cls._youtube_word_count(str(caption or "")) > 50:
            errors.append("caption exceeds 50 whitespace-separated words")
        hashtag_tokens = [token for token in tokens if token.startswith("#")]
        if len(hashtag_tokens) != 5:
            errors.append("caption must contain exactly five hashtags")
        valid_hashtag = re.compile(r"^#[\w]+$", flags=re.UNICODE)
        if any(not valid_hashtag.fullmatch(tag) for tag in hashtag_tokens):
            errors.append("hashtags must use # followed by letters, numbers, or underscores")
        if len({tag.casefold() for tag in hashtag_tokens}) != len(hashtag_tokens):
            errors.append("hashtags must be unique")
        hashtag_line = lines[-1].split() if lines else []
        if hashtag_line != hashtag_tokens:
            errors.append("five hashtags must share their own final line")
        if required_hashtag and required_hashtag.casefold() not in {
            tag.casefold() for tag in hashtag_tokens
        }:
            errors.append("caption is missing the configured channel hashtag")
        body_lines = lines[:-1] if hashtag_line == hashtag_tokens else lines
        if len(body_lines) != 2:
            errors.append("caption must contain exactly two complete sentences on separate lines")
        for line in body_lines:
            errors.extend(cls._youtube_caption_body_errors(line, language))
        emoji_count = cls._youtube_emoji_count(" ".join(body_lines))
        if emoji_count not in {3, 4}:
            errors.append("caption must contain three or four emojis")
        return list(dict.fromkeys(errors))

    @staticmethod
    def _youtube_text_looks_english(text: str) -> bool:
        value = str(text or "")
        romanized_hindi = re.compile(
            r"\b(aakhir|aur|hai|hain|ka|kahaan|kahan|kaise|ke|ki|kya|mein|paisa|"
            r"se|tak|tum|tumhara|yeh|woh)\b", flags=re.IGNORECASE,
        )
        return bool(re.search(r"[A-Za-z]", value)) and not bool(
            re.search(r"[\u0900-\u097f]", value) or romanized_hindi.search(value)
        )

    @staticmethod
    def _youtube_text_matches_language(text: str, language: str) -> bool:
        value = str(text or "")
        normalized = str(language or "").casefold()
        if normalized.startswith(("hi", "hindi")):
            return bool(re.search(r"[\u0900-\u097f]", value))
        if normalized.startswith(("en", "english")):
            return AutopilotService._youtube_text_looks_english(value)
        return bool(value.strip())

    @staticmethod
    def _youtube_title_matches_language(title: str, language: str = "English") -> bool:
        return AutopilotService._youtube_text_matches_language(title, language)

    @staticmethod
    def _deterministic_youtube_title(narration: str = "", language: str = "English") -> str:
        """Use narration itself when a language-safe title cannot be generated."""
        if str(language or "").casefold().startswith(("hi", "hindi")):
            first = re.split(r"(?<=[.!?।॥])\s*", str(narration or ""), maxsplit=1)[0]
            return first.strip()[:100]
        return "The Story Behind This Idea"

    @staticmethod
    def _youtube_caption_parts(raw_caption: str, raw_hashtags) -> tuple[str, list[str]]:
        inline_hashtags = re.findall(r"(?<!\w)#[\w]+", str(raw_caption or ""), flags=re.UNICODE)
        body_with_lines = re.sub(r"(?<!\w)#\S+", " ", str(raw_caption or ""), flags=re.UNICODE)
        body_lines = [" ".join(line.split()).strip(" ,;—–-") for line in body_with_lines.splitlines()]
        body = "\n".join(line for line in body_lines if line)
        if isinstance(raw_hashtags, str):
            hashtags = [raw_hashtags, *inline_hashtags]
        elif isinstance(raw_hashtags, (list, tuple)):
            hashtags = [*raw_hashtags, *inline_hashtags]
        else:
            hashtags = inline_hashtags
        return body, hashtags

    @staticmethod
    def _compose_youtube_caption(body: str, hashtags: list[str]) -> str:
        body_lines = [" ".join(line.split()) for line in str(body or "").splitlines()]
        body_lines = [line for line in body_lines if line]
        return "\n".join([*body_lines, " ".join(hashtags)])

    @staticmethod
    def _youtube_derived_hashtags(job: ProductionJob, narration: str) -> list[str]:
        profile = job.profile_snapshot or {}
        sources = [job.topic, profile.get("niche", ""), *profile.get("topic_preferences", []), narration[:2400]]
        stopwords = {
            "about", "after", "again", "also", "are", "because", "been", "being", "between",
            "from", "have", "here", "into", "just", "more", "most", "over", "that", "their",
            "there", "these", "they", "this", "through", "what", "when", "where", "which", "while",
            "with", "your", "you", "the", "and", "for", "but", "not", "how", "why", "who", "does",
            "can", "will", "क्या", "कैसे", "कहाँ", "कब", "क्यों", "तुम्हारी", "तुम्हारे", "तुम", "आप",
            "अगर", "असल", "और", "एक", "इस", "इसे", "है", "हो", "हैं", "का", "की", "के", "को", "में",
            "नहीं", "पर", "से", "तक", "तो", "वह", "यह", "या", "जो", "भी", "कि", "लिए", "उसके",
        }
        candidates = []
        for source in sources:
            if not isinstance(source, str):
                continue
            words = re.findall(r"[^\W_]+", source, flags=re.UNICODE)
            meaningful = [word for word in words if len(word) > 1 and word.casefold() not in stopwords]
            if 2 <= len(meaningful) <= 4:
                candidates.append("#" + "".join(word[:1].upper() + word[1:] for word in meaningful))
            candidates.extend("#" + word[:1].upper() + word[1:] for word in meaningful)
        if str(profile.get("language", "")).lower().startswith("hi"):
            candidates.append("#HindiFacts")
        seed_source = str(job.topic or profile.get("niche", ""))
        seed_words = [
            word for word in re.findall(r"[^\W_]+", seed_source, flags=re.UNICODE)
            if len(word) > 1 and word.casefold() not in stopwords
        ]
        if seed_words:
            seed = seed_words[0][:1].upper() + seed_words[0][1:]
            candidates.extend([f"#{seed}Facts", f"#{seed}Explained"])
        candidates.append("#YouTubeShorts")
        return llm._normalize_hashtags(candidates, 20)

    @classmethod
    def _youtube_caption_hashtags(cls, raw_hashtags, job: ProductionJob, narration: str) -> list[str]:
        generic = {"#shorts", "#viral", "#trending", "#fyp", "#video", "#reels", "#creator", "#content"}
        candidates = llm._normalize_hashtags(raw_hashtags, 20)
        candidates = [tag for tag in candidates if tag.casefold() not in generic]
        candidates.extend(cls._youtube_derived_hashtags(job, narration))
        candidates.extend(["#TopicFacts", "#ScriptExplained", "#VideoExplained", "#YouTubeShorts"])
        channel_tag = cls._youtube_channel_hashtag(job)
        selected = []
        for tag in candidates:
            if channel_tag and tag.casefold() == channel_tag.casefold():
                continue
            if tag.casefold() not in {item.casefold() for item in selected}:
                selected.append(tag)
            if len(selected) == 4:
                break
        if channel_tag:
            selected.append(channel_tag)
        return selected[:5]

    @classmethod
    def _deterministic_youtube_caption(
        cls, narration: str, job: ProductionJob, hashtags: list[str],
        language: str = "English",
    ) -> str:
        script = re.sub(r"(?<!\w)#\S+", " ", str(narration or ""), flags=re.UNICODE)
        script = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", " ", script)
        script = " ".join(script.split())
        sentences = re.split(r"(?<=[.!?।॥])\s+", script)
        complete_sentences = []
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if (sentence[-1] not in ".!?।॥"
                    or cls._youtube_caption_body_errors(sentence, language)):
                continue
            if len(sentence.split()) <= 8:
                complete_sentences.append(sentence)
            if len(complete_sentences) == 2:
                break

        if str(language or "").casefold().startswith(("hi", "hindi")):
            if len(complete_sentences) >= 2:
                lines = [
                    f"{complete_sentences[0]} 🔍🤔",
                    f"{complete_sentences[1]} 💡✨",
                ]
                return cls._compose_youtube_caption("\n".join(lines), hashtags)
            # Preserve the actual narration; do not invent translated copy if
            # the script has fewer than two short, complete Hindi sentences.
            return cls._compose_youtube_caption("\n".join(complete_sentences), hashtags)

        subject = "this idea"
        hook = f"What makes {subject} surprising? 🔍🤔"
        explanation = "The video explains the main process."

        if complete_sentences and complete_sentences[0].endswith(("?", "？")):
            hook = f"{complete_sentences[0]} 🔍🤔"
            if len(complete_sentences) > 1:
                explanation = complete_sentences[1]
            else:
                explanation = "The video explains the idea."
        elif len(complete_sentences) > 1:
            explanation = complete_sentences[1]
        elif complete_sentences:
            explanation = complete_sentences[0]
        lines = [hook, f"{explanation} 💡✨"]
        caption = cls._compose_youtube_caption("\n".join(lines), hashtags)
        if cls._youtube_caption_validation_errors(
            caption, cls._youtube_channel_hashtag(job), language
        ):
            fallback_lines = ["What is the key idea? 🔍🤔", "The narration explains the process. 💡✨"]
            caption = cls._compose_youtube_caption("\n".join(fallback_lines), hashtags)
        return caption

    def ensure_youtube_metadata(self, job_id: str) -> dict:
        """Return persistent script-aware YouTube copy, generating it if absent or stale."""
        job = self.store.get_job(job_id)
        if job.state != "generated":
            raise AutopilotError("YouTube caption generation requires a completed video")
        narration = job.script or ""
        task_manifest = {}
        if job.mpt_task_id:
            manifest_path = Path(utils.task_dir(job.mpt_task_id)) / "script.json"
            try:
                task_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                narration = str(task_manifest.get("spoken_script") or task_manifest.get("script") or narration)
            except (OSError, ValueError, TypeError):
                pass
        language, context_text, context_fingerprint = self._youtube_caption_context(job)
        narration_fingerprint = hashlib.sha256(narration.encode("utf-8")).hexdigest()
        caption_source = self._youtube_caption_source(job, narration)
        caption_source_fingerprint = hashlib.sha256(caption_source.encode("utf-8")).hexdigest()
        channel_hashtag = self._youtube_channel_hashtag(job)
        metadata = job.youtube_metadata or {}
        if (metadata.get("narration_fingerprint") == narration_fingerprint
                and metadata.get("context_fingerprint") == context_fingerprint
                and not self._youtube_caption_validation_errors(
                    metadata.get("caption", ""), channel_hashtag, language,
                )):
            return metadata

        def generate(feedback=""):
            return llm.generate_social_metadata(
                video_subject=job.topic,
                video_script=caption_source,
                language=language,
                platform="youtube_shorts",
                channel_context=context_text,
                caption_feedback=feedback,
                max_attempts=1,
            )

        task_metadata = task_manifest.get("youtube_metadata_by_video", [])
        output_name = Path(job.output_artifacts[0]).name if job.output_artifacts else ""
        saved_for_video = next((
            item for item in task_metadata
            if isinstance(item, dict)
            and item.get("video_name") == output_name
            and item.get("narration_fingerprint") == narration_fingerprint
            and item.get("caption_source_fingerprint") == caption_source_fingerprint
        ), None)
        generated = saved_for_video or generate()
        body, raw_hashtags = self._youtube_caption_parts(
            generated.get("caption", ""), generated.get("hashtags"),
        )
        hashtags = self._youtube_caption_hashtags(raw_hashtags, job, narration)
        caption = self._compose_youtube_caption(body, hashtags)
        validation_errors = self._youtube_caption_validation_errors(
            caption, channel_hashtag, language
        )
        if not self._youtube_title_matches_language(generated.get("title", ""), language):
            validation_errors.append(f"title must be written in {language}")
        if validation_errors:
            feedback = (
                "Rewrite the caption to fix these local validation errors: "
                + "; ".join(validation_errors)
                + ". Use exactly two complete sentences on separate lines, at most 10 words per sentence, "
                  "include three or four emojis, remain faithful to the narration, and stay under 50 words total. "
                  "Use exactly five relevant hashtags, and include the configured channel hashtag. Previous caption: "
                + caption[:600]
            )
            try:
                rewritten = generate(feedback)
                rewritten_body, rewritten_raw_tags = self._youtube_caption_parts(
                    rewritten.get("caption", ""), rewritten.get("hashtags"),
                )
                rewritten_tags = self._youtube_caption_hashtags(rewritten_raw_tags, job, narration)
                rewritten_caption = self._compose_youtube_caption(rewritten_body, rewritten_tags)
                rewritten_errors = self._youtube_caption_validation_errors(
                    rewritten_caption, channel_hashtag, language
                )
                if not self._youtube_title_matches_language(rewritten.get("title", ""), language):
                    rewritten_errors.append(f"title must be written in {language}")
                if not rewritten_errors:
                    generated, caption, hashtags = rewritten, rewritten_caption, rewritten_tags
                    validation_errors = []
                else:
                    validation_errors = rewritten_errors
            except Exception as exc:
                logger.warning(
                    "YouTube caption rewrite failed; using deterministic script fallback: "
                    "job_id={}, error_type={}", job_id, type(exc).__name__,
                )

        if validation_errors:
            caption = self._deterministic_youtube_caption(
                narration, job, hashtags, language,
            )
            hashtags = caption.split()[-5:]
        result = {
            "platform": "youtube",
            "title": (str(generated.get("title") or "")[:100]
                      if self._youtube_title_matches_language(generated.get("title", ""), language)
                      else self._deterministic_youtube_title(narration, language)),
            "caption": caption,
            "hashtags": hashtags,
            "language": language,
            "channel_hashtag": channel_hashtag,
            "narration_fingerprint": narration_fingerprint,
            "caption_source_fingerprint": caption_source_fingerprint,
            "context_fingerprint": context_fingerprint,
            "generated_at": _now(),
        }
        self.store.save_youtube_metadata(job_id, result)
        return result

    def get_youtube_metadata(self, job_id: str) -> dict | None:
        """Read already-prepared, current YouTube metadata without provider calls."""
        job = self.store.get_job(job_id)
        if job.state != "generated":
            return None
        narration = job.script or ""
        task_manifest = {}
        if job.mpt_task_id:
            manifest_path = Path(utils.task_dir(job.mpt_task_id)) / "script.json"
            try:
                task_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                narration = str(task_manifest.get("spoken_script") or task_manifest.get("script") or narration)
            except (OSError, ValueError, TypeError):
                pass
        language, _context_text, context_fingerprint = self._youtube_caption_context(job)
        narration_fingerprint = hashlib.sha256(narration.encode("utf-8")).hexdigest()
        caption_source = self._youtube_caption_source(job, narration)
        caption_source_fingerprint = hashlib.sha256(caption_source.encode("utf-8")).hexdigest()
        channel_hashtag = self._youtube_channel_hashtag(job)
        candidates = [job.youtube_metadata or {}]
        output_name = Path(job.output_artifacts[0]).name if job.output_artifacts else ""
        task_metadata = task_manifest.get("youtube_metadata_by_video", [])
        for item in task_metadata:
            if (not isinstance(item, dict) or item.get("video_name") != output_name
                    or item.get("narration_fingerprint") != narration_fingerprint
                    or item.get("caption_source_fingerprint") != caption_source_fingerprint):
                continue
            body, raw_hashtags = self._youtube_caption_parts(
                item.get("caption", ""), item.get("hashtags"),
            )
            hashtags = self._youtube_caption_hashtags(raw_hashtags, job, narration)
            candidates.append({
                **item, "caption": self._compose_youtube_caption(body, hashtags),
                "hashtags": hashtags, "context_fingerprint": context_fingerprint,
                "language": language, "channel_hashtag": channel_hashtag,
            })
        for metadata in candidates:
            if (metadata.get("narration_fingerprint") != narration_fingerprint
                    or metadata.get("context_fingerprint") != context_fingerprint
                    or self._youtube_caption_validation_errors(
                        metadata.get("caption", ""), channel_hashtag, language,
                    )
                    or not self._youtube_title_matches_language(metadata.get("title", ""), language)):
                continue
            return metadata
        return None

    def youtube_caption_validation_errors(self, job_id: str, caption: str) -> list[str]:
        metadata = self.get_youtube_metadata(job_id)
        if metadata is None:
            return ["generate or prepare the YouTube caption before editing"]
        return self._youtube_caption_validation_errors(
            caption, metadata.get("channel_hashtag", ""), metadata.get("language", "English"),
        )

    def save_youtube_caption(self, job_id: str, caption: str) -> dict:
        metadata = self.ensure_youtube_metadata(job_id)
        normalized = "\n".join(
            " ".join(line.split()) for line in str(caption or "").splitlines()
            if line.strip()
        )
        errors = self._youtube_caption_validation_errors(
            normalized, metadata.get("channel_hashtag", ""), metadata.get("language", "English"),
        )
        if errors:
            raise AutopilotError("Invalid YouTube caption: " + "; ".join(errors))
        hashtags = normalized.split()[-5:]
        metadata = {**metadata, "caption": normalized, "hashtags": hashtags}
        self.store.save_youtube_metadata(job_id, metadata)
        return metadata

    def _material_sources_for_job(self, job: ProductionJob) -> list[dict]:
        if not job.mpt_task_id:
            return []
        try:
            manifest = Path(utils.task_dir(job.mpt_task_id)) / "script.json"
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            sources = payload.get("material_sources", [])
            return [item for item in sources if isinstance(item, dict)] if isinstance(sources, list) else []
        except (OSError, ValueError, TypeError):
            return []

    def _material_provenance(self, job: ProductionJob, task_state: dict | None = None) -> dict:
        records = self._material_sources_for_job(job)
        if not records:
            return {"status": "unknown", "available": False, "records": [],
                    "note": "No retained per-asset provenance manifest was available."}
        complete = all(item.get("provider") and item.get("source_page") and item.get("asset_id")
                       for item in records)
        rights_evidence = all(item.get("license") or item.get("license_url") or item.get("license_terms_url")
                              for item in records)
        return {"status": "metadata_present_needs_human_review" if complete else "incomplete",
                "available": True, "records": records,
                "rights_evidence_present": rights_evidence,
                "note": "Provider/source metadata is not proof of commercial-use rights; verify the applicable license for every asset."}

    def repetition_report(self, job: ProductionJob) -> dict:
        warnings, blocking = [], []
        current_topic = normalize_content(job.topic)
        current_sentences = [part.strip() for part in re.split(r"(?<=[.!?।])\s+", job.script) if part.strip()]
        current_hook = normalize_content(current_sentences[0]) if current_sentences else ""
        previous = [item for item in self.store.list_jobs(job.channel_id, limit=_HISTORY_SCAN_LIMIT)
                    if item.job_id != job.job_id]
        for other in previous:
            topic_ratio = SequenceMatcher(None, current_topic, normalize_content(other.topic)).ratio()
            script_ratio = _script_similarity(job.script, other.script)
            other_sentences = [part.strip() for part in re.split(r"(?<=[.!?।])\s+", other.script) if part.strip()]
            hook_ratio = SequenceMatcher(None, current_hook,
                normalize_content(other_sentences[0]) if other_sentences else "").ratio() if current_hook else 0
            if topic_ratio >= _NEAR_DUPLICATE_TOPIC:
                warnings.append({"kind": "near_duplicate_topic", "job_id": other.job_id, "similarity": round(topic_ratio, 2)})
            if script_ratio >= _NEAR_DUPLICATE_SCRIPT:
                warnings.append({"kind": "near_duplicate_script", "job_id": other.job_id, "similarity": round(script_ratio, 2)})
                blocking.append("near_duplicate_script")
            if hook_ratio >= 0.88:
                warnings.append({"kind": "repeated_hook", "job_id": other.job_id, "similarity": round(hook_ratio, 2)})
        sources = self._material_sources_for_job(job)
        current_ids = {str(item.get("asset_id")) for item in sources if item.get("asset_id")}
        reused = []
        for other in previous:
            previous_ids = {str(item.get("asset_id")) for item in self._material_sources_for_job(other) if item.get("asset_id")}
            repeated = sorted(current_ids & previous_ids)
            if repeated:
                reused.append({"job_id": other.job_id, "asset_ids": repeated})
        if reused:
            warnings.append({"kind": "reused_source_assets", "matches": reused})
        if len(current_sentences) and any(
                len([part for part in re.split(r"(?<=[.!?।])\s+", other.script) if part.strip()]) == len(current_sentences)
                and SequenceMatcher(None, current_hook,
                    normalize_content((re.split(r"(?<=[.!?।])\s+", other.script) or [""])[0])).ratio() > 0.65
                for other in previous[:50]):
            warnings.append({"kind": "similar_video_structure", "detail": "sentence count and opening structure resemble recent scripts"})
        return {"warnings": warnings, "blocking_warnings": blocking,
                "status": "human_review_required" if warnings else "no_local_warning_detected"}

    def review_content_quality(self, job_id: str, reviewer: str, review) -> dict:
        report = self.qa_report(job_id)
        if not report["technical_checks_passed"]:
            raise AutopilotError("technical video QA must pass before originality and rights approval")
        if review.rights_status == "documented":
            job = self.store.get_job(job_id)
            materials = self._material_sources_for_job(job)
            required_assets = {str(item.get("asset_id") or item.get("local_file") or item.get("source_page"))
                               for item in materials}
            provided_assets = [item.asset_key for item in review.rights_evidence if item.evidence.strip()]
            if not required_assets or len(provided_assets) != len(set(provided_assets)) or set(provided_assets) != required_assets:
                raise AutopilotError("rights review needs exactly one cited license/permission record for every retained video asset key")
        return self.store.record_content_review(job_id, reviewer, review)

    def readiness_report(self, channel_id: str) -> dict:
        profile = self.profile_store.get(channel_id)
        jobs = self.store.list_jobs(channel_id, limit=1)
        latest = jobs[0] if jobs else None
        connection = self.youtube_connections.connection_for_channel(channel_id)
        connection_ok = bool(profile.publishing_backend == "youtube_direct"
            and profile.youtube_connection_id and profile.youtube_channel_id and connection
            and connection.get("state") == "connected"
            and connection.get("connection_id") == profile.youtube_connection_id
            and connection.get("youtube_channel_id") == profile.youtube_channel_id)
        result = {
            "channel_id": channel_id,
            "youtube_connection": "connected" if connection_ok else ("not_configured" if profile.publishing_backend == "youtube_direct" else "not_applicable_upload_post"),
            "ypp_eligibility_metrics": {"status": "unknown", "note": "MPT does not currently read authorized YouTube Analytics/YPP eligibility metrics."},
            "copyright_claims": {"status": "unknown", "note": "The Data API does not reliably expose copyright claim state."},
            "latest_job_id": latest.job_id if latest else None,
            "automatic_public_opt_in": profile.auto_public_enabled,
            "assessment": "human_review_required",
        }
        with self.store._connection() as conn:
            publication = conn.execute("SELECT publisher_backend,receipt_json FROM publications WHERE channel_id=? ORDER BY created_at DESC LIMIT 1",
                                       (channel_id,)).fetchone()
        if publication and publication["publisher_backend"] == "youtube_direct" and publication["receipt_json"]:
            receipt = json.loads(publication["receipt_json"])
            reason = (receipt.get("status") or {}).get("rejectionReason")
            if reason:
                result["copyright_claims"] = {"status": "youtube_reported_rejection", "reason": reason,
                    "note": "This is a reported upload rejection reason, not a complete copyright-claim inventory."}
        if latest:
            content = self.store.content_review(latest.job_id)
            provenance = self._material_provenance(latest)
            result.update({
                "technical_video_qa": "passed" if latest.state == "generated" and self.qa_report(latest.job_id)["technical_checks_passed"] else "unknown_or_failed",
                "original_content_review": content["originality_status"],
                "footage_license_review": content["rights_status"],
                "footage_provenance": provenance["status"],
                "repetition_warnings": self.repetition_report(latest)["warnings"],
            })
        else:
            result.update({"technical_video_qa": "unknown",
                           "original_content_review": "human_review_required", "footage_license_review": "unknown",
                           "footage_provenance": "unknown", "repetition_warnings": []})
        return result

    def review_job(self, job_id: str, decision: str, reviewer: str = "operator", notes: str = "") -> dict:
        report = self.qa_report(job_id)
        if not report["passed"]:
            labels = {
                "artifacts": "video file availability",
                "duration": "duration requirements",
                "subtitles": "subtitle file availability",
                "audio": "audio file availability",
                "material_references": "source media references",
                "orientation": "video orientation",
                "audio_track": "audio track integrity",
            }
            failed_checks = [labels.get(name, name.replace("_", " "))
                             for name, passed in (report.get("checks") or {}).items() if not passed]
            details = ", ".join(failed_checks) if failed_checks else "technical media checks"
            raise AutopilotError(
                f"Approval blocked by video QA ({details}). Open View Details for the full report."
            )
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        job = self.store.get_job(job_id)
        if decision == "approve" and job.profile_snapshot.get("publishing_policy") in {"automatic_public", "scheduled_public"}:
            if not self._public_requirements_pass(job):
                raise AutopilotError("public publishing requires channel opt-in and passing originality, rights, repetition, and video QA gates")
        state = "approved" if decision == "approve" else "rejected"
        with self.store._connection() as conn:
            conn.execute("INSERT INTO job_qa VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET state=excluded.state,report_json=excluded.report_json,reviewed_by=excluded.reviewed_by,reviewed_at=excluded.reviewed_at,updated_at=excluded.updated_at",
                         (job_id, state, json.dumps({**report, "notes": notes[:2000]}), reviewer[:200], _now(), _now()))
        if decision == "approve" and job.profile_snapshot.get("publishing_policy") in {"automatic_public", "scheduled_public"}:
            return self.publish_job(job_id)
        return {"job_id": job_id, "qa_state": state, "report": report}

    def upload_post_destination(self, job: ProductionJob, platform: str) -> dict | None:
        """Return a live, confirmed Upload-Post destination for this job profile."""
        if platform not in {"instagram", "youtube"}:
            return None
        profile_username = (job.profile_snapshot.get("upload_post_username")
                            or config.app.get("upload_post_username", ""))
        api_key = config.app.get("upload_post_api_key", "")
        if not profile_username or not api_key or not config.app.get("upload_post_enabled", False):
            return None
        publisher = UploadPostService({
            "upload_post_api_key": api_key, "upload_post_username": profile_username,
            "upload_post_enabled": True,
        })
        try:
            account = publisher.connected_platform(platform, profile_username)
        except Exception:
            return None
        if not account:
            return None
        return {**account, "account_username": profile_username}

    def publish_job(self, job_id: str, platform: str = "youtube",
                    destination_account_id: str | None = None) -> dict:
        if platform not in {"youtube", "instagram"}:
            raise AutopilotError("unsupported publication platform")
        job = self.store.get_job(job_id)
        profile = job.profile_snapshot
        backend = profile.get("publishing_backend", "upload_post")
        if platform == "instagram" and backend == "youtube_direct":
            backend = "upload_post"
        if platform == "instagram":
            destination = self.upload_post_destination(job, platform)
            if not destination:
                raise AutopilotError("Instagram is not connected to the configured Upload-Post profile")
            resolved_destination_id = destination["username"]
            if destination_account_id and destination_account_id != resolved_destination_id:
                raise AutopilotError("Instagram destination changed; refresh the connected account before publishing")
        elif backend == "youtube_direct":
            resolved_destination_id = profile.get("youtube_channel_id") or ""
        else:
            destination = self.upload_post_destination(job, platform)
            if not destination:
                raise AutopilotError(f"{platform.capitalize()} is not connected to the configured Upload-Post profile")
            resolved_destination_id = destination["username"]

        with self.store._connection() as conn:
            qa = conn.execute("SELECT state,reviewed_by FROM job_qa WHERE job_id=?", (job_id,)).fetchone()
            existing = conn.execute(
                "SELECT * FROM publications WHERE job_id=? AND platform=? AND destination_account_id=?",
                (job_id, platform, resolved_destination_id),
            ).fetchone()
            if existing is None:
                # Rows written by older clients after migration may still carry
                # the schema default destination. Adopt only an unambiguous row.
                existing = conn.execute(
                    "SELECT * FROM publications WHERE job_id=? AND platform=? AND destination_account_id='' AND account_username IS ?",
                    (job_id, platform,
                     profile.get("upload_post_username") or config.app.get("upload_post_username", "")
                     if backend != "youtube_direct" else None),
                ).fetchone()
                if existing is not None and resolved_destination_id:
                    conn.execute("UPDATE publications SET destination_account_id=? WHERE publication_id=?",
                                 (resolved_destination_id, existing["publication_id"]))
        policy = profile.get("publishing_policy", "manual")
        operator_managed = policy in {"manual", "private", "unlisted"}
        qa_ready = bool(qa and (qa["state"] == "approved"
                                or (operator_managed and qa["state"] == "qa_passed")))
        if job.state != "generated" or not qa_ready:
            raise AutopilotError("publication requires a successfully generated video with completed QA")
        # The render pipeline already recorded successful technical QA before
        # setting the job to generated. Manual/private uploads should not rerun
        # that check against transient worker state; unattended public policies
        # still require fresh QA and explicit approval.
        if not operator_managed:
            fresh_qa = self.qa_report(job_id)
            if not fresh_qa["passed"]:
                raise AutopilotError("publication blocked because current artifact QA failed")
        if policy in {"automatic_public", "scheduled_public"}:
            task_state = sm.state.get_task(job.mpt_task_id) if job.mpt_task_id else None
            verification = self._fact_verification_for_job(job, task_state)
            if (verification["status"] != "verified"
                    and not profile.get("allow_unverified_publication", False)):
                raise AutopilotError(
                    "unattended public publishing of unverified facts requires explicit channel opt-in"
                )
            if not self._public_requirements_pass(job):
                raise AutopilotError("public publishing requires channel opt-in and passing originality, rights, repetition, and technical QA checks")
        if policy == "automatic_public" and not profile.get("auto_public_enabled", False):
            raise AutopilotError("automatic public publishing is not explicitly enabled for this channel")
        if not self._job_runnable(job):
            raise AutopilotError("channel is paused; publication is held until it is resumed")
        title, description = self._publication_metadata(job)
        youtube_metadata = None
        if platform == "youtube":
            youtube_metadata = self.ensure_youtube_metadata(job_id)
            title = youtube_metadata["title"]
            description = youtube_metadata["caption"]
        if backend == "youtube_direct":
            if platform != "youtube":
                raise AutopilotError("the direct YouTube connector cannot publish Instagram videos")
            return self._publish_youtube_direct(job, existing, title=title, description=description)
        if platform == "instagram":
            self._validate_instagram_video(job)
        return self._publish_upload_post_platform(
            job, platform, resolved_destination_id, existing, title=title,
            description=description, youtube_metadata=youtube_metadata,
        )

    @staticmethod
    def _validate_instagram_video(job: ProductionJob) -> None:
        if not job.output_artifacts or not os.path.isfile(job.output_artifacts[0]):
            raise AutopilotError("Instagram upload requires a readable completed video")
        path = job.output_artifacts[0]
        try:
            youtube_direct._validate_mp4(path)
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,sample_rate,channels,pix_fmt",
                 "-of", "json", path], capture_output=True, text=True, timeout=20, check=False,
            )
            data = json.loads(probe.stdout) if probe.returncode == 0 else {}
            streams = data.get("streams") or []
            video_stream = next(item for item in streams if item.get("codec_type") == "video")
            audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), None)
            duration = float((data.get("format") or {}).get("duration"))
            numerator, denominator = (video_stream.get("avg_frame_rate") or "0/1").split("/", 1)
            fps = float(numerator) / max(float(denominator), 1.0)
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError, StopIteration) as exc:
            raise AutopilotError("Instagram could not validate this video's media metadata") from exc
        size = os.path.getsize(path)
        width, height = int(video_stream.get("width") or 0), int(video_stream.get("height") or 0)
        ratio = width / height if width and height else 0
        if size > 300 * 1024 * 1024 or not 3 <= duration <= 900:
            raise AutopilotError("Instagram Reels require a 3–900 second video no larger than 300 MB")
        if (not width or not height or max(width, height) > 1920 or not 0.01 <= ratio <= 10
                or not 23 <= fps <= 60):
            raise AutopilotError("Instagram Reels require supported dimensions, aspect ratio, and 23–60 FPS")
        if video_stream.get("codec_name") not in {"h264", "hevc"} or not str(video_stream.get("pix_fmt", "")).startswith("yuv420"):
            raise AutopilotError("Instagram Reels require H.264/HEVC video with 4:2:0 pixel format")
        if (not audio_stream or audio_stream.get("codec_name") != "aac"
                or int(audio_stream.get("sample_rate") or 0) > 48000
                or int(audio_stream.get("channels") or 0) not in {1, 2}):
            raise AutopilotError("Instagram Reels require AAC audio with one or two channels at 48 kHz or below")

    def _publish_upload_post_platform(self, job: ProductionJob, platform: str,
                                      destination_account_id: str, existing,
                                      *, title: str, description: str = "",
                                      youtube_metadata: dict | None = None) -> dict:
        profile = job.profile_snapshot
        username = (profile.get("upload_post_username")
                    or config.app.get("upload_post_username", ""))
        if not username:
            raise AutopilotError("this job has no pinned Upload-Post profile")
        account = {"upload_post_api_key": config.app.get("upload_post_api_key", ""),
                   "upload_post_username": username, "upload_post_enabled": True}
        publisher = UploadPostService(account)
        publication_id = existing["publication_id"] if existing else str(uuid4())
        retry = False
        if existing:
            if existing["state"] in {"published", "scheduled"}:
                return dict(existing)
            if existing["state"] in {"unknown", "uploading"}:
                status = publisher.check_status(existing["provider_request_id"])
                if status.get("status") == "completed":
                    result = _platform_receipt_result(status.get("results"), platform)
                    if result.get("success") is True:
                        resolved_state = "scheduled" if profile.get("publishing_policy") == "scheduled_public" else "published"
                        with self.store._connection() as conn:
                            conn.execute(
                                "UPDATE publications SET state=?,provider_video_id=?,provider_url=?,receipt_json=?,failure_details=NULL,updated_at=? WHERE publication_id=?",
                                (resolved_state, result.get("video_reel_id") or result.get("publish_id") or result.get("post_id") or result.get("id"),
                                 result.get("url") or result.get("post_url"), _safe_receipt(status), _now(), publication_id),
                            )
                        return {"job_id": job.job_id, "platform": platform, "state": resolved_state, "receipt": status}
                if status.get("status") == "failed":
                    with self.store._connection() as conn:
                        conn.execute("UPDATE publications SET state='failed',receipt_json=?,failure_details=?,updated_at=? WHERE publication_id=?",
                                     (_safe_receipt(status), _safe_failure(status.get("message") or "Upload-Post reports failure"), _now(), publication_id))
                raise AutopilotError("prior upload outcome is not confirmed; provider status was checked and no retry was submitted")
            if existing["state"] == "failed":
                status = publisher.check_status(existing["provider_request_id"])
                receipt = json.loads(existing["receipt_json"] or "{}")
                confirmed_failure = status.get("status") == "failed" or (
                    receipt.get("success") is False and not receipt.get("request_id")
                )
                if not confirmed_failure:
                    raise AutopilotError("retry is blocked until Upload-Post confirms the prior request failed")
                retry = True
            elif existing["state"] != "failed":
                return dict(existing)

        policy = profile.get("publishing_policy", "manual")
        schedule_date = None
        if policy == "scheduled_public":
            if not job.intended_publication_slot or job.intended_publication_slot <= datetime.now(timezone.utc):
                raise AutopilotError("scheduled publication requires a future pinned publication slot")
            schedule_date = job.intended_publication_slot.astimezone(
                ZoneInfo(profile.get("timezone", "UTC"))
            ).replace(tzinfo=None).isoformat()
        attempt = (int(existing["attempt_count"]) + 1) if retry else 1
        request_id = hashlib.sha256(
            f"mpt-publication:{job.job_id}:{platform}:{destination_account_id}:{attempt}".encode()
        ).hexdigest()[:32]
        now = _now()
        if not retry:
            try:
                with self.store._connection() as conn:
                    conn.execute("""INSERT INTO publications
                        (publication_id,job_id,channel_id,profile_version,account_username,idempotency_key,
                         provider_request_id,state,provider_video_id,provider_url,receipt_json,failure_details,
                         attempt_count,created_at,updated_at,publisher_backend,platform,destination_account_id,next_check_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'uploading', NULL, NULL, NULL, NULL, 1, ?, ?,
                                'upload_post', ?, ?, ?)""",
                        (publication_id, job.job_id, job.channel_id, job.profile_version, username,
                         request_id, request_id, now, now, platform, destination_account_id,
                         (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()))
            except sqlite3.IntegrityError as exc:
                raise AutopilotError("publication already reserved for this destination; reconcile before retrying") from exc
        else:
            with self.store._connection() as conn:
                cursor = conn.execute("""UPDATE publications SET state='uploading',failure_details=NULL,
                    attempt_count=?,idempotency_key=?,provider_request_id=?,next_check_at=?,updated_at=?
                    WHERE publication_id=? AND state='failed'""",
                    (attempt, request_id, request_id,
                     (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(), now, publication_id))
                if cursor.rowcount != 1:
                    raise AutopilotError("publication retry reservation changed; reconcile before retrying")
        try:
            youtube_extra = None
            if platform == "youtube":
                privacy = {"private": "private", "unlisted": "unlisted",
                           "scheduled_public": "private", "automatic_public": "public"}.get(policy, "private")
                youtube_extra = {
                    "privacyStatus": privacy,
                    "selfDeclaredMadeForKids": bool(profile.get("youtube_made_for_kids", False)),
                    "youtube_title": title[:100],
                    "youtube_description": description,
                    "tags": (youtube_metadata or {}).get("hashtags", []),
                }
            result = publisher.upload_video(
                job.output_artifacts[0], title, platforms=[platform], youtube_extra=youtube_extra,
                request_id=request_id, idempotency_key=request_id,
                scheduled_date=schedule_date, timezone_name=profile.get("timezone"),
                external_id=job.job_id,
            )
            platform_result = _platform_receipt_result(result.get("results"), platform)
            success = result.get("success") is True and platform_result.get("success") is True
            state = (("scheduled" if schedule_date else "published") if success
                     else ("unknown" if result.get("request_id") else "failed"))
            remote_id = (platform_result.get("video_id") or platform_result.get("video_reel_id")
                         or platform_result.get("publish_id") or platform_result.get("post_id")
                         or platform_result.get("id"))
            remote_url = platform_result.get("url") or platform_result.get("post_url")
            next_check = ((job.intended_publication_slot.astimezone(timezone.utc)
                           + timedelta(minutes=5)).isoformat() if state == "scheduled" else
                          (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
                          if state == "unknown" else None)
            with self.store._connection() as conn:
                conn.execute("""UPDATE publications SET state=?,provider_request_id=?,provider_video_id=?,
                    provider_url=?,receipt_json=?,failure_details=?,updated_at=?,provider_schedule_id=?,next_check_at=?
                    WHERE publication_id=?""",
                    (state, result.get("request_id", request_id), remote_id, remote_url, _safe_receipt(result),
                     None if success else _safe_failure(result.get("error") or platform_result.get("error") or "upload failed"),
                     _now(), result.get("job_id") or result.get("schedule_id") if state == "scheduled" else None,
                     next_check, publication_id))
            return {"job_id": job.job_id, "platform": platform, "state": state, "receipt": result}
        except Exception as exc:
            with self.store._connection() as conn:
                conn.execute("UPDATE publications SET state='unknown',failure_details=?,next_check_at=?,updated_at=? WHERE publication_id=?",
                             (_safe_failure(f"{type(exc).__name__}: {exc}"),
                              (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), _now(), publication_id))
            raise AutopilotError("upload outcome is uncertain; reconcile the persisted request before retrying") from exc

    def _publish_youtube_direct(
        self, job: ProductionJob, existing, *, title: str | None = None,
        description: str | None = None,
    ):
        profile = job.profile_snapshot
        connection_id = profile.get("youtube_connection_id")
        expected_channel_id = profile.get("youtube_channel_id")
        if not connection_id or not expected_channel_id:
            raise AutopilotError("this immutable job has no verified YouTube connection and destination")
        if not job.output_artifacts or not os.path.isfile(job.output_artifacts[0]):
            raise AutopilotError("generated video artifact is missing")
        if profile.get("publishing_policy") == "scheduled_public":
            if not job.intended_publication_slot or job.intended_publication_slot <= datetime.now(timezone.utc):
                raise AutopilotError("scheduled public publishing requires a future pinned publication slot")
        try:
            connection = self.youtube_connections.get_connection(connection_id)
        except youtube_direct.YouTubePublisherError as exc:
            raise AutopilotError("pinned YouTube connection is missing or disconnected") from exc
        if connection.get("local_channel_id") != job.channel_id or connection.get("youtube_channel_id") != expected_channel_id:
            raise AutopilotError("pinned YouTube connection is missing, changed, or bound to another destination")
        context = self._direct_session_context(job, connection_id, expected_channel_id, job.output_artifacts[0])

        # Re-read the destination-scoped reservation here; callers may have raced
        # after their initial status read.
        with self.store._connection() as conn:
            current = conn.execute(
                "SELECT * FROM publications WHERE job_id=? AND platform='youtube' AND destination_account_id=?",
                (job.job_id, expected_channel_id),
            ).fetchone()
        existing = current
        publication_id = existing["publication_id"] if existing else str(uuid4())
        retry = False
        if existing:
            if existing["state"] in {"published", "scheduled"}:
                return dict(existing)
            for field, expected in (("channel_id", job.channel_id),
                                    ("youtube_connection_id", connection_id),
                                    ("youtube_channel_id", expected_channel_id)):
                if existing[field] is not None and existing[field] != expected:
                    raise AutopilotError("publication is pinned to a different channel or connection")
            if existing["state"] in {"unknown", "uploading"}:
                if existing["retry_after"] and datetime.fromisoformat(existing["retry_after"]) > datetime.now(timezone.utc):
                    raise AutopilotError("YouTube asked MPT to wait before retrying this resumable session")
                if not existing["resumable_session_url"]:
                    raise AutopilotError("direct upload outcome is uncertain and no resumable session was persisted; inspect YouTube Studio before any new attempt")
                try:
                    session_url, legacy_unverified = self._load_direct_session(
                        existing, job, connection_id, expected_channel_id, job.output_artifacts[0], context)
                    _, access = self.youtube_publisher.access_token(connection_id)
                    result = self.youtube_publisher.resume(session_url, job.output_artifacts[0], access, expected_channel_id,
                                                           session_context=context, legacy_unverified=legacy_unverified,
                                                           on_session=lambda url: self._save_direct_session(job.job_id, url, context))
                    return self._record_direct_receipt(job, result)
                except Exception as exc:
                    retry_after = getattr(exc, "retry_after", None)
                    with self.store._connection() as conn:
                        conn.execute("UPDATE publications SET state='unknown',failure_details=?,retry_after=?,updated_at=? WHERE publication_id=?",
                                     (_safe_failure(f"{type(exc).__name__}: {exc}"),
                                      (datetime.now(timezone.utc) + timedelta(seconds=retry_after)).isoformat() if retry_after else None,
                                      _now(), publication_id))
                    raise AutopilotError("resumable upload remains uncertain; the same session was reconciled and no new upload was started") from exc
            if existing["state"] == "failed":
                if existing["retry_after"] and datetime.fromisoformat(existing["retry_after"]) > datetime.now(timezone.utc):
                    raise AutopilotError("YouTube rate limit is active; retry after the recorded Retry-After time")
                if existing["resumable_session_url"]:
                    with self.store._connection() as conn:
                        conn.execute("UPDATE publications SET state='unknown',failure_details=NULL,updated_at=? WHERE publication_id=?",
                                     (_now(), publication_id))
                    try:
                        session_url, legacy_unverified = self._load_direct_session(
                            existing, job, connection_id, expected_channel_id, job.output_artifacts[0], context)
                        _, access = self.youtube_publisher.access_token(connection_id)
                        result = self.youtube_publisher.resume(session_url, job.output_artifacts[0], access, expected_channel_id,
                                                               session_context=context, legacy_unverified=legacy_unverified,
                                                               on_session=lambda url: self._save_direct_session(job.job_id, url, context))
                        return self._record_direct_receipt(job, result)
                    except Exception as exc:
                        retry_after = getattr(exc, "retry_after", None)
                        with self.store._connection() as conn:
                            conn.execute("UPDATE publications SET state='unknown',failure_details=?,retry_after=?,updated_at=? WHERE publication_id=?",
                                         (_safe_failure(f"{type(exc).__name__}: {exc}"),
                                          (datetime.now(timezone.utc) + timedelta(seconds=retry_after)).isoformat() if retry_after else None,
                                          _now(), publication_id))
                        raise AutopilotError("same resumable session remains uncertain; no new insert was started") from exc
                if not (existing["failure_details"] or "").startswith("confirmed_rejection:"):
                    raise AutopilotError("previous YouTube insert outcome is not confirmed rejected; inspect YouTube Studio before creating another upload")
                # Only a persisted, provider-confirmed rejection with no session URL
                # permits an explicit operator retry to make a fresh videos.insert.
                with self.store._connection() as conn:
                    cursor = conn.execute("UPDATE publications SET state='uploading',failure_details=NULL,retry_after=NULL,attempt_count=attempt_count+1,updated_at=? WHERE publication_id=? AND state='failed' AND resumable_session_url IS NULL",
                                          (_now(), publication_id))
                    if cursor.rowcount != 1:
                        raise AutopilotError("publication retry reservation changed concurrently; reconcile before retrying")
                retry = True
            elif existing["state"] not in {"unknown", "uploading"}:
                return dict(existing)
        request_id = hashlib.sha256(
            f"mpt-publication:{job.job_id}:youtube:{expected_channel_id}".encode()
        ).hexdigest()[:32]
        now = _now()
        if not retry:
            try:
                with self.store._connection() as conn:
                    conn.execute("""INSERT INTO publications
                        (publication_id,job_id,channel_id,profile_version,account_username,idempotency_key,
                         provider_request_id,state,provider_video_id,provider_url,receipt_json,failure_details,
                         attempt_count,created_at,updated_at,publisher_backend,youtube_connection_id,
                         youtube_channel_id,platform,destination_account_id)
                        VALUES (?, ?, ?, ?, NULL, ?, ?, 'uploading', NULL, NULL, NULL, NULL, 1, ?, ?,
                                'youtube_direct', ?, ?, 'youtube', ?)""",
                        (publication_id, job.job_id, job.channel_id, job.profile_version,
                         request_id, request_id, now, now, connection_id, expected_channel_id,
                         expected_channel_id))
            except sqlite3.IntegrityError as exc:
                raise AutopilotError("publication reservation already exists; reconcile it before retrying") from exc
        policy = profile.get("publishing_policy", "manual")
        privacy = {"private": "private", "unlisted": "unlisted", "scheduled_public": "private", "automatic_public": "public"}.get(policy, "private")
        publish_at = job.intended_publication_slot.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if policy == "scheduled_public" and job.intended_publication_slot else None
        try:
            result = self.youtube_publisher.publish(
                connection_id=connection_id, expected_channel_id=expected_channel_id,
                video_path=job.output_artifacts[0], title=title or job.topic,
                description=description or job.script, privacy=privacy,
                made_for_kids=bool(profile.get("youtube_made_for_kids", False)),
                contains_synthetic_media=bool(profile.get("contains_synthetic_media", False)),
                publish_at=publish_at,
                session_context=context,
                on_session=lambda url: self._save_direct_session(job.job_id, url, context),
            )
            return self._record_direct_receipt(job, result)
        except Exception as exc:
            confirmed = bool(getattr(exc, "confirmed", False))
            retry_after = getattr(exc, "retry_after", None)
            failure = _safe_failure(f"{type(exc).__name__}: {exc}")
            if confirmed:
                failure = f"confirmed_rejection: {failure}"
            with self.store._connection() as conn:
                conn.execute("UPDATE publications SET state=?,failure_details=?,retry_after=?,updated_at=? WHERE publication_id=?",
                             ("failed" if confirmed else "unknown", failure,
                              (datetime.now(timezone.utc) + timedelta(seconds=retry_after)).isoformat() if retry_after else None,
                              _now(), publication_id))
            if confirmed:
                raise AutopilotError("YouTube rejected the publication; inspect the recorded failure before retrying") from exc
            raise AutopilotError("YouTube upload outcome is uncertain; the persisted resumable session must be reconciled before retrying") from exc

    @staticmethod
    def _direct_session_context(job: ProductionJob, connection_id: str, channel_id: str,
                                artifact_path: str) -> dict:
        return youtube_direct._session_binding(
            job_id=job.job_id, local_channel_id=job.channel_id, connection_id=connection_id,
            youtube_channel_id=channel_id, artifact_sha256=youtube_direct._artifact_sha256(artifact_path))

    def _load_direct_session(self, row, job: ProductionJob, connection_id: str,
                             channel_id: str, artifact_path: str, context: dict) -> tuple[str, bool]:
        raw = row["resumable_session_url"]
        session, legacy_format, legacy_unverified = self.youtube_connections.decode_session(raw, context)
        if legacy_format:
            # Old sessions have no persisted job/channel/artifact binding. Preserve the
            # remote session in a versioned encrypted envelope, but only reconcile it;
            # never send additional bytes until an operator resolves that unknown.
            migrated = self.youtube_connections.encrypt_session(session, context, legacy_unverified=True)
            with self.store._connection() as conn:
                conn.execute("UPDATE publications SET resumable_session_url=?,updated_at=? WHERE publication_id=? AND resumable_session_url=?",
                             (migrated, _now(), row["publication_id"], raw))
            legacy_unverified = True
        return session, legacy_unverified

    def _save_direct_session(self, job_id: str, session_url: str, context: dict):
        encrypted = self.youtube_connections.encrypt_session(session_url, context)
        with self.store._connection() as conn:
            row = conn.execute("SELECT channel_id,youtube_connection_id,youtube_channel_id,publisher_backend,state FROM publications WHERE job_id=? AND publisher_backend='youtube_direct' AND platform='youtube' AND destination_account_id=?",
                               (job_id, context["youtube_channel_id"])).fetchone()
            if (row is None or row["publisher_backend"] != "youtube_direct" or row["channel_id"] != context["local_channel_id"]
                    or row["youtube_connection_id"] not in (None, context["connection_id"])
                    or row["youtube_channel_id"] not in (None, context["youtube_channel_id"])
                    or row["state"] not in {"uploading", "unknown"}):
                raise AutopilotError("refusing to persist a YouTube session for a different job or destination")
            cursor = conn.execute("UPDATE publications SET resumable_session_url=?,youtube_connection_id=COALESCE(youtube_connection_id,?),youtube_channel_id=COALESCE(youtube_channel_id,?),updated_at=? WHERE job_id=? AND publisher_backend='youtube_direct' AND platform='youtube' AND destination_account_id=? AND state IN ('uploading','unknown')",
                         (encrypted, context["connection_id"], context["youtube_channel_id"], _now(), job_id,
                          context["youtube_channel_id"]))
            if cursor.rowcount != 1:
                raise AutopilotError("publication changed before its YouTube session could be persisted")

    def _record_direct_receipt(self, job: ProductionJob, result: dict) -> dict:
        if (not isinstance(result, dict)
                or not isinstance(result.get("snippet"), dict)
                or result["snippet"].get("channelId") != job.profile_snapshot.get("youtube_channel_id")
                or not isinstance(result.get("id"), str) or not result["id"]):
            raise AutopilotError("YouTube receipt does not match the immutable job destination")
        video_id = result["id"]
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        state = "scheduled" if job.profile_snapshot.get("publishing_policy") == "scheduled_public" else "published"
        receipt = {"id": video_id, "snippet": result.get("snippet", {}), "status": result.get("status", {})}
        next_check = (job.intended_publication_slot.astimezone(timezone.utc) + timedelta(minutes=5)).isoformat() if state == "scheduled" and job.intended_publication_slot else None
        with self.store._connection() as conn:
            youtube_channel_id = job.profile_snapshot.get("youtube_channel_id")
            cursor = conn.execute("UPDATE publications SET state=?,provider_video_id=?,provider_url=?,receipt_json=?,failure_details=NULL,next_check_at=?,youtube_connection_id=COALESCE(youtube_connection_id,?),youtube_channel_id=COALESCE(youtube_channel_id,?),updated_at=? WHERE job_id=? AND publisher_backend='youtube_direct' AND platform='youtube' AND destination_account_id=? AND channel_id=? AND (youtube_connection_id IS NULL OR youtube_connection_id=?) AND (youtube_channel_id IS NULL OR youtube_channel_id=?)",
                         (state, video_id, video_url, _safe_receipt(receipt), next_check,
                          job.profile_snapshot.get("youtube_connection_id"), job.profile_snapshot.get("youtube_channel_id"),
                          _now(), job.job_id, youtube_channel_id, job.channel_id, job.profile_snapshot.get("youtube_connection_id"),
                          job.profile_snapshot.get("youtube_channel_id")))
            if cursor.rowcount != 1:
                raise AutopilotError("YouTube receipt could not be bound to its immutable publication record")
        return {"job_id": job.job_id, "state": state, "video_id": video_id, "url": video_url}

    def _public_requirements_pass(self, job: ProductionJob) -> bool:
        if not job.profile_snapshot.get("auto_public_enabled", False):
            return False
        content = self.store.content_review(job.job_id)
        if content.get("originality_status") != "original" or content.get("rights_status") != "documented":
            return False
        try:
            qa = self.qa_report(job.job_id)
        except (InvalidTransition, LookupError):
            return False
        return bool(qa.get("technical_checks_passed") and not qa.get("repetition_warnings"))

    def _record_publication_preflight_failure(self, job: ProductionJob, username: str | None, reason: str):
        request_id = hashlib.sha256(f"mpt-autopilot:{job.job_id}".encode()).hexdigest()[:32]
        now = _now()
        with self.store._connection() as conn:
            conn.execute("INSERT OR IGNORE INTO publications (publication_id,job_id,channel_id,profile_version,account_username,idempotency_key,provider_request_id,state,provider_video_id,provider_url,receipt_json,failure_details,attempt_count,created_at,updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', NULL, NULL, NULL, ?, 0, ?, ?)",
                         (str(uuid4()), job.job_id, job.channel_id, job.profile_version, username, request_id, request_id, reason, now, now))

    def draft_script(self, profile: ChannelProfile, topic: str) -> str:
        settings = resolve_generation_settings(channel_profile=profile, topic=topic)
        with config.runtime_config_lock():
            result = llm.generate_script(
                video_subject=topic,
                language=profile.language,
                paragraph_number=1,
                video_script_prompt=_script_prompt(
                    profile, topic, settings.effective_visual_style
                ),
                visual_style=settings.effective_visual_style,
                narrator_preset=settings.narrator_preset,
                profanity_level=settings.profanity_level,
                savage_quality_policy=getattr(profile, "savage_quality_policy", "advisory"),
            )
        if not result or result.startswith("Error: "):
            if script_quality.parse_failed_draft(str(result or "")):
                raise AutopilotError(str(result))
            if isinstance(result, str) and result.startswith("Error: "):
                detail = result.removeprefix("Error: ").strip()
                if detail.startswith((
                    "video_script_prompt exceeds ",
                    "custom_system_prompt exceeds ",
                )):
                    raise AutopilotError(detail)
            raise AutopilotError("script drafting failed; check the configured MPT text provider")
        return result.strip()

    def _manual_profile(self, channel_id: str, overrides: dict) -> ChannelProfile:
        allowed = {
            "target_duration_seconds", "video_aspect", "stock_video_style", "visual_style",
            "stock_media_policy",
            "language", "font_name", "narrator_preset", "profanity_level", "video_source",
            "asmr_categories", "tts_provider",
            "savage_quality_policy",
            "fact_verification_policy", "duration_enforcement", "hindi_alignment_policy",
            "bgm_type", "bgm_file", "bgm_volume", "voice_name", "tts_model",
            "subtitle_enabled", "subtitle_position", "subtitle_display_mode",
            "subtitle_animation", "voice_rate", "voice_volume", "allow_unverified_publication",
            "intro_enabled", "outro_enabled",
            "channel_branding",
        }
        if set(overrides) - allowed:
            raise AutopilotError("unsupported manual video setting")
        profile = self.profile_store.get(channel_id)
        if not profile.enabled:
            raise AutopilotError("channel profile is disabled")
        profile_values = {**profile.model_dump(mode="json"), **overrides}
        pinned_branding = overrides.get("channel_branding")
        if isinstance(pinned_branding, dict):
            for segment in ("intro", "outro"):
                item = pinned_branding.get(segment)
                if isinstance(item, dict):
                    profile_values[f"{segment}_enabled"] = bool(item.get("enabled"))
                    profile_values[f"{segment}_text"] = str(item.get("text") or "")
                    profile_values[f"{segment}_audio"] = item.get("audio")
        profile_values.pop("channel_branding", None)
        if "tts_provider" in overrides:
            settings = dict(profile_values.get("generation_settings") or {})
            settings["tts_provider"] = overrides["tts_provider"]
            profile_values["generation_settings"] = settings
            # The public task override is flattened for the caller; the saved
            # channel schema stores provider choice in generation_settings.
            profile_values.pop("tts_provider", None)
        return ChannelProfile.model_validate({
            **profile_values,
            "publishing_policy": "manual", "auto_public_enabled": False,
        })

    def list_manual_drafts(self, channel_id: str) -> list[ContentPlan]:
        """Include retained plans whose deleted manual job no longer exists."""
        with self.store._connection() as conn:
            rows = conn.execute("SELECT p.* FROM content_plans p WHERE p.channel_id=? "
                                "AND p.intended_publication_slot IS NULL AND NOT EXISTS "
                                "(SELECT 1 FROM production_jobs j WHERE j.plan_id=p.plan_id) "
                                "ORDER BY p.created_at DESC", (channel_id,)).fetchall()
        return [self.store._plan_from_row(row) for row in rows
                if json.loads(row["profile_json"]).get("publishing_policy") == "manual"
                and row["state"] in {"planned", "ready", "job_created"}]

    def prepare_manual_draft(self, channel_id: str, plan_id: str,
                             overrides: dict, script: str = "") -> ContentPlan:
        """Apply current channel settings to an unstarted draft before job creation."""
        profile = self._manual_profile(channel_id, overrides)
        target = profile.target_duration_seconds
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM content_plans WHERE plan_id=? AND channel_id=?",
                               (plan_id, channel_id)).fetchone()
            if row is None:
                raise LookupError("content plan not found")
            if (row["state"] not in {"planned", "ready", "job_created"} or row["intended_publication_slot"]
                    or json.loads(row["profile_json"]).get("publishing_policy") != "manual"
                    or conn.execute("SELECT 1 FROM production_jobs WHERE plan_id=?", (plan_id,)).fetchone()):
                raise InvalidTransition("only an unstarted, unscheduled draft can be reused")
            narration = script.strip() or row["script"]
            changed = narration != row["script"]
            if changed:
                ContentPlanCreate(topic=row["topic"], script=narration)
                self.store._assert_unique_content(conn, channel_id, row["topic"], narration,
                                                  bool(row["cross_channel_check"]), ignore_plan_id=plan_id)
            estimate = estimate_narration_duration(narration, profile.language, profile.voice_rate)
            requested_style = overrides.get("visual_style", row["visual_style_override"] or profile.visual_style)
            resolved_style = resolve_generation_settings(
                task_override={**overrides, "visual_style": requested_style},
                channel_profile=profile, topic=row["topic"],
            )
            effective_style = resolved_style.effective_visual_style
            style_reason = resolved_style.visual_style_reason
            conn.execute("UPDATE content_plans SET profile_version=?,profile_json=?,duration_estimate_seconds=?,"
                         "min_duration_seconds=?,max_duration_seconds=?,visual_style_override=?,effective_visual_style=?,"
                         "visual_style_reason=?,updated_at=?,script=?,script_fingerprint=?,state=? "
                         "WHERE plan_id=? AND channel_id=?",
                         (profile.version, profile.model_dump_json(), estimate, max(1, target - 5),
                          target, overrides.get("visual_style", row["visual_style_override"] or ""),
                          effective_style, style_reason, _now(), narration,
                          fingerprint(narration) if changed else row["script_fingerprint"],
                          "planned" if changed or row["state"] == "job_created" else row["state"],
                          plan_id, channel_id))
        return self.store.get_plan(plan_id, channel_id)

    def plan(self, channel_id: str, payload: ContentPlanCreate,
             manual_overrides: dict | None = None,
             source_job_id: str | None = None) -> ContentPlan:
        channel_profile = self.profile_store.get(channel_id)
        profile = (self._manual_profile(channel_id, manual_overrides)
                   if manual_overrides is not None else channel_profile)
        if not profile.enabled:
            raise AutopilotError("channel profile is disabled")
        topic = payload.topic.strip()
        if len(topic) < 2:
            raise AutopilotError("topic must contain at least two non-space characters")
        _duration_bounds(profile, payload)
        cross_channel_duplicates = (
            profile.check_cross_channel_duplicates
            if payload.check_cross_channel_duplicates is None
            else payload.check_cross_channel_duplicates
        )
        source_plan_id = None
        if source_job_id:
            source = self.store.get_job(source_job_id)
            if source.channel_id != channel_id:
                raise LookupError("source video not found for this channel")
            source_plan_id = source.plan_id
        self.store.assert_topic_available(
            channel_id, topic, cross_channel_duplicates, ignore_plan_id=source_plan_id
        )
        # Resolve before an empty-script draft so a plan style override is
        # already present in the prompt. Explicit manual task settings retain
        # priority over plan, profile, and process defaults.
        settings = resolve_generation_settings(
            task_override=manual_overrides,
            plan_override={"visual_style": payload.visual_style_override},
            channel_profile=channel_profile,
            topic=topic,
        )
        profile = profile.model_copy(update={
            "visual_style": settings.visual_style,
            "narrator_preset": settings.narrator_preset,
            "profanity_level": settings.profanity_level,
            "voice_name": settings.voice_name,
            "tts_model": settings.tts_model,
            "voice_rate": settings.voice_rate,
            "voice_volume": settings.voice_volume,
        })
        script = payload.script.strip() or self.draft_script(profile, topic)
        if not script:
            raise AutopilotError("script is required")
        return self.store.create_plan(payload.model_copy(update={
            "topic": topic,
            "visual_style_override": (
                settings.visual_style if payload.visual_style_override is not None else None
            ),
            "check_cross_channel_duplicates": cross_channel_duplicates,
        }), profile, script, source_job_id=source_job_id)

    def ready(self, channel_id: str, plan_id: str,
              accept_duration_estimate: bool = False) -> ContentPlan:
        return self.store.mark_plan_ready(plan_id, channel_id, accept_duration_estimate)

    def create_job(self, channel_id: str, plan_id: str,
                   parent_job_id: str | None = None,
                   video_terms: str | list[str] | None = None) -> ProductionJob:
        return self.store.create_job(
            plan_id, channel_id, self.profile_store, parent_job_id, video_terms
        )

    def start_job(self, job_id: str) -> ProductionJob:
        job = self.store.get_job(job_id)
        if not self._job_runnable(job):
            raise AutopilotError("channel is disabled or paused; resume it before starting this job")
        self.store.queue(job_id)
        with self._submit_lock:
            if job_id not in self._active_submissions:
                self._active_submissions.add(job_id)
                try:
                    self._executor.submit(self._run_scheduled_job, job_id)
                except Exception as exc:
                    self._active_submissions.discard(job_id)
                    current = self.store.get_job(job_id)
                    if current.state == "queued":
                        self.store.transition(job_id, "failed", failure_details=f"queue submission failed: {type(exc).__name__}")
                    raise AutopilotError("production job could not be submitted to the worker") from exc
        return self.store.get_job(job_id)

    def _run_scheduled_job(self, job_id: str):
        try:
            self._runner(job_id)
        finally:
            with self._submit_lock:
                self._active_submissions.discard(job_id)

    def retry_job(self, job_id: str) -> ProductionJob:
        job = self.store.get_job(job_id)
        if job.state != "failed":
            raise InvalidTransition("only failed jobs can be retried")
        self.store.transition(job_id, "ready", failure_details=None)
        return self.start_job(job_id)

    def failed_manual_audio_job(self, channel_id: str, topic: str, script: str = "") -> ProductionJob | None:
        """Find an exact-topic manual audio failure eligible for settings-based recovery."""
        normalized_topic = normalize_content(topic)
        for job in self.store.list_jobs(channel_id, 100):
            if (job.state != "failed" or job.profile_snapshot.get("publishing_policy") != "manual"
                    or normalize_content(job.topic) != normalized_topic
                    or (script.strip() and script.strip() != job.script)):
                continue
            task_state = sm.state.get_task(job.mpt_task_id) if job.mpt_task_id else None
            if task_state and task_state.get("failed_stage") == "audio":
                return job
        return None

    def failed_manual_job(self, channel_id: str, topic: str, script: str = "") -> ProductionJob | None:
        receipts = {item["job_id"] for item in self.store.list_publications(channel_id, 1000)}
        for job in self.store.list_jobs(channel_id, 500):
            if (job.state == "failed" and job.profile_snapshot.get("publishing_policy") == "manual"
                    and not job.intended_publication_slot and not job.output_artifacts
                    and job.job_id not in receipts and normalize_content(job.topic) == normalize_content(topic)
                    and (not script.strip() or script.strip() == job.script)):
                return job
        return None

    def resume_manual_job(self, job_id: str) -> ProductionJob:
        job = self.store.get_job(job_id)
        eligible = self.failed_manual_job(job.channel_id, job.topic, job.script)
        if eligible is None or eligible.job_id != job_id:
            raise InvalidTransition("this failed video cannot be resumed safely from Create Video")
        task_state = sm.state.get_task(job.mpt_task_id) if job.mpt_task_id else None
        if task_state and task_state.get("failed_stage") == "audio":
            from app.services.channel_profiles import resolve_profile_voice
            current_voice = resolve_profile_voice(self.profile_store.get(job.channel_id))
            if current_voice != job.profile_snapshot.get("voice_name"):
                return self.retry_with_current_channel_settings(job_id)
        return self.retry_job(job_id)

    def retry_with_stock_footage(self, job_id: str) -> ProductionJob:
        """Operator-approved fallback for scarce ASMR stock; keep the pinned profile."""
        failed = self.store.get_job(job_id)
        if (failed.state != "failed"
                or failed.profile_snapshot.get("stock_video_style") != "satisfying_asmr_cutting"
                or "Insufficient distinct ASMR footage" not in (failed.failure_details or "")):
            raise InvalidTransition("stock fallback is available only for failed ASMR footage shortages")
        profile = ChannelProfile.model_validate(failed.profile_snapshot).model_copy(update={
            "stock_video_style": "default", "visual_style": "relevant_stock",
            "publishing_policy": "manual", "auto_public_enabled": False,
        })
        plan = self.store.create_manual_settings_retry_plan(job_id, profile)
        replacement = self.create_job(failed.channel_id, plan.plan_id)
        return self.start_job(replacement.job_id)

    def retry_with_current_channel_settings(self, job_id: str) -> ProductionJob:
        """Create a separate manual job when an operator corrected channel settings."""
        failed = self.store.get_job(job_id)
        task_state = sm.state.get_task(failed.mpt_task_id) if failed.mpt_task_id else None
        if not task_state or task_state.get("failed_stage") != "audio":
            raise AutopilotError("this recovery action is only available for a recorded audio-stage failure")
        profile = self.profile_store.get(failed.channel_id)
        if not profile.enabled:
            raise AutopilotError("enable the channel profile before creating a replacement attempt")
        from app.services.channel_profiles import resolve_profile_voice
        profile = profile.model_copy(update={
            "voice_name": resolve_profile_voice(profile),
            "publishing_policy": "manual",
            "auto_public_enabled": False,
        })
        if profile.publishing_backend == "youtube_direct":
            from app.services.channel_profiles import verify_publisher_binding
            verify_publisher_binding(profile)
        plan = self.store.create_manual_settings_retry_plan(job_id, profile)
        replacement = self.create_job(failed.channel_id, plan.plan_id)
        return self.start_job(replacement.job_id)

    def cancel_job(self, job_id: str) -> ProductionJob:
        return self.store.transition(job_id, "cancelled")

    def _run_job(self, job_id: str):
        candidate = self.store.get_job(job_id)
        if not self._job_runnable(candidate):
            return
        # Pinned retries keep their task directory and validated audio/timing caches.
        task_id = candidate.mpt_task_id or str(uuid4())
        job = self.store.claim(job_id, task_id, f"{socket.gethostname()}:{os.getpid()}")
        if job is None:
            return
        artifacts: list[str] = []
        try:
            params = TaskVideoRequest.model_validate(job.params_snapshot)
            sm.state.update_task(
                task_id, state=const.TASK_STATE_PROCESSING, progress=0, video_subject=job.topic,
                autopilot_job_id=job.job_id,
            )
            # The MPT pipeline reads process-wide provider and voice settings.
            # Hold the same lock as WebUI generation so those settings cannot
            # change halfway through an Autopilot job.
            with config.runtime_config_lock():
                task_service.start(task_id, params, suppress_auto_upload=True)
            task_state = sm.state.get_task(task_id) or {}
            try:
                self.store.save_fact_verification(job_id, task_state)
            except Exception as exc:
                logger.warning(
                    "fact verification outcome could not be saved to job snapshot: job_id={}, error_type={}",
                    job_id, type(exc).__name__,
                )
            try:
                manifest = Path(utils.task_dir(task_id)) / "script.json"
                selection = json.loads(manifest.read_text(encoding="utf-8")).get(
                    "visual_style_selection", {}
                )
                self.store.save_visual_selection(job_id, selection)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                logger.warning(
                    f"visual selection could not be saved to job snapshot: job_id={job_id}, error={exc}"
                )
            artifacts = list(task_state.get("videos") or [])
            artifacts += list(task_state.get("combined_videos") or [])
            artifacts = list(dict.fromkeys(str(item) for item in artifacts if item))
            if task_state.get("state") != const.TASK_STATE_COMPLETE or not artifacts:
                details = _safe_failure(task_state.get("error") or "MPT generation did not produce output artifacts")
                saved_artifacts = list(dict.fromkeys(job.output_artifacts + artifacts))
                self.store.transition(job_id, "failed", failure_details=str(details),
                                      output_artifacts_json=json.dumps(saved_artifacts))
                return
            durations = [probe_video_duration(path) for path in artifacts]
            duration = durations[0]
            outside_range = any(
                not job.min_duration_seconds <= value <= job.max_duration_seconds
                for value in durations
            )
            if outside_range and job.duration_enforcement == "warn_only":
                logger.warning(
                    "Autopilot rendered duration differs from requested range: "
                    "job_id={}, requested={}–{}s, durations={}",
                    job_id, job.min_duration_seconds, job.max_duration_seconds, durations,
                )
            if outside_range and job.duration_enforcement == "strict":
                details = (
                    f"rendered duration {duration:.2f}s is outside the requested "
                    f"{job.min_duration_seconds:.2f}–{job.max_duration_seconds:.2f}s range; "
                    "artifact retained; no automatic rerender was attempted"
                )
                saved_artifacts = list(dict.fromkeys(job.output_artifacts + artifacts))
                self.store.transition(job_id, "failed", failure_details=details,
                                      output_artifacts_json=json.dumps(saved_artifacts),
                                      actual_duration_seconds=duration,
                                      duration_outside_requested_range=1)
                return
            saved_artifacts = list(dict.fromkeys(job.output_artifacts + artifacts))
            generated = self.store.transition(job_id, "generated", output_artifacts_json=json.dumps(saved_artifacts),
                                              actual_duration_seconds=duration,
                                              duration_outside_requested_range=int(outside_range))
            try:
                report = self.qa_report(job_id)
            except Exception as exc:
                self.store.transition(job_id, "failed", failure_details=(
                    f"post-generation QA could not complete: {_safe_failure(f'{type(exc).__name__}: {exc}')}; "
                    "output artifacts retained"
                ))
                return
            if report["passed"]:
                try:
                    self.ensure_youtube_metadata(job_id)
                except Exception as exc:
                    logger.warning(
                        "YouTube caption preparation deferred until upload: job_id={}, error_type={}",
                        job_id, type(exc).__name__,
                    )
            with self.store._connection() as conn:
                conn.execute("INSERT OR REPLACE INTO job_qa VALUES (?, ?, ?, NULL, NULL, ?)",
                             (job_id, "qa_passed" if report["passed"] else "qa_failed", json.dumps(report), _now()))
            if not report["passed"]:
                failed = [name for name, passed in report["checks"].items() if not passed]
                self.store.transition(job_id, "failed", failure_details=(
                    "post-generation QA failed: " + ", ".join(failed) + "; output artifacts retained"
                ))
                return
            policy = generated.profile_snapshot.get("publishing_policy")
            auto_private = policy in {"private", "unlisted"}
            auto_public = policy in {"automatic_public", "scheduled_public"}
            auto_public_allowed = bool(report.get("automatic_publication_allowed"))
            if (report["passed"] and (auto_private or (auto_public and auto_public_allowed))
                    and self._channel_runnable(generated.channel_id)):
                with self.store._connection() as conn:
                    conn.execute("UPDATE job_qa SET state='approved',reviewed_by='automatic-policy-after-qa',reviewed_at=?,updated_at=? WHERE job_id=?",
                                 (_now(), _now(), job_id))
                self.publish_job(job_id)
            elif report["passed"] and auto_public and not auto_public_allowed:
                verification = report.get("fact_verification", {})
                logger.warning(
                    "automatic public upload withheld: fact status={} opt_in={}",
                    verification.get("status", "unverified"),
                    bool(generated.profile_snapshot.get("allow_unverified_publication", False)),
                )
        except Exception as exc:
            logger.error(f"autopilot generation failed, job_id={job_id}, error_type={type(exc).__name__}")
            try:
                current = self.store.get_job(job_id)
                if current.state == "generating":
                    self.store.transition(job_id, "failed",
                                          failure_details=_safe_failure(f"{type(exc).__name__}: {exc}"),
                                          output_artifacts_json=json.dumps(list(dict.fromkeys(job.output_artifacts + artifacts))))
            except Exception:
                logger.exception(f"failed to persist Autopilot job failure, job_id={job_id}")


_service: AutopilotService | None = None
_service_lock = threading.Lock()


def get_service() -> AutopilotService:
    global _service
    with _service_lock:
        if _service is None:
            _service = AutopilotService()
        return _service
