"""Local-first Savage Animated Explainer background renderer."""

from __future__ import annotations

import math
import json
import hashlib
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np
from loguru import logger
from moviepy import VideoClip, VideoFileClip
from PIL import Image, ImageDraw, ImageFont

from app.services import character_library, emotion_storyboard, hindi_script, material, task_artifacts, visual_review
from app.utils import utils
from app.utils.subtitle_reader import file_to_subtitles


WIDTH, HEIGHT, FPS = 1080, 1920, 30
_BG = (250, 249, 244)
_TOP_PANEL = (96, 350, 984, 820)


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


def _cached_stock_candidates(query: str, provider: str) -> list[dict]:
    """Find decodable local files backed by source/licensing records in MPT's cache."""
    cache_dir = Path(utils.storage_dir("cache_material_search"))
    video_dir = Path(utils.storage_dir("cache_videos"))
    query_terms = {term for term in re.findall(r"[a-z0-9]+", query.casefold())
                   if term not in {"a", "the", "above", "and", "moving", "footage"}}
    candidates: dict[str, dict] = {}
    for cache_file in cache_dir.glob("*.json"):
        try:
            items = json.loads(cache_file.read_text(encoding="utf-8")).get("items", [])
        except (OSError, ValueError, TypeError):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            info = item.get("source_info")
            if not isinstance(info, dict) or str(info.get("provider", "")).casefold() != provider.casefold():
                continue
            license_name = str(info.get("license") or info.get("license_name") or "").strip()
            source_page = str(info.get("source_page") or "").strip()
            url = str(item.get("url") or "").strip()
            if not license_name or not source_page or not url:
                continue
            tags = info.get("tags") or []
            metadata_text = " ".join((str(info.get("description", "")),
                                      str(info.get("title", "")),
                                      " ".join(map(str, tags)))).casefold()
            matched = query_terms & set(re.findall(r"[a-z0-9]+", metadata_text))
            if len(matched) < 2:
                continue
            digest = hashlib.md5(url.encode("utf-8")).hexdigest()
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
                "tags": list(tags) if isinstance(tags, list) else [],
                "relevance_score": len(matched),
            }
            previous = candidates.get(digest)
            if previous is None or candidate["relevance_score"] > previous["relevance_score"]:
                candidates[digest] = candidate
    return sorted(candidates.values(), key=lambda value: (-value["relevance_score"], value["asset_id"]))


def _search_and_cache_stock_candidate(query: str, provider: str, task_id: str) -> list[dict]:
    """Use MPT's configured provider search/cache and existing downloader for a beat."""
    searchers = {
        "pixabay": material.search_videos_pixabay,
        "coverr": material.search_videos_coverr,
        "pexels": material.search_videos_pexels,
    }
    searcher = searchers.get(provider)
    if searcher is None:
        return []
    try:
        material.get_api_key(f"{provider}_api_keys")
        items = material._search_videos_with_cache(
            provider=provider, search_videos=searcher, search_term=query,
            minimum_duration=3, video_aspect="16:9",
        )
    except Exception as exc:
        logger.warning(f"optional {provider} storyboard search failed: {type(exc).__name__}")
        return []
    scored = []
    query_terms = {term for term in re.findall(r"[a-z0-9]+", query.casefold())
                   if term not in {"a", "the", "above", "and", "moving", "footage"}}
    for item in items:
        info = item.source_info if isinstance(item.source_info, dict) else {}
        if not info.get("license") and provider != "coverr":
            continue
        tags = info.get("tags") or []
        words = set(re.findall(r"[a-z0-9]+", " ".join(map(str, tags)).casefold()))
        score = len(query_terms & words)
        if score < 1:
            continue
        try:
            path = material._save_material_item(item, str(Path(utils.task_dir(task_id)) / "top-panel-media"))
        except Exception:
            continue
        media = _probe_video(path)
        if not media:
            continue
        scored.append({
            "provider": provider, "asset_id": str(info.get("asset_id") or ""),
            "source_page": info.get("source_page"),
            "license": info.get("license") or "Coverr License",
            "license_url": info.get("license_url") or "https://coverr.co/license",
            "url": item.url, "path": str(path),
            "sha256": task_artifacts.file_digest(path),
            "duration": media["duration"], "width": media["width"],
            "height": media["height"], "codec": media["codec"],
            "tags": list(tags) if isinstance(tags, list) else [],
            "relevance_score": score,
        })
    return sorted(scored, key=lambda value: (-value["relevance_score"], value["asset_id"]))


