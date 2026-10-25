import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from webui.autopilot_view import group_video_attempts, sync_manual_style_state


def test_manual_style_defaults_to_savage_and_keeps_later_user_selection():
    styles = {
        "Savage Animated Explainer": "savage_animated_explainer",
        "Relevant Stock": "relevant_stock",
        "ASMR Cutting": "asmr_cutting",
        "AUTO": "auto",
    }
    state = {
        "simple_style_channel-a": "ASMR Cutting",
        "simple_style_profile_version_channel-a": 9,
    }
    sync_manual_style_state(state, "channel-a", 9, "asmr_cutting", styles)
    assert state["simple_style_channel-a"] == "Savage Animated Explainer"
    state["simple_style_channel-a"] = "Relevant Stock"
    sync_manual_style_state(state, "channel-a", 9, "asmr_cutting", styles)
    assert state["simple_style_channel-a"] == "Relevant Stock"


def job(job_id, created=1, **changes):
    values = dict(job_id=job_id, channel_id="channel-a", topic="समुद्र के जीव",
                  script="समुद्र में जीव चमकते हैं।", created_at=created,
                  intended_publication_slot=None, profile_snapshot={
                      "publishing_policy": "manual", "publishing_backend": "youtube_direct",
                      "youtube_connection_id": "connection-a", "youtube_channel_id": "youtube-a"})
    values.update(changes)
    return SimpleNamespace(**values)


def test_retries_collapse_into_latest_attempt_without_hiding_distinct_scripts():
    original = job("asmr", created=1)
    fallback = job("stock", created=2)
    revised = job("revision", created=3, script="A revised narration")
    groups = group_video_attempts([original, fallback, revised])
    assert [[entry.job_id for entry in group] for group in groups] == [
        ["revision"], ["stock", "asmr"]]


def test_channels_pinned_destinations_and_scheduled_videos_remain_separate():
    first = job("first")
    destination = job("other-destination", profile_snapshot={
        **first.profile_snapshot, "youtube_channel_id": "youtube-b"})
    channel = job("other-channel", channel_id="channel-b")
    scheduled = job("scheduled", intended_publication_slot="2026-10-24T09:00:00Z")
    assert len(group_video_attempts([first, destination, channel, scheduled])) == 4


@pytest.mark.parametrize("state,action", [("ready", "Start generation"), ("failed", "Retry video")])
def test_video_card_has_one_clear_action_without_duplicate_widget_keys(state, action):
    # Execute the real card renderer in isolation, without booting the application
    # or accessing channel storage/providers. This catches duplicate Start buttons.
    main = Path(__file__).resolve().parents[2] / "webui" / "Main.py"
    source = main.read_text()
    renderer = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "_render_autopilot_video_attempt")
    code = '''
import streamlit as st
from types import SimpleNamespace
from datetime import datetime
autopilot_service = SimpleNamespace(get_service=lambda: None)
_autopilot_failure_help = lambda message: message
job = SimpleNamespace(job_id="test-job", topic="Test topic", script="Test script",
    state=STATE, created_at=datetime(2026, 10, 23), profile_snapshot={"display_name": "Channel"},
    params_snapshot={"stock_video_style": "default"}, mpt_task_id=None,
        visual_style="relevant_stock", effective_visual_style="relevant_stock", visual_style_reason="",
    failure_details="", output_artifacts=[], intended_publication_slot=None)
'''.replace("STATE", repr(state))
    code += ast.get_source_segment(source, renderer)
    code += '\n_render_autopilot_video_attempt(job, "channel-a", None)\n'
    app = AppTest.from_string(code).run()
    assert not app.exception
    assert len([button for button in app.button if button.label == action]) == 1
    assert not app.expander[0].proto.expanded


def test_portrait_preview_is_collapsed_and_audio_qa_error_is_explained(tmp_path):
    main = Path(__file__).resolve().parents[2] / "webui" / "Main.py"
    source = main.read_text()
    nodes = ast.parse(source).body
    failure_helper = next(node for node in nodes
                          if isinstance(node, ast.FunctionDef) and node.name == "_autopilot_failure_help")
    renderer = next(node for node in nodes
                    if isinstance(node, ast.FunctionDef) and node.name == "_render_autopilot_video_attempt")
    artifact = tmp_path / "portrait.mp4"
    artifact.write_bytes(b"mock video bytes")
    code = f'''
import os
import streamlit as st
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
autopilot_service = SimpleNamespace(get_service=lambda: None)
_autopilot_thumbnail = lambda *_: None
job = SimpleNamespace(job_id="qa-job", topic="Test topic", script="Test script",
    state="failed", created_at=datetime(2026, 10, 23),
    profile_snapshot={{"display_name": "Channel"}},
    params_snapshot={{"stock_video_style": "default"}}, mpt_task_id=None,
        visual_style="relevant_stock", effective_visual_style="relevant_stock", visual_style_reason="",
    failure_details="post-generation QA failed: audio_track; output artifacts retained",
    output_artifacts=[{str(artifact)!r}], intended_publication_slot=None)
'''
    code += ast.get_source_segment(source, failure_helper)
    code += "\n" + ast.get_source_segment(source, renderer)
    code += '\n_render_autopilot_video_attempt(job, "channel-a", None)\n'
    app = AppTest.from_string(code).run()
    assert not app.exception
    assert any("audio track did not pass" in item.value for item in app.error)
    assert [item.label for item in app.expander] == ["Preview rendered video", "Script and more actions"]
    assert not any(item.label == "Preview rendered video" and item.proto.expanded for item in app.expander)
    assert len([button for button in app.button if button.label == "Retry video"]) == 1


@pytest.mark.parametrize("style,label", [
    ("asmr_cutting", "ASMR Cutting"),
    ("relevant_stock", "Relevant Stock"),
])
def test_video_card_identifies_rendering_snapshot_and_task(style, label):
    main = Path(__file__).resolve().parents[2] / "webui" / "Main.py"
    source = main.read_text()
    renderer = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "_render_autopilot_video_attempt")
    code = f'''
import streamlit as st
from types import SimpleNamespace
from datetime import datetime
autopilot_service = SimpleNamespace(get_service=lambda: None)
sm = SimpleNamespace(state=SimpleNamespace(get_task=lambda _: None))
_autopilot_failure_help = lambda message: message
job = SimpleNamespace(job_id="12345678-job", topic="Test topic", script="Test script",
    state="failed", created_at=datetime(2026, 10, 23),
    profile_snapshot={{"display_name": "Channel"}},
        params_snapshot={{"visual_style": {style!r}}},
        visual_style={style!r}, effective_visual_style={style!r}, visual_style_reason="",
    mpt_task_id="87654321-task", failure_details="", output_artifacts=[],
    intended_publication_slot=None)
'''
    code += ast.get_source_segment(source, renderer)
    code += '\n_render_autopilot_video_attempt(job, "channel-a", None)\n'
    app = AppTest.from_string(code).run()
    assert not app.exception
    assert any(label in item.value and "Job 12345678 · Task 87654321" in item.value
               for item in app.get("caption"))
