import unittest
from unittest.mock import patch

from app.models.schema import VideoParams
from app.services import material, task
from app.services.state import MemoryState


class TestTaskReviewAndDuration(unittest.TestCase):
    def run_to_audio(self, params, task_id):
        with patch.object(task.utils, "check_ffmpeg_ready", return_value=True), \
             patch.object(task.sm, "state", MemoryState()):
            return task._run_pipeline(task_id, params, stop_at="audio")

    def test_factual_script_runs_without_review_gate(self):
        params = VideoParams(video_subject="Butterflies", video_script="Butterflies bask in sunlight.", video_source="local", visual_style="relevant_stock")
        with patch.object(task.utils, "check_ffmpeg_ready", return_value=True), \
             patch.object(task, "generate_audio", return_value=("audio.mp3", 4, object())) as tts:
            result = self.run_to_audio(params, "factual-task")
        tts.assert_called_once()
        self.assertEqual(result["audio_file"], "audio.mp3")

    def test_approved_creative_duration_recovery_regenerates_tts_and_is_bounded(self):
        params = VideoParams(
            video_subject="A fictional story", video_script="A short fictional story.",
            video_source="local",
            min_duration_seconds=25, max_duration_seconds=30,
            duration_enforcement="strict",
            duration_recovery_attempts=1, duration_recovery_approved=True,
        )
        state = MemoryState()
        with patch.object(task.utils, "check_ffmpeg_ready", return_value=True), \
             patch.object(task.sm, "state", state), \
             patch.object(task, "generate_audio", return_value=("audio.mp3", 20, object())) as tts, \
             patch.object(task.voice, "get_audio_duration", side_effect=[18, 27]), \
             patch.object(task.llm, "generate_script", return_value="A longer fictional story.") as revise, \
             patch.object(task, "generate_terms", return_value=["fictional moon"]), \
             patch.object(task, "save_script_data"):
            result = task._run_pipeline("duration-recovery", params, stop_at="audio")
        self.assertEqual(tts.call_count, 2)
        self.assertEqual(revise.call_count, 1)
        self.assertEqual(tts.call_args.args[2], "A longer fictional story.")
        self.assertEqual(result["audio_file"], "audio.mp3")

    def test_duration_recovery_stops_at_configured_retry_limit(self):
        params = VideoParams(
            video_subject="A fictional story", video_script="A short fictional story.",
            video_source="local",
            min_duration_seconds=25, max_duration_seconds=30,
            duration_enforcement="strict",
            duration_recovery_attempts=1, duration_recovery_approved=True,
        )
        with patch.object(task.utils, "check_ffmpeg_ready", return_value=True), \
             patch.object(task.sm, "state", MemoryState()), \
             patch.object(task, "generate_audio", return_value=("audio.mp3", 20, object())) as tts, \
             patch.object(task.voice, "get_audio_duration", side_effect=[18, 40]), \
             patch.object(task.llm, "generate_script", return_value="A revised fictional story.") as revise, \
             patch.object(task, "generate_terms", return_value=["fictional story"]), \
             patch.object(task, "save_script_data"), \
             patch.object(task, "_mark_task_failed", return_value={"failed": True, "stage": "audio"}) as failed:
            result = task._run_pipeline("bounded-recovery", params, stop_at="audio")
        self.assertEqual(result["stage"], "audio")
        self.assertEqual(tts.call_count, 2)
        revise.assert_called_once()
        self.assertEqual(failed.call_args.kwargs["details"]["duration_recovery_attempts"], 1)

    def test_flexible_duration_accepts_short_and_long_narration_without_retry(self):
        for minimum, maximum, actual, outside in (
            (40, 45, 39.70, True),
            (20, 25, 120, True),
            (30, 35, 15, True),
            (25, 30, 25, False),
        ):
            with self.subTest(minimum=minimum, maximum=maximum, actual=actual):
                params = VideoParams(
                    video_subject="A short explanation", video_script="A short explanation.",
                    video_source="local", min_duration_seconds=minimum,
                    max_duration_seconds=maximum, duration_enforcement="flexible",
                    fact_verification_policy="off",
                )
                state = MemoryState()
                with patch.object(task.utils, "check_ffmpeg_ready", return_value=True), \
                     patch.object(task.sm, "state", state), \
                     patch.object(task, "generate_audio", return_value=("audio.mp3", round(actual), object())) as synthesize, \
                     patch.object(task.voice, "get_audio_duration", return_value=actual), \
                     patch.object(task, "save_script_data"):
                    result = task._run_pipeline(f"flexible-{minimum}-{maximum}-{actual}", params, stop_at="audio")
                synthesize.assert_called_once()
                self.assertEqual(result["actual_narration_duration_seconds"], actual)
                self.assertEqual(result["duration_outside_requested_range"], outside)
                self.assertEqual(params.min_duration_seconds, minimum)
                self.assertEqual(params.duration_enforcement, "flexible")

    def test_strict_duration_still_fails_for_audio_below_requested_minimum(self):
        params = VideoParams(
            video_subject="A short explanation", video_script="A short explanation.",
            video_source="local", min_duration_seconds=40, max_duration_seconds=45,
            duration_enforcement="strict",
            fact_verification_policy="off",
        )
        state = MemoryState()
        with patch.object(task.utils, "check_ffmpeg_ready", return_value=True), \
             patch.object(task.sm, "state", state), \
             patch.object(task, "generate_audio", return_value=("audio.mp3", 40, object())) as synthesize, \
             patch.object(task.voice, "get_audio_duration", return_value=39.70), \
             patch.object(task, "save_script_data"):
            result = task._run_pipeline("strict-duration", params, stop_at="audio")
        self.assertEqual(result["failed_stage"], "audio")
        synthesize.assert_called_once()
        self.assertEqual(state.get_task("strict-duration")["failed_stage"], "audio")

    def test_auto_material_shortage_fails_without_switching_pinned_style_after_tts(self):
        params = VideoParams(
            video_subject="Butterfly migration", video_script="Butterflies migrate.",
            video_source="pexels", visual_style="auto", fact_verification_policy="off",
        )
        state = MemoryState()
        with patch.dict(task.generation_settings.config.app, {"pexels_api_keys": ["configured"]}), \
             patch.object(task.utils, "check_ffmpeg_ready", return_value=True), \
             patch.object(task.sm, "state", state), \
             patch.object(task, "generate_terms", return_value="butterflies"), \
             patch.object(task, "save_script_data"), \
             patch.object(task, "generate_audio", return_value=("audio.mp3", 28, object())), \
             patch.object(task, "generate_subtitle", return_value="subtitle.srt"), \
             patch.object(task, "get_video_materials", side_effect=material.InsufficientRelevantStockError("no usable footage")):
            task._run_pipeline("auto-material-shortage", params, stop_at="video")

        self.assertEqual(params.effective_visual_style, "relevant_stock")
        self.assertEqual(params.stock_video_style, "default")
        self.assertEqual(state.get_task("auto-material-shortage")["failed_stage"], "materials")


if __name__ == "__main__":
    unittest.main()
