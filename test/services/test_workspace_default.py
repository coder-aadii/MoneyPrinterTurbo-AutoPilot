import ast
from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_channel_autopilot_is_the_initial_workspace():
    source = (Path(__file__).resolve().parents[2] / "webui" / "Main.py").read_text()
    application = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "_render_application"
    )
    code = """
import streamlit as st
def _render_top_bar():
    pass
def _render_channel_autopilot():
    st.session_state['opened_workspace'] = 'Channel Autopilot'
"""
    code += ast.get_source_segment(source, application)
    code += "\n_render_application()\n"

    app = AppTest.from_string(code, default_timeout=30).run()

    assert not app.exception
    assert app.radio(key="mpt_workspace").value == "Channel Autopilot"
    assert app.session_state["opened_workspace"] == "Channel Autopilot"
