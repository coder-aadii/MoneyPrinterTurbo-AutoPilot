"""Explicitly reviewed, licensed local cutting clips for ASMR jobs."""

import hashlib
import json
import math
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from loguru import logger

from app.models.schema import MaterialInfo, VideoAspect
from app.services import asmr
from app.utils import utils


def probe(path: Path) -> tuple[float, int, int]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    return float(data["format"]["duration"]), int(stream["width"]), int(stream["height"])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_path(item: MaterialInfo) -> str:
    """Recheck the approved bytes immediately before the renderer receives them."""
    source = item.source_info or {}
    file = Path(item.url)
    if (item.provider != "curated_asmr" or file.is_symlink() or not file.is_file()
            or _sha256(file) != source.get("curated_sha256")):
        raise ValueError("curated ASMR clip changed after manifest validation")
    return str(file)


def load_clips(categories: list[str], video_aspect: VideoAspect) -> list[tuple[str, MaterialInfo]]:
    """Load hash-pinned clips with explicit human ASMR-quality approval."""
    library = Path(utils.storage_dir("asmr_library"))
    manifest = library / "manifest.json"
    if not manifest.is_file():
        return []
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))["clips"]
        if not isinstance(entries, list):
            raise ValueError("clips must be a list")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Invalid ASMR library manifest: {exc}") from exc

    selected = set(categories)
    seen_hashes = set()
    seen_source_ranges: dict[str, list[tuple[float, float] | None]] = {}
    clips = []
    for index, entry in enumerate(entries):
        try:
            if not isinstance(entry, dict) or entry.get("category") not in selected:
                continue
            filename = entry["file"]
            digest = entry["sha256"]
            license_name = entry["license"]
            source_url = entry.get("source_url", "")
            if (not isinstance(filename, str) or Path(filename).name != filename
                    or not filename.lower().endswith(".mp4") or not isinstance(digest, str)
                    or len(digest) != 64 or not isinstance(license_name, str)
                    or not license_name.strip() or entry.get("reviewed_cutting") is not True):
                raise ValueError("file, SHA-256, license and reviewed_cutting=true are required")
            quality_review = entry.get("quality_review")
            if (not isinstance(quality_review, dict)
                    or quality_review.get("status") != "approved"
                    or not str(quality_review.get("reviewer", "")).strip()
                    or not quality_review.get("sampled_timestamps_seconds")
                    or not str(quality_review.get("notes", "")).strip()):
                logger.info(
                    "ASMR clip excluded pending explicit quality review: "
                    f"file={filename}, reason={quality_review.get('reason', 'not reviewed') if isinstance(quality_review, dict) else 'not reviewed'}"
                )
                continue
            if license_name != "self-owned" and (
                    not isinstance(source_url, str)
                    or urlsplit(source_url).scheme != "https"
                    or not urlsplit(source_url).netloc):
                raise ValueError("licensed stock requires a public HTTPS source_url")
            file = library / filename
            if file.is_symlink() or not file.is_file() or file.stat().st_size > 512 * 1024 * 1024:
                raise ValueError("clip is missing, linked, or exceeds 512 MB")
            actual_hash = _sha256(file)
            if actual_hash != digest.lower():
                raise ValueError("clip SHA-256 differs from reviewed manifest")
            if actual_hash in seen_hashes:
                continue
            duration, width, height = probe(file)
            if not math.isfinite(duration) or duration < 1 or min(width, height) < 1:
                raise ValueError("clip has no usable video duration")
            source_id = entry.get("source_id")
            source_key = (asmr.canonical_media_url(source_url)
                          if source_url else str(source_id or "").strip())
            raw_start = entry.get("source_start_seconds")
            raw_end = entry.get("source_end_seconds")
            source_range = None
            if raw_start is not None or raw_end is not None:
                if (not isinstance(raw_start, (int, float))
                        or not isinstance(raw_end, (int, float))
                        or not math.isfinite(raw_start) or not math.isfinite(raw_end)
                        or raw_start < 0 or raw_end <= raw_start
                        or abs((raw_end - raw_start) - duration) > 0.5):
                    raise ValueError("source range must match the trimmed file duration")
                source_range = (float(raw_start), float(raw_end))
            if source_key:
                previous_ranges = seen_source_ranges.get(source_key, [])
                if any(source_range is None or previous is None or
                       max(source_range[0], previous[0]) < min(source_range[1], previous[1]) - 0.02
                       for previous in previous_ranges):
                    raise ValueError("clip duplicates or overlaps another trim of its source")
            target_width, target_height = VideoAspect(video_aspect).to_resolution()
            crop_width = min(width, height * target_width / target_height)
            crop_height = min(height, width * target_height / target_width)
            if crop_width < target_width or crop_height < target_height:
                raise ValueError("centered cover crop is smaller than output resolution")
            term = asmr.category_queries([entry["category"]])[0][1][0]
            item = MaterialInfo(
                provider="curated_asmr", url=str(file), duration=int(duration),
                source_info={
                    "provider": "curated_asmr", "asset_id": actual_hash,
                    "search_term": term, "tags": [term, "asmr"],
                    "source_page": source_url, "license": license_name,
                    "curated_sha256": actual_hash, "curated_reviewed": True,
                    "asmr_quality_review": "approved",
                    "asmr_quality_review_notes": quality_review["notes"],
                    "asmr_reviewed_timestamps_seconds": quality_review["sampled_timestamps_seconds"],
                    "source_id": source_id, "source_start_seconds": raw_start,
                    "source_end_seconds": raw_end,
                    "asmr_relevance_score": 10,
                    "asmr_selection_reason": "entire local clip reviewed as cutting",
                    "rendition": {"width": width, "height": height},
                },
            )
            clips.append((term, item))
            seen_hashes.add(actual_hash)
            if source_key:
                seen_source_ranges.setdefault(source_key, []).append(source_range)
        except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError) as exc:
            logger.warning(f"skipping ASMR library clip {index}: {exc}")
    return clips
