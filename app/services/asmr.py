"""Helpers for the opt-in satisfying ASMR stock-video selection preset."""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import unquote, urlsplit, urlunsplit

from app.models.schema import MaterialInfo


DEFAULT_STOCK_VIDEO_STYLE = "default"
SATISFYING_ASMR_CUTTING = "satisfying_asmr_cutting"
MAX_ASMR_SEARCH_QUERIES = 12
DEFAULT_ASMR_CATEGORIES = ("kinetic_sand", "soap")

_MATERIALS = {
    "kinetic_sand": ("kinetic sand", "sand"),
    "soap": ("soap",),
    "foam": ("foam", "sponge"),
    "jelly": ("jelly", "gelatin"),
    "crystal": ("crystal", "glass"),
    "lava_cube": ("lava cube", "lava"),
}
_ACTIONS = {
    "cutting": (
        "cutting", "cut", "slicing", "slice", "chopping", "chop",
        "dicing", "dice", "trimming", "trim",
    ),
    "shaving": ("shaving", "shave"),
    "peeling": ("peeling", "peel"),
    "carving": ("carving", "carve"),
}
_EXPANSIONS = {
    ("kinetic_sand", "cutting"): (
        "kinetic sand slicing", "kinetic sand carving", "satisfying kinetic sand cutting",
        "kinetic sand texture cutting", "kinetic sand ASMR slicing",
    ),
    ("soap", "cutting"): (
        "soap shaving", "soap carving", "soap slicing", "satisfying soap cutting",
        "soap texture carving", "soap ASMR slicing",
    ),
    ("soap", "shaving"): (
        "soap cutting", "soap carving", "soap slicing", "satisfying soap shaving",
    ),
    ("foam", "cutting"): (
        "foam slicing", "sponge cutting", "foam carving", "satisfying foam cutting",
        "sponge texture slicing",
    ),
    ("foam", "slicing"): (
        "foam cutting", "sponge slicing", "foam carving", "satisfying sponge cutting",
    ),
    ("jelly", "cutting"): (
        "jelly slicing", "gelatin cutting", "jelly carving", "satisfying jelly cutting",
        "jelly texture slicing",
    ),
    ("jelly", "slicing"): (
        "jelly cutting", "gelatin slicing", "jelly carving", "satisfying gelatin cutting",
    ),
    ("crystal", "cutting"): (
        "crystal slicing", "glass cutting", "crystal carving", "satisfying crystal cutting",
    ),
    ("crystal", "slicing"): (
        "crystal cutting", "glass slicing", "crystal carving", "satisfying glass cutting",
    ),
    ("lava_cube", "cutting"): (
        "lava cube slicing", "lava cube carving", "satisfying lava cube cutting",
    ),
    ("lava_cube", "slicing"): (
        "lava cube cutting", "lava cube carving", "satisfying lava cube slicing",
    ),
}
_QUALITY_CUES = (
    "asmr", "satisfying", "macro", "close up", "closeup", "texture", "textured",
)
ASMR_CATEGORY_QUERIES = {
    "kinetic_sand": ("kinetic sand cutting", "kinetic sand slicing"),
    "soap": ("soap cutting", "soap shaving"),
    "foam": ("foam slicing", "sponge cutting"),
    "jelly": ("jelly slicing", "gelatin cutting"),
    "crystal": ("crystal cutting", "glass cutting"),
    "lava_cube": ("lava cube cutting", "lava cube slicing"),
}


def category_queries(categories: Iterable[str]) -> list[tuple[str, list[str]]]:
    """Return deterministic bounded queries in the caller's category priority order."""
    result = []
    seen = set()
    for category in categories:
        if category not in ASMR_CATEGORY_QUERIES or category in seen:
            continue
        seen.add(category)
        result.append((category, list(ASMR_CATEGORY_QUERIES[category])))
    return result


def _contains_phrase(text: str, phrase: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text))


def classify_asmr_term(term: str) -> tuple[str, str] | None:
    """Return supported material/action families only for explicit ASMR queries."""
    normalized = re.sub(r"[^a-z0-9]+", " ", str(term or "").lower()).strip()
    if not normalized:
        return None

    material = next(
        (
            family
            for family, phrases in _MATERIALS.items()
            if any(_contains_phrase(normalized, phrase) for phrase in phrases)
        ),
        None,
    )
    action = next(
        (
            family
            for family, phrases in _ACTIONS.items()
            if any(_contains_phrase(normalized, phrase) for phrase in phrases)
        ),
        None,
    )
    if material is None or action is None:
        return None
    # Treat slicing as cutting for expansion/ranking while preserving the input query.
    if action == "cutting" and _contains_phrase(normalized, "slice"):
        action = "slicing"
    return material, action


