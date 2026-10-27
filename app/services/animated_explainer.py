"""Local-first Savage Animated Explainer background renderer."""

from __future__ import annotations

import math
import json
import hashlib
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np
from loguru import logger
from moviepy import VideoClip, VideoFileClip
from PIL import Image, ImageDraw, ImageFont

from app.services import alignment_quality, character_library, emotion_storyboard, hindi_script, material, scene_spec, task_artifacts, visual_review
from app.utils import utils
from app.utils.subtitle_reader import file_to_subtitles


WIDTH, HEIGHT, FPS = 1080, 1920, 30
_BG = (250, 249, 244)
_TOP_PANEL = (96, 350, 984, 820)
MAX_STOCK_CLIP_SECONDS = 5.0
MAX_STORYBOARD_QUERIES = 3


def _stock_candidate_keys(candidate: dict) -> set[str]:
    """Return stable identities so renditions of one source are never reused."""
    provider = str(candidate.get("provider") or "").casefold()
    keys = set()
    asset_id = str(candidate.get("asset_id") or "").strip()
    url = str(candidate.get("url") or "").strip()
    digest = str(candidate.get("sha256") or "").strip().casefold()
    if asset_id:
        keys.update((asset_id, f"asset:{provider}:{asset_id}"))
    if url:
        keys.add(f"url:{url.split('#', 1)[0]}")
    if digest:
        keys.add(f"sha256:{digest}")
    return keys


def _stock_candidate_is_used(candidate: dict, used_keys: set[str]) -> bool:
    return bool(_stock_candidate_keys(candidate) & used_keys)


@lru_cache(maxsize=4)
def _font(path: str, size: int):
    return ImageFont.truetype(path, size=size)


def _font_path() -> str:
    root = Path(__file__).resolve().parents[2]
    for name in ("NotoSansDevanagari-VF.ttf", "Lohit-Devanagari.ttf"):
        candidate = root / "resource" / "fonts" / name
        if candidate.is_file():
            return str(candidate)
    return "DejaVuSans.ttf"


@lru_cache(maxsize=12)
def _pose(name: str) -> Image.Image:
    return character_library.load_pose(name, canvas=(560, 786))


def _read_word_cues(subtitle_path: str | None) -> list[tuple[float, float, str]]:
    cues = []
    for _index, timing, word in file_to_subtitles(subtitle_path or ""):
        stamps = re.findall(r"(\d{2}:\d{2}:\d{2},\d{3})", timing)
        if len(stamps) != 2 or not word.strip():
            continue
        def seconds(stamp: str) -> float:
            hours, minutes, rest = stamp.split(":")
            whole, millis = rest.split(",")
            return int(hours) * 3600 + int(minutes) * 60 + int(whole) + int(millis) / 1000
        cues.append((seconds(stamps[0]), seconds(stamps[1]), word.strip()))
    return cues


def _caption_groups(cues: list[tuple[float, float, str]]) -> list[dict]:
    groups = []
    words = []
    for index, cue in enumerate(cues):
        words.append((index, cue))
        word = cue[2]
        if len(words) >= 4 or re.search(r"[।.!?]$", word):
            groups.append({"start": words[0][1][0], "end": words[-1][1][1], "words": words})
            words = []
    if words:
        groups.append({"start": words[0][1][0], "end": words[-1][1][1], "words": words})
    return groups


def _sentence_groups(cues: list[tuple[float, float, str]], script: str,
                     duration: float, offset: float) -> list[dict]:
    """Use measured phrase endpoints without implying individual word alignment."""
    if (not cues or alignment_quality.spoken_words(" ".join(cue[2] for cue in cues))
            != alignment_quality.spoken_words(script)):
        raise ValueError("sentence captions do not cover the spoken script")
    previous_end = 0.0
    groups = []
    for start, end, text in cues:
        if not (0 <= previous_end <= start < end <= duration + .001):
            raise ValueError("sentence caption timing is invalid")
        groups.append({"start": start + offset, "end": end + offset,
                       "sentence": text})
        previous_end = end
    return groups


def _draw_sentence_caption(draw: ImageDraw.ImageDraw, font_path: str, text: str) -> None:
    font = _font(font_path, 43)
    lines = []
    line = ""
    for word in text.split():
        next_line = f"{line} {word}".strip()
        if line and draw.textbbox((0, 0), next_line, font=font, stroke_width=4)[2] > WIDTH - 140:
            lines.append(line)
            line = word
        else:
            line = next_line
    if line:
        lines.append(line)
    if len(lines) > 5:
        raise ValueError("sentence caption exceeds the explainer safe area")
    line_height = 51
    top = 855 + (5 - len(lines)) * 10
    for index, line in enumerate(lines):
        draw.text((WIDTH // 2, top + index * line_height), line, font=font,
                  anchor="mt", fill=(255, 230, 0), stroke_width=4,
                  stroke_fill=(28, 34, 42))


def _display_cues(cues: list[tuple[float, float, str]], display_map: list[dict]) -> list[tuple[float, float, str]]:
    """Render editable words over the measured timing span of spoken words."""
    mapped = []
    for item in display_map:
        start, end = item.get("spoken_start"), item.get("spoken_end")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
            continue
        if end > len(cues):
            return cues
        mapped.append((cues[start][0], cues[end - 1][1], str(item.get("text", ""))))
    return mapped or cues


def _task_display_map(task_id: str) -> list[dict]:
    try:
        with open(os.path.join(utils.task_dir(task_id), "script.json"), encoding="utf-8") as source:
            data = json.load(source)
        saved = data.get("display_word_map")
        if isinstance(saved, list):
            return saved
        script = data.get("script", "")
        return hindi_script.build_representations(
            script, data.get("params", {}).get("video_language", "hi")
        ).display_map()
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def _visual_timeline(
    beats: list[dict], cues: list[tuple[float, float, str]],
    spoken_script: str | None, narration_duration: float, narration_offset: float,
) -> tuple[list[dict], list[tuple[float, float, str]]]:
    """Use verified word cues or schedule visuals without claiming alignment."""
    expected_count = max((int(beat.get("end_word", 0)) for beat in beats), default=0)
    cue_words = [alignment_quality.spoken_words(text) for _, _, text in cues]
    expected_words = alignment_quality.spoken_words(spoken_script) if spoken_script else []
    word_cues_valid = bool(
        beats and expected_count > 0 and len(cues) == expected_count
        and all(len(words) == 1 for words in cue_words)
        and (not expected_words or [words[0] for words in cue_words] == expected_words)
        and all(math.isfinite(start) and math.isfinite(end) and end > start
                for start, end, _ in cues)
    )
    if word_cues_valid:
        shifted = [(start + narration_offset, end + narration_offset, text)
                   for start, end, text in cues]
        resolved = emotion_storyboard.resolve(beats, shifted)
        return [{**beat, "timing_source": "measured_word_alignment"} for beat in resolved], shifted
    if beats and cues and expected_words and [word for _start, _end, text in cues
                                              for word in alignment_quality.spoken_words(text)] == expected_words:
        # Phrase endpoints can time a beat when they coincide with its word
        # range. They do not establish karaoke timing inside that phrase.
        ranges = []
        cursor = 0
        for start, end, text in cues:
            count = len(alignment_quality.spoken_words(text))
            ranges.append((cursor, cursor + count, start, end))
            cursor += count
        by_start = {start_word: start for start_word, _end_word, start, _end in ranges}
        by_end = {end_word: end for _start_word, end_word, _start, end in ranges}
        if all(beat.get("start_word") in by_start and beat.get("end_word") in by_end
               for beat in beats):
            return ([{**beat, "start": by_start[beat["start_word"]] + narration_offset,
                      "end": by_end[beat["end_word"]] + narration_offset,
                      "timing_source": "measured_phrase_alignment"}
                     for beat in beats], [])
    return emotion_storyboard.schedule_visuals_from_duration(
        beats, narration_duration, offset=narration_offset,
    ), []


def _probe_video(path: str | Path) -> dict | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_name,width,height", "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=20,
        )
        data = json.loads(result.stdout)
        duration = float(data.get("format", {}).get("duration", 0))
        stream = next((item for item in data.get("streams", [])
                       if item.get("codec_name") and item.get("width") and item.get("height")), None)
        if duration <= 0 or not stream:
            return None
        return {"duration": duration, "width": int(stream["width"]), "height": int(stream["height"]),
                "codec": stream["codec_name"]}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return None


STOCK_MEDIA_POLICIES = {"manual_review", "auto_eligible"}


def resolve_stock_media_policy(policy: str | None = None) -> str:
    """Resolve and validate the task's saved stock approval policy."""
    value = str(policy or material.config.app.get(
        "stock_media_policy", "manual_review"
    )).strip().casefold()
    if value not in STOCK_MEDIA_POLICIES:
        logger.warning("stock_media_policy_invalid value={!r}; using manual_review", value)
        return "manual_review"
    return value


