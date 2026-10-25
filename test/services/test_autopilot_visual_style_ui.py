"""Exercise the actual Channel Autopilot page without providers or user storage."""

import ast
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest
from webui.autopilot_view import migrate_create_video_style
from app.services.visual_style import SAVAGE_ANIMATED_EXPLAINER


def _channel_page(initial_style="savage_animated_explainer", initial_script="An existing narration script."):
    source = (Path(__file__).resolve().parents[2] / "webui" / "Main.py").read_text()
    nodes = ast.parse(source).body
    definitions = [
        node for node in nodes
        if (isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name)
                    and target.id in {"VISUAL_STYLE_LABELS", "VISUAL_STYLE_VALUES"}
                    for target in node.targets))
        or (isinstance(node, ast.FunctionDef)
            and node.name in {"_render_channel_autopilot", "_manual_video_topic"})
    ]
    code = '''
import streamlit as st
import hashlib
import json
import re
from types import SimpleNamespace
from datetime import datetime, time as clock_time
from app.models.channel_profile import ChannelProfile
from app.services import visual_style as visual_style_service
from app.services import generation_settings as generation_settings_service
from app.services import llm, script_quality

profile = ChannelProfile(channel_id="test-channel", display_name="Test channel",
    language="hi", visual_style=st.session_state.get("saved_visual_style", INITIAL_STYLE),
    version=2 if "saved_visual_style" in st.session_state else 1,
    voice_name="test-voice", publishing_backend="youtube_direct")
channel_profiles = SimpleNamespace(store=SimpleNamespace(list=lambda: [profile]))
service = SimpleNamespace(
    store=SimpleNamespace(list_slots=lambda _: []),
    _topic_for_slot=lambda selected_profile, slot: record_generation("topic", selected_profile) or "ISS gravity",
    draft_script=lambda selected_profile, topic: record_generation("script", selected_profile) or "Generated narration",
    schedule_state=lambda _: {
        "paused": False,
        "buffer": {"validated": 0, "target": 6, "in_progress": 0},
    },
    youtube_connections=SimpleNamespace(connection_for_channel=lambda _: None))
autopilot_service = SimpleNamespace(get_service=lambda: service)
config = SimpleNamespace(ui={"tts_server": "azure-tts-v1"}, app={})
voice = SimpleNamespace(is_gemini_voice=lambda _: False)
_autopilot_connection_label = lambda *_: "Test channel"
_render_autopilot_videos = lambda *_: None
_render_channel_autopilot_advanced = lambda: None
_render_autopilot_active_job = lambda *_: None

def record_generation(action, selected_profile):
    if action == "script":
        st.session_state["script_provider_calls"] = st.session_state.get("script_provider_calls", 0) + 1
    st.session_state[f"{action}_generation_settings"] = {
        "visual_style": selected_profile.visual_style,
        "narrator_preset": selected_profile.narrator_preset,
        "profanity_level": selected_profile.profanity_level,
    }

def _autopilot_create_video(profile, topic, script, duration, aspect, style, language, **kwargs):
    st.session_state["submitted_visual_style"] = style
    st.session_state["submitted_topic"] = topic
    st.session_state["submitted_script"] = script
    return SimpleNamespace(job_id="test-job")

def _autopilot_save_profile(profile, **changes):
    st.session_state["saved_visual_style"] = changes["visual_style"]
'''.replace("INITIAL_STYLE", repr(initial_style))
    code += "\n" + "\n\n".join(ast.get_source_segment(source, node) for node in definitions)
    code += "\n_render_channel_autopilot()\n"
    app = AppTest.from_string(code, default_timeout=30)
    app.session_state["simple_topic_test-channel"] = "ISS gravity"
    app.session_state["simple_draft_test-channel"] = initial_script
    return app.run()


@pytest.mark.parametrize("initial_style", ["asmr_cutting", "savage_animated_explainer"])
def test_create_video_page_offers_animated_style_and_submits_internal_value(initial_style):
    app = _channel_page(initial_style)
    assert not app.exception
    dropdown = app.selectbox(key="simple_style_test-channel")
    assert dropdown.options == [
        "Savage Animated Explainer", "Relevant Stock", "ASMR Cutting", "AUTO",
    ]
    dropdown.select("Savage Animated Explainer").run()
    app.button(key="simple_generate").click().run()
    assert not app.exception
    assert app.session_state["submitted_visual_style"] == "savage_animated_explainer"