def expand_asmr_search_terms(
    search_terms: Iterable[str],
    *,
    max_queries: int = MAX_ASMR_SEARCH_QUERIES,
) -> list[str]:
    """Keep only material+action queries and add a bounded set of synonyms."""
    limit = max(0, int(max_queries))
    if limit == 0:
        return []
    expanded: list[str] = []
    seen: set[str] = set()
    for raw_term in search_terms:
        term = str(raw_term or "").strip()
        classification = classify_asmr_term(term)
        if not term or classification is None:
            continue
        for candidate in (term, *_EXPANSIONS.get(classification, ())):
            normalized = re.sub(r"\s+", " ", candidate.lower()).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                expanded.append(candidate)
                if len(expanded) >= limit:
                    return expanded
    return expanded


def _metadata_text(item: MaterialInfo) -> str:
    source = item.source_info if isinstance(item.source_info, dict) else {}
    parts = []
    for field in ("tags", "title", "description", "alt"):
        value = source.get(field)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(entry) for entry in value if isinstance(entry, str))
    source_page = source.get("source_page")
    if isinstance(source_page, str):
        parts.append(unquote(urlsplit(source_page).path.replace("-", " ").replace("/", " ")))
    return re.sub(r"[^a-z0-9]+", " ", " ".join(parts).lower()).strip()


def score_asmr_candidate(item: MaterialInfo, search_term: str) -> int:
    """Score explicit material/action metadata; return zero when either is absent."""
    expected = classify_asmr_term(search_term)
    if expected is None:
        return 0
    metadata = _metadata_text(item)
    material = expected[0]
    material_match = any(
        _contains_phrase(metadata, phrase) for phrase in _MATERIALS[material]
    )
    action_match = any(
        _contains_phrase(metadata, phrase) for phrase in _ACTIONS[expected[1]]
    )
    if not material_match or not action_match:
        return 0

    score = 8  # Both required content dimensions are present in provider metadata.
    score += 2 * sum(_contains_phrase(metadata, cue) for cue in _QUALITY_CUES)
    return score


def has_human_asmr_quality_approval(item: MaterialInfo) -> bool:
    """Metadata matches are not a substitute for sampled, human visual review."""
    source = item.source_info if isinstance(item.source_info, dict) else {}
    review = source.get("quality_review")
    if not isinstance(review, dict):
        return source.get("asmr_quality_review") == "approved" and bool(
            source.get("asmr_reviewed_timestamps_seconds")
            and str(source.get("asmr_quality_review_notes", "")).strip()
        )
    return (
        review.get("status") == "approved"
        and bool(str(review.get("reviewer", "")).strip())
        and bool(review.get("sampled_timestamps_seconds"))
        and bool(str(review.get("notes", "")).strip())
    )


def canonical_media_url(url: str) -> str:
    """Normalize a media URL for duplicate detection without changing download URLs."""
    try:
        parsed = urlsplit(str(url or "").strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return str(url or "").strip()
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        netloc = host if port is None or (parsed.scheme.lower(), port) in {
            ("http", 80), ("https", 443)
        } else f"{host}:{port}"
        path = parsed.path.rstrip("/") or "/"
        return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))
    except ValueError:
        return str(url or "").strip()


def rank_asmr_candidates(
    search_results: Iterable[tuple[str, Iterable[MaterialInfo]]],
) -> list[tuple[str, MaterialInfo]]:
    """Rank metadata-confirmed candidates and deduplicate IDs and canonical URLs."""
    scored = []
    for search_term, items in search_results:
        for item in items:
            if not has_human_asmr_quality_approval(item):
                continue
            score = score_asmr_candidate(item, search_term)
            if score > 0:
                scored.append((score, search_term, item))
    scored.sort(key=lambda candidate: candidate[0], reverse=True)

    ranked = []
    seen_asset_ids: set[tuple[str, str]] = set()
    seen_content_hashes: set[str] = set()
    seen_urls: set[str] = set()
    for score, search_term, item in scored:
        source = item.source_info if isinstance(item.source_info, dict) else {}
        provider = str(item.provider or source.get("provider") or "").lower()
        asset_id = source.get("asset_id")
        content_hash = str(
            source.get("content_sha256") or source.get("sha256") or ""
        ).strip().lower()
        canonical_url = canonical_media_url(item.url)
        source_page = source.get("source_page")
        canonical_page = canonical_media_url(source_page) if source_page else ""
        asset_key = (provider, str(asset_id)) if asset_id not in (None, "") else None
        if (canonical_url in seen_urls or (canonical_page and canonical_page in seen_urls)
                or (content_hash and content_hash in seen_content_hashes)
                or (asset_key and asset_key in seen_asset_ids)):
            continue
        seen_urls.add(canonical_url)
        if canonical_page:
            seen_urls.add(canonical_page)
        if asset_key:
            seen_asset_ids.add(asset_key)
        if content_hash:
            seen_content_hashes.add(content_hash)
        item.source_info = dict(source)
        item.source_info["asmr_relevance_score"] = score
        material, action = classify_asmr_term(search_term)
        cues = [cue for cue in _QUALITY_CUES if _contains_phrase(_metadata_text(item), cue)]
        reason = f"metadata confirms {material} material and {action} action"
        if cues:
            reason += "; quality cues: " + ", ".join(cues)
        item.source_info["asmr_selection_reason"] = reason
        ranked.append((search_term, item))
    return ranked
