from pathlib import Path

from PIL import ImageChops

from app.services import character_library


def test_supplied_pack_contains_all_verified_transparent_poses():
    report = character_library.inspect_pack()
    assert report["asset_version"] == "3.0.0"
    assert [pose["name"] for pose in report["poses"]] == list(character_library.POSE_NAMES)
    assert all(pose["alpha"] and len(pose["sha256"]) == 64 for pose in report["poses"])
    assert report["canvas_dimensions"] == [306, 429]
    assert report["anchor"] == {
        "type": "ground_center", "coordinate_system": "top-left-origin",
        "x": 153, "y": 429,
    }
    assert not (Path(report["manifest"]).parent / "versions").exists()


def test_aliases_load_the_same_canonical_sprite_without_duplicate_files():
    canonical = character_library.load_pose("explaining")
    alias = character_library.load_pose("neutral")
    assert ImageChops.difference(canonical, alias).getbbox() is None
    report = character_library.inspect_pack()
    assert report["aliases"]["neutral"] == "explaining"
    assert len(report["poses"]) == 22


def test_pose_loading_normalizes_canvas_and_preserves_alpha():
    expected = (512, 640)
    anchors = []
    for name in character_library.POSE_NAMES:
        pose = character_library.load_pose(name, canvas=expected)
        assert pose.mode == "RGBA"
        assert pose.size == expected
        alpha = pose.getchannel("A")
        assert alpha.getextrema()[0] == 0
        anchors.append(alpha.getbbox()[3])
    assert len(set(anchors)) == 1


def test_missing_pack_reports_user_upload_location(tmp_path: Path):
    try:
        character_library.inspect_pack(tmp_path)
    except FileNotFoundError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("missing manifest should be reported")
