import ast
import copy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_new_versions_remain_visible_as_separate_gallery_cards():
    original = job("original", created=1)
    version = job("version", created=2, parent_job_id="original")
    assert [[entry.job_id for entry in group]
            for group in group_video_attempts([original, version])] == [["version"], ["original"]]


def test_gallery_edit_callback_persists_source_and_routes_to_create_video():
    main = Path(__file__).resolve().parents[2] / "webui" / "Main.py"
    tree = ast.parse(main.read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    callback = functions["_queue_autopilot_video_edit"]
    callback_calls = {
        node.func.attr for node in ast.walk(callback)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "get_job" in callback_calls
    assert any(isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Subscript)
                       and isinstance(target.value, ast.Attribute)
                       and target.value.attr == "session_state"
                       and isinstance(target.slice, ast.Constant)
                       and target.slice.value == "autopilot_main_tabs"
                       for target in node.targets)
               for node in ast.walk(callback))
    card = functions["_render_autopilot_gallery_card"]
    edit_button = next(node for node in ast.walk(card)
                       if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Attribute)
                       and node.func.attr == "button"
                       and node.args and isinstance(node.args[0], ast.Constant)
                       and node.args[0].value == "Edit as a new version")
    assert any(keyword.arg == "on_click" and isinstance(keyword.value, ast.Name)
               and keyword.value.id == "_queue_autopilot_video_edit"
               for keyword in edit_button.keywords)


def test_gallery_edit_callback_fires_and_keeps_source_in_session_state():
    main = Path(__file__).resolve().parents[2] / "webui" / "Main.py"
    source = main.read_text(encoding="utf-8")
    callback = _main_function("_queue_autopilot_video_edit")
    code = '''
import streamlit as st
from types import SimpleNamespace
source_job = SimpleNamespace(job_id="source-job", channel_id="channel-a",
    topic="A saved topic", script="A saved narration", params_snapshot={"visual_style": "relevant_stock"})
autopilot_service = SimpleNamespace(get_service=lambda: SimpleNamespace(
    store=SimpleNamespace(get_job=lambda _: source_job)))
'''
    code += ast.get_source_segment(source, callback)
    code += '''
st.button("Edit", key="edit_source", on_click=_queue_autopilot_video_edit,
    args=("source-job", "channel-a"))
'''
    app = AppTest.from_string(code).run()
    assert not app.exception
    app.button(key="edit_source").click().run()
    assert not app.exception
    assert app.session_state["autopilot_main_tabs"] == "Create Video"
    assert app.session_state["autopilot_edit_job"]["job_id"] == "source-job"
    assert app.session_state["autopilot_edit_job"]["params_snapshot"]["visual_style"] == "relevant_stock"


def test_gallery_edit_callback_reports_a_deleted_source_video():
    main = Path(__file__).resolve().parents[2] / "webui" / "Main.py"
    source = main.read_text(encoding="utf-8")
    callback = _main_function("_queue_autopilot_video_edit")
    code = '''
import streamlit as st
from types import SimpleNamespace
def missing_job(_):
    raise LookupError("production job not found")
autopilot_service = SimpleNamespace(get_service=lambda: SimpleNamespace(
    store=SimpleNamespace(get_job=missing_job)))
'''
    code += ast.get_source_segment(source, callback)
    code += '''
st.button("Edit", key="edit_missing", on_click=_queue_autopilot_video_edit,
    args=("deleted-source", "channel-a"))
'''
    app = AppTest.from_string(code).run()
    assert not app.exception
    app.button(key="edit_missing").click().run()
    assert app.session_state["autopilot_edit_error"] == "production job not found"
    assert app.session_state["autopilot_main_tabs"] == "Create Video"