def _stock_eligibility(candidate: dict, *, required_seconds: float,
                       policy: str, query: str = "") -> tuple[bool, str, dict | None]:
    """Fail closed unless source, media integrity, format, duration and relevance pass.

    Manual Review uses the existing hash-bound human approval. Automatic uses
    only a query- and content-hash-bound local-vision decision. No local vision
    model is installed here, so unseen footage is rejected as uncertain.
    """
    if policy not in STOCK_MEDIA_POLICIES:
        return False, "invalid_stock_media_policy", None
    provider = str(candidate.get("provider") or "").casefold()
    if provider not in material.STOCK_VIDEO_PROVIDER_PRIORITY:
        return False, "unsupported_provider", None
    if not candidate.get("source_page") or not candidate.get("license"):
        return False, "missing_source_or_license", None
    path = candidate.get("path")
    if not path:
        return False, "missing_local_file", None
    media = _probe_video(path)
    if not media:
        return False, "invalid_or_undecodable_media", None
    try:
        current_hash = task_artifacts.file_digest(path)
    except (OSError, ValueError, TypeError):
        return False, "file_integrity_check_failed", None
    if not current_hash or current_hash != candidate.get("sha256"):
        return False, "media_hash_mismatch", None
    if (media["width"] < _TOP_PANEL[2] - _TOP_PANEL[0]
            or media["height"] < _TOP_PANEL[3] - _TOP_PANEL[1]
            or media["width"] <= media["height"]):
        return False, "not_sufficient_resolution_landscape", None
    if media["duration"] + 1 / FPS < required_seconds:
        return False, "clip_too_short", None
    if policy == "manual_review":
        approval = visual_review.human_approval(str(candidate.get("asset_id") or ""), current_hash)
        if not approval:
            return False, "visual_relevance_unverified", None
        return True, "manual_review_approved", approval

    decision = visual_review.cached_auto_relevance(current_hash, query)
    if decision is None:
        visual_review.record_auto_relevance(
            current_hash, query, "uncertain", verifier="conservative_policy",
            model="unavailable",
            score=None,
            reason="No supported local vision model is installed; metadata cannot establish visual relevance.",
        )
        decision = visual_review.cached_auto_relevance(current_hash, query)
        return False, "automatic_relevance_unverified", decision
    if decision.get("verifier") != "local_vision":
        return False, "automatic_relevance_unverified", decision
    if decision.get("decision") != "relevant" or float(decision.get("score") or 0) < .85:
        return False, f"automatic_visual_check_{decision.get('decision', 'uncertain')}", decision
    return True, "local_vision_relevance_passed", decision


def _cached_stock_candidates(query: str, provider: str) -> list[dict]:
    """Find local files in the exact provider/query search caches."""
    video_dir = Path(utils.storage_dir("cache_videos"))
    candidates: dict[str, dict] = {}
    for minimum_duration in range(3, math.ceil(MAX_STOCK_CLIP_SECONDS) + 1):
        try:
            items = material.material_cache.load_material_search_cache(
                provider=provider, search_term=query, minimum_duration=minimum_duration,
                video_aspect=material.VideoAspect.landscape,
            )
        except Exception as exc:
            logger.warning(
                "stock_cache_lookup_failed provider={} query={!r} error={}",
                provider, query, type(exc).__name__,
            )
            continue
        if not items:
            continue
        for item in items:
            info = item.source_info if isinstance(item.source_info, dict) else {}
            if str(info.get("provider", "")).casefold() != provider.casefold():
                continue
            license_name = str(info.get("license") or info.get("license_name") or "").strip()
            source_page = str(info.get("source_page") or "").strip()
            url = str(item.url or "").strip()
            if not license_name or not source_page or not url:
                continue
            digest = hashlib.md5(url.split("#", 1)[0].encode("utf-8")).hexdigest()
            local = video_dir / f"vid-{digest}.mp4"
            if not local.is_file():
                continue
            media = _probe_video(local)
            if not media:
                continue
            candidate = {
                "provider": provider, "asset_id": str(info.get("asset_id") or ""),
                "source_page": source_page, "license": license_name,
                "license_url": info.get("license_url"), "url": url,
                "path": str(local), "sha256": task_artifacts.file_digest(str(local)),
                "duration": media["duration"], "width": media["width"],
                "height": media["height"], "codec": media["codec"],
                "tags": list(info.get("tags") or []),
                "relevance_score": 1,
            }
            previous = candidates.get(digest)
            if previous is None or candidate["relevance_score"] > previous["relevance_score"]:
                candidates[digest] = candidate
                logger.info("stock_cache_candidate provider={} query={!r} id={} cache_hit=True "
                            "file_verified=True license={} relevance=needs_visual_policy_check",
                            provider, query, candidate["asset_id"], bool(license_name),
                            )
    return sorted(candidates.values(), key=lambda value: (-value["relevance_score"], value["asset_id"]))


MAX_STOCK_REPLACEMENTS_PER_TERM = 3


def _prepare_stock_candidate(candidate: dict, task_id: str, seconds: float) -> str:
    """Transcode and decode the exact short panel clip before accepting its source."""
    if seconds <= 0 or seconds > MAX_STOCK_CLIP_SECONDS:
        return ""
    panel_dir = Path(utils.task_dir(task_id)) / "top-panel-media"
    panel_dir.mkdir(parents=True, exist_ok=True)
    prepared = panel_dir / f"{candidate['sha256'][:16]}-{round(seconds * 1000)}ms-panel.mp4"
    if not prepared.is_file():
        result = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", candidate["path"], "-t", f"{seconds:.3f}", "-an", "-sn", "-dn",
            "-vf", "scale=888:470:force_original_aspect_ratio=increase,crop=888:470,fps=30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-pix_fmt", "yuv420p", str(prepared),
        ], check=False, capture_output=True, text=True)
        if result.returncode or not prepared.is_file():
            try:
                prepared.unlink(missing_ok=True)
            except OSError:
                pass
            return ""
    clip = None
    try:
        clip = VideoFileClip(str(prepared), audio=False)
        clip.get_frame(0)
        if not math.isfinite(float(clip.duration)) or clip.duration + 1 / FPS < seconds:
            clip.close()
            clip = None
            prepared.unlink(missing_ok=True)
            return ""
        return str(prepared)
    except (OSError, ValueError, IndexError):
        try:
            prepared.unlink(missing_ok=True)
        except OSError:
            pass
        return ""
    finally:
        if clip is not None:
            try:
                clip.close()
            except Exception:
                pass


