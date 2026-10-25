import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import config
from app.models.schema import MaterialInfo, VideoParams
from app.services import asmr, llm, material, material_cache, task
from app.services.state import MemoryState


def _candidate(asset_id, url, *, tags, provider="pixabay", duration=8):
    return MaterialInfo(
        provider=provider,
        url=url,
        duration=duration,
        source_info={
            "provider": provider,
            "asset_id": asset_id,
            "tags": tags,
            "source_page": f"https://example.test/video/{asset_id}",
            "asmr_quality_review": "approved",
            "asmr_reviewed_timestamps_seconds": [1.0, 3.0],
            "asmr_quality_review_notes": "Human checked the cutting action and texture changes.",
        },
    )


class TestAsmrStock(unittest.TestCase):
    def test_metadata_tags_alone_do_not_approve_asmr_quality(self):
        candidate = _candidate(
            "jelly", "https://cdn.test/jelly.mp4", tags="pink jelly slicing satisfying macro"
        )
        candidate.source_info.pop("asmr_quality_review")
        candidate.source_info.pop("asmr_reviewed_timestamps_seconds")
        candidate.source_info.pop("asmr_quality_review_notes")
        self.assertEqual(asmr.rank_asmr_candidates([("jelly slicing", [candidate])]), [])

    def test_existing_pink_jelly_clip_is_preserved_but_not_premium_asmr_eligible(self):
        from app.models.schema import VideoAspect
        from app.services import asmr_library

        self.assertEqual(asmr_library.load_clips(["jelly"], VideoAspect.portrait), [])

    def test_keyword_expansion_requires_material_and_action_and_is_bounded(self):
        terms = asmr.expand_asmr_search_terms(
            ["kinetic sand cutting", "unrelated landscape", "soap shaving"],
            max_queries=4,
        )
        self.assertEqual(
            terms,
            [
                "kinetic sand cutting",
                "kinetic sand slicing",
                "kinetic sand carving",
                "satisfying kinetic sand cutting",
            ],
        )
        self.assertEqual(asmr.expand_asmr_search_terms(["soap cutting"], max_queries=0), [])

    def test_asmr_queries_are_deterministic_and_independent_of_narration(self):
        with patch.object(llm, "_generate_response") as generate:
            terms = llm.generate_terms(
                "Hindi fact about space",
                "A script about black holes.",
                stock_video_style=asmr.SATISFYING_ASMR_CUTTING,
                asmr_categories=["foam", "jelly"],
            )
        generate.assert_not_called()
        self.assertEqual(
            terms,
            ["foam slicing", "sponge cutting", "jelly slicing", "gelatin cutting"],
        )

    def test_category_defaults_are_validated_and_task_scoped(self):
        first = VideoParams(video_subject="one")
        second = VideoParams(video_subject="two", asmr_categories=["foam", "jelly"])
        self.assertEqual(first.asmr_categories, ["kinetic_sand", "soap"])
        self.assertEqual(asmr.category_queries(second.asmr_categories)[0][0], "foam")
        with self.assertRaises(ValueError):
            VideoParams(video_subject="bad", asmr_categories=["space"])

    def test_category_profiles_remain_independent_under_concurrency(self):
        from concurrent.futures import ThreadPoolExecutor

        profiles = [
            VideoParams(video_subject="Hindi facts", stock_video_style=asmr.SATISFYING_ASMR_CUTTING, asmr_categories=["kinetic_sand", "soap"]),
            VideoParams(video_subject="Science", stock_video_style=asmr.SATISFYING_ASMR_CUTTING, asmr_categories=["foam", "jelly"]),
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda params: task.generate_terms("parallel", params, "narration"),
                profiles,
            ))
        self.assertEqual(results[0], ["kinetic sand cutting", "kinetic sand slicing", "soap cutting", "soap shaving"])
        self.assertEqual(results[1], ["foam slicing", "sponge cutting", "jelly slicing", "gelatin cutting"])

    def test_asmr_ignores_explicit_fact_terms_and_script_order_setting(self):
        params = VideoParams(
            video_subject="Hindi fact about space",
            video_terms=["black holes"],
            match_materials_to_script=True,
            stock_video_style=asmr.SATISFYING_ASMR_CUTTING,
            asmr_categories=["foam"],
        )
        with patch.object(task.twelvelabs, "rerank_terms_by_subject") as rerank:
            terms = task.generate_terms("asmr-independent", params, "Black holes are strange.")
        rerank.assert_not_called()
        self.assertEqual(terms, ["foam slicing", "sponge cutting"])

    def test_usable_coverage_uses_clip_cap_and_render_margin(self):
        candidates = [
            _candidate("long", "https://cdn.test/long.mp4", tags="soap cutting", duration=12),
            _candidate("short", "https://cdn.test/short.mp4", tags="soap cutting", duration=3),
        ]
        self.assertEqual(sum(material._asmr_usable_duration(
            item.duration, 5, "random", 1) for item in candidates), 15)
        self.assertEqual(sum(material._asmr_usable_duration(
            item.duration, 5, "sequential", 1) for item in candidates), 8)
        self.assertEqual(material.video._get_required_video_duration(7), 7.1)

    def test_ranking_requires_both_metadata_dimensions_and_deduplicates(self):
        weak = _candidate(
            "weak", "https://cdn.test/weak.mp4", tags="kinetic sand satisfying"
        )
        duplicate_lower = _candidate(
            "same", "https://cdn.test/shared.mp4?token=one", tags="soap cutting"
        )
        duplicate_higher = _candidate(
            "same", "https://cdn.test/other.mp4", tags="soap cutting asmr macro"
        )
        url_duplicate = _candidate(
            "different", "https://CDN.test/other.mp4?token=two", tags="soap cutting"
        )
        unrelated = _candidate(
            "unrelated", "https://cdn.test/other.mp4", tags="soap bar"
        )

        ranked = asmr.rank_asmr_candidates(
            [
                ("kinetic sand cutting", [weak]),
                ("soap cutting", [duplicate_lower, duplicate_higher, url_duplicate, unrelated]),
            ]
        )

        self.assertEqual(len(ranked), 1)
        self.assertIs(ranked[0][1], duplicate_higher)
        self.assertEqual(ranked[0][1].source_info["asmr_relevance_score"], 12)
        self.assertIn("soap material and cutting action", ranked[0][1].source_info["asmr_selection_reason"])

    def test_expanded_cutting_terms_keep_material_and_action_and_texture_context(self):
        terms = asmr.expand_asmr_search_terms(["soap cutting"], max_queries=6)
        self.assertEqual(terms[:4], [
            "soap cutting", "soap shaving", "soap carving", "soap slicing",
        ])
        self.assertTrue(any("satisfying" in term for term in terms))
        self.assertTrue(any("texture" in term for term in terms))
        self.assertTrue(all(asmr.classify_asmr_term(term) for term in terms))

    def test_candidate_content_hashes_are_deduplicated(self):
        first = _candidate("first", "https://cdn.test/one.mp4", tags="soap cutting")
        duplicate = _candidate("second", "https://cdn.test/two.mp4", tags="soap cutting asmr")
        first.source_info["content_sha256"] = "a" * 64
        duplicate.source_info["content_sha256"] = "a" * 64
        ranked = asmr.rank_asmr_candidates([
            ("soap cutting", [first]), ("soap cutting", [duplicate]),
        ])
        self.assertEqual(len(ranked), 1)
        self.assertIs(ranked[0][1], duplicate)

    def test_canonical_url_dedup_removes_query_and_fragment(self):
        self.assertEqual(
            asmr.canonical_media_url("HTTPS://CDN.test:443/a.mp4/?sig=1#x"),
            "https://cdn.test/a.mp4",
        )

    def test_source_page_duplicates_are_removed_and_best_score_kept(self):
        first = _candidate("one", "https://cdn.test/1.mp4", tags="soap cutting")
        duplicate = _candidate(
            "two", "https://cdn.test/2.mp4", tags="soap cutting asmr macro"
        )
        duplicate.source_info["source_page"] = first.source_info["source_page"]
        ranked = asmr.rank_asmr_candidates(
            [("soap cutting", [first]), ("soap cutting", [duplicate])]
        )
        self.assertEqual([item for _, item in ranked], [duplicate])

    def test_category_search_tops_up_until_duration_then_stops(self):
        kinetic = [
            _candidate("sand1", "https://cdn.test/sand1.mp4", tags="kinetic sand cutting", duration=5),
            _candidate("sand2", "https://cdn.test/sand2.mp4", tags="kinetic sand slicing", duration=5),
        ]
        soap = [_candidate("soap1", "https://cdn.test/soap.mp4", tags="soap cutting", duration=5)]
        calls = []

        def search(search_terms, **_kwargs):
            calls.append(list(search_terms))
            return [(term, kinetic if "kinetic sand" in term else soap) for term in search_terms]

        def download(materials, **_kwargs):
            return [
                (term, item, f"/tmp/{item.source_info['asset_id']}.mp4")
                for term, item in materials
            ]

        with patch("app.services.material._search_terms_in_parallel", side_effect=search), patch(
            "app.services.material._download_materials_in_parallel", side_effect=download
        ), patch("app.services.material._persist_material_sources"), patch(
            "app.services.material.asmr_library._sha256", side_effect=lambda path: path.stem
        ), patch("app.services.material.asmr_library.probe", return_value=(5.0, 1080, 1920)):
            paths = material.download_videos(
                task_id="asmr-early-stop", search_terms=[], source="pixabay",
                audio_duration=5, max_clip_duration=5,
                stock_video_style=asmr.SATISFYING_ASMR_CUTTING,
                asmr_categories=["kinetic_sand", "soap"],
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(paths), 2)

    def test_random_playback_does_not_shuffle_candidate_ranking_before_download(self):
        low = _candidate(
            "low", "https://cdn.test/low.mp4", tags="kinetic sand cutting", duration=5
        )
        high = _candidate(
            "high", "https://cdn.test/high.mp4",
            tags="kinetic sand cutting asmr macro", duration=5,
        )
        selected_order = []

        def download(materials, **_kwargs):
            selected_order.extend(item.source_info["asset_id"] for _, item in materials)
            return [(term, item, f"/tmp/{item.source_info['asset_id']}.mp4") for term, item in materials]

        with patch(
            "app.services.material._search_terms_in_parallel",
            return_value=[("kinetic sand cutting", [low, high])],
        ), patch(
            "app.services.material._download_materials_in_parallel", side_effect=download
        ), patch("app.services.material._persist_material_sources"), patch(
            "app.services.material.asmr_library._sha256", side_effect=lambda path: path.stem
        ), patch("app.services.material.asmr_library.probe", return_value=(5.0, 1080, 1920)):
            material.download_videos(
                task_id="asmr-ranked-random", search_terms=[], source="pixabay",
                audio_duration=5, max_clip_duration=5,
                video_concat_mode="random",
                stock_video_style=asmr.SATISFYING_ASMR_CUTTING,
                asmr_categories=["kinetic_sand"],
            )
        self.assertEqual(selected_order, ["high", "low"])

    def test_insufficient_duration_falls_back_by_category_then_fails_clearly(self):
        calls = []

        def search(search_terms, **_kwargs):
            calls.append(list(search_terms))
            term = search_terms[0]
            if "kinetic sand" in term:
                return [(term, [_candidate("sand", "https://cdn.test/sand.mp4", tags="kinetic sand cutting", duration=5)])]
            return [(term, [_candidate("soap", "https://cdn.test/soap.mp4", tags="soap cutting", duration=5)])]

        with patch("app.services.material.asmr_library.load_clips", return_value=[]), patch(
            "app.services.material._search_terms_in_parallel", side_effect=search
        ), patch(
            "app.services.material._download_materials_in_parallel"
        ) as download:
            with self.assertRaisesRegex(
                material.NoRelevantAsmrMaterialsError,
                r"required 10.1s usable duration, quality-approved found 10.0s; metadata-only candidates offer 10.0s.*kinetic_sand, soap",
            ):
                material.download_videos(
                    task_id="asmr-insufficient", search_terms=[], source="pixabay",
                    audio_duration=10, max_clip_duration=5,
                    stock_video_style=asmr.SATISFYING_ASMR_CUTTING,
                    asmr_categories=["kinetic_sand", "soap"],
                )
        self.assertEqual(len(calls), 2)
        download.assert_not_called()

    def test_cache_and_attribution_keep_safe_candidate_metadata(self):
        item = _candidate("123", "https://cdn.test/a.mp4", tags=["soap", "cutting"])
        item.source_info["thumbnail"] = "https://cdn.test/thumb.jpg?token=private"
        item.source_info["asmr_selection_reason"] = "metadata confirms soap and cutting"
        item.source_info["license"] = "Pixabay Content License"
        item.source_info["license_url"] = "https://pixabay.com/service/license-summary/"
        cached = material_cache._cached_source_info(item)
        self.assertEqual(cached["tags"], ["soap", "cutting"])
        self.assertEqual(cached["thumbnail"], "https://cdn.test/thumb.jpg")
        record = material._material_source_record(item, "/tmp/video.mp4")
        self.assertEqual(record["tags"], ["soap", "cutting"])
        self.assertEqual(record["thumbnail"], "https://cdn.test/thumb.jpg")
        self.assertEqual(cached["asmr_selection_reason"], item.source_info["asmr_selection_reason"])
        self.assertEqual(record["asmr_selection_reason"], item.source_info["asmr_selection_reason"])
        self.assertEqual(cached["license"], item.source_info["license"])
        self.assertEqual(cached["license_url"], item.source_info["license_url"])

    def test_previous_normal_search_cache_version_remains_readable(self):
        item = _candidate("cache-v2", "https://cdn.test/cache-v2.mp4", tags="soap cutting")
        with tempfile.TemporaryDirectory() as directory, patch.object(
            material_cache, "_cache_dir", return_value=Path(directory)
        ):
            self.assertTrue(material_cache.save_material_search_cache(
                "pixabay", "soap cutting", 5, "9:16", [item]
            ))
            cache_path = material_cache._cache_path("pixabay", "soap cutting", 5, "9:16")
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            payload["version"] = 2
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
            cached = material_cache.load_material_search_cache(
                "pixabay", "soap cutting", 5, "9:16"
            )
        self.assertEqual(cached[0].source_info["tags"], "soap cutting")

    def test_provider_adapters_retain_tags_and_thumbnail_metadata(self):
        pexels = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "id": 123,
                        "url": "https://www.pexels.com/video/soap-cutting-123/",
                        "duration": 8,
                        "alt": "Soap cutting close up",
                        "image": "https://images.test/soap.jpg",
                        "tags": ["soap", "cutting"],
                        "video_files": [
                            {
                                "id": 1,
                                "width": 1080,
                                "height": 1920,
                                "link": "https://cdn.test/soap.mp4",
                            }
                        ],
                    }
                ]
            }
        )
        pixabay = SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            json=lambda: {
                "hits": [
                    {
                        "id": 456,
                        "pageURL": "https://pixabay.com/videos/soap-cutting-456/",
                        "duration": 8,
                        "tags": "soap, cutting, asmr",
                        "picture": "https://images.test/pixabay-soap.jpg",
                        "videos": {
                            "small": {
                                "url": "https://cdn.test/pixabay-soap.mp4",
                                "width": 1080,
                                "height": 1920,
                                "thumbnail": "https://images.test/pixabay-video.jpg",
                            }
                        },
                    }
                ]
            },
        )

        with patch.dict(
            config.app,
            {"pexels_api_keys": ["test-pexels"], "pixabay_api_keys": ["test-pixabay"]},
        ), patch(
            "app.services.material.requests.get", side_effect=[pexels, pixabay]
        ):
            pexels_items = material.search_videos_pexels("soap cutting", 5)
            pixabay_items = material.search_videos_pixabay("soap cutting", 5)

        self.assertEqual(pexels_items[0].source_info["tags"], ["soap", "cutting"])
        self.assertEqual(
            pexels_items[0].source_info["thumbnail"], "https://images.test/soap.jpg"
        )
        self.assertEqual(pixabay_items[0].source_info["tags"], ["soap", "cutting", "asmr"])
        self.assertEqual(pexels_items[0].source_info["license"], "Pexels License")
        self.assertEqual(pixabay_items[0].source_info["license"], "Pixabay Content License")
        self.assertEqual(
            pixabay_items[0].source_info["thumbnail"],
            "https://images.test/pixabay-video.jpg",
        )
        self.assertNotIn("title", pixabay_items[0].source_info)

    def test_download_fails_clearly_when_no_candidate_is_relevant(self):
        with patch("app.services.material.asmr_library.load_clips", return_value=[]), patch(
            "app.services.material._search_videos_with_cache", return_value=[]
        ):
            with self.assertRaisesRegex(
                material.NoRelevantAsmrMaterialsError,
                "No reviewed local clips or stock videos matched ASMR",
            ):
                material.download_videos(
                    task_id="asmr-no-match",
                    search_terms=["kinetic sand cutting"],
                    source="pixabay",
                    stock_video_style=asmr.SATISFYING_ASMR_CUTTING,
                )

    def test_task_marks_no_relevant_stock_as_material_failure(self):
        task_id = "asmr-task-no-match"
        state = MemoryState()
        state.update_task(task_id, state=0, progress=40)
        params = VideoParams(
            video_subject="Satisfying cuts",
            video_source="pixabay",
            stock_video_style=asmr.SATISFYING_ASMR_CUTTING,
        )

        with patch.object(task.sm, "state", state), patch.object(
            task.material,
            "download_videos",
            side_effect=material.NoRelevantAsmrMaterialsError(
                "No relevant metadata candidates."
            ),
        ):
            result = task.get_video_materials(
                task_id, params, ["kinetic sand cutting"], audio_duration=5
            )

        self.assertIsNone(result)
        failed = state.get_task(task_id)
        self.assertEqual(failed["failed_stage"], "materials")
        self.assertEqual(failed["error"], "No relevant metadata candidates.")

    def test_default_style_remains_the_model_default(self):
        self.assertEqual(VideoParams(video_subject="topic").stock_video_style, "default")


if __name__ == "__main__":
    unittest.main()
