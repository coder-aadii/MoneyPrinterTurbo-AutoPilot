import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.models.schema import MaterialInfo
from app.services import material, visual_review


class TestVisualReview(unittest.TestCase):
    def test_sampled_usable_frames_require_human_semantic_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir) / "task"
            task_dir.mkdir()

            def fake_ffmpeg(command, **_kwargs):
                output = Path(command[-1])
                output.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (1080, 1920), "blue").save(output)
                return type("Result", (), {"returncode": 0, "stderr": ""})()

            with patch.object(visual_review.utils, "task_dir", return_value=str(task_dir)), \
                 patch.object(visual_review.subprocess, "run", side_effect=fake_ffmpeg):
                result = visual_review.sample_clip(
                    "clip.mp4", "task", "asset-1", usable_seconds=5, video_aspect="9:16"
                )
            self.assertEqual(result["status"], "human_review_required")
            self.assertEqual(result["semantic_verification"], "not_automated")
            self.assertEqual(len(result["sampled_frames"]), 5)
            self.assertEqual(result["sampled_frames"][0]["timestamp_seconds"], .4)
            self.assertTrue(result["sampled_frames"][0]["path"].startswith("visual-review/"))

    def test_low_resolution_sample_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir) / "task"
            task_dir.mkdir()

            def fake_ffmpeg(command, **_kwargs):
                output = Path(command[-1])
                output.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (240, 320), "pink").save(output)
                return type("Result", (), {"returncode": 0, "stderr": ""})()

            with patch.object(visual_review.utils, "task_dir", return_value=str(task_dir)), \
                 patch.object(visual_review.subprocess, "run", side_effect=fake_ffmpeg):
                result = visual_review.sample_clip(
                    "clip.mp4", "task", "asset-2", usable_seconds=5
                )
            self.assertEqual(result["status"], "rejected")

    def test_manual_approval_is_bound_to_asset_hash_and_audit_reason(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = Path(temp_dir) / "reviews.json"
            with patch.object(visual_review, "_registry_path", return_value=registry):
                self.assertIsNone(visual_review.human_approval("asset", "hash"))
                visual_review.record_human_review(
                    "asset", "hash", "approved", "reviewer", "Butterfly scene is visible through the usable range."
                )
                self.assertEqual(visual_review.human_approval("asset", "hash")["status"], "approved")
                self.assertIsNone(visual_review.human_approval("asset", "different-hash"))
                self.assertEqual(json.loads(registry.read_text())[0]["reviewer"], "reviewer")

    def test_misleading_metadata_match_is_not_visual_approval(self):
        candidate = MaterialInfo(
            url="https://cdn.example/clip.mp4", duration=8, provider="pexels",
            source_info={"asset_id": "misleading", "title": "Butterfly theme kitchen jelly slicing"},
        )
        # Token overlap can find candidates, but must never satisfy the visual gate.
        self.assertTrue(material._is_contextually_relevant(candidate, "butterfly close up"))
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(visual_review, "_registry_path", return_value=Path(temp_dir) / "reviews.json"):
                self.assertIsNone(visual_review.human_approval("misleading", "content-hash"))

    def test_human_rejection_is_persisted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = Path(temp_dir) / "reviews.json"
            with patch.object(visual_review, "_registry_path", return_value=registry):
                visual_review.record_human_review(
                    "asset", "hash", "rejected", "reviewer", "Scene shows food preparation, not butterfly footage."
                )
                self.assertIsNone(visual_review.human_approval("asset", "hash"))
                self.assertEqual(json.loads(registry.read_text())[0]["status"], "rejected")

    def test_automatic_relevance_cache_is_bound_to_file_hash_and_query(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = Path(temp_dir) / "auto-relevance.json"
            content_hash = "a" * 64
            with patch.object(visual_review, "_auto_relevance_path", return_value=registry):
                visual_review.record_auto_relevance(
                    content_hash, "jellyfish underwater", "relevant",
                    verifier="local_vision", model="local-test-model", score=.93,
                    reason="Sampled frames visibly contain jellyfish underwater.",
                )
                accepted = visual_review.cached_auto_relevance(
                    content_hash, "jellyfish underwater"
                )
                self.assertEqual(accepted["decision"], "relevant")
                self.assertIsNone(visual_review.cached_auto_relevance(
                    "b" * 64, "jellyfish underwater"
                ))
                self.assertIsNone(visual_review.cached_auto_relevance(
                    content_hash, "different visual query"
                ))
                self.assertEqual(json.loads(registry.read_text())[0]["sha256"], content_hash)

    def test_conservative_policy_cannot_auto_approve_from_uncertainty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(visual_review, "_auto_relevance_path",
                              return_value=Path(temp_dir) / "auto-relevance.json"):
                with self.assertRaises(ValueError):
                    visual_review.record_auto_relevance(
                        "a" * 64, "clip query", "relevant",
                        verifier="conservative_policy", model="unavailable", score=.99,
                        reason="No semantic visual evidence is available.",
                    )


if __name__ == "__main__":
    unittest.main()