def _search_and_cache_stock_candidate(
    query: str,
    provider: str,
    task_id: str,
    *,
    required_count: int = 1,
    minimum_duration: int = 3,
    clip_seconds: float | None = None,
    exclude_asset_ids: set[str] | None = None,
    stock_media_policy: str = "manual_review",
) -> list[dict]:
    """Fetch only needed, valid candidates and try same-query replacements on failure."""
    searchers = {
        "pixabay": material.search_videos_pixabay,
        "coverr": material.search_videos_coverr,
        "pexels": material.search_videos_pexels,
    }
    searcher = searchers.get(provider)
    if searcher is None:
        return []
    if not material.stock_video_provider_configured(provider):
        logger.info(f"skipping unconfigured {provider} storyboard fallback")
        return []
    try:
        items = material._search_videos_with_cache(
            provider=provider, search_videos=searcher, search_term=query,
            minimum_duration=minimum_duration, video_aspect="16:9",
        )
    except Exception as exc:
        logger.warning(f"optional {provider} storyboard search failed: {type(exc).__name__}")
        return []
    logger.info("stock_search provider={} query={!r} result_count={} candidate_ids={}",
                provider, query, len(items),
                [str(item.source_info.get("asset_id")) for item in items
                 if isinstance(item.source_info, dict)])
    if (stock_media_policy == "auto_eligible"
            and not visual_review.local_vision_verifier_available()):
        logger.info(
            "stock_search_rejected provider={} query={!r} reason=local_vision_unavailable "
            "result_count={} downloaded=0",
            provider, query, len(items),
        )
        return []
    ranked = []
    query_terms = {term for term in re.findall(r"[a-z0-9]+", query.casefold())
                   if term not in {"a", "the", "above", "and", "moving", "footage"}}
    if not query_terms:
        return []
    for item in items:
        info = item.source_info if isinstance(item.source_info, dict) else {}
        if not info.get("license") and provider != "coverr":
            continue
        rendition = info.get("rendition")
        if isinstance(rendition, dict):
            try:
                width, height = int(rendition.get("width")), int(rendition.get("height"))
            except (TypeError, ValueError):
                width = height = 0
            if (width < _TOP_PANEL[2] - _TOP_PANEL[0]
                    or height < _TOP_PANEL[3] - _TOP_PANEL[1]
                    or width <= height):
                logger.info(
                    "stock_candidate_rejected provider={} query={!r} id={} "
                    "reason=search_rendition_not_landscape",
                    provider, query, info.get("asset_id"),
                )
                continue
        tags = info.get("tags") or []
        searchable_text = " ".join((str(info.get("title") or ""),
                                     str(info.get("description") or ""),
                                     " ".join(map(str, tags))))
        words = set(re.findall(r"[a-z0-9]+", searchable_text.casefold()))
        score = len(query_terms & words)
        # The provider search is itself relevance evidence. Pexels often returns
        # sparse titles/tags, so one metadata match is enough; when metadata is
        # absent, retain the provider-ranked result for the human review gate.
        if words and score < 1:
            continue
        if not words:
            score = 1
        ranked.append((score, float(item.duration or 0), item, info, tags))
    ranked.sort(key=lambda candidate: (-candidate[0], -candidate[1]))
    scored = []
    excluded = {str(asset_id) for asset_id in (exclude_asset_ids or set())}
    unique_ranked = []
    seen_candidate_keys: set[str] = set()
    for candidate in ranked:
        _score, _duration, item, info, _tags = candidate
        candidate_info = {
            "provider": provider,
            "asset_id": info.get("asset_id"),
            "url": item.url,
        }
        keys = _stock_candidate_keys(candidate_info)
        if keys & excluded or keys & seen_candidate_keys:
            continue
        unique_ranked.append(candidate)
        seen_candidate_keys.update(keys)
    ranked = unique_ranked
    target = max(1, int(required_count))
    attempt_limit = target + MAX_STOCK_REPLACEMENTS_PER_TERM
    attempts = 0
    approved_count = 0
    seen_content_hashes: set[str] = set()
    for score, _duration, item, info, tags in ranked:
        asset_id = str(info.get("asset_id") or item.url)
        if attempts >= attempt_limit:
            break
        attempts += 1
        persistent_media_dir = Path(utils.storage_dir("cache_videos"))
        cache_path = persistent_media_dir / (
            f"vid-{hashlib.md5(item.url.encode('utf-8')).hexdigest()}.mp4"
        )
        cache_hit = cache_path.is_file()
        try:
            path = material._save_material_item(
                item, str(persistent_media_dir)
            )
        except Exception as exc:
            logger.info("stock_candidate_rejected provider={} query={!r} id={} reason=download_{}",
                        provider, query, asset_id, type(exc).__name__)
            continue
        if not path:
            logger.info("stock_candidate_rejected provider={} query={!r} id={} reason=no_local_file",
                        provider, query, asset_id)
            continue
        media = _probe_video(path)
        if not media or media["duration"] < minimum_duration:
            logger.info("stock_candidate_rejected provider={} query={!r} id={} reason=media_validation",
                        provider, query, asset_id)
            continue
        if (media["width"] < _TOP_PANEL[2] - _TOP_PANEL[0]
                or media["height"] < _TOP_PANEL[3] - _TOP_PANEL[1]
                or media["width"] <= media["height"]):
            logger.info(
                "stock_candidate_rejected provider={} query={!r} id={} "
                "reason=not_sufficient_resolution_landscape",
                provider, query, asset_id,
            )
            continue
        logger.info("stock_candidate_local provider={} query={!r} id={} cache_hit={} downloaded={} "
                    "verified=True license={} relevance_score={}",
                    provider, query, asset_id, cache_hit, not cache_hit,
                    bool(info.get("license")), score)
        candidate = {
            "provider": provider, "asset_id": asset_id,
            "source_page": info.get("source_page"),
            "license": info.get("license") or "Coverr License",
            "license_url": info.get("license_url") or "https://coverr.co/license",
            "url": item.url, "path": str(path),
            "sha256": task_artifacts.file_digest(path),
            "duration": media["duration"], "width": media["width"],
            "height": media["height"], "codec": media["codec"],
            "tags": list(tags) if isinstance(tags, list) else [],
            "relevance_score": score,
        }
        if stock_media_policy == "manual_review":
            candidate["human_approval"] = visual_review.human_approval(
                asset_id, candidate["sha256"]
            )
        content_hash_key = f"sha256:{str(candidate['sha256']).casefold()}"
        if content_hash_key in excluded or content_hash_key in seen_content_hashes:
            continue
        seen_content_hashes.add(content_hash_key)
        if candidate.get("human_approval"):
            if clip_seconds is not None:
                candidate["prepared_path"] = _prepare_stock_candidate(
                    candidate, task_id, clip_seconds
                )
                if not candidate["prepared_path"]:
                    continue
            approved_count += 1
        else:
            try:
                candidate["visual_review"] = visual_review.sample_clip(
                    path, task_id, asset_id,
                    usable_seconds=min(float(media["duration"]), MAX_STOCK_CLIP_SECONDS),
                    video_aspect="16:9",
                )
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        scored.append(candidate)
        if approved_count >= target:
            break
    return sorted(scored, key=lambda value: (-value["relevance_score"], value["asset_id"]))