def test_edit_form_prepopulation_is_applied_once_across_reruns():
    main = Path(__file__).resolve().parents[2] / "webui" / "Main.py"
    source = main.read_text(encoding="utf-8")
    tree = ast.parse(source)
    helper = next(node for node in tree.body
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "_apply_autopilot_edit_form_state")
    namespace = {"VISUAL_STYLE_VALUES": {
        "Savage Animated Explainer": "savage_animated_explainer",
        "Relevant Stock": "relevant_stock",
    }}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(main), "exec"), namespace)
    profile = SimpleNamespace(channel_id="channel-a", target_duration_seconds=30)
    edited = {
        "job_id": "source-a", "channel_id": "channel-a", "topic": "A source topic",
        "script": "Original editable script", "params_snapshot": {
            "target_duration_seconds": 42, "video_aspect": "16:9",
            "visual_style": "relevant_stock", "video_language": "en",
            "narrator_preset": "standard", "profanity_level": "mild",
            "fact_verification_policy": "strict", "subtitle_enabled": False,
        },
    }
    state = {}
    namespace["_apply_autopilot_edit_form_state"](state, edited, profile)
    assert state["simple_topic_channel-a"] == "A source topic"
    assert state["simple_draft_channel-a"] == "Original editable script"
    assert state["simple_duration_channel-a"] == 42
    assert state["simple_format_channel-a"] == "Landscape (16:9)"
    assert state["simple_style_channel-a"] == "Relevant Stock"
    assert state["simple_language_channel-a"] == "English"
    assert state["simple_fact_policy_channel-a"] == "strict"
    sync_manual_style_state(
        state, "channel-a", 1, "savage_animated_explainer",
        namespace["VISUAL_STYLE_VALUES"], 30, "9:16",
    )
    assert state["simple_style_channel-a"] == "Relevant Stock"
    assert state["simple_duration_channel-a"] == 42
    state["simple_draft_channel-a"] = "User's edited narration"
    namespace["_apply_autopilot_edit_form_state"](state, edited, profile)
    assert state["simple_draft_channel-a"] == "User's edited narration"


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


def _main_function(name):
    main = Path(__file__).resolve().parents[2] / "webui" / "Main.py"
    source = main.read_text(encoding="utf-8")
    node = next(item for item in ast.parse(source).body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name)
    return copy.deepcopy(node)


def test_gallery_card_uses_playable_thumbnail_and_one_overflow_menu():
    node = _main_function("_render_autopilot_gallery_card")
    close_menu = _main_function("_close_autopilot_gallery_action_menu")
    edit_callback = _main_function("_queue_autopilot_video_edit")
    code = '''
import html
import streamlit as st
from datetime import datetime
from types import SimpleNamespace
autopilot_service = SimpleNamespace(get_service=lambda: SimpleNamespace())
sm = SimpleNamespace(state=SimpleNamespace(get_task=lambda _: None))
_autopilot_video_output = lambda _: None
_autopilot_failure_help = lambda message: message
job = SimpleNamespace(job_id="gallery-test", topic="A long title", state="failed",
    output_artifacts=[], created_at=datetime(2026, 10, 23), params_snapshot={"video_aspect": "9:16"},
    profile_snapshot={}, actual_duration_seconds=31.2, duration_estimate_seconds=31.2,
    mpt_task_id=None, failure_details="", intended_publication_slot=None,
    script="", visual_style="relevant_stock")
'''
    code += ast.get_source_segment(
        (Path(__file__).resolve().parents[2] / "webui" / "Main.py").read_text(encoding="utf-8"),
        close_menu,
    ) + "\n"
    code += ast.get_source_segment(
        (Path(__file__).resolve().parents[2] / "webui" / "Main.py").read_text(encoding="utf-8"),
        edit_callback,
    ) + "\n"
    code += ast.get_source_segment(
        (Path(__file__).resolve().parents[2] / "webui" / "Main.py").read_text(encoding="utf-8"),
        node,
    )
    code += '\n_render_autopilot_gallery_card(job, "channel-a", None, [])\n'
    app = AppTest.from_string(code).run()
    assert not app.exception
    labels = [button.label for button in app.button]
    assert labels.count("Play video") == 1
    assert "Preview" not in labels
    app.button(key="gallery_play_gallery-test").click().run()
    assert not app.exception
    assert len(app.get("toast")) == 1
    source = (Path(__file__).resolve().parents[2] / "webui" / "Main.py").read_text()
    card_source = ast.get_source_segment(source, node)
    assert card_source.count("st.popover(") == 1
    popover = next(item for item in ast.walk(node)
                   if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
                   and item.func.attr == "popover")
    assert next(keyword.value.value for keyword in popover.keywords
                if keyword.arg == "width") == 190
    assert next(keyword.value.value for keyword in popover.keywords
                if keyword.arg == "on_change") == "rerun"


