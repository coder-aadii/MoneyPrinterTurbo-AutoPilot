"""Versioned raster pose library for the Facts Sun Le Bhai narrator."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

from app.paths import resource_directory


BASE_POSE_NAMES = (
    "explaining", "waving", "pointing_up", "pointing_right", "confused",
    "angry", "shocked", "laughing", "thinking", "skeptical",
    "celebrating", "presenting",
)
EXPRESSION_POSE_NAMES = (
    "deadpan", "smug", "facepalm", "exasperated_sigh", "whispering",
    "idea_reveal", "nervous_sweat", "frustrated_burst", "dramatic_pause",
    "serious_fact",
)
POSE_NAMES = BASE_POSE_NAMES + EXPRESSION_POSE_NAMES
_PACK_NAME = "facts_sun_le_bhai"


def pack_root() -> Path:
    """Return the single bundled canonical avatar pack."""
    bundled = resource_directory("characters") / _PACK_NAME
    if (bundled / "manifest.json").is_file():
        return bundled
    raise FileNotFoundError(
        "Facts Sun Le Bhai canonical pose pack is unavailable under "
        "resource/characters/facts_sun_le_bhai."
    )


def inspect_pack(root: str | Path | None = None) -> dict:
    """Validate manifest paths, hashes, PNG alpha and report a content version."""
    root = Path(root) if root is not None else pack_root()
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    canvas = manifest.get("canvas_dimensions")
    if (not isinstance(canvas, list) or len(canvas) != 2
            or any(not isinstance(value, int) or value <= 0 for value in canvas)):
        raise ValueError("pose manifest must declare positive common canvas_dimensions")
    anchor = manifest.get("anchor")
    if (not isinstance(anchor, dict) or anchor.get("type") != "ground_center"
            or anchor.get("x") != canvas[0] // 2 or anchor.get("y") != canvas[1]):
        raise ValueError("pose manifest must declare a centered ground anchor")
    entries = manifest.get("poses")
    by_name = {entry.get("name"): entry for entry in entries or [] if isinstance(entry, dict)}
    pose_names = set(by_name)
    if (not set(BASE_POSE_NAMES).issubset(pose_names)
            or pose_names - set(POSE_NAMES)
            or len(entries or []) != len(pose_names)):
        raise ValueError("pose manifest must contain the 12 core poses and only supported optional expressions")

    aliases = manifest.get("aliases", {})
    if (not isinstance(aliases, dict)
            or any(not isinstance(alias, str) or not alias
                   or not isinstance(target, str) or target not in pose_names
                   or alias in pose_names for alias, target in aliases.items())):
        raise ValueError("pose aliases must map unique alias names to canonical pose IDs")

    root = root.resolve()
    pose_records = []
    for name in POSE_NAMES:
        if name not in by_name:
            continue
        entry = by_name[name]
        filename = entry.get("file", "")
        if not isinstance(filename, str) or not filename:
            raise ValueError(f"unsafe or missing filename for pose {name}")
        path = (root / filename).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"unsafe path for pose {name}") from exc
        if not path.is_file():
            raise FileNotFoundError(f"missing {name} pose: {filename}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if entry.get("sha256") and digest != entry["sha256"]:
            raise ValueError(f"SHA-256 mismatch for {name} pose")
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.format != "PNG" or image.mode not in {"RGBA", "LA"}:
                raise ValueError(f"{name} pose must be a transparent PNG")
            if list(image.size) != canvas or entry.get("dimensions") != canvas:
                raise ValueError(f"{name} pose does not match the common canvas dimensions")
            alpha = image.getchannel("A")
            if alpha.getbbox() is None:
                raise ValueError(f"{name} pose is fully transparent")
            if alpha.getextrema() == (255, 255):
                raise ValueError(f"{name} pose has no transparent pixels")
            alpha_bbox = list(alpha.getbbox())
            if entry.get("alpha_bbox") != alpha_bbox:
                raise ValueError(f"{name} pose alpha bounds do not match the manifest")
            pose_anchor = entry.get("anchor")
            if (not isinstance(pose_anchor, dict)
                    or pose_anchor.get("type") != anchor["type"]
                    or pose_anchor.get("x") != anchor["x"]
                    or pose_anchor.get("y") != anchor["y"]):
                raise ValueError(f"{name} pose anchor does not match the pack anchor")
            pose_records.append({"name": name, "file": filename, "sha256": digest,
                                 "size": list(image.size), "alpha_bbox": alpha_bbox,
                                 "anchor": pose_anchor, "alpha": True})

    content = "\n".join(item["sha256"] for item in pose_records).encode("ascii")
    version = (
        manifest.get("asset_version")
        or manifest.get("version")
        or hashlib.sha256(content).hexdigest()[:16]
    )
    return {"name": manifest.get("name", _PACK_NAME), "asset_version": version,
            "manifest": str(manifest_path), "canvas_dimensions": canvas,
            "anchor": anchor, "aliases": aliases, "poses": pose_records}


def load_pose(name: str, *, canvas: tuple[int, int] = (306, 429), root: str | Path | None = None) -> Image.Image:
    """Load a pose on a shared transparent canvas, centered and ground-aligned."""
    report = inspect_pack(root or pack_root())
    canonical_name = report["aliases"].get(name, name)
    if canonical_name not in POSE_NAMES:
        raise ValueError(f"unsupported character pose: {name}")
    root = Path(root).resolve() if root is not None else pack_root()
    record = next(item for item in report["poses"] if item["name"] == canonical_name)
    with Image.open(root / record["file"]) as source:
        image = source.convert("RGBA")
    # Several supplied cells contain tiny detached opaque flecks at the very
    # bottom edge. Remove only small connected components in that edge band;
    # preserve the source PNG and its hash in the approved library.
    alpha = np.asarray(image.getchannel("A")).copy()
    edge_y = max(0, image.height - 12)
    band = alpha[edge_y:, :] > 32
    visited = np.zeros(band.shape, dtype=bool)
    for row, column in zip(*np.nonzero(band)):
        if visited[row, column]:
            continue
        stack = [(int(row), int(column))]
        visited[row, column] = True
        component = []
        while stack:
            cy, cx = stack.pop()
            component.append((cy, cx))
            for ny in range(max(0, cy - 1), min(band.shape[0], cy + 2)):
                for nx in range(max(0, cx - 1), min(band.shape[1], cx + 2)):
                    if band[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))
        if len(component) < 100:
            for cy, cx in component:
                alpha[edge_y + cy, cx] = 0
    image.putalpha(Image.fromarray(alpha, mode="L"))
    bounds = image.getchannel("A").getbbox()
    if bounds is None:
        raise ValueError(f"{name} pose is fully transparent")
    subject = image.crop(bounds)
    scale = min(canvas[0] / subject.width, canvas[1] / subject.height)
    size = (max(1, round(subject.width * scale)), max(1, round(subject.height * scale)))
    subject = subject.resize(size, Image.Resampling.LANCZOS)
    normalized = Image.new("RGBA", canvas, (0, 0, 0, 0))
    x = (canvas[0] - subject.width) // 2
    y = canvas[1] - subject.height
    normalized.alpha_composite(subject, (x, y))
    return normalized