def resolve_storyboard_media(
    task_id: str,
    beats: list[dict],
    *,
    provider: str = "pixabay",
    allow_unapproved_preview: bool = False,
    stock_terms: list[str] | None = None,
    stock_media_policy: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Use one keyword per narration topic and one reviewed clip per <=5s window."""
    if isinstance(stock_terms, str):
        usable_terms = [term.strip() for term in re.split(r"[,，\n]", stock_terms) if term.strip()]
    else:
        usable_terms = [str(term).strip() for term in (stock_terms or []) if str(term).strip()]
    policy = resolve_stock_media_policy(stock_media_policy)
    if usable_terms and beats:
        if len(usable_terms) != len(beats):
            raise ValueError(
                "Savage stock keywords must map one-to-one to narration beats: "
                f"received {len(usable_terms)} keyword(s) for {len(beats)} beat(s). "
                "Repeat a keyword for each beat that stays on the same topic."
            )
        searched_queries = set()
        for index, (beat, query) in enumerate(zip(beats, usable_terms)):
            spec_data = beat.get("scene_spec")
            if spec_data:
                spec = scene_spec.validate_scene_spec(spec_data)
                # Stock footage is an independent top-panel layer; keep the
                # narration-linked diagram as fallback for every primitive.
                use_stock = (query.casefold() in searched_queries or
                             len(searched_queries) < MAX_STORYBOARD_QUERIES)
                if use_stock:
                    searched_queries.add(query.casefold())
                    spec = scene_spec.validate_scene_spec({**spec.model_dump(), "stock_query": query})
                beats[index] = {**beat, "scene_spec": spec.model_dump(),
                                "visual_mode": "stock_video" if use_stock else "illustration",
                                "stock_query": query if use_stock else None,
                                "effective_media_type": "illustration"}
            else:
                # Legacy saved beat data remains renderable after an update.
                beats[index] = {**beat, "visual_mode": "stock_video",
                                "stock_query": query, "effective_media_type": "illustration"}
    elif usable_terms:
        raise ValueError(
            "Savage stock keywords cannot be mapped because narration timing produced no beats."
        )

    windows_by_query: dict[str, dict] = {}
    report = []
    for beat_index, beat in enumerate(beats):
        query = str(beat.get("stock_query") or "").strip()
        if beat.get("visual_mode") != "stock_video" or not query:
            continue
        try:
            start, end = float(beat["start"]), float(beat["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(start) and math.isfinite(end) and end > start):
            continue
        windows = []
        cursor = start
        while cursor < end:
            clip_end = min(cursor + MAX_STOCK_CLIP_SECONDS, end)
            windows.append({"beat_index": beat_index, "start": cursor, "end": clip_end})
            cursor = clip_end
        group_key = query.casefold()
        group = windows_by_query.setdefault(
            group_key, {"query": query, "windows": [], "candidates": []}
        )
        group["windows"].extend(windows)

    used_assets: set[str] = set()
    media_segments = []
    provider_order = material.stock_video_provider_order(provider)
    logger.info("stock_storyboard_provider requested={} eligible={}", provider, provider_order)
    for group in windows_by_query.values():
        query = group["query"]
        windows = group["windows"]
        needed_count = len(windows)
        minimum_duration = max(
            3, math.ceil(max(window["end"] - window["start"] for window in windows))
        )
        clip_seconds = max(window["end"] - window["start"] for window in windows)
        candidates = [
            item
            for candidate_provider in provider_order
            for item in _cached_stock_candidates(query, candidate_provider)
            if float(item.get("duration", 0)) >= minimum_duration
            and not _stock_candidate_is_used(item, used_assets)
        ]
        unique_candidates = []
        cached_ids: set[str] = set()
        for candidate in candidates:
            keys = _stock_candidate_keys(candidate)
            if keys & cached_ids:
                continue
            unique_candidates.append(candidate)
            cached_ids.update(keys)
        candidates = unique_candidates
        logger.info("stock_storyboard_query provider={} query={!r} windows={} cached_candidates={}",
                    provider, query, needed_count,
                    [item.get("asset_id") for item in candidates])
        approved_cached = 0
        for candidate in candidates:
            eligible, reason, approval = _stock_eligibility(
                candidate, required_seconds=clip_seconds, policy=policy, query=query,
            )
            candidate["human_approval"] = approval
            candidate["eligibility_reason"] = reason
            if eligible:
                candidate["prepared_path"] = _prepare_stock_candidate(
                    candidate, task_id, clip_seconds
                )
                candidate["preparation_failed"] = not bool(candidate["prepared_path"])
                if candidate["prepared_path"]:
                    approved_cached += 1
        for candidate_provider in provider_order:
            if approved_cached >= needed_count:
                break
            excluded_keys = set(cached_ids) | set(used_assets)
            for candidate in candidates:
                excluded_keys.update(_stock_candidate_keys(candidate))
            found = _search_and_cache_stock_candidate(
                query,
                candidate_provider,
                task_id,
                required_count=needed_count - approved_cached,
                minimum_duration=minimum_duration,
                clip_seconds=clip_seconds,
                exclude_asset_ids=excluded_keys,
                stock_media_policy=policy,
            )
            candidates.extend(found)
            logger.info("stock_storyboard_results provider={} query={!r} result_count={} candidate_ids={}",
                        candidate_provider, query, len(found),
                        [item.get("asset_id") for item in found])
            approved_cached += sum(bool(item.get("human_approval")) for item in found)

        for window in windows:
            beat = beats[window["beat_index"]]
            required_seconds = window["end"] - window["start"]
            selected = None
            selected_evidence = None
            pending_candidate = None
            first_rejected = None
            for candidate in candidates:
                if candidate.get("preparation_failed"):
                    continue
                if (_stock_candidate_is_used(candidate, used_assets)
                        or float(candidate.get("duration", 0)) < required_seconds):
                    continue
                eligible, reason, evidence = _stock_eligibility(
                    candidate, required_seconds=required_seconds, policy=policy, query=query,
                )
                candidate["eligibility_reason"] = reason
                if eligible:
                    selected, selected_evidence = candidate, evidence
                    break
                if first_rejected is None:
                    first_rejected = (candidate, reason, evidence)
                if reason == "visual_relevance_unverified" and pending_candidate is None:
                    pending_candidate = candidate
            if selected is None:
                selected = pending_candidate

            entry = {
                "beat_id": beat.get("beat_id"), "start": window["start"], "end": window["end"],
                "timing_source": beat.get("timing_source"),
                "short_visual_title": beat.get("short_visual_title"),
                "query": query, "planned_type": "stock_video",
                "effective_type": "illustration",
                "selection_reason": None, "visual_rationale": beat.get("visual_rationale"),
                "fallback_reason": None,
                "provider": None, "asset_id": None, "source_page": None, "license": None,
                "license_url": None, "sha256": None, "local_path": None,
                "approval_status": "not_found",
                "source_url": None, "provider_download_url": None,
                "eligibility_policy": policy,
            }
            if not selected:
                if first_rejected:
                    rejected, reason, evidence = first_rejected
                    entry.update({key: rejected.get(key) for key in (
                        "provider", "asset_id", "source_page", "license", "license_url", "sha256"
                    )})
                    entry["local_path"] = rejected.get("path")
                    entry["source_url"] = rejected.get("source_page")
                    entry["approval_status"] = "rejected"
                    entry["rejection_reason"] = reason
                    entry["relevance_evidence"] = evidence
                    entry["fallback_reason"] = f"Candidate rejected: {reason}."
                else:
                    entry["fallback_reason"] = "No relevant, decodable candidate met the requested clip duration."
                reject_reason = first_rejected[1] if first_rejected else "no_candidate_found"
                reject_evidence = (
                    first_rejected[2].get("reason")
                    if first_rejected and isinstance(first_rejected[2], dict) else None
                )
                logger.info("stock_storyboard_rejected beat={} query={!r} reason={} detail={!r}",
                            beat.get("beat_id"), query, reject_reason, reject_evidence)
                beat["effective_media_type"] = "illustration"
                beat["top_visual"] = (
                    "cloud_illustration"
                    if beat.get("top_visual") == "stock_clouds" else beat.get("top_visual")
                )
                report.append(entry)
                continue

            used_assets.update(_stock_candidate_keys(selected))
            entry.update({key: selected.get(key) for key in (
                "provider", "asset_id", "source_page", "license", "license_url", "sha256"
            )})
            entry["source_url"] = selected.get("source_page")
            if selected.get("provider") != "coverr":
                entry["provider_download_url"] = selected.get("url")
            entry["local_path"] = selected["path"]
            entry["approval_status"] = (
                "approved" if policy == "manual_review" else "auto_eligible"
            ) if selected_evidence else "human_review_required"
            entry["eligibility_policy"] = policy
            entry["selection_reason"] = selected.get("eligibility_reason")
            entry["relevance_evidence"] = selected_evidence
            if selected_evidence:
                if policy == "manual_review":
                    entry["human_approval"] = selected_evidence
                else:
                    entry["automatic_relevance"] = selected_evidence
                beat["effective_media_type"] = "stock_video"
                beat["selected_source"] = {key: selected.get(key) for key in (
                    "provider", "asset_id", "source_page", "license", "license_url", "sha256"
                )}
                beat["selected_source"].update({
                    "search_term": query,
                    "selection_reason": selected.get("eligibility_reason"),
                    "relevance_evidence": selected_evidence,
                })
                media_segments.append({
                    **selected, "start": window["start"], "end": window["end"],
                    "search_term": query,
                    "approved": True,
                })
                entry["effective_type"] = "stock_video"
            else:
                try:
                    entry["visual_review"] = visual_review.sample_clip(
                        selected["path"], task_id, selected["asset_id"],
                        usable_seconds=min(float(selected["duration"]), required_seconds),
                        video_aspect="16:9",
                    )
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
                if allow_unapproved_preview:
                    beat["effective_media_type"] = "stock_video_review_only"
                    media_segments.append({
                        **selected, "start": window["start"], "end": window["end"],
                        "search_term": query,
                        "approved": False,
                    })
                    entry["effective_type"] = "stock_video_review_only"
                    entry["fallback_reason"] = (
                        "Shown only in the visibly watermarked local review preview; approval is pending."
                    )
                else:
                    beat["effective_media_type"] = "illustration"
                    beat["top_visual"] = (
                        "cloud_illustration"
                        if beat.get("top_visual") == "stock_clouds" else beat.get("top_visual")
                    )
                    entry["fallback_reason"] = (
                        "Candidate failed the approval requirement; the illustrated scene is used."
                    )
            logger.info("stock_storyboard_decision beat={} query={!r} candidate={} provider={} "
                        "license={} local_verified={} relevance={} policy={} approval={} effective={} reason={!r}",
                        beat.get("beat_id"), query, entry["asset_id"], entry["provider"],
                        bool(entry["license"]), bool(selected.get("path") and _probe_video(selected["path"])),
                        selected.get("relevance_score"), policy, entry["approval_status"],
                        entry["effective_type"], entry["fallback_reason"])
            report.append(entry)
    report_path = Path(utils.task_dir(task_id)) / "top-panel-storyboard.json"
    try:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning(f"could not save optional top-panel storyboard report: {type(exc).__name__}")
    return beats, media_segments


def cleanup_stock_media(task_id: str) -> list[str]:
    """Remove prepared task clips while keeping shared-cache Pexels sources intact."""
    task_root = Path(utils.task_dir(task_id)).resolve()
    media_dir = task_root / "top-panel-media"
    if not media_dir.exists() or media_dir.is_symlink():
        return []
    try:
        media_dir.resolve().relative_to(task_root)
        files = [str(item) for item in media_dir.iterdir() if item.is_file()]
        shutil.rmtree(media_dir)
    except (OSError, ValueError) as exc:
        logger.warning(f"could not clean task-local stock media: {type(exc).__name__}")
        return []

    report_path = task_root / "top-panel-storyboard.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if isinstance(report, list):
            for item in report:
                if not isinstance(item, dict) or not item.get("local_path"):
                    continue
                try:
                    Path(item["local_path"]).resolve().relative_to(media_dir.resolve())
                except (OSError, ValueError):
                    if Path(item["local_path"]).is_file():
                        item["shared_cache_retained"] = True
                    continue
                item["local_path"] = None
                item["local_media_removed"] = True
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(f"could not update stock metadata after cleanup: {type(exc).__name__}")
    return files


def _draw_gravity_diagram(draw: ImageDraw.ImageDraw, font_path: str) -> None:
    """Original, explicitly illustrative ISS free-fall diagram for the test fact."""
    draw.ellipse((275, 380, 805, 910), fill=(109, 183, 235), outline=(42, 82, 118), width=8)
    draw.arc((150, 300, 930, 1030), start=188, end=350, fill=(40, 63, 95), width=7)
    draw.ellipse((730, 385, 794, 425), fill=(255, 230, 0), outline=(38, 45, 55), width=5)
    draw.line((762, 431, 700, 580), fill=(232, 78, 65), width=12)
    draw.polygon(((700, 580), (691, 541), (725, 558)), fill=(232, 78, 65))
    draw.rounded_rectangle((86, 475, 330, 620), radius=24, fill=(255, 242, 224),
                           outline=(210, 214, 217), width=4)
    draw.text((208, 547), "लगभग 90% g", font=_font(font_path, 35),
              fill=(40, 45, 52), anchor="mm")
    draw.rounded_rectangle((585, 690, 960, 805), radius=24, fill=(255, 255, 255),
                           outline=(210, 214, 217), width=4)
    draw.text((772, 748), "ISS + इंसान = साथ में गिरना", font=_font(font_path, 30),
              fill=(40, 45, 52), anchor="mm")


def _draw_iss_icon(draw: ImageDraw.ImageDraw, x: int, y: int, scale: float = 1.0) -> None:
    width, height = round(190 * scale), round(56 * scale)
    draw.rounded_rectangle((x - width // 2, y - height // 2, x + width // 2, y + height // 2),
                           radius=10, fill=(226, 231, 236), outline=(42, 56, 70), width=5)
    draw.rectangle((x - width, y - 30 * scale, x - width // 2, y + 30 * scale),
                   fill=(82, 142, 201), outline=(42, 56, 70), width=4)
    draw.rectangle((x + width // 2, y - 30 * scale, x + width, y + 30 * scale),
                   fill=(82, 142, 201), outline=(42, 56, 70), width=4)


def _draw_visual_scene(draw: ImageDraw.ImageDraw, font_path: str, visual: str,
                       phase: float, time_seconds: float) -> None:
    navy, blue, red, yellow = (40, 55, 70), (68, 145, 211), (226, 80, 65), (255, 230, 0)
    if visual in {"gravity_misconception", "iss_gravity_comparison"}:
        draw.ellipse((155, 405, 615, 865), fill=(104, 177, 226), outline=navy, width=8)
        draw.arc((75, 325, 760, 950), 195, 350, fill=navy, width=8)
        _draw_iss_icon(draw, 795, 490)
        if visual == "gravity_misconception":
            draw.text((760, 650), "¿GRAVEDAD CERO?", font=_font(font_path, 44), fill=red, anchor="mm")
            draw.text((770, 735), "नहीं भाई!", font=_font(font_path, 48), fill=navy, anchor="mm")
            for x in (735, 790, 845):
                draw.line((x, 540, x - 16, 650), fill=red, width=8)
                draw.polygon(((x - 16, 650), (x - 30, 624), (x - 1, 628)), fill=red)
        else:
            draw.rounded_rectangle((115, 515, 470, 660), radius=28, fill=(255, 255, 255), outline=navy, width=5)
            draw.text((290, 585), "धरती 100%", font=_font(font_path, 44), fill=navy, anchor="mm")
            draw.rounded_rectangle((575, 700, 950, 845), radius=28, fill=(255, 242, 210), outline=navy, width=5)
            draw.text((762, 770), "ISS पर करीब 90%", font=_font(font_path, 38), fill=navy, anchor="mm")
            draw.line((300, 685, 730, 685), fill=yellow, width=24)
            draw.text((510, 635), "फिर भी बहुत ज़्यादा", font=_font(font_path, 30), fill=navy, anchor="mm")
    elif visual == "orbiting_iss":
        draw.ellipse((350, 535, 730, 915), fill=(104, 177, 226), outline=navy, width=8)
        draw.arc((115, 300, 970, 1155), 185, 355, fill=navy, width=8)
        angle = math.radians(205 + phase * 105)
        cx, cy = 540 + round(390 * math.cos(angle)), 730 + round(390 * math.sin(angle))
        _draw_iss_icon(draw, cx, cy, 0.72)
        draw.line((cx + 30, cy - 20, cx + 125, cy - 48), fill=blue, width=12)
        draw.polygon(((cx + 125, cy - 48), (cx + 90, cy - 62), (cx + 99, cy - 28)), fill=blue)
        draw.line((cx, cy + 32, cx - 24, cy + 145), fill=red, width=10)
        draw.polygon(((cx - 24, cy + 145), (cx - 40, cy + 111), (cx - 7, cy + 117)), fill=red)
        # The synchronized phrase captions occupy y=960; the orbit arrows
        # communicate this beat without duplicating text beneath them.
    elif visual == "shared_freefall":
        _draw_iss_icon(draw, 545, 460, 1.0)
        y = 650 + round(95 * math.sin(phase * math.pi))
        for x, label in ((310, "यात्री"), (540, "ISS"), (770, "पेन")):
            draw.ellipse((x - 29, y - 34, x + 29, y + 24), fill=yellow, outline=navy, width=5)
            draw.line((x, y + 28, x, y + 80), fill=navy, width=7)
            draw.line((x, y + 55, x - 28, y + 76), fill=navy, width=6)
            draw.line((x, y + 55, x + 28, y + 76), fill=navy, width=6)
            draw.line((x + 68, y - 50, x + 68, y + 96), fill=red, width=8)
            draw.polygon(((x + 68, y + 96), (x + 54, y + 70), (x + 82, y + 70)), fill=red)
            draw.text((x, y + 135), label, font=_font(font_path, 30), fill=navy, anchor="mm")
        # Narration captions already state the takeaway; keep this area clear.
    else:
        draw.rounded_rectangle((280, 430, 800, 990), radius=38, fill=(255, 255, 255), outline=navy, width=8)
        draw.rectangle((320, 480, 760, 630), fill=(230, 237, 244), outline=navy, width=5)
        y = 700 + round(90 * math.sin(phase * math.pi))
        for x in (420, 540, 660):
            draw.ellipse((x - 20, y - 20, x + 20, y + 20), fill=yellow, outline=navy, width=4)
            draw.line((x, y + 25, x, y + 95), fill=red, width=7)
            draw.polygon(((x, y + 100), (x - 12, y + 78), (x + 12, y + 78)), fill=red)
            # The aligned phrase caption occupies this lower portion of the
            # panel; narration supplies these labels without obscuring it.
        # Avoid collision with the synchronized captions at y=960.


def _draw_scene_label(draw: ImageDraw.ImageDraw, font_path: str,
                      text: str, box: tuple[int, int, int, int]) -> None:
    """Fit validated narration text into a reusable scene card."""
    left, top, right, bottom = box
    for size in range(48, 25, -2):
        font = _font(font_path, size)
        lines = []
        line = ""
        for word in text.split():
            candidate = f"{line} {word}".strip()
            if line and draw.textbbox((0, 0), candidate, font=font)[2] > right-left-40:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
        if (len(lines) <= 2 and len(lines) * (size + 10) <= bottom-top-24
                and all(draw.textbbox((0, 0), value, font=font)[2] <= right-left-40
                        for value in lines)):
            y = (top+bottom - len(lines)*(size+10))//2
            for value in lines:
                draw.text(((left+right)//2, y), value, font=font,
                          fill=(40, 55, 70), anchor="mt")
                y += size+10
            return
    raise ValueError("scene label does not fit the top panel")


def _draw_scene_spec(draw: ImageDraw.ImageDraw, font_path: str,
                     value: dict | scene_spec.SceneSpec, phase: float) -> None:
    """Render only validated, fixed-layout primitives; never execute scene data."""
    scene = scene_spec.validate_scene_spec(value)
    navy, yellow, blue = (40, 55, 70), (255, 230, 0), (225, 239, 248)
    if scene.primitive == "label":
        boxes = [(155, 455, 925, 705)]
    elif scene.primitive == "counter":
        boxes = [(155, 415, 925, 620), (220, 655, 860, 785)]
    elif len(scene.labels) > 2:
        count = len(scene.labels)
        gap = 28
        width = (830 - gap*(count-1)) // count
        boxes = [(125 + index*(width+gap), 465,
                  125 + index*(width+gap) + width, 715)
                 for index in range(count)]
    else:
        boxes = [(125, 445, 475, 735), (605, 445, 955, 735)]
    for index, box in enumerate(boxes):
        if index >= len(scene.labels):
            break
        draw.rounded_rectangle(box, radius=26, fill=blue if index % 2 else (255, 255, 255),
                               outline=navy, width=5)
        _draw_scene_label(draw, font_path, scene.labels[index], box)
    if scene.primitive in {"flow", "transformation", "timeline"}:
        progress = max(0.0, min(1.0, phase))
        for left_box, right_box in zip(boxes, boxes[1:]):
            x0, x1 = left_box[2] + 5, right_box[0] - 5
            endpoint = x0 + round((x1-x0) * progress)
            draw.line((x0, 590, endpoint, 590), fill=yellow, width=12)
            draw.polygon(((endpoint, 590), (endpoint-16, 579), (endpoint-16, 601)), fill=yellow)
        if scene.primitive == "transformation":
            draw.text((540, 775), "परिवर्तन", font=_font(font_path, 30),
                      fill=navy, anchor="mm")
    elif scene.primitive == "comparison":
        draw.text((540, 590), "vs", font=_font(font_path, 45),
                  fill=navy, anchor="mm")
    elif scene.primitive == "counter" and len(scene.labels) == 1:
        draw.text((540, 735), "संख्या · चित्रात्मक", font=_font(font_path, 30),
                  fill=navy, anchor="mm")
    if scene.illustrative:
        draw.text((540, 812), "चित्रात्मक", font=_font(font_path, 22),
                  fill=(90, 100, 110), anchor="mm")


def _draw_topic_scene(
    draw: ImageDraw.ImageDraw, font_path: str, beat: dict | None, phase: float,
    topic: str = "",
) -> None:
    """Draw a topic-neutral comparison whose beat follows aligned narration."""
    if beat and beat.get("scene_spec"):
        _draw_scene_spec(draw, font_path, beat["scene_spec"], phase)
        return
    navy, red, green, yellow = (40, 55, 70), (226, 80, 65), (56, 142, 90), (255, 230, 0)
    visual = (beat or {}).get("top_visual", "topic_diagram")
    card_left = (80, 350, 500, 820)
    card_right = (580, 350, 1000, 820)
    if visual == "cloud_reveal":
        draw.ellipse((370, 415, 710, 755), fill=(255, 230, 0), outline=navy, width=8)
        draw.text((540, 585), "सच में?!", font=_font(font_path, 64), fill=navy, anchor="mm")
        return
    if visual in {"cloud_illustration", "stock_clouds"}:
        for x, y, radius in ((350, 620, 95), (470, 550, 130), (620, 610, 110), (735, 645, 80)):
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=(225,239,248), outline=(82,125,153), width=5)
        return
    if visual == "cloud_plane_comparison":
        for x, y, radius in ((260, 640, 85), (365, 580, 125), (500, 640, 90)):
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=(255,255,255), outline=(112,145,168), width=5)
        # Simple airplane silhouette gives the spoken comparison a visual anchor.
        draw.polygon(((620, 605), (810, 560), (900, 585), (810, 620), (890, 645),
                      (850, 660), (750, 630), (685, 670), (660, 655), (700, 620)),
                     fill=(76, 105, 132))
        draw.text((540, 770), "बादल vs प्लेन", font=_font(font_path, 42), fill=navy, anchor="mm")
        return
    if visual == "cloud_droplets":
        for x, y, radius in ((360, 560, 78), (465, 520, 100), (580, 540, 92), (690, 570, 74)):
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=(255,255,255), outline=(112,145,168), width=4)
        for x,y in ((385,710),(465,755),(555,700),(650,750),(735,700)):
            draw.ellipse((x-12,y-20,x+12,y+20), fill=(77,164,219), outline=navy, width=2)
        draw.text((540, 790), "पानी + बर्फ के कण", font=_font(font_path, 35), fill=navy, anchor="mm")
        return
    if visual == "cloud_weight_comparison":
        # A deliberately labeled visual comparison, not a scaled scientific model.
        draw.line((540, 455, 540, 735), fill=navy, width=16)
        draw.line((350, 520, 730, 520), fill=navy, width=14)
        draw.line((380, 520, 380, 650), fill=red, width=8)
        draw.line((700, 520, 700, 650), fill=red, width=8)
        draw.arc((285, 610, 475, 730), 0, 180, fill=red, width=12)
        draw.arc((605, 610, 795, 730), 0, 180, fill=red, width=12)
        draw.rounded_rectangle((175, 450, 375, 505), radius=22, fill=(255,245,202), outline=navy, width=4)
        draw.text((275, 478), "500 टन", font=_font(font_path, 34), fill=navy, anchor="mm")
        draw.polygon(((630, 660),(680,640),(760,642),(800,660),(755,675),(695,675)), fill=(76,105,132))
        draw.text((540, 785), "बादल vs Boeing 737", font=_font(font_path, 34), fill=navy, anchor="mm")
        return
    if visual == "cloud_punchline":
        for x,y,r in ((340,600,85),(450,540,115),(580,550,110),(700,610,90)):
            draw.ellipse((x-r,y-r,x+r,y+r), fill=(255,255,255), outline=(112,145,168), width=5)
        draw.ellipse((485,545,505,565), fill=navy)
        draw.ellipse((565,545,585,565), fill=navy)
        draw.arc((510,555,560,600), 10, 170, fill=navy, width=5)
        draw.line((485,680,465,730), fill=navy, width=7)
        draw.line((585,680,610,730), fill=navy, width=7)
        draw.text((540, 785), "आसमान में ओवरटाइम!", font=_font(font_path, 36), fill=red, anchor="mm")
        return
    if visual == "topic_hook":
        draw.rounded_rectangle((180, 390, 900, 780), radius=70, fill=(255, 255, 255), outline=navy, width=8)
        draw.ellipse((405, 455, 675, 725), fill=yellow, outline=navy, width=8)
        draw.text((540, 590), "?", font=_font(font_path, 180), fill=navy, anchor="mm")
        return
    if visual == "topic_myth_correction":
        draw.rounded_rectangle(card_left, radius=34, fill=(255, 238, 234), outline=red, width=6)
        draw.rounded_rectangle(card_right, radius=34, fill=(235, 248, 239), outline=green, width=6)
        draw.text((290, 420), "मान्यता", font=_font(font_path, 48), fill=navy, anchor="mm")
        draw.text((790, 420), "तथ्य", font=_font(font_path, 48), fill=navy, anchor="mm")
        draw.text((290, 615), "×", font=_font(font_path, 150), fill=red, anchor="mm")
        draw.text((790, 615), "✓", font=_font(font_path, 130), fill=green, anchor="mm")
        draw.line((430, 610, 650, 610), fill=yellow, width=24)
        draw.polygon(((650, 610), (610, 580), (610, 640)), fill=yellow)
        return
    if visual == "topic_reveal":
        radius = 115 + round(22 * math.sin(phase * math.pi))
        draw.ellipse((540 - radius, 580 - radius, 540 + radius, 580 + radius),
                     fill=(255, 242, 198), outline=yellow, width=14)
        draw.rounded_rectangle((210, 745, 870, 825), radius=25, fill=(255, 255, 255), outline=navy, width=4)
        draw.text((540, 785), "असल बात सामने आई", font=_font(font_path, 42), fill=navy, anchor="mm")
        return

    if visual == "terminology_roast":
        for box, label, color in (
            ((125, 430, 485, 700), "Client", (235, 242, 255)),
            ((595, 430, 955, 700), "Server", (235, 248, 239)),
        ):
            draw.rounded_rectangle(box, radius=36, fill=color, outline=navy, width=7)
            draw.text(((box[0] + box[2]) // 2, 565), label,
                      font=_font(font_path, 64), fill=navy, anchor="mm")
        draw.text((540, 785), "नाम भारी, काम सीधा", font=_font(font_path, 43),
                  fill=red, anchor="mm")
        return

    if visual in {"restaurant_setup", "restaurant_order", "kitchen_server"}:
        draw.rounded_rectangle((135, 420, 945, 805), radius=36,
                               fill=(250, 242, 224), outline=navy, width=7)
        if visual == "restaurant_setup":
            draw.rectangle((190, 485, 890, 590), fill=(223, 237, 247), outline=navy, width=5)
            draw.line((540, 485, 540, 590), fill=navy, width=5)
            draw.rectangle((270, 635, 810, 675), fill=(150, 91, 55), outline=navy, width=5)
            draw.line((330, 675, 330, 765), fill=navy, width=12)
            draw.line((750, 675, 750, 765), fill=navy, width=12)
            draw.text((540, 735), "रेस्टोरेंट", font=_font(font_path, 42), fill=navy, anchor="mm")
        elif visual == "restaurant_order":
            draw.rectangle((270, 610, 810, 655), fill=(150, 91, 55), outline=navy, width=5)
            draw.line((330, 655, 330, 770), fill=navy, width=12)
            draw.line((750, 655, 750, 770), fill=navy, width=12)
            draw.ellipse((440, 500, 510, 570), fill=yellow, outline=navy, width=5)
            draw.line((475, 570, 475, 635), fill=navy, width=9)
            draw.ellipse((615, 500, 685, 570), fill=(255, 255, 255), outline=navy, width=5)
            draw.line((650, 570, 650, 635), fill=navy, width=9)
            draw.text((540, 745), "ग्राहक → ऑर्डर", font=_font(font_path, 43), fill=navy, anchor="mm")
        else:
            draw.rounded_rectangle((360, 505, 720, 690), radius=28,
                                   fill=(255, 255, 255), outline=navy, width=6)
            draw.arc((440, 535, 640, 700), 0, 180, fill=red, width=14)
            draw.line((470, 622, 610, 622), fill=red, width=12)
            draw.text((540, 735), "किचन तैयार करता है", font=_font(font_path, 43), fill=navy, anchor="mm")
        return

    if visual == "client_server_summary":
        draw.rounded_rectangle((145, 465, 455, 705), radius=30,
                               fill=(235, 242, 255), outline=navy, width=6)
        draw.rounded_rectangle((625, 465, 935, 705), radius=30,
                               fill=(235, 248, 239), outline=navy, width=6)
        draw.text((300, 585), "Client", font=_font(font_path, 54), fill=navy, anchor="mm")
        draw.text((780, 585), "Server", font=_font(font_path, 54), fill=navy, anchor="mm")
        draw.line((465, 540, 610, 540), fill=red, width=11)
        draw.polygon(((610, 540), (580, 520), (580, 560)), fill=red)
        draw.line((615, 640, 470, 640), fill=green, width=11)
        draw.polygon(((470, 640), (500, 620), (500, 660)), fill=green)
        draw.text((540, 505), "Request →", font=_font(font_path, 32), fill=red, anchor="mm")
        draw.text((540, 680), "← Response", font=_font(font_path, 32), fill=green, anchor="mm")
        return

    if visual == "request_response_flow":
        labels = (("Request", 150), ("Process", 430), ("Response", 710))
        for label, x in labels:
            draw.rounded_rectangle((x, 520, x + 220, 680), radius=28,
                                   fill=(255, 255, 255), outline=navy, width=6)
            draw.text((x + 110, 600), label, font=_font(font_path, 38),
                      fill=navy, anchor="mm")
        for x in (375, 655):
            draw.line((x, 600, x + 45, 600), fill=red, width=12)
            draw.polygon(((x + 45, 600), (x + 20, 582), (x + 20, 618)), fill=red)
        return

    if visual == "browser_youtube_exchange":
        for box, label, color in (
            ((115, 470, 435, 715), "Browser", (235, 242, 255)),
            ((645, 470, 965, 715), "YouTube\nServer", (255, 239, 232)),
        ):
            draw.rounded_rectangle(box, radius=28, fill=color, outline=navy, width=6)
            draw.text(((box[0] + box[2]) // 2, 590), label,
                      font=_font(font_path, 46), fill=navy, anchor="mm", align="center")
        draw.line((440, 540, 635, 540), fill=red, width=10)
        draw.polygon(((635, 540), (604, 520), (604, 560)), fill=red)
        draw.line((640, 650, 445, 650), fill=green, width=10)
        draw.polygon(((445, 650), (476, 630), (476, 670)), fill=green)
        draw.text((540, 745), "Request  •  Response", font=_font(font_path, 38), fill=navy, anchor="mm")
        return

    if visual == "legacy_village_city_comparison":
        draw.rounded_rectangle(card_left, radius=34, fill=(228, 244, 222), outline=green, width=6)
        draw.rounded_rectangle(card_right, radius=34, fill=(228, 237, 249), outline=navy, width=6)
        draw.text((290, 395), "गाँव", font=_font(font_path, 48), fill=navy, anchor="mm")
        draw.text((790, 395), "शहर", font=_font(font_path, 48), fill=navy, anchor="mm")
        # Rural homes, fields and trees on one side; dense, distinct skyline on the other.
        draw.rectangle((100, 690, 480, 770), fill=(132, 190, 102))
        for x, y, width, height in ((185, 555, 135, 115), (335, 600, 105, 85)):
            draw.polygon(((x - 12, y), (x + width // 2, y - 75), (x + width + 12, y)), fill=(176, 93, 63))
            draw.rectangle((x, y, x + width, y + height), fill=(245, 217, 164), outline=navy, width=5)
            draw.rectangle((x + width // 2 - 15, y + 40, x + width // 2 + 15, y + height), fill=(111, 75, 55))
        for x in (135, 440):
            draw.rectangle((x - 9, 580, x + 9, 715), fill=(112, 77, 48))
            draw.ellipse((x - 48, 505, x + 48, 605), fill=(65, 145, 87), outline=green, width=4)
        draw.rectangle((600, 715, 980, 770), fill=(102, 110, 126))
        for x, width, height in ((625, 72, 205), (710, 95, 285), (820, 70, 235), (900, 74, 325)):
            draw.rectangle((x, 715 - height, x + width, 715), fill=(117, 139, 165), outline=navy, width=4)
            for wx in range(x + 14, x + width - 8, 24):
                for wy in range(715 - height + 18, 700, 34):
                    draw.rectangle((wx, wy, wx + 9, wy + 13), fill=yellow)
        return

    # Optional legacy templates remain available only by explicit visual ID.
    if visual in {"legacy_bird", "legacy_bird_weather"}:
        draw.ellipse((380, 475, 700, 690), fill=(255, 230, 0), outline=navy, width=8)
        draw.ellipse((575, 500, 620, 545), fill=navy)
        draw.polygon(((690, 560), (800, 520), (790, 600)), fill=red)
        draw.arc((290, 420, 550, 730), 200, 350, fill=navy, width=20)
        if visual == "legacy_bird_weather":
            draw.ellipse((690, 390, 855, 555), fill=yellow, outline=navy, width=6)
            draw.arc((155, 660, 420, 840), 180, 360, fill=(120, 155, 185), width=22)
            for x in (220, 285, 350):
                draw.line((x, 710, x - 25, 770), fill=(60, 142, 211), width=10)
        return
    if visual == "legacy_planet":
        draw.ellipse((330, 410, 740, 820), fill=(102, 167, 218), outline=navy, width=8)
        for x, y in ((215, 500), (850, 650), (760, 390), (250, 745)):
            draw.ellipse((x - 15, y - 15, x + 15, y + 15), fill=yellow)
        draw.arc((215, 365, 850, 870), 190, 345, fill=navy, width=8)
        return

    # Fallback stays an original diagram and avoids pretending to depict an
    # unnamed animal, discovery, or scientific result.
    for index, x in enumerate((300, 540, 780)):
        y = 585 + round(24 * math.sin(phase * math.pi + index))
        draw.ellipse((x - 70, y - 70, x + 70, y + 70),
                     fill=(255, 255, 255), outline=navy, width=8)
    draw.line((375, 585, 465, 585), fill=yellow, width=18)
    draw.polygon(((465, 585), (430, 560), (430, 610)), fill=yellow)
    draw.line((615, 585, 705, 585), fill=yellow, width=18)
    draw.polygon(((705, 585), (670, 560), (670, 610)), fill=yellow)


def _topic_short_title(topic: str) -> str:
    text = str(topic or "").strip()
    words = re.findall(r"[\w-]+", text, flags=re.UNICODE)
    return " ".join(words[:3]) if words else "आज का सच"


def _fit_heading(draw: ImageDraw.ImageDraw, title: str, font_path: str,
                 *, max_width: int = 812, max_height: int = 112,
                 max_font_size: int = 62, max_lines: int = 2):
    """Word-wrap and fit a semantic heading using the same PIL shaping as render."""
    candidates = [str(title or "").strip(), "आज का सच"]
    for candidate in dict.fromkeys(candidates):
        words = candidate.split()
        if not words:
            continue
        for size in range(max_font_size, 25, -2):
            font = _font(font_path, size)
            lines: list[str] = []
            current = ""
            too_wide = False
            for word in words:
                trial = f"{current} {word}".strip()
                box = draw.textbbox((0, 0), trial, font=font)
                if box[2] - box[0] <= max_width:
                    current = trial
                    continue
                if not current:
                    too_wide = True
                    break
                lines.append(current)
                current = word
                word_box = draw.textbbox((0, 0), current, font=font)
                if word_box[2] - word_box[0] > max_width:
                    too_wide = True
                    break
            if too_wide:
                continue
            if current:
                lines.append(current)
            if len(lines) > max_lines:
                continue
            heights = []
            for line in lines:
                box = draw.textbbox((0, 0), line, font=font)
                heights.append(box[3] - box[1])
            line_gap = max(2, size // 10)
            total_height = sum(heights) + line_gap * (len(lines) - 1)
            if total_height <= max_height:
                return lines, font, line_gap
    raise ValueError("No safe short heading fits the configured top heading area")


def _draw_frame(topic: str, duration: float, time_seconds: float,
                groups: list[dict], beats: list[dict],
                stock_segments: list[dict] | None = None) -> np.ndarray:
    canvas = Image.new("RGB", (WIDTH, HEIGHT), _BG)
    draw = ImageDraw.Draw(canvas)
    font_path = _font_path()

    # Topic card and two-part explanatory diagram keep the image area intentional
    # even when no external media provider or licensed asset is available.
    draw.rounded_rectangle((56, 70, WIDTH - 56, 1140), radius=44,
                           fill=(255, 255, 255), outline=(222, 225, 218), width=5)
    active_beat = next((beat for beat in beats
                        if beat["start"] <= time_seconds < beat["end"]), None)
    if active_beat is None:
        active_beat = next((beat for beat in reversed(beats)
                            if beat["end"] <= time_seconds), None)
    title = (active_beat or {}).get("short_visual_title") or _topic_short_title(topic)
    title_lines, title_font, line_gap = _fit_heading(draw, title, font_path)
    draw.rounded_rectangle((96, 106, WIDTH - 96, 244), radius=32,
                           fill=(255, 230, 0))
    line_height = title_font.size + line_gap
    y = 175 - line_height * (len(title_lines) - 1) / 2
    for line in title_lines:
        bbox = draw.textbbox((0, 0), line, font=title_font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        if text_width > 812 or text_height > 112:
            raise ValueError("Rendered heading exceeded its safe panel bounds")
        draw.text((WIDTH // 2, round(y)), line, font=title_font, fill=(30, 35, 40),
                  anchor="mm", stroke_width=1, stroke_fill=(30, 35, 40))
        y += line_height
    active_group = next((i for i, group in enumerate(groups)
                         if group["start"] <= time_seconds < group["end"]), None)
    speech_active = active_group is not None
    pose = _pose(active_beat["pose"] if active_beat else "explaining")
    phase = 0.0
    if active_beat:
        phase = max(0.0, min(1.0, (time_seconds - active_beat["start"]) /
                              max(0.001, active_beat["end"] - active_beat["start"])))
    legacy_space_visuals = {
        "gravity_misconception", "iss_gravity_comparison",
        "earth_iss_gravity_comparison", "orbiting_iss", "shared_freefall",
        "iss_orbit_context", "freefall_elevator_payoff",
    }
    if active_beat and active_beat.get("top_visual") in legacy_space_visuals and not active_beat.get("scene_spec"):
        _draw_visual_scene(draw, font_path, active_beat["top_visual"], phase, time_seconds)
    else:
        _draw_topic_scene(draw, font_path, active_beat, phase, topic)
    for segment in stock_segments or []:
        if not segment["start"] <= time_seconds < segment["end"]:
            continue
        local_time = min(max(0.0, time_seconds - segment["start"]),
                         max(0.0, float(segment["clip"].duration) - 1 / FPS))
        rgb = segment["clip"].get_frame(local_time)
        panel = Image.fromarray(rgb).convert("RGB").resize(
            (_TOP_PANEL[2] - _TOP_PANEL[0], _TOP_PANEL[3] - _TOP_PANEL[1]),
            Image.Resampling.LANCZOS,
        )
        fade = min(1.0, (time_seconds - segment["start"]) / .18,
                   (segment["end"] - time_seconds) / .18)
        if fade < 1:
            base = canvas.crop(_TOP_PANEL)
            panel = Image.blend(base, panel, max(0.0, fade))
        canvas.paste(panel, (_TOP_PANEL[0], _TOP_PANEL[1]))
        panel_draw = ImageDraw.Draw(canvas)
        panel_draw.rectangle(_TOP_PANEL, outline=(35, 44, 56), width=5)
        if not segment.get("approved", False):
            label = "REVIEW ONLY  •  NOT APPROVED"
            label_font = _font(font_path, 25)
            bounds = panel_draw.textbbox((0, 0), label, font=label_font)
            w, h = bounds[2] - bounds[0], bounds[3] - bounds[1]
            panel_draw.rounded_rectangle((_TOP_PANEL[0] + 12, _TOP_PANEL[1] + 12,
                                          _TOP_PANEL[0] + w + 36, _TOP_PANEL[1] + h + 30),
                                         radius=10, fill=(190, 35, 35))
            panel_draw.text((_TOP_PANEL[0] + 24, _TOP_PANEL[1] + 21), label,
                            font=label_font, fill=(255, 255, 255))
    if active_group is not None and "sentence" in groups[active_group]:
        _draw_sentence_caption(draw, font_path, groups[active_group]["sentence"])
    elif active_group is not None:
        group = groups[active_group]
        active_word = next((index for index, cue in group["words"]
                            if cue[0] <= time_seconds < cue[1]), None)
        font_size = 48
        font = _font(font_path, font_size)
        word_sizes = [draw.textbbox((0, 0), cue[2], font=font, stroke_width=5)[2]
                      for _, cue in group["words"]]
        total_width = sum(word_sizes) + 20 * (len(word_sizes) - 1)
        while total_width > WIDTH - 96 and font_size > 32:
            font_size -= 2
            font = _font(font_path, font_size)
            word_sizes = [draw.textbbox((0, 0), cue[2], font=font, stroke_width=5)[2]
                          for _, cue in group["words"]]
            total_width = sum(word_sizes) + 20 * (len(word_sizes) - 1)
        x = max(48, (WIDTH - total_width) // 2)
        for (index, cue), word_width in zip(group["words"], word_sizes):
            draw.text((x, 960), cue[2], font=font,
                      fill=(255, 230, 0) if index == active_word else (255, 255, 255),
                      stroke_width=5, stroke_fill=(28, 34, 42))
            x += word_width + 20
    bob = round(5 * math.sin(time_seconds * 4.0)) if speech_active else 0
    canvas.paste(pose, ((WIDTH - pose.width) // 2, 1055 + bob), pose)
    return np.asarray(canvas)


def render_background(task_id: str, topic: str, duration: float, *, output_path: str | None = None,
                      subtitle_path: str | None = None,
                      emotion_beats: list[dict] | None = None,
                      display_word_map: list[dict] | None = None,
                      spoken_script: str | None = None,
                      narration_duration_seconds: float | None = None,
                      narration_offset_seconds: float = 0.0,
                      stock_provider: str = "pixabay",
                      stock_terms: list[str] | None = None,
                      caption_mode: str = "word_by_word",
                      preview_unapproved_stock: bool = False,
                      stock_media_policy: str | None = None) -> str:
    """Render a 1080×1920, 30 FPS local explainer scene with the saved narrator."""
    if not math.isfinite(float(duration)) or duration <= 0:
        raise ValueError("explainer duration must be a positive finite number")
    narration_duration = (float(narration_duration_seconds)
                          if narration_duration_seconds is not None
                          else float(duration) - float(narration_offset_seconds))
    if (not math.isfinite(narration_duration) or narration_duration <= 0
            or not math.isfinite(float(narration_offset_seconds))
            or narration_offset_seconds < 0
            or narration_offset_seconds + narration_duration > duration + 0.01):
        raise ValueError("narration visual duration must fit within the audio track")
    assets = character_library.inspect_pack()
    output = output_path or os.path.join(utils.task_dir(task_id), "savage-explainer-background.mp4")
    subtitle_cues = _read_word_cues(subtitle_path)
    beats, cues = _visual_timeline(
        emotion_beats or [], subtitle_cues, spoken_script,
        narration_duration, narration_offset_seconds,
    )
    display_word_map = display_word_map or _task_display_map(task_id)
    if caption_mode == "sentence":
        groups = _sentence_groups(subtitle_cues, spoken_script or "",
                                  narration_duration, narration_offset_seconds)
    else:
        groups = _caption_groups(_display_cues(cues, display_word_map))
    if subtitle_path and not groups:
        raise ValueError("enabled explainer captions have no valid renderable cues")
    if beats and beats[0].get("timing_source") == "estimated_visual_schedule":
        logger.warning("visual scene timing is estimated from measured narration duration; word captions are unavailable")
    beats, stock_segments = resolve_storyboard_media(
        task_id, beats, provider=stock_provider,
        stock_terms=stock_terms,
        allow_unapproved_preview=preview_unapproved_stock,
        stock_media_policy=stock_media_policy,
    )
    prepared_segments = []
    for segment in stock_segments:
        seconds = float(segment["end"]) - float(segment["start"])
        if seconds <= 0 or seconds > float(segment["duration"]):
            continue
        prepared_path = segment.get("prepared_path") or _prepare_stock_candidate(
            segment, task_id, seconds
        )
        if not prepared_path or not Path(prepared_path).is_file():
            continue
        try:
            panel_clip = VideoFileClip(str(prepared_path), audio=False)
            panel_clip.get_frame(0)
        except (OSError, ValueError, IndexError):
            continue
        prepared_segments.append({**segment, "clip": panel_clip})
    fingerprint = task_artifacts.cache_key({
        "renderer": task_artifacts.file_digest(__file__),
        "emotion_resolver": task_artifacts.file_digest(emotion_storyboard.__file__),
        "assets": assets, "font": task_artifacts.file_digest(_font_path()),
        "topic": topic, "duration": duration, "captions": groups, "beats": beats,
        "stock_segments": [
            {key: segment.get(key) for key in (
                "start", "end", "path", "sha256", "approved", "asset_id", "provider"
            )}
            for segment in prepared_segments
        ],
        "preview_unapproved_stock": preview_unapproved_stock,
        "stock_media_policy": stock_media_policy or material.config.app.get(
            "stock_media_policy", "manual_review"
        ),
        "display_word_map": display_word_map,
        "size": [WIDTH, HEIGHT], "fps": FPS, "output": str(Path(output).resolve()),
    })

    def validate_media(filename):
        with VideoFileClip(filename, audio=False) as rendered:
            if (not math.isfinite(rendered.duration)
                    or abs(rendered.duration - duration) > 1 / FPS + 0.01
                    or tuple(rendered.size) != (WIDTH, HEIGHT)):
                raise ValueError("cached explainer dimensions or duration do not match narration")
            rendered.get_frame(0)
            rendered.get_frame(max(0, duration - 1 / FPS))

    cached = task_artifacts.load_cached_artifact(task_id, "explainer", fingerprint)
    if cached:
        try:
            validate_media(cached["path"])
            return cached["path"]
        except (OSError, ValueError):
            pass
    clip = VideoClip(
        lambda t: _draw_frame(topic, duration, float(t), groups, beats, prepared_segments),
        duration=float(duration),
    )
    try:
        clip.write_videofile(output, fps=FPS, codec="libx264", audio=False,
                             preset="ultrafast", logger=None,
                             ffmpeg_params=["-pix_fmt", "yuv420p"])
    finally:
        clip.close()
        for segment in prepared_segments:
            segment["clip"].close()
    validate_media(output)
    rendered_sources = []
    for beat in beats:
        overlapping = [segment for segment in prepared_segments
                       if segment["start"] < beat["end"] and segment["end"] > beat["start"]]
        if overlapping:
            segment = overlapping[0]
            selected_source = beat.get("selected_source") or {}
            source = {
                "type": "stock_video", "provider": segment["provider"],
                "asset_id": segment["asset_id"], "sha256": segment["sha256"],
                "approved": bool(segment.get("approved")),
                "source_url": selected_source.get("source_page"),
                "license": selected_source.get("license"),
                "license_url": selected_source.get("license_url"),
                "search_term": segment.get("search_term"),
                "selection_reason": selected_source.get("selection_reason"),
                "relevance_evidence": selected_source.get("relevance_evidence"),
            }
        else:
            source = {"type": "original_illustration", "visual": beat.get("top_visual")}
        rendered_sources.append({"beat_id": beat.get("beat_id"),
                                 "start": beat["start"], "end": beat["end"], **source})
        logger.info("top_panel_rendered beat={} start={:.2f} end={:.2f} source={}",
                    beat.get("beat_id"), beat["start"], beat["end"], source)
    task_artifacts._write_json_atomic(
        Path(utils.task_dir(task_id)) / "top-panel-rendered-media.json",
        {"caption_mode": caption_mode, "output": output, "sources": rendered_sources},
    )
    task_artifacts.cache_artifact(task_id, "explainer", fingerprint, output)
    return output
