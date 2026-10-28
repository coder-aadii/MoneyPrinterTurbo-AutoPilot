"""Immutable channel-scoped intro/outro media assets."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.services import voice
from app.utils import utils

MAX_BRANDING_AUDIO_BYTES = 20 * 1024 * 1024
MAX_BRANDING_SUBTITLE_BYTES = 1024 * 1024
MAX_BRANDING_VIDEO_BYTES = 100 * 1024 * 1024
MAX_PROFILE_IMAGE_BYTES = 10 * 1024 * 1024


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


def _resolve_hashed_asset(channel_id: str, key: str, *, kind: str,
                          extensions: tuple[str, ...]) -> Path:
    if kind not in {"profile", "intro", "outro"}:
        raise ValueError("invalid channel branding asset kind")
    extension_pattern = "|".join(re.escape(item) for item in extensions)
    if not isinstance(key, str) or not re.fullmatch(
        rf"{kind}/v[1-9][0-9]*-[a-f0-9]{{64}}\.(?:{extension_pattern})", key,
    ):
        raise ValueError("invalid channel branding asset reference")
    root = _root(channel_id)
    result = (root / key).resolve()
    if not result.is_relative_to(root) or not result.is_file():
        raise FileNotFoundError("saved channel branding asset is unavailable")
    return result


def resolve_video(channel_id: str, key: str, *, expected_kind: str) -> Path:
    return _resolve_hashed_asset(
        channel_id, key, kind=expected_kind, extensions=("mp4",),
    )


def resolve_profile_image(channel_id: str, key: str) -> Path:
    return _resolve_hashed_asset(
        channel_id, key, kind="profile", extensions=("png",),
    )


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
        if len(cues) > MAX_BRANDING_SUBTITLE_BYTES:
            raise ValueError("branding subtitles must be no larger than 1 MB")
        try:
            cue_text = cues.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("branding subtitles must be UTF-8 SRT text") from exc
        if not re.search(r"\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3}", cue_text):
            raise ValueError("branding subtitle file must contain timed SRT cues")
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


def save_profile_image(channel_id: str, content: bytes, *, version: int) -> dict:
    """Validate and store a bounded channel image as an immutable PNG."""
    if not content or len(content) > MAX_PROFILE_IMAGE_BYTES:
        raise ValueError("channel profile images must be non-empty and no larger than 10 MB")
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(content)) as source:
            if source.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("channel profile image must be PNG, JPEG, or WebP")
            if source.width > 8192 or source.height > 8192 or source.width * source.height > 40_000_000:
                raise ValueError("channel profile image dimensions are too large")
            image = ImageOps.exif_transpose(source).convert("RGBA")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("channel profile image could not be decoded") from exc
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    normalized = output.getvalue()
    digest = hashlib.sha256(normalized).hexdigest()
    key = f"profile/v{max(1, int(version))}-{digest}.png"
    root = _root(channel_id)
    target = (root / key).resolve()
    if not target.is_relative_to(root):
        raise ValueError("invalid channel profile image path")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not target.exists():
        fd, temporary = tempfile.mkstemp(prefix=".profile-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(normalized)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {
        "asset_key": key, "content_hash": digest, "version": max(1, int(version)),
        "mime_type": "image/png", "width": image.width, "height": image.height,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def save_video(channel_id: str, kind: str, content: bytes, *, version: int,
               audio_hash: str, subtitle_hash: str, avatar_hash: str = "",
               aspect: str = "16:9") -> dict:
    if kind not in {"intro", "outro"} or not content or len(content) > MAX_BRANDING_VIDEO_BYTES:
        raise ValueError("branding video must be a non-empty MP4 no larger than 100 MB")
    if aspect not in {"9:16", "16:9"}:
        raise ValueError("branding video aspect must be vertical 9:16 or horizontal 16:9")
    digest = hashlib.sha256(content).hexdigest()
    root = _root(channel_id)
    segment_dir = root / kind
    segment_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = f"{kind}/v{max(1, int(version))}-{digest}.mp4"
    target = (root / key).resolve()
    if not target.is_relative_to(root):
        raise ValueError("invalid channel branding video path")
    if not target.exists():
        fd, temporary = tempfile.mkstemp(prefix=".video-", suffix=".mp4", dir=segment_dir)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=format_name,duration", "-show_entries", "stream=codec_type",
                 "-of", "json", temporary],
                capture_output=True, text=True, timeout=15, check=False,
            )
            try:
                media = json.loads(probe.stdout) if probe.returncode == 0 else {}
                duration = float(media.get("format", {}).get("duration", 0))
                streams = {stream.get("codec_type") for stream in media.get("streams", [])}
                container = str(media.get("format", {}).get("format_name", ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                duration, streams, container = 0.0, set(), ""
            if (not math.isfinite(duration) or duration <= 0
                    or not {"video", "audio"}.issubset(streams)
                    or not ({"mp4", "mov"} & set(container.split(",")))):
                raise ValueError("branding video must be a readable MP4 with video and audio")
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {
        "asset_key": key, "content_hash": digest,
        "audio_hash": audio_hash, "subtitle_hash": subtitle_hash,
        "avatar_hash": avatar_hash or None, "version": max(1, int(version)),
        "aspect": aspect,
        "layout_version": 2,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def generate_video(channel_id: str, kind: str, audio: dict, *, display_name: str,
                   aspect: str, profile_image: dict | None = None,
                   output_path: str, version: int = 1) -> dict:
    """Render a local, audio-bearing branded segment using saved audio and SRT."""
    if kind not in {"intro", "outro"}:
        raise ValueError("invalid branding segment")
    if aspect not in {"9:16", "16:9"}:
        raise ValueError("branding video aspect must be vertical 9:16 or horizontal 16:9")
    audio_path = resolve_asset(channel_id, str((audio or {}).get("asset_key") or ""),
                               expected_kind=kind)
    audio_bytes = audio_path.read_bytes()
    audio_hash = hashlib.sha256(audio_bytes).hexdigest()
    if audio_hash != (audio or {}).get("content_hash"):
        raise ValueError("saved branding audio failed its content-hash check")
    subtitle_path = read_subtitle(channel_id, audio, kind)
    if not subtitle_path:
        raise ValueError("save a matching SRT subtitle file before generating branding video")
    from app.utils.subtitle_reader import file_to_subtitles
    records = file_to_subtitles(subtitle_path)
    if not records:
        raise ValueError("saved branding subtitles contain no timed cues")
    subtitle_bytes = Path(subtitle_path).read_bytes()
    subtitle_hash = hashlib.sha256(subtitle_bytes).hexdigest()
    duration = voice.get_audio_duration(str(audio_path))
    if duration <= 0:
        raise ValueError("saved branding audio has zero duration")
    cues = []
    for _index, timing, text in records:
        stamps = re.findall(r"(\d{2}:\d{2}:\d{2},\d{3})", timing)
        if len(stamps) != 2 or not text.strip():
            continue
        start, end = (_srt_seconds(value) for value in stamps)
        if 0 <= start < end <= duration + 0.05:
            cues.append((start, min(end, duration), text.strip()))
    if not cues:
        raise ValueError("saved branding subtitles do not align with the audio duration")

    image_path = None
    avatar_hash = ""
    if kind == "outro" and profile_image:
        image_path = resolve_profile_image(
            channel_id, str(profile_image.get("asset_key") or ""),
        )
        image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if image_hash != profile_image.get("content_hash"):
            raise ValueError("channel profile image failed its content-hash check")
        avatar_hash = image_hash

    from moviepy import AudioFileClip, VideoClip
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    from app.services import character_library
    import numpy as np

    normalized_aspect = str(aspect or "9:16").strip()
    width, height = (1920, 1080) if normalized_aspect == "16:9" else (1080, 1920)
    font_path = (Path(__file__).resolve().parents[2] / "resource" / "fonts"
                 / "NotoSansDevanagari-VF.ttf")
    font_path = str(font_path) if font_path.is_file() else "DejaVuSans.ttf"
    title_font = ImageFont.truetype(font_path, 64 if normalized_aspect == "9:16" else 58)
    caption_font = ImageFont.truetype(font_path, 43)
    avatar = character_library.load_pose("waving", canvas=(560, 786))
    visual_box = None
    avatar_position = None
    caption_area = None
    title_y = None
    if normalized_aspect == "16:9":
        visual_box = (100, 230, 1180, 690)
        avatar = character_library.load_pose("waving", canvas=(400, 560))
        avatar_position = (1380, 380)
        caption_area = (100, 745, 1250)
        title_y = 135
    else:
        visual_box = (96, 350, 984, 820)
        avatar_position = ((width - avatar.width) // 2, 1055)
        caption_area = (855, 48, width - 48)
        title_y = 175
    visual_size = (visual_box[2] - visual_box[0], visual_box[3] - visual_box[1])
    profile_visual = None
    if image_path:
        with Image.open(image_path) as source:
            profile_visual = ImageOps.fit(source.convert("RGB"), visual_size,
                                  method=Image.Resampling.LANCZOS)

    def frame(time_seconds: float):
        canvas = Image.new("RGB", (width, height), (250, 249, 244))
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((56, 70, width - 56,
                                1140 if normalized_aspect == "9:16" else height - 45),
                               radius=44, fill=(255, 255, 255),
                               outline=(222, 225, 218), width=5)
        title = str(display_name or "Channel").strip()[:64]
        title_box = ((96, 106, width - 96, 244) if normalized_aspect == "9:16"
                     else (100, 70, width - 100, 200))
        draw.rounded_rectangle(title_box, radius=32, fill=(255, 230, 0))
        draw.text((width // 2, title_y), title, font=title_font,
                  fill=(30, 35, 40), anchor="mm")
        x0, y0, x1, y1 = visual_box
        draw.rounded_rectangle(visual_box, radius=24, fill=(250, 249, 244),
                               outline=(35, 44, 56), width=4)
        if profile_visual:
            canvas.paste(profile_visual, (x0, y0))
            draw.rounded_rectangle(visual_box, radius=24,
                                   outline=(35, 44, 56), width=4)
        else:
            visual_label = "WELCOME" if kind == "intro" else "THANK YOU"
            visual_font = ImageFont.truetype(font_path, 64)
            draw.text(((x0 + x1) // 2, (y0 + y1) // 2), visual_label,
                      font=visual_font, fill=(31, 40, 50), anchor="mm")
        avatar_bob = round(5 * math.sin(float(time_seconds) * 4.0))
        avatar_x, avatar_y = avatar_position
        canvas.paste(avatar, (avatar_x, avatar_y + avatar_bob), avatar)
        active = next((text for start, end, text in cues
                       if start <= time_seconds < end), "")
        if active:
            words = active.split()
            lines = []
            current = ""
            for word in words:
                trial = f"{current} {word}".strip()
                if draw.textbbox((0, 0), trial, font=caption_font)[2] <= caption_area[2]:
                    current = trial
                else:
                    if current:
                        lines.append(current)
                    current = word
            if current:
                lines.append(current)
            line_height = 58
            top, left, max_width = caption_area
            for index, line in enumerate(lines):
                draw.text((width // 2 if normalized_aspect == "9:16" else left,
                           top + index * line_height), line,
                          font=caption_font, fill=(255, 230, 0), anchor="mt",
                          stroke_width=4, stroke_fill=(28, 34, 42))
        return np.asarray(canvas)

    clip = VideoClip(frame, duration=float(duration))
    voice_track = AudioFileClip(str(audio_path))
    try:
        clip.with_audio(voice_track).write_videofile(
            output_path, fps=30, codec="libx264", audio_codec="aac",
            preset="veryfast", logger=None, ffmpeg_params=["-pix_fmt", "yuv420p"],
        )
    finally:
        clip.close()
        voice_track.close()
    return save_video(
        channel_id, kind, Path(output_path).read_bytes(),
        version=version,
        audio_hash=audio_hash, subtitle_hash=subtitle_hash, avatar_hash=avatar_hash,
        aspect=aspect,
    )


def _srt_seconds(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


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