def test_animated_channel_default_saves_and_loads_in_create_video_dropdown():
    app = _channel_page()
    assert not app.exception
    defaults = next(item for item in app.selectbox if item.label == "Default visuals")
    defaults.select("Savage Animated Explainer")
    next(item for item in app.button if item.label == "Save channel defaults").click().run()
    assert not app.exception
    assert app.session_state["saved_visual_style"] == "savage_animated_explainer"
    assert app.selectbox(key="simple_style_test-channel").value == "Savage Animated Explainer"


def test_new_channel_defaults_to_savage_animated_explainer():
    app = _channel_page()
    assert not app.exception
    assert app.selectbox(key="simple_style_test-channel").value == "Savage Animated Explainer"


def test_both_ai_buttons_use_current_style_personality_and_profanity_without_overwriting_script():
    app = _channel_page(initial_script="")
    assert not app.exception
    app.selectbox(key="simple_style_test-channel").select("Savage Animated Explainer").run()
    app.selectbox(key="simple_narrator_test-channel").select("Desi Savage Hindi").run()
    app.selectbox(key="simple_profanity_test-channel").select("Mild").run()

    app.button(key="simple_idea").click().run()
    assert not app.exception
    expected = {
        "visual_style": "savage_animated_explainer",
        "narrator_preset": "desi_savage_aggressive",
        "profanity_level": "mild",
    }
    assert app.session_state["topic_generation_settings"] == expected
    assert app.session_state["script_generation_settings"] == expected

    app.session_state["simple_draft_test-channel"] = "User-authored script stays intact."
    app.selectbox(key="simple_style_test-channel").select("Relevant Stock").run()
    assert app.session_state["simple_draft_test-channel"] == "User-authored script stays intact."


def test_editing_topic_does_not_implicitly_request_a_second_script_draft():
    app = _channel_page(initial_script="")
    assert not app.exception
    app.text_area(key="simple_topic_test-channel").set_value("A new topic").run()
    assert not app.exception
    assert "script_generation_settings" not in app.session_state
    app.button(key="simple_script_generate_test-channel").click().run()
    assert not app.exception
    assert app.session_state["script_provider_calls"] == 1
    assert app.session_state["script_generation_settings"]["visual_style"] in {
        "savage_animated_explainer", "relevant_stock", "asmr_cutting", "auto"
    }


def test_topic_field_accepts_large_pastes_and_reports_length_without_truncating():
    app = _channel_page(initial_script="")
    assert not app.exception
    pasted_topic = "A pasted topic or notes block. " * 30

    topic_box = app.text_area(key="simple_topic_test-channel")
    assert topic_box.max_chars in (None, 0)
    topic_box.set_value(pasted_topic).run()

    assert not app.exception
    assert app.session_state["simple_topic_test-channel"] == pasted_topic
    assert any("over 500 characters" in warning.value for warning in app.warning)


def test_user_can_generate_video_from_narration_without_entering_topic():
    app = _channel_page(initial_script="यह मेरी पूरी लिखी हुई हिंदी narration है।")
    assert not app.exception
    app.text_area(key="simple_topic_test-channel").set_value("").run()

    generate = app.button(key="simple_generate")
    assert not generate.disabled
    generate.click().run()

    assert not app.exception
    assert app.session_state["submitted_topic"] == "यह मेरी पूरी लिखी हुई हिंदी narration है।"
    assert app.session_state["submitted_script"] == "यह मेरी पूरी लिखी हुई हिंदी narration है।"


def test_existing_create_video_session_migrates_stale_default_but_keeps_explicit_choice():
    labels = {SAVAGE_ANIMATED_EXPLAINER: "Savage Animated Explainer"}
    values = {"ASMR Cutting": "asmr_cutting", "Savage Animated Explainer": SAVAGE_ANIMATED_EXPLAINER}
    old_session = {"visual_style_select": "ASMR Cutting"}
    assert migrate_create_video_style(old_session, labels, values)
    assert old_session["visual_style_select"] == "Savage Animated Explainer"
    assert old_session["_apply_style_defaults_before_render"] is True

    explicit_session = {"visual_style_select": "ASMR Cutting", "_visual_style_user_selected": True}
    assert migrate_create_video_style(explicit_session, labels, values)
    assert explicit_session["visual_style_select"] == "ASMR Cutting"
