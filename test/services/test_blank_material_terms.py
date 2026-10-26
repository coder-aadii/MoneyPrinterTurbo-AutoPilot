from unittest.mock import patch
import pytest
from app.models.schema import VideoParams
from app.services import task as tm
from app.services.state import MemoryState

@pytest.mark.parametrize("terms", [" , ， ", ["", " ", "\t"]])
def test_supplied_empty_search_terms_fail_before_reranking(terms):
    params = VideoParams(video_subject="Coffee", video_terms=terms)
    state = MemoryState()
    with (patch.object(tm.sm, "state", state),
          patch.object(tm.llm, "generate_terms") as generate,
          patch.object(tm.twelvelabs, "rerank_terms_by_subject") as rerank):
        result = tm.generate_terms("task-1", params, "Coffee script")
    assert result is None
    assert state.get_task("task-1")["state"] == tm.const.TASK_STATE_FAILED
    generate.assert_not_called()
    rerank.assert_not_called()

def test_empty_terms_are_removed_without_reordering_valid_terms():
    params = VideoParams(
        video_subject="Coffee", video_terms=" beans , ， roasting , ",
        visual_style="relevant_stock",
    )
    params.effective_visual_style = "relevant_stock"
    with patch.object(tm.twelvelabs, "rerank_terms_by_subject", side_effect=lambda **kwargs: kwargs["search_terms"]) as rerank:
        result = tm.generate_terms("task-1", params, "Coffee script")
    assert result == ["beans", "roasting"]
    assert rerank.call_args.kwargs["search_terms"] == ["beans", "roasting"]


def test_savage_uses_editable_terms_without_regenerating_them():
    params = VideoParams(
        video_subject="Weather-predicting birds",
        video_terms="Siberian tit, bird singing, rainy clouds",
        visual_style="savage_animated_explainer",
    )
    params.effective_visual_style = "savage_animated_explainer"
    with patch.object(tm.twelvelabs, "rerank_terms_by_subject", side_effect=lambda **kwargs: kwargs["search_terms"]) as rerank, \
            patch.object(tm.llm, "generate_terms") as generate:
        result = tm.generate_terms("task-1", params, "पहला विषय। दूसरा विषय। तीसरा विषय।")
    assert result == ["Siberian tit", "bird singing", "rainy clouds"]
    generate.assert_not_called()
    rerank.assert_not_called()


def test_savage_generates_terms_for_each_narration_beat():
    params = VideoParams(
        video_subject="Weather-predicting birds",
        visual_style="savage_animated_explainer",
    )
    params.effective_visual_style = "savage_animated_explainer"
    script = (
        "पहला विषय। दूसरा विषय। तीसरा विषय। चौथा विषय। पांचवां विषय। छठा विषय।"
    )
    with patch.object(tm.llm, "generate_terms", return_value=[f"topic {index}" for index in range(6)]) as generate, \
            patch.object(tm.twelvelabs, "rerank_terms_by_subject", side_effect=lambda **kwargs: kwargs["search_terms"]):
        result = tm.generate_terms("task-1", params, script)
    assert result == [f"topic {index}" for index in range(6)]
    assert generate.call_args.kwargs["amount"] == 6
    assert generate.call_args.kwargs["match_script_order"] is True


def test_user_keywords_override_prebuilt_contextual_terms():
    params = VideoParams(
        video_subject="Butterfly thermoregulation",
        video_terms="butterfly wing close up, sunlit garden",
        visual_style="savage_animated_explainer",
    )
    params.effective_visual_style = "savage_animated_explainer"
    with patch.object(tm.twelvelabs, "rerank_terms_by_subject", side_effect=lambda **kwargs: kwargs["search_terms"]):
        result = tm.generate_terms(
            "task-1", params,
            "तितलियाँ धूप में पंख फैलाकर ताप लेती हैं। उनके पंख सूरज की गर्मी सोखते हैं।",
        )
    assert result == ["butterfly wing close up", "sunlit garden"]


def test_savage_rejects_short_ai_keyword_list_instead_of_mapping_wrong_topics():
    params = VideoParams(
        video_subject="Bird weather cues", visual_style="savage_animated_explainer",
    )
    params.effective_visual_style = "savage_animated_explainer"
    state = MemoryState()
    with patch.object(tm.sm, "state", state), \
            patch.object(tm.llm, "generate_terms", return_value=["bird"]):
        result = tm.generate_terms(
            "task-1", params, "पहले पक्षी पंख फैलाते हैं। फिर बारिश आने पर चहकते हैं।"
        )
    assert result is None
    assert state.get_task("task-1")["state"] == tm.const.TASK_STATE_FAILED
    assert "one-to-one" in state.get_task("task-1")["error"]


def test_savage_rejects_short_user_keyword_list_instead_of_reusing_last_term():
    params = VideoParams(
        video_subject="Bird weather cues", video_terms="bird",
        visual_style="savage_animated_explainer",
    )
    params.effective_visual_style = "savage_animated_explainer"
    state = MemoryState()
    with patch.object(tm.sm, "state", state):
        result = tm.generate_terms(
            "task-1", params, "पहले पक्षी पंख फैलाते हैं। फिर बारिश आने पर चहकते हैं।"
        )
    assert result is None
    assert state.get_task("task-1")["state"] == tm.const.TASK_STATE_FAILED
    assert "one-to-one" in state.get_task("task-1")["error"]
