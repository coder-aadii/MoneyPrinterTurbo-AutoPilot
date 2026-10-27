"""Local sampled-frame evidence and explicit human approvals for stock clips."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

from app.utils import utils


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "asset"))[:96] or "asset"


def _registry_path() -> Path:
    return Path(__file__).resolve().parents[2] / "storage" / "visual_reviews" / "relevant_stock.json"


def _auto_relevance_path() -> Path:
    return Path(__file__).resolve().parents[2] / "storage" / "visual_reviews" / "auto_relevance.json"


def local_vision_verifier_available() -> bool:
    """No supported local image/video semantic verifier is bundled with MPT."""
    return False


def cached_auto_relevance(content_hash: str, query: str) -> dict | None:
    """Return a local-vision decision scoped to exact bytes and search intent."""
    query_hash = hashlib.sha256(" ".join(query.casefold().split()).encode("utf-8")).hexdigest()
    try:
        records = json.loads(_auto_relevance_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    for record in records if isinstance(records, list) else []:
        if (isinstance(record, dict) and record.get("sha256") == content_hash
                and record.get("query_sha256") == query_hash
                and record.get("verifier") in {"local_vision", "conservative_policy"}
                and record.get("decision") in {"relevant", "irrelevant", "uncertain"}):
            return record
    return None


def record_auto_relevance(
    content_hash: str,
    query: str,
    decision: str,
    *,
    verifier: str,
    model: str,
    score: float | None,
    reason: str,
) -> None:
    """Persist a completed local vision result, bound to clip bytes and query."""
    if (verifier not in {"local_vision", "conservative_policy"}
            or decision not in {"relevant", "irrelevant", "uncertain"}):
        raise ValueError("automatic relevance decisions require a supported verifier")
    if not re.fullmatch(r"[0-9a-f]{64}", content_hash.casefold()):
        raise ValueError("automatic relevance requires the actual SHA-256 file hash")
    if not model.strip() or len(reason.strip()) < 10:
        raise ValueError("automatic relevance model and reason are required")
    if verifier != "local_vision" and decision == "relevant":
        raise ValueError("conservative policy cannot mark footage relevant without visual evidence")
    if decision == "relevant" and (score is None or not 0.0 <= float(score) <= 1.0):
        raise ValueError("a relevant automatic decision requires a bounded confidence score")
    query_hash = hashlib.sha256(" ".join(query.casefold().split()).encode("utf-8")).hexdigest()
    path = _auto_relevance_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        records = []
    records = [item for item in records if not (
        isinstance(item, dict) and item.get("sha256") == content_hash
        and item.get("query_sha256") == query_hash
    )]
    records.append({
        "sha256": content_hash.casefold(), "query_sha256": query_hash,
        "decision": decision, "verifier": verifier, "model": model.strip()[:160],
        "score": score, "reason": reason.strip()[:1200],
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    })
    descriptor, temp_name = tempfile.mkstemp(prefix=".auto-relevance-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(records, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def human_approval(asset_id: str, content_hash: str) -> dict | None:
    try:
        records = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    for record in records if isinstance(records, list) else []:
        if (isinstance(record, dict) and record.get("asset_id") == str(asset_id)
                and record.get("sha256") == content_hash
                and record.get("status") == "approved"
                and record.get("reviewer") and record.get("reason")):
            return record
    return None


def record_human_review(asset_id: str, content_hash: str, status: str, reviewer: str, reason: str) -> None:
    if status not in {"approved", "rejected"}:
        raise ValueError("visual review status must be approved or rejected")
    if len(reviewer.strip()) < 2 or len(reason.strip()) < 10:
        raise ValueError("reviewer and a detailed visual review reason are required")
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        records = []
    records = [item for item in records if not (
        isinstance(item, dict) and item.get("asset_id") == str(asset_id)
        and item.get("sha256") == content_hash
    )]
    records.append({
        "asset_id": str(asset_id), "sha256": content_hash, "status": status,
        "reviewer": reviewer.strip()[:120], "reason": reason.strip()[:1200],
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    })
    descriptor, temp_name = tempfile.mkstemp(prefix=".visual-review-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(records, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def sample_clip(
    video_path: str | Path,
    task_id: str,
    asset_id: str,
    *,
    usable_seconds: float,
    video_aspect: str = "9:16",
) -> dict:
    """Extract five local frames from the exact leading range eligible for use."""
    duration = max(0.0, float(usable_seconds))
    if duration <= 0:
        raise ValueError("visual review requires a positive usable clip range")
    sample_times = [round(duration * fraction, 3) for fraction in (.08, .29, .5, .71, .92)]
    frame_dir = Path(utils.task_dir(task_id)) / "visual-review" / _safe_id(asset_id)
    frame_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, second in enumerate(sample_times, start=1):
        frame_path = frame_dir / f"sample-{index:02d}.jpg"
        result = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(second),
            "-i", str(video_path), "-frames:v", "1", "-q:v", "3", "-y", str(frame_path),
        ], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if result.returncode or not frame_path.is_file():
            continue
        with Image.open(frame_path) as image:
            image = image.convert("RGB")
            width, height = image.size
            edge = ImageStat.Stat(image.filter(ImageFilter.FIND_EDGES).convert("L")).stddev[0]
        aspect = width / height
        target_aspect = 9 / 16 if video_aspect == "9:16" else aspect
        crop_ratio = min(aspect / target_aspect, target_aspect / aspect)
        frames.append({
            "timestamp_seconds": second,
            "path": frame_path.relative_to(Path(utils.task_dir(task_id))).as_posix(),
            "width": width,
            "height": height,
            "edge_contrast_score": round(edge, 2),
            "portrait_crop_retained_dimension_ratio": round(crop_ratio, 3),
            "technical_quality": "low_resolution" if min(width, height) < 360 else "sampled",
        })
    if len(frames) < 3:
        status = "rejected"
        reason = "Could not extract at least three readable representative frames."
    elif any(item["technical_quality"] == "low_resolution" for item in frames):
        status = "rejected"
        reason = "Sampled footage resolution is too low for a reliable vertical render."
    else:
        status = "human_review_required"
        reason = (
            "Local frame sampling and technical checks completed; semantic scene relevance, "
            "cutting/action quality, and portrait subject crop require human confirmation."
        )
    return {
        "status": status,
        "reason": reason,
        "usable_range_seconds": [0.0, duration],
        "sampled_frames": frames,
        "semantic_verification": "not_automated",
    }