def resolve_storyboard_media(
    task_id: str,
    beats: list[dict],
    *,
    provider: str = "pixabay",
    allow_unapproved_preview: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Resolve beat media to approved local files; pending assets remain fallbacks."""
    used_assets: set[str] = set()
    media_segments = []
    report = []
    for beat in beats:
        entry = {
            "beat_id": beat.get("beat_id"), "start": beat.get("start"), "end": beat.get("end"),
            "short_visual_title": beat.get("short_visual_title"),
            "query": beat.get("stock_query"), "planned_type": beat.get("visual_mode"),
            "effective_type": beat.get("visual_mode"),
            "selection_reason": beat.get("visual_rationale"), "fallback_reason": None,
            "provider": None, "asset_id": None, "source_page": None, "license": None,
            "license_url": None, "sha256": None, "local_path": None,
            "approval_status": "not_required",
            "source_url": None, "provider_download_url": None,
        }
        if beat.get("visual_mode") != "stock_video" or not beat.get("stock_query"):
            report.append(entry)
            continue
        candidates = _cached_stock_candidates(beat["stock_query"], provider)
        if not candidates:
            candidates = _search_and_cache_stock_candidate(
                beat["stock_query"], provider, task_id
            )
        selected = next((item for item in candidates
                         if item["asset_id"] not in used_assets), None)
        if not selected:
            entry["fallback_reason"] = "No licensed, relevant, locally decodable provider candidate was available."
            beat["effective_media_type"] = "illustration"
            beat["top_visual"] = "cloud_illustration" if "cloud" in str(beat.get("top_visual")) else beat.get("top_visual")
            report.append(entry)
            continue
        used_assets.add(selected["asset_id"])
        approval = visual_review.human_approval(selected["asset_id"], selected["sha256"])
        entry.update({key: selected.get(key) for key in (
            "provider", "asset_id", "source_page", "license", "license_url", "sha256", "path"
        )})
        entry["source_url"] = selected.get("source_page")
        if selected.get("provider") != "coverr":
            entry["provider_download_url"] = selected.get("url")
        entry["local_path"] = selected["path"]
        entry["approval_status"] = "approved" if approval else "human_review_required"
        if approval:
            entry["human_approval"] = approval
        if not approval:
            try:
                entry["visual_review"] = visual_review.sample_clip(
                    selected["path"], task_id, selected["asset_id"],
                    usable_seconds=min(float(selected["duration"]), max(.5, beat["end"]-beat["start"])),
                    video_aspect="16:9",
                )
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        if approval:
            beat["effective_media_type"] = "stock_video"
            entry["effective_type"] = "stock_video"
            beat["selected_source"] = {key: selected.get(key) for key in (
                "provider", "asset_id", "source_page", "license", "license_url", "sha256"
            )}
            media_segments.append({**selected, "start": beat["start"], "end": beat["end"], "approved": True})
        elif allow_unapproved_preview:
            beat["effective_media_type"] = "stock_video_review_only"
            entry["effective_type"] = "stock_video_review_only"
            media_segments.append({**selected, "start": beat["start"], "end": beat["end"], "approved": False})
            entry["fallback_reason"] = "Shown only in the visibly watermarked local review preview; approval is pending."
        else:
            beat["effective_media_type"] = "illustration"
            entry["effective_type"] = "illustration"
            beat["top_visual"] = "cloud_illustration" if "cloud" in str(beat.get("top_visual")) else beat.get("top_visual")
            entry["fallback_reason"] = "Human visual approval is required before this source can appear in a final render."
        report.append(entry)
    report_path = Path(utils.task_dir(task_id)) / "top-panel-storyboard.json"
    try:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning(f"could not save optional top-panel storyboard report: {type(exc).__name__}")
    return beats, media_segments


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


def _draw_topic_scene(
    draw: ImageDraw.ImageDraw, font_path: str, beat: dict | None, phase: float,
    topic: str = "",
) -> None:
    """Draw a topic-neutral comparison whose beat follows aligned narration."""
    navy, red, green, yellow = (40, 55, 70), (226, 80, 65), (56, 142, 90), (255, 230, 0)
    visual = (beat or {}).get("top_visual", "topic_diagram")
    text = str((beat or {}).get("text", "")).casefold()
    subject_text = f"{topic} {text}".casefold()
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

    if (any(word in subject_text for word in ("गाँव", "गांव", "village"))
            and any(word in subject_text for word in ("शहर", "city"))):
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

    # Use simple local symbols only when the narration names the subject.
    if any(term in text for term in ("चिड़िया", "पक्षी", "bird", "tit")):
        draw.ellipse((380, 475, 700, 690), fill=(255, 230, 0), outline=navy, width=8)
        draw.ellipse((575, 500, 620, 545), fill=navy)
        draw.polygon(((690, 560), (800, 520), (790, 600)), fill=red)
        draw.arc((290, 420, 550, 730), 200, 350, fill=navy, width=20)
        if any(term in text for term in ("मौसम", "बारिश", "धूप", "weather", "rain")):
            draw.ellipse((690, 390, 855, 555), fill=yellow, outline=navy, width=6)
            draw.arc((155, 660, 420, 840), 180, 360, fill=(120, 155, 185), width=22)
            for x in (220, 285, 350):
                draw.line((x, 710, x - 25, 770), fill=(60, 142, 211), width=10)
        return
    if any(term in text for term in ("space", "अंतरिक्ष", "ग्रह", "planet", "moon", "चाँद")):
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
    folded = text.casefold()
    if "बादल" in text or "cloud" in folded:
        return "बादल का वजन"
    if "client" in folded and "server" in folded:
        return "Client vs Server"
    if "iss" in folded or "astronaut" in folded or "अंतरिक्ष" in text:
        return "ISS में ZERO-G?"
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
    is_iss_fact = any(token in f"{topic} {topic.lower()}" for token in
                      ("ISS", "iss", "gravity", "गुरुत्व", "आईएसएस", "अंतरिक्ष स्टेशन"))
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
    if is_iss_fact:
        if active_beat:
            phase = max(0.0, min(1.0, (time_seconds - active_beat["start"]) /
                                  max(0.001, active_beat["end"] - active_beat["start"])))
            _draw_visual_scene(draw, font_path, active_beat["top_visual"], phase, time_seconds)
        else:
            _draw_visual_scene(draw, font_path, "gravity_misconception", 0.0, time_seconds)
    else:
        phase = 0.0
        if active_beat:
            phase = max(0.0, min(1.0, (time_seconds - active_beat["start"]) /
                                  max(0.001, active_beat["end"] - active_beat["start"])))
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
    if active_group is not None:
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
                      stock_provider: str = "pixabay",
                      preview_unapproved_stock: bool = False) -> str:
    """Render a 1080×1920, 30 FPS local explainer scene with the saved narrator."""
    if not math.isfinite(float(duration)) or duration <= 0:
        raise ValueError("explainer duration must be a positive finite number")
    assets = character_library.inspect_pack()
    output = output_path or os.path.join(utils.task_dir(task_id), "savage-explainer-background.mp4")
    cues = _read_word_cues(subtitle_path)
    display_word_map = display_word_map or _task_display_map(task_id)
    groups = _caption_groups(_display_cues(cues, display_word_map))
    beats = emotion_storyboard.resolve(emotion_beats or [], cues)
    beats, stock_segments = resolve_storyboard_media(
        task_id, beats, provider=stock_provider,
        allow_unapproved_preview=preview_unapproved_stock,
    )
    prepared_segments = []
    panel_dir = Path(utils.task_dir(task_id)) / "top-panel-media"
    panel_dir.mkdir(parents=True, exist_ok=True)
    for segment in stock_segments:
        seconds = float(segment["end"]) - float(segment["start"])
        if seconds <= 0 or seconds > float(segment["duration"]):
            continue
        prepared_path = panel_dir / (
            f"{segment['sha256'][:16]}-{round(seconds * 1000)}ms-panel.mp4"
        )
        if not prepared_path.is_file():
            result = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", segment["path"], "-t", f"{seconds:.3f}", "-an", "-sn", "-dn",
                "-vf", "scale=888:470:force_original_aspect_ratio=increase,crop=888:470,fps=30",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                "-pix_fmt", "yuv420p", str(prepared_path),
            ], check=False, capture_output=True, text=True)
            if result.returncode or not prepared_path.is_file():
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
    task_artifacts.cache_artifact(task_id, "explainer", fingerprint, output)
    return output