def test_gallery_media_lookup_rejects_paths_outside_storage(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    inside = storage / "final-video.mp4"
    inside.write_bytes(b"video")
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"private")

    node = _main_function("_autopilot_video_output")
    namespace = {
        "os": os,
        "Path": Path,
        "utils": SimpleNamespace(storage_dir=lambda: str(storage)),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "gallery.py", "exec"), namespace)
    lookup = namespace["_autopilot_video_output"]
    assert lookup(SimpleNamespace(output_artifacts=[str(inside)])) == str(inside)
    assert lookup(SimpleNamespace(output_artifacts=[str(outside)])) is None


def test_gallery_thumbnail_is_a_small_cached_frame_and_corrupt_input_falls_back():
    node = _main_function("_autopilot_thumbnail")
    node.decorator_list = []
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=b"jpeg-frame"))
    namespace = {
        "subprocess": SimpleNamespace(run=run, TimeoutExpired=TimeoutError),
        "utils": SimpleNamespace(get_ffmpeg_binary=lambda: "ffmpeg"),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "gallery.py", "exec"), namespace)
    thumbnail = namespace["_autopilot_thumbnail"]("/storage/video.mp4", 42)
    assert thumbnail == b"jpeg-frame"
    command = run.call_args.args[0]
    assert command[command.index("-ss") + 1] == "1"
    assert command[command.index("-vf") + 1] == "scale=480:-2"
    assert command[-1] == "pipe:1"

    run.return_value = SimpleNamespace(returncode=1, stdout=b"")
    assert namespace["_autopilot_thumbnail"]("/storage/broken.mp4", 42) is None


def test_gallery_player_navigation_updates_the_selected_video(tmp_path):
    main = Path(__file__).resolve().parents[2] / "webui" / "Main.py"
    source = main.read_text(encoding="utf-8")
    nodes = ast.parse(source).body
    player = next(node for node in nodes
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "_render_autopilot_gallery_video_modal")
    dismiss = next(node for node in nodes
                   if isinstance(node, ast.FunctionDef)
                   and node.name == "_dismiss_autopilot_gallery_player")
    select_player = next(node for node in nodes
                         if isinstance(node, ast.FunctionDef)
                         and node.name == "_select_autopilot_gallery_player")
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"synthetic test media")
    code = f'''
import streamlit as st
from datetime import datetime
from types import SimpleNamespace
first = SimpleNamespace(job_id="first", topic="First clip", created_at=datetime(2026, 10, 24), params_snapshot={{"video_aspect": "9:16"}})
second = SimpleNamespace(job_id="second", topic="Second clip", created_at=datetime(2026, 10, 23), params_snapshot={{"video_aspect": "9:16"}})
videos = [(first, {str(video_path)!r}), (second, {str(video_path)!r})]
st.session_state.setdefault("gallery_play_job_id", "first")
'''
    code += ast.get_source_segment(source, dismiss) + "\n"
    code += ast.get_source_segment(source, select_player) + "\n"
    code += ast.get_source_segment(source, player)
    code += '''
_render_autopilot_gallery_video_modal(videos)
'''
    app = AppTest.from_string(code).run()
    assert not app.exception
    assert any(item.value == "First clip" for item in app.get("caption"))
    app.button(key="gallery_player_next").click().run()
    assert not app.exception
    assert any(item.value == "Second clip" for item in app.get("caption"))
    assert any(item.value == "2 of 2" for item in app.get("caption"))


def test_gallery_removal_requires_confirmation_and_targets_only_selected_job():
    main = Path(__file__).resolve().parents[2] / "webui" / "Main.py"
    source = main.read_text(encoding="utf-8")
    dialog = next(node for node in ast.parse(source).body
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "_render_autopilot_gallery_delete_dialog")
    code = '''
import streamlit as st
from types import SimpleNamespace
st.session_state.setdefault("removed", [])
service = SimpleNamespace(store=SimpleNamespace(delete_job=lambda job_id, channel_id: st.session_state["removed"].append((job_id, channel_id))))
autopilot_service = SimpleNamespace(get_service=lambda: service)
job = SimpleNamespace(job_id="selected-job", topic="Selected video")
'''
    code += ast.get_source_segment(source, dialog)
    code += '''
_render_autopilot_gallery_delete_dialog(job, "channel-a")
'''
    app = AppTest.from_string(code).run()
    assert not app.exception
    assert app.session_state["removed"] == []
    app.button(key="gallery_delete_confirm").click().run()
    assert not app.exception
    assert app.session_state["removed"] == [("selected-job", "channel-a")]
