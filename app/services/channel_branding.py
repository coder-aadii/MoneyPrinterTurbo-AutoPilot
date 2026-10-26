"""Immutable channel-scoped intro/outro media assets."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.services import voice
from app.utils import utils

MAX_BRANDING_AUDIO_BYTES = 20 * 1024 * 1024


def _root(channel_id: str) -> Path:
    # Hashing keeps user-controlled channel identifiers out of filesystem paths.
    if not isinstance(channel_id, str) or not channel_id or len(channel_id) > 128:
        raise ValueError("invalid channel identifier")
    return Path(utils.storage_dir(os.path.join(
        "autopilot", "channel-branding", hashlib.sha256(
            channel_id.encode("utf-8")).hexdigest()
    ), create=True)).resolve()


def resolve_asset(channel_id: str, key: str, *, expected_kind: str | None = None) -> Path:
    if expected_kind not in {None, "intro", "outro"}:
        raise ValueError("invalid branding segment")
    if not isinstance(key, str) or not re.fullmatch(
        r"(?:intro|outro)/v[1-9][0-9]*-[a-f0-9]{64}\.(?:mp3|wav)(?:\.srt)?", key
    ):
        raise ValueError("invalid channel branding asset reference")
    if expected_kind and not key.startswith(expected_kind + "/"):
        raise ValueError("branding asset does not belong to this segment")
    root = _root(channel_id)
    result = (root / key).resolve()
    if not result.is_relative_to(root) or not result.is_file():
        raise FileNotFoundError("saved channel branding audio is unavailable")
    return result


def save_asset(channel_id: str, kind: str, content: bytes, *, extension: str,
               version: int, provider: str = "upload", model: str = "",
               voice_name: str = "", request_hash: str = "",
               cues: bytes | None = None) -> dict:
    if kind not in {"intro", "outro"} or extension.lower() not in {"mp3", "wav"}:
        raise ValueError("branding audio must be an MP3 or WAV file")
    if not content or len(content) > MAX_BRANDING_AUDIO_BYTES:
        raise ValueError("branding audio must be non-empty and no larger than 20 MB")
    root = _root(channel_id)
    segment_dir = root / kind
    segment_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    key = f"{kind}/v{version}-{digest}.{extension.lower()}"
    target = (root / key).resolve()
    if not target.is_relative_to(root):
        raise ValueError("invalid channel branding path")
    if not target.exists():
        fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=segment_dir)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    duration = voice.get_audio_duration(str(target))
    if duration <= 0:
        target.unlink(missing_ok=True)
        raise ValueError("branding audio could not be decoded or has zero duration")
    cue_key = None
    if cues:
        cue_key = key + ".srt"
        cue_path = root / cue_key
        cue_path.write_bytes(cues)
    return {
        "asset_key": key, "content_hash": digest, "duration_seconds": round(duration, 4),
        "provider": provider, "model": model, "voice": voice_name,
        "request_hash": request_hash or None,
        "version": version, "updated_at": datetime.now(timezone.utc).isoformat(),
        "subtitle_key": cue_key,
    }


def read_subtitle(channel_id: str, asset: dict | None, kind: str) -> str | None:
    key = (asset or {}).get("subtitle_key")
    if not key:
        return None
    if not isinstance(key, str) or not key.endswith(".srt"):
        raise ValueError("invalid branding subtitle reference")
    return str(resolve_asset(channel_id, key, expected_kind=kind))


def offset_srt(source: str, offset: float) -> str:
    """Shift every SRT cue by a measured segment offset."""
    import re

    def shift(match):
        hours, minutes, seconds, millis = map(int, match.groups())
        total = ((hours * 60 + minutes) * 60 + seconds) + millis / 1000 + offset
        total_ms = max(0, round(total * 1000))
        h, remainder = divmod(total_ms, 3_600_000)
        m, remainder = divmod(remainder, 60_000)
        s, ms = divmod(remainder, 1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"

    return re.sub(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", shift,
                  Path(source).read_text(encoding="utf-8"))
