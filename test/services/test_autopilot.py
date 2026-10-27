import hashlib
import json
import threading
import unittest
import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.models import const
from app.models.autopilot import ContentPlanCreate
from app.models.channel_profile import ChannelProfile
from app.controllers.v1 import autopilot as autopilot_controller
from app.services import autopilot, channel_profiles
from app.services.state import MemoryState


class TestAutopilot(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = Path(self.temp.name) / "profiles.sqlite3"
        self.service = autopilot.AutopilotService(self.database)
        self.addCleanup(self.service._executor.shutdown, wait=True)
        self.social_metadata_patcher = patch.object(
            autopilot.llm, "generate_social_metadata",
            return_value={"title": "Generated test title",
                          "caption": "Curious about this? 🔍🤔\nHere is the complete answer. 💡✨",
                          "hashtags": ["#One", "#Two", "#Three", "#Four", "#Five"]},
        )
        self.social_metadata_patcher.start()
        self.addCleanup(self.social_metadata_patcher.stop)
        self.profiles = [
            self.service.profile_store.create(ChannelProfile(
                display_name="Hindi ASMR Facts", language="hi", niche="Hindi facts",
                script_style="Short spoken Hindi", target_duration_seconds=28,
                stock_video_style="satisfying_asmr_cutting", asmr_categories=["kinetic_sand", "soap"],
                publishing_backend="upload_post", upload_post_username="account-a",
            )),
            self.service.profile_store.create(ChannelProfile(
                display_name="Science Facts", language="en", niche="Science",
                target_duration_seconds=55, asmr_categories=["foam", "jelly"],
                publishing_backend="upload_post", upload_post_username="account-b",
            )),
        ]

    def tearDown(self):
        self.temp.cleanup()

    def test_manual_job_saves_selected_stock_policy_in_snapshot(self):
        profile = self.profiles[1]
        for policy, topic, script in (
            ("manual_review", "Jellyfish in moonlit water", "Glowing jellyfish swim beneath a moonlit sea."),
            ("auto_eligible", "Ancient stars and planets", "Distant planets orbit ancient stars across the galaxy."),
        ):
            plan = self.service.plan(
                profile.channel_id,
                ContentPlanCreate(
                    topic=topic, script=script,
                    min_duration_seconds=5, max_duration_seconds=60,
                    visual_style_override="savage_animated_explainer",
                ),
                manual_overrides={"stock_media_policy": policy},
            )
            self.service.ready(profile.channel_id, plan.plan_id)
            job = self.service.create_job(profile.channel_id, plan.plan_id)
            self.assertEqual(job.profile_snapshot["stock_media_policy"], policy)
            self.assertEqual(job.params_snapshot["stock_media_policy"], policy)

    @staticmethod
    def hindi_script(words=66, topic="fact"):
        token = autopilot.normalize_content(topic).replace(" ", "_")
        return " ".join(f"तथ्य{token}{index}" for index in range(words))

    def plan(self, profile=None, topic="Why the Moon looks larger", script=None):
        profile = profile or self.profiles[0]
        return self.service.plan(profile.channel_id, ContentPlanCreate(
            topic=topic,
            script=script or self.hindi_script(topic=topic),
            min_duration_seconds=25,
            max_duration_seconds=30,
        ))

    def ready_job(self, topic="Why the Moon looks larger", script=None, profile=None):
        plan = self.plan(profile=profile, topic=topic, script=script)
        reviewed = self.service.ready(plan.channel_id, plan.plan_id)
        return self.service.create_job(plan.channel_id, reviewed.plan_id)

    def test_two_channels_keep_separate_history_and_profile_snapshots(self):
        first_plan = self.plan(topic="Moon illusion")
        second_script = "A star is a luminous sphere of plasma in space."
        second_plan = self.plan(self.profiles[1], "What makes a star", second_script)
        self.assertNotEqual(first_plan.channel_id, second_plan.channel_id)
        self.assertEqual(first_plan.profile_version, 1)
        self.assertEqual(second_plan.profile_version, 1)
        self.assertEqual(len(self.service.store.list_plans(first_plan.channel_id)), 1)
        self.assertEqual(len(self.service.store.list_plans(second_plan.channel_id)), 1)

    def test_manual_stock_keywords_are_saved_in_restartable_job_snapshot(self):
        profile = self.service.profile_store.create(ChannelProfile(
            display_name="Savage Stock Channel", language="hi", niche="Hindi facts",
            visual_style="savage_animated_explainer", publishing_backend="upload_post",
            upload_post_username="account-c",
        ))
        plan = self.service.plan(profile.channel_id, ContentPlanCreate(
            topic="How eggs become food", script="अंडे में प्रोटीन होता है।",
            min_duration_seconds=5, max_duration_seconds=30,
        ))
        self.service.ready(profile.channel_id, plan.plan_id, accept_duration_estimate=True)

        job = self.service.create_job(
            profile.channel_id, plan.plan_id, video_terms="eggs, protein source"
        )

        self.assertEqual(job.params_snapshot["video_terms"], "eggs, protein source")
        self.assertEqual(
            autopilot.AutopilotStore(self.database)
            .get_job(job.job_id).params_snapshot["video_terms"],
            "eggs, protein source",
        )

    def test_task_visual_style_override_is_persisted_through_job_snapshot(self):
        profile = self.profiles[1]
        plan = self.service.plan(
            profile.channel_id,
            ContentPlanCreate(
                topic="Butterfly thermoregulation",
                script="तितलियाँ धूप में पंख फैलाकर ताप लेती हैं।",
                min_duration_seconds=25,
                max_duration_seconds=30,
                visual_style_override="relevant_stock",
            ),
        )
        self.assertEqual(plan.visual_style_override, "relevant_stock")
        self.assertEqual(plan.effective_visual_style, "relevant_stock")
        ready = self.service.ready(profile.channel_id, plan.plan_id, True)
        job = self.service.create_job(profile.channel_id, ready.plan_id)
        self.assertEqual(job.visual_style, "relevant_stock")
        self.assertEqual(job.effective_visual_style, "relevant_stock")
        self.assertEqual(job.params_snapshot["visual_style"], "relevant_stock")

    def test_youtube_metadata_is_generated_once_persisted_and_limited_to_50_words(self):
        profile = self.profiles[1]
        profile = self.service.profile_store.update(
            profile.channel_id, profile.model_copy(update={"channel_hashtag": "#FactsSunLeBhai"}),
            profile.version,
        )
        job = self.ready_job(
            topic="What makes a star", script="A star is a sphere of plasma.",
            profile=profile,
        )
        self.service.store.transition(job.job_id, "queued")
        self.service.store.transition(job.job_id, "generating")
        self.service.store.transition(job.job_id, "generated")
        invalid = {
            "title": "How stars shine",
            "caption": "A concise hook " + "useful context " * 40,
            "hashtags": ["#Stars", "#Space", "#Science", "#Astronomy", "#Shorts"],
        }
        rewritten = {
            "title": "How stars shine",
            "caption": "Why do stars shine? 🔭🤔\nThey release visible light! 🌟💡",
            "hashtags": ["#Stars", "#Space", "#Astronomy", "#Science", "#FactsSunLeBhai"],
        }
        with patch.object(autopilot.llm, "generate_social_metadata", side_effect=[invalid, rewritten]) as call:
            metadata = self.service.ensure_youtube_metadata(job.job_id)
            reloaded = autopilot.AutopilotStore(self.database).get_job(job.job_id)
            cached = self.service.ensure_youtube_metadata(job.job_id)

        self.assertLessEqual(len(metadata["caption"].split()), 50)
        self.assertEqual(metadata["title"], "How stars shine")
        self.assertEqual(len(metadata["hashtags"]), 5)
        self.assertEqual(metadata["hashtags"][-1], "#FactsSunLeBhai")
        self.assertEqual(reloaded.youtube_metadata, metadata)
        self.assertEqual(cached, metadata)
        self.assertEqual(call.call_count, 2)
        self.assertIn("Rewrite the caption", call.call_args.kwargs["caption_feedback"])
        self.assertEqual(call.call_args.kwargs["platform"], "youtube_shorts")
        self.assertEqual(call.call_args.kwargs["language"], "English")
        self.assertIn("Science Facts", call.call_args.kwargs["channel_context"])

    def test_upload_metadata_reuses_caption_saved_with_completed_task(self):
        job = self.ready_job(
            topic="What makes a star", script="A star is a sphere of plasma.",
            profile=self.profiles[1],
        )
        self.service.store.transition(job.job_id, "queued")
        self.service.store.transition(job.job_id, "generating")
        self.service.store.transition(job.job_id, "generated")
        job = self.service.store.get_job(job.job_id)
        task_id = "caption-saved-at-render"
        narration = "A star is a sphere of plasma."
        video_path = Path(self.temp.name) / "final.mp4"
        source = autopilot.AutopilotService._youtube_caption_source(job, narration)
        metadata_record = {
            "video_index": 1,
            "video_name": video_path.name,
            "narration_fingerprint": hashlib.sha256(narration.encode()).hexdigest(),
            "caption_source_fingerprint": hashlib.sha256(source.encode()).hexdigest(),
            "title": "What Makes Stars Shine",
            "caption": "Why do stars shine? 🔭🤔\nThey release visible light! 🌟💡",
            "hashtags": ["#Stars", "#Space", "#Astronomy", "#Science", "#Shorts"],
        }
        manifest_dir = Path(self.temp.name) / "task"
        manifest_dir.mkdir()
        (manifest_dir / "script.json").write_text(json.dumps({
            "spoken_script": narration,
            "youtube_metadata_by_video": [metadata_record],
        }), encoding="utf-8")
        with self.service.store._connection() as conn:
            conn.execute(
                "UPDATE production_jobs SET mpt_task_id=?,output_artifacts_json=? WHERE job_id=?",
                (task_id, json.dumps([str(video_path)]), job.job_id),
            )

        with patch.object(autopilot.utils, "task_dir", return_value=str(manifest_dir)), \
                patch.object(autopilot.llm, "generate_social_metadata") as generate:
            result = self.service.ensure_youtube_metadata(job.job_id)
            reopened = self.service.ensure_youtube_metadata(job.job_id)

        self.assertEqual(generate.call_count, 0)
        self.assertEqual(result, reopened)
        self.assertEqual(result["title"], "What Makes Stars Shine")
        self.assertEqual(
            self.service.store.get_job(job.job_id).youtube_metadata,
            result,
        )
        self.assertEqual(result["narration_fingerprint"], metadata_record["narration_fingerprint"])

    def test_youtube_caption_context_includes_each_videos_voice_and_channel_branding(self):
        profile = self.profiles[1]
        profile = self.service.profile_store.update(
            profile.channel_id,
            profile.model_copy(update={
                "voice_name": "gemini:Puck-Upbeat",
                "voice_rate": 1.1,
                "intro_enabled": True,
                "intro_text": "Welcome to Science Facts.",
                "intro_audio": {"asset_key": "private-intro-asset"},
                "outro_enabled": True,
                "outro_text": "Follow for more science.",
                "outro_audio": {"asset_key": "private-outro-asset"},
                "channel_hashtag": "#ScienceFacts",
            }), profile.version,
        )
        plan = self.plan(profile, "Why stars shine", "Stars emit light from hot plasma.")
        plan = self.service.ready(profile.channel_id, plan.plan_id, True)
        job = self.service.create_job(profile.channel_id, plan.plan_id)
        for state in ("queued", "generating", "generated"):
            self.service.store.transition(job.job_id, state)
        generated = {
            "title": "Why Do Stars Shine?",
            "caption": "Why do stars shine? 🔭🤔\nHot plasma releases visible light. 🌟✨",
            "hashtags": ["#Stars", "#Space", "#Astronomy", "#Science", "#ScienceFacts"],
        }
        with patch.object(autopilot.llm, "generate_social_metadata", return_value=generated) as call:
            self.service.ensure_youtube_metadata(job.job_id)

        context = json.loads(call.call_args.kwargs["channel_context"])
        self.assertEqual(call.call_args.kwargs["language"], "English")
        self.assertEqual(context["caption_language"], "English")
        self.assertEqual(context["voice_name"], "gemini:Puck-Upbeat")
        self.assertEqual(context["narrator_preset"], profile.narrator_preset)
        self.assertEqual(context["intro"]["text"], "Welcome to Science Facts.")
        self.assertTrue(context["intro"]["audio_present"])
        self.assertTrue(context["outro"]["audio_present"])
        self.assertNotIn("private-intro-asset", call.call_args.kwargs["channel_context"])

    def test_youtube_hinglish_caption_rewrites_to_two_short_complete_sentences(self):
        job = self.ready_job(
            topic="Paisa economy mein kaise ghoomta hai",
            script="Aakhir paisa dukaan se bank tak kaise jata hai? Economy mein ghoomta rehta hai.",
        )
        self.service.store.transition(job.job_id, "queued")
        self.service.store.transition(job.job_id, "generating")
        self.service.store.transition(job.job_id, "generated")
        invalid = {"title": "Paisa", "caption": "Aakhir paisa kahan jata hai?" * 4,
                   "hashtags": ["#Money", "#Economy", "#Flow", "#Facts", "#Shorts"]}
        valid = {
            "title": "पैसा कहाँ जाता है?",
            "caption": "आखिर पैसा कहाँ जाता है? 💸🤔\nयह दुकानों से बैंकों तक घूमता है! 🔄✨",
            "hashtags": ["#MoneyFlow", "#Economy", "#CircularFlow", "#HindiFacts", "#HindiASMRFacts"],
        }
        with patch.object(autopilot.llm, "generate_social_metadata", side_effect=[invalid, valid]) as call:
            metadata = self.service.ensure_youtube_metadata(job.job_id)

        self.assertEqual(len(metadata["caption"].splitlines()), 3)
        self.assertTrue(all(len(line.split()) <= 10 for line in metadata["caption"].splitlines()[:2]))
        self.assertEqual(autopilot.AutopilotService._youtube_emoji_count(metadata["caption"]), 4)
        self.assertEqual(len(metadata["hashtags"]), 5)
        self.assertEqual(call.call_count, 2)

    def test_youtube_caption_rejects_non_english_and_dangling_endings(self):
        caption = (
            "बताओ, आज क्या? 🔍🤔\n"
            "पैसा बाजार में घूमता रहता है। 💡✨\n"
            "#MoneyFlow #Economy #CircularFlow #HindiFacts #HindiASMRFacts"
        )
        errors = autopilot.AutopilotService._youtube_caption_validation_errors(
            caption, "#HindiASMRFacts", "English",
        )
        self.assertIn("incomplete phrase or question", "; ".join(errors))
        self.assertIn("caption must be written in English", "; ".join(errors))

    def test_youtube_long_script_fallback_keeps_complete_sentences(self):
        job = self.ready_job(
            topic="Paisa economy",
            script=("पैसा आखिर कहाँ जाता है? दुकान से सप्लायर तक घूमता है। "
                    "कर्मचारी उससे अपना खर्च चलाते हैं। ") * 20,
        )
        self.service.store.transition(job.job_id, "queued")
        self.service.store.transition(job.job_id, "generating")
        self.service.store.transition(job.job_id, "generated")
        invalid = {"title": "पैसा कहाँ जाता है?", "caption": "बताओ, आज क्या?", "hashtags": []}
        with patch.object(autopilot.llm, "generate_social_metadata", side_effect=[invalid, invalid]) as call:
            metadata = self.service.ensure_youtube_metadata(job.job_id)

        lines = metadata["caption"].splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(
            autopilot.AutopilotService._youtube_caption_validation_errors(
                metadata["caption"], metadata["channel_hashtag"], metadata["language"],
            ), [],
        )
        self.assertEqual(lines[0], "पैसा आखिर कहाँ जाता है? 🔍🤔")
        self.assertEqual(lines[1], "दुकान से सप्लायर तक घूमता है। 💡✨")
        self.assertTrue(any("\u0900" <= char <= "\u097f" for line in lines[:2] for char in line))
        self.assertLessEqual(len(metadata["caption"].split()), 50)
        self.assertEqual(metadata["title"], "पैसा कहाँ जाता है?")
        self.assertEqual(call.call_count, 2)

    def test_youtube_caption_edit_requires_two_sentences_emojis_and_channel_hashtag(self):
        job = self.ready_job(topic="How stars shine", script="Stars emit light.")
        self.service.store.transition(job.job_id, "queued")
        self.service.store.transition(job.job_id, "generating")
        self.service.store.transition(job.job_id, "generated")
        with patch.object(autopilot.llm, "generate_social_metadata", return_value={
            "title": "Stars", "caption": "Stars shine? 🔭🤔\nThey glow. 🌟💡",
            "hashtags": ["#Stars", "#Space", "#Science", "#Astronomy", "#HindiASMRFacts"],
        }):
            with self.assertRaisesRegex(autopilot.AutopilotError, "exactly five hashtags"):
                self.service.save_youtube_caption(job.job_id, "Stars shine. #Stars #Space")
            with self.assertRaisesRegex(autopilot.AutopilotError, "50 whitespace-separated words"):
                self.service.save_youtube_caption(
                    job.job_id,
                    ("one two three four five six seven eight nine ten.\n" * 5)
                    + "one two three four five six seven eight nine ten. 🔍🤔💡✨\n"
                    + "#Stars #Space #Science #Astronomy #HindiASMRFacts",
                )
            edited = self.service.save_youtube_caption(
                job.job_id,
                "तारे चमकते हैं? 🔭🤔\nवे रोशनी देते हैं। 🌟💡\n#Stars #Space #Science #Astronomy #HindiASMRFacts",
            )
        self.assertEqual(edited["caption"].splitlines()[-1].split(), edited["hashtags"])

    def test_manual_video_version_keeps_source_immutable_and_links_parent(self):
        original = self.ready_job(topic="Version this video", script="Original narration remains saved.")
        original_snapshot = original.model_copy(deep=True)
        profile = self.profiles[0]
        plan = self.service.plan(
            profile.channel_id,
            ContentPlanCreate(
                topic=original.topic,
                script="Edited narration for a separate version.",
                min_duration_seconds=25,
                max_duration_seconds=30,
            ),
            manual_overrides={
                "target_duration_seconds": 30,
                "voice_name": "azure-tts-v1:en-US-JennyNeural-Female",
                "subtitle_enabled": False,
                "fact_verification_policy": "advisory",
            },
            source_job_id=original.job_id,
        )
        ready = self.service.ready(profile.channel_id, plan.plan_id, True)
        version = self.service.create_job(
            profile.channel_id, ready.plan_id, parent_job_id=original.job_id
        )

        saved_original = self.service.store.get_job(original.job_id)
        self.assertNotEqual(version.job_id, original.job_id)
        self.assertEqual(version.parent_job_id, original.job_id)
        self.assertEqual(version.script, "Edited narration for a separate version.")
        self.assertEqual(version.params_snapshot["subtitle_enabled"], False)
        self.assertEqual(version.params_snapshot["fact_verification_policy"], "advisory")
        self.assertEqual(saved_original.script, original_snapshot.script)
        self.assertEqual(saved_original.params_snapshot, original_snapshot.params_snapshot)

    def test_new_version_rejects_missing_or_other_channel_parent(self):
        plan = self.plan(topic="Invalid version parent")
        ready = self.service.ready(plan.channel_id, plan.plan_id, True)
        with self.assertRaises(LookupError):
            self.service.create_job(plan.channel_id, ready.plan_id, parent_job_id="deleted-source")

    def test_fact_policy_and_publication_opt_in_are_pinned_in_job_snapshot(self):
        profile = self.profiles[0].model_copy(update={
            "visual_style": "savage_animated_explainer",
            "fact_verification_policy": "off",
            "allow_unverified_publication": True,
        })
        profile = self.service.profile_store.update(
            profile.channel_id, profile, profile.version
        )
        plan = self.plan(profile, topic="A pinned mode snapshot")
        ready = self.service.ready(profile.channel_id, plan.plan_id, True)
        job = self.service.create_job(profile.channel_id, ready.plan_id)
        self.assertEqual(job.params_snapshot["fact_verification_policy"], "off")
        self.assertTrue(job.params_snapshot["allow_unverified_publication"])

        self.service.store.save_fact_verification(job.job_id, {
            "fact_verification_state": "unavailable",
            "fact_verification_script_hash": "script-hash",
            "fact_verification_report": {"state": "unavailable", "timed_out": True},
        })
        saved = self.service.store.get_job(job.job_id)
        self.assertEqual(saved.fact_verification_state, "unavailable")
        self.assertEqual(saved.params_snapshot["fact_verification_state"], "unavailable")
        self.assertTrue(saved.params_snapshot["allow_unverified_publication"])

    def test_fact_verification_saves_the_policy_used_by_a_failed_attempt(self):
        job = self.ready_job(topic="Persist runtime fact policy")
        self.service.store.save_fact_verification(job.job_id, {
            "fact_verification_state": "unavailable",
            "fact_verification_policy": "strict",
            "fact_verification_script_hash": "script-hash",
            "fact_verification_report": {"state": "unavailable"},
        })
        saved = self.service.store.get_job(job.job_id)
        self.assertEqual(saved.params_snapshot["fact_verification_policy"], "strict")

    def test_cross_channel_duplicate_check_is_opt_in_and_job_snapshots_stay_isolated(self):
        first = self.plan(topic="A fact about water")
        second = self.plan(self.profiles[1], "A fact about water", "Water expands when it freezes.")
        ready_first = self.service.ready(first.channel_id, first.plan_id, True)
        ready_second = self.service.ready(second.channel_id, second.plan_id, True)
        first_job = self.service.create_job(first.channel_id, ready_first.plan_id)
        second_job = self.service.create_job(second.channel_id, ready_second.plan_id)
        self.assertEqual(first_job.profile_snapshot["upload_post_username"], "account-a")
        self.assertEqual(second_job.profile_snapshot["upload_post_username"], "account-b")
        self.assertEqual(first_job.params_snapshot["asmr_categories"], ["kinetic_sand", "soap"])
        self.assertEqual(second_job.params_snapshot["asmr_categories"], ["foam", "jelly"])
        with self.assertRaises(autopilot.DuplicateContent):
            self.service.plan(self.profiles[1].channel_id, ContentPlanCreate(
                topic="A fact about water", script="A different script about water.",
                check_cross_channel_duplicates=True,
            ))

    def test_profile_duplicate_policy_is_used_when_plan_has_no_override(self):
        self.plan(topic="Shared topic policy")
        profile = self.profiles[1]
        updated = profile.model_copy(update={"check_cross_channel_duplicates": True})
        self.service.profile_store.update(profile.channel_id, updated, profile.version)
        with self.assertRaises(autopilot.DuplicateContent):
            self.service.plan(profile.channel_id, ContentPlanCreate(
                topic="Shared topic policy", script="A unique script for this channel."
            ))

    def test_exact_and_near_topic_duplicates_are_rejected_atomically(self):
        self.plan(topic="Why the Moon looks larger")
        with self.assertRaises(autopilot.DuplicateContent):
            self.plan(topic="WHY THE MOON LOOKS LARGER!")

    def test_devanagari_marks_are_preserved_in_topic_normalization(self):
        topic = "ऑक्टोपस के तीन दिल क्यों होते हैं?"
        self.assertEqual(autopilot.normalize_content(topic),
                         "ऑक्टोपस के तीन दिल क्यों होते हैं")
        self.plan(topic=topic)
        with self.assertRaises(autopilot.DuplicateContent):
            self.plan(topic="ऑक्टोपस के तीन दिल क्यों होते हैं!")

    def test_near_duplicate_scripts_are_rejected_even_with_a_different_title(self):
        script = self.hindi_script(topic="Moon appearance")
        self.plan(topic="Moon appearance", script=script)
        words = script.split()
        words[40] = "अनोखा"
        with self.assertRaises(autopilot.DuplicateContent):
            self.plan(topic="A different title", script=" ".join(words))

    def test_concurrent_plan_creation_reserves_topic_once(self):
        barrier = threading.Barrier(2)
        profile = self.profiles[0]
        payload = ContentPlanCreate(topic="Ocean animal fact", script=self.hindi_script())

        def create():
            barrier.wait()
            try:
                return self.service.plan(profile.channel_id, payload)
            except autopilot.DuplicateContent:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(sum(item is not None for item in results), 1)

    def test_operator_review_and_duration_estimate_gate(self):
        profile = self.profiles[0]
        plan = self.service.plan(profile.channel_id, ContentPlanCreate(
            topic="A very short fact", script="सूर्य एक तारा है।",
            min_duration_seconds=25, max_duration_seconds=30,
            duration_enforcement="strict",
        ))
        with self.assertRaises(autopilot.DurationPlanningError):
            self.service.ready(plan.channel_id, plan.plan_id, False)
        self.service.ready(plan.channel_id, plan.plan_id, True)
        with patch.object(autopilot.llm, "generate_script", return_value="नमूना स्क्रिप्ट") as generate:
            script = self.service.draft_script(self.profiles[0], "समुद्र का तथ्य")
        self.assertEqual(script, "नमूना स्क्रिप्ट")
        kwargs = generate.call_args.kwargs
        self.assertEqual(kwargs["language"], "hi")
        self.assertIn("Hook → Interesting Fact → Explanation → Payoff", kwargs["video_script_prompt"])
        self.assertIn("clear payoff", kwargs["video_script_prompt"])

    def test_operator_approval_error_names_failed_media_qa_checks(self):
        report = {"passed": False, "checks": {"artifacts": True, "audio_track": False}}
        with patch.object(self.service, "qa_report", return_value=report):
            with self.assertRaisesRegex(autopilot.AutopilotError, "audio track integrity"):
                self.service.review_job("job-with-invalid-audio", "approve")

    def test_flexible_plan_does_not_gate_on_estimated_script_length(self):
        plan = self.plan(topic="A flexible short fact", script="सूर्य एक तारा है।")
        self.assertEqual(plan.duration_enforcement, "flexible")
        ready = self.service.ready(plan.channel_id, plan.plan_id, False)
        self.assertEqual(ready.state, "ready")

    def test_topic_and_script_drafting_pass_visual_personality_and_profanity_choices(self):
        profile = self.profiles[0].model_copy(update={
            "niche": "science for curious viewers",
            "script_style": "Use natural spoken Hindi and simple examples.",
            "topic_preferences": ["space science"],
            "visual_style": "savage_animated_explainer",
            "narrator_preset": "desi_savage_aggressive",
            "profanity_level": "mild",
        })
        with patch.object(autopilot.llm, "generate_topic", return_value="ISS पर अंतरिक्ष यात्रियों को भारहीनता क्यों महसूस होती है?") as generate:
            topic = self.service._topic_for_slot(profile, datetime.now(timezone.utc))
        self.assertEqual(topic, "ISS पर अंतरिक्ष यात्रियों को भारहीनता क्यों महसूस होती है?")
        topic_kwargs = generate.call_args.kwargs
        self.assertEqual(topic_kwargs["language"], "hi")
        self.assertEqual(topic_kwargs["preferences"], ["space science"])
        self.assertIn("sarcastic Hindi cartoon", topic_kwargs["style_guidance"])
        self.assertIn("slot_date", topic_kwargs)

        with patch.object(autopilot.llm, "generate_script", return_value="Hindi script") as generate:
            script = self.service.draft_script(profile, "ISS gravity")
        self.assertEqual(script, "Hindi script")
        script_kwargs = generate.call_args.kwargs
        self.assertEqual(script_kwargs["visual_style"], "savage_animated_explainer")
        self.assertEqual(script_kwargs["narrator_preset"], "desi_savage_aggressive")
        self.assertEqual(script_kwargs["profanity_level"], "mild")
        self.assertIn("science for curious viewers", script_kwargs["video_script_prompt"])
        self.assertIn("Use natural spoken Hindi and simple examples.", script_kwargs["video_script_prompt"])
        self.assertIn("Channel topic preferences: space science", script_kwargs["video_script_prompt"])
        full_prompt = autopilot.llm.build_script_prompt(**{
            key: value for key, value in script_kwargs.items()
            if key != "savage_quality_policy"
        })
        self.assertIn("specific, memorable situational hook", full_prompt)
        self.assertIn("Narration personality: Desi Savage Hindi", full_prompt)
        self.assertIn("Profanity intensity: mild", full_prompt)

    def test_savage_profile_draft_does_not_duplicate_preset_past_prompt_limit(self):
        profile = self.profiles[1].model_copy(update={
            "language": "hi",
            "visual_style": "savage_animated_explainer",
            "niche": "science " * 40,
            "script_style": "Use short spoken Hindi and avoid formal words. " * 20,
            "topic_preferences": ["space science " * 8, "everyday examples " * 8],
        })
        captured = {}

        def build_without_provider(**kwargs):
            captured.update(kwargs)
            prompt_args = {key: value for key, value in kwargs.items()
                           if key != "savage_quality_policy"}
            self.assertTrue(autopilot.llm.build_script_prompt(**prompt_args))
            return "Drafted Hindi script"

        with patch.object(autopilot.llm, "generate_script", side_effect=build_without_provider):
            result = self.service.draft_script(profile, "Kya tumhe pata hai, bazar me money kese flow hota hai?")

        self.assertEqual(result, "Drafted Hindi script")
        self.assertLess(
            len(captured["video_script_prompt"]),
            autopilot.llm.MAX_SCRIPT_PROMPT_LENGTH,
        )

    def test_local_prompt_limit_error_is_not_reported_as_provider_failure(self):
        with patch.object(
            autopilot.llm, "generate_script",
            return_value="Error: video_script_prompt exceeds 2000 characters; shorten it and retry.",
        ):
            with self.assertRaisesRegex(
                autopilot.AutopilotError, "video_script_prompt exceeds 2000 characters"
            ):
                self.service.draft_script(self.profiles[0], "Short topic")

    def test_empty_script_draft_applies_plan_style_before_llm_prompt_and_keeps_task_personality(self):
        profile = self.profiles[1].model_copy(update={
            "visual_style": "relevant_stock",
            "narrator_preset": "desi_savage_aggressive",
            "profanity_level": "savage_unfiltered",
        })
        self.service.profile_store.update(profile.channel_id, profile, profile.version)
        with patch.object(autopilot.llm, "generate_script", return_value="Drafted Hindi script") as generate:
            plan = self.service.plan(
                profile.channel_id,
                ContentPlanCreate(
                    topic="Does gravity disappear on the ISS?",
                    visual_style_override="savage_animated_explainer",
                    min_duration_seconds=25,
                    max_duration_seconds=30,
                ),
                manual_overrides={"narrator_preset": "standard", "profanity_level": "mild"},
            )
        args = generate.call_args.kwargs
        self.assertEqual(args["visual_style"], "savage_animated_explainer")
        self.assertEqual(args["narrator_preset"], "standard")
        self.assertEqual(args["profanity_level"], "mild")
        prompt = autopilot.llm.build_script_prompt(**{
            key: value for key, value in args.items()
            if key != "savage_quality_policy"
        })
        self.assertIn("hybrid explainer", prompt)
        self.assertIn("Narration personality: Standard", prompt)
        self.assertIn("Profanity intensity: mild", prompt)
        self.assertEqual(plan.visual_style_override, "savage_animated_explainer")

    def test_selected_generation_settings_are_pinned_in_manual_job_snapshot(self):
        profile = self.profiles[0]
        profile = self.service.profile_store.update(
            profile.channel_id,
            profile.model_copy(update={
                "intro_enabled": True,
                "intro_text": "सुन भाई, aaj ka fact!",
                "intro_audio": {
                    "asset_key": "intro/v1-" + "a" * 64 + ".mp3",
                    "content_hash": "a" * 64,
                    "duration_seconds": 1.4,
                    "version": 1,
                },
            }),
            profile.version,
        )
        plan = self.service.plan(
            profile.channel_id,
            ContentPlanCreate(
                topic="ISS gravity snapshot",
                script="ISS पर धरती की ग्रैविटी मौजूद रहती है।",
                min_duration_seconds=25,
                max_duration_seconds=30,
            ),
            manual_overrides={
                "visual_style": "savage_animated_explainer",
                "narrator_preset": "desi_savage_aggressive",
                "profanity_level": "mild",
            },
        )
        ready = self.service.ready(profile.channel_id, plan.plan_id, True)
        job = self.service.create_job(profile.channel_id, ready.plan_id)
        self.assertEqual(job.visual_style, "savage_animated_explainer")
        self.assertEqual(job.params_snapshot["visual_style"], "savage_animated_explainer")
        self.assertEqual(job.params_snapshot["narrator_preset"], "desi_savage_aggressive")
        self.assertEqual(job.params_snapshot["profanity_level"], "mild")
        self.assertEqual(job.params_snapshot["channel_id"], profile.channel_id)
        self.assertEqual(job.params_snapshot["channel_branding"]["intro"]["text"],
                         "सुन भाई, aaj ka fact!")
        self.assertEqual(job.params_snapshot["channel_branding"]["intro"]["audio"]["version"], 1)
        resolved = job.params_snapshot["resolved_generation_settings"]
        self.assertEqual(resolved["visual_style"], "savage_animated_explainer")
        self.assertEqual(resolved["narrator_preset"], "desi_savage_aggressive")
        self.assertEqual(resolved["profanity_level"], "mild")
        self.assertEqual(resolved["tts_model"], profile.tts_model)

    def test_manual_tts_provider_override_reaches_saved_profile_and_task_preview(self):
        profile = self.profiles[0]
        selected = self.service._manual_profile(
            profile.channel_id, {"tts_provider": "gemini-tts"}
        )
        self.assertEqual(selected.generation_settings["tts_provider"], "gemini-tts")
        preview = channel_profiles.build_preview(selected)
        self.assertEqual(preview.params.tts_provider, "gemini-tts")
        self.assertEqual(
            preview.params.resolved_generation_settings.tts_provider, "gemini-tts"
        )

    def test_failure_details_redact_configured_provider_credentials(self):
        with patch.object(autopilot.config, "app", {"provider_api_key": "test-secret-123"}):
            safe = autopilot._safe_failure("provider error test-secret-123")
        self.assertNotIn("test-secret-123", safe)
        self.assertIn("[redacted]", safe)

    def test_autopilot_api_routes_use_existing_auth_dependency(self):
        paths = {route.path for route in autopilot_controller.router.routes}
        self.assertIn("/api/v1/channels/{channel_id}/content-plans", paths)
        self.assertIn("/api/v1/production-jobs/{job_id}/start", paths)
        self.assertTrue(any(
            dependency.dependency is autopilot_controller.base.verify_token
            for dependency in autopilot_controller.router.dependencies
        ))

    def test_job_pins_profile_and_generation_parameters_across_profile_edit(self):
        job = self.ready_job(topic="Pinned topic")
        profile = self.profiles[0]
        updated = profile.model_copy(update={"asmr_categories": ["foam", "jelly"]})
        self.service.profile_store.update(profile.channel_id, updated, profile.version)
        reopened = autopilot.AutopilotService(self.database)
        self.addCleanup(reopened._executor.shutdown, wait=True)
        restored = reopened.store.get_job(job.job_id)
        self.assertEqual(restored.profile_version, profile.version)
        self.assertEqual(restored.params_snapshot["asmr_categories"], ["kinetic_sand", "soap"])
        self.assertEqual(restored.profile_snapshot["upload_post_username"], "account-a")
        self.assertEqual(restored.state, "ready")

    def test_hindi_asmr_word_settings_survive_job_snapshot_and_profile_edit(self):
        profile = self.profiles[0]
        saved = self.service.profile_store.update(
            profile.channel_id,
            profile.model_copy(update={"subtitle_display_mode": "word_by_word",
                                       "voice_name": "gemini:Puck-Upbeat"}),
            profile.version,
        )
        job = self.ready_job(topic="Hindi ASMR word snapshot")
        self.service.profile_store.update(
            saved.channel_id,
            saved.model_copy(update={"stock_video_style": "default",
                                     "subtitle_display_mode": "sentence"}),
            saved.version,
        )
        params = self.service.store.get_job(job.job_id).params_snapshot
        self.assertEqual(params["stock_video_style"], "satisfying_asmr_cutting")
        self.assertEqual(params["asmr_categories"], ["kinetic_sand", "soap"])
        self.assertEqual(params["subtitle_display_mode"], "word_by_word")
        self.assertEqual(params["voice_name"], "gemini:Puck-Upbeat")

    def test_job_snapshot_and_queue_state_survive_store_reopen(self):
        job = self.ready_job(topic="Persistent job")
        self.service.store.queue(job.job_id)
        reopened = autopilot.AutopilotStore(self.database)
        recovered = reopened.get_job(job.job_id)
        self.assertEqual(recovered.state, "queued")
        self.assertEqual(recovered.topic, "Persistent job")
        self.assertEqual(recovered.profile_version, 1)
        self.assertEqual(recovered.params_snapshot["video_subject"], "Persistent job")

    def test_service_restart_submits_durable_queued_job_to_worker(self):
        job = self.ready_job(topic="Resume queued worker task")
        self.service.store.queue(job.job_id)
        started = threading.Event()
        calls = []
        lock = threading.Lock()
        def run(_job_id):
            with lock:
                calls.append(_job_id)
            started.set()
        reopened = autopilot.AutopilotService(self.database, runner=run)
        self.addCleanup(reopened.close, wait=True)
        self.assertTrue(started.wait(timeout=2))
        threading.Event().wait(0.05)
        self.assertEqual(calls, [job.job_id])
        self.assertEqual(reopened.store.get_job(job.job_id).state, "queued")

    def test_restart_marks_dead_generation_worker_failed_without_auto_retry(self):
        job = self.ready_job(topic="Interrupted worker")
        self.service.store.queue(job.job_id)
        self.service.store.claim(job.job_id, "old-task", f"{autopilot.socket.gethostname()}:999999999")
        reopened = autopilot.AutopilotService(self.database)
        self.addCleanup(reopened._executor.shutdown, wait=True)
        recovered = reopened.store.get_job(job.job_id)
        self.assertEqual(recovered.state, "failed")
        self.assertIn("inspect task artifacts", recovered.failure_details)
        self.assertEqual(recovered.attempt_count, 1)

    def test_state_machine_rejects_invalid_transitions_and_double_job_for_plan(self):
        job = self.ready_job(topic="State machine")
        with self.assertRaises(autopilot.InvalidTransition):
            self.service.store.transition(job.job_id, "generated")
        with self.assertRaises(autopilot.InvalidTransition):
            self.service.create_job(job.channel_id, job.plan_id)
        self.assertEqual(self.service.store.get_job(job.job_id).state, "ready")

    def test_delete_job_removes_local_history_and_allows_same_video_to_be_regenerated(self):
        job = self.ready_job(topic="Regenerate after permanent deletion")
        self.service.store.transition(job.job_id, "cancelled")
        result = self.service.store.delete_job(job.job_id, job.channel_id)
        with self.assertRaises(LookupError):
            self.service.store.get_job(job.job_id)
        with self.assertRaises(LookupError):
            self.service.store.get_plan(job.plan_id, job.channel_id)
        self.assertEqual(result["cleanup_errors"], [])
        replacement_plan = self.plan(topic=job.topic, script=job.script)
        self.assertEqual(replacement_plan.topic, job.topic)

    def test_delete_job_rejects_active_and_wrong_channel_jobs(self):
        job = self.ready_job(topic="Do not delete active job")
        self.service.store.queue(job.job_id)
        with self.assertRaises(autopilot.InvalidTransition):
            self.service.store.delete_job(job.job_id, job.channel_id)
        with self.assertRaisesRegex(autopilot.AutopilotError, "selected channel"):
            self.service.store.delete_job(job.job_id, self.profiles[1].channel_id)

    def test_deleted_manual_job_can_be_created_again_from_scratch(self):
        job = self.ready_job(topic="Recover deleted video")
        self.service.store.transition(job.job_id, "cancelled")
        self.service.store.delete_job(job.job_id, job.channel_id)
        self.assertEqual(self.service.list_manual_drafts(job.channel_id), [])
        self.assertEqual(self.service.list_manual_drafts(self.profiles[1].channel_id), [])
        plan = self.plan(topic=job.topic, script=job.script)
        self.service.ready(job.channel_id, plan.plan_id, True)
        replacement = self.service.create_job(job.channel_id, plan.plan_id)
        self.assertEqual(replacement.script, job.script)
        self.assertEqual(replacement.profile_snapshot["publishing_policy"], "manual")
        self.assertEqual(self.service.list_manual_drafts(job.channel_id), [])
        persisted = self.service.store.get_job(replacement.job_id)
        self.assertEqual(persisted.script, job.script)
        self.assertEqual(persisted.state, "ready")
        self.assertEqual(self.service.store.list_publications(job.channel_id), [])
        with self.assertRaises(autopilot.InvalidTransition):
            self.service.prepare_manual_draft(job.channel_id, plan.plan_id, {}, script="Another script")
        with self.assertRaises(autopilot.DuplicateContent):
            self.plan(topic=job.topic)

    def test_failed_manual_material_job_resumes_without_a_new_plan_or_schedule(self):
        job = self.ready_job(topic="Retry material selection")
        self.service.store.queue(job.job_id)
        self.service.store.claim(job.job_id, "material-task", "worker")
        self.service.store.transition(job.job_id, "failed", failure_details="No relevant footage")
        self.service.set_channel_paused(job.channel_id, True, "Recurring schedule is off")
        completed = threading.Event()
        self.service._runner = lambda _job_id: completed.set()
        candidate = self.service.failed_manual_job(job.channel_id, job.topic, job.script)
        self.assertEqual(candidate.job_id, job.job_id)
        resumed = self.service.resume_manual_job(job.job_id)
        self.assertTrue(completed.wait(2))
        self.assertEqual(resumed.job_id, job.job_id)
        self.assertEqual(len(self.service.store.list_plans(job.channel_id)), 1)

    def test_stock_fallback_keeps_script_pinned_destination_and_channel_defaults(self):
        job = self.ready_job(topic="Insufficient ASMR supply")
        self.service.store.queue(job.job_id)
        self.service.store.claim(job.job_id, "scarce-stock-task", "worker")
        self.service.store.transition(job.job_id, "failed", failure_details=(
            "Insufficient distinct ASMR footage: required 35.1s usable duration, found 5.0s"))
        completed = threading.Event()
        self.service._runner = lambda _job_id: completed.set()
        replacement = self.service.retry_with_stock_footage(job.job_id)
        self.assertTrue(completed.wait(2))
        self.assertNotEqual(replacement.job_id, job.job_id)
        self.assertEqual(replacement.script, job.script)
        self.assertEqual(replacement.params_snapshot["stock_video_style"], "default")
        self.assertEqual(replacement.profile_snapshot["upload_post_username"],
                         job.profile_snapshot["upload_post_username"])
        self.assertEqual(replacement.profile_snapshot["publishing_policy"], "manual")
        self.assertEqual(self.service.profile_store.get(job.channel_id).stock_video_style,
                         "satisfying_asmr_cutting")
        self.assertEqual(self.service.store.get_job(job.job_id).state, "failed")
        self.assertEqual(self.service.store.list_publications(job.channel_id), [])
        with self.assertRaises(autopilot.DuplicateContent):
            self.service.retry_with_stock_footage(job.job_id)

    def test_stock_fallback_rejects_other_failure_stages(self):
        job = self.ready_job(topic="Do not change visuals for an audio failure")
        self.service.store.queue(job.job_id)
        self.service.store.claim(job.job_id, "audio-task", "worker")
        self.service.store.transition(job.job_id, "failed", failure_details="TTS failed")
        with self.assertRaises(autopilot.InvalidTransition):
            self.service.retry_with_stock_footage(job.job_id)

    def test_delete_job_removes_published_receipt_locally_without_remote_delete(self):
        job = self.ready_job(topic="Delete local record, keep remote post")
        self.service.store.transition(job.job_id, "cancelled")
        with self.service.store._connection() as conn:
            conn.execute("""INSERT INTO publications (
                publication_id,job_id,channel_id,profile_version,idempotency_key,provider_request_id,
                state,provider_video_id,provider_url,created_at,updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("publication-id", job.job_id, job.channel_id, job.profile_version,
                 "idem-key", "provider-request", "published", "remote-id",
                 "https://youtu.be/remote-id", autopilot._now(), autopilot._now()))
        result = self.service.store.delete_job(job.job_id, job.channel_id)
        self.assertTrue(result["deleted"])
        with self.assertRaises(LookupError):
            self.service.store.get_job(job.job_id)
        with self.service.store._connection() as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM publications WHERE publication_id='publication-id'"
            ).fetchone())
        new_plan = self.plan(topic=job.topic, script=job.script)
        self.assertEqual(new_plan.topic, job.topic)

    def test_delete_job_keeps_uncertain_publication_for_reconciliation(self):
        job = self.ready_job(topic="Keep uncertain upload for reconciliation")
        self.service.store.transition(job.job_id, "cancelled")
        with self.service.store._connection() as conn:
            conn.execute("""INSERT INTO publications (
                publication_id,job_id,channel_id,profile_version,idempotency_key,provider_request_id,
                state,created_at,updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("publication-id", job.job_id, job.channel_id, job.profile_version,
                 "idem-key", "provider-request", "unknown", autopilot._now(), autopilot._now()))
        with self.assertRaisesRegex(autopilot.AutopilotError, "needs reconciliation"):
            self.service.store.delete_job(job.job_id, job.channel_id)
        self.assertEqual(self.service.store.get_job(job.job_id).state, "cancelled")

    def test_delete_job_removes_task_files_and_task_state(self):
        job = self.ready_job(topic="Delete generated local task files")
        self.service.store.transition(job.job_id, "cancelled")
        task_id = "hard-delete-task"
        storage_root = Path(self.temp.name) / "storage"
        task_directory = storage_root / "tasks" / task_id
        task_directory.mkdir(parents=True)
        generated = task_directory / "final.mp4"
        generated.write_bytes(b"local generated video")
        with self.service.store._connection() as conn:
            conn.execute(
                "UPDATE production_jobs SET mpt_task_id=?,output_artifacts_json=? WHERE job_id=?",
                (task_id, json.dumps([str(generated)]), job.job_id),
            )
        state = MemoryState()
        state.update_task(task_id, state=const.TASK_STATE_COMPLETE)
        def storage_dir(sub_dir="", create=False):
            return str(storage_root / sub_dir if sub_dir else storage_root)
        with patch.object(autopilot.utils, "storage_dir", side_effect=storage_dir), \
                patch.object(autopilot.sm, "state", state):
            self.service.store.delete_job(job.job_id, job.channel_id)
        self.assertFalse(task_directory.exists())
        self.assertIsNone(state.get_task(task_id))

    def test_publication_migration_preserves_youtube_receipt_and_allows_second_destination(self):
        job = self.ready_job(topic="Migrate platform publication identity")
        with self.service.store._connection() as conn:
            conn.execute("""INSERT INTO publications (
                publication_id,job_id,channel_id,profile_version,account_username,idempotency_key,
                provider_request_id,state,provider_video_id,provider_url,receipt_json,attempt_count,
                created_at,updated_at,publisher_backend,youtube_connection_id,youtube_channel_id,
                resumable_session_url,platform,destination_account_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("legacy-publication", job.job_id, job.channel_id, job.profile_version, None,
                 "legacy-idempotency", "legacy-request", "unknown", "yt-remote", "https://youtu.be/yt-remote",
                 '{"legacy":true}', 2, autopilot._now(), autopilot._now(), "youtube_direct",
                 "legacy-connection", "UC-legacy", "encrypted-session", "youtube", "UC-legacy"))
            conn.execute("CREATE UNIQUE INDEX legacy_publication_per_job ON publications(job_id)")

        self.service.store._initialize()

        with self.service.store._connection() as conn:
            legacy = conn.execute("SELECT * FROM publications WHERE publication_id='legacy-publication'").fetchone()
            conn.execute("""INSERT INTO publications (
                publication_id,job_id,channel_id,profile_version,account_username,idempotency_key,
                provider_request_id,state,attempt_count,created_at,updated_at,publisher_backend,
                platform,destination_account_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'uploading', 1, ?, ?, 'upload_post', 'instagram', ?)""",
                ("instagram-publication", job.job_id, job.channel_id, job.profile_version, "account-a",
                 "instagram-idempotency", "instagram-request", autopilot._now(), autopilot._now(), "178000000"))
            publications = conn.execute("SELECT platform,destination_account_id FROM publications WHERE job_id=?",
                                        (job.job_id,)).fetchall()
            indexes = conn.execute("PRAGMA index_list(publications)").fetchall()
            unique_job_only = any(index["unique"] and
                                  [col["name"] for col in conn.execute(f"PRAGMA index_info({index['name']})")] == ["job_id"]
                                  for index in indexes)
        self.assertEqual(legacy["provider_video_id"], "yt-remote")
        self.assertEqual(legacy["provider_url"], "https://youtu.be/yt-remote")
        self.assertEqual(legacy["resumable_session_url"], "encrypted-session")
        self.assertEqual({(row["platform"], row["destination_account_id"]) for row in publications},
                         {("youtube", "UC-legacy"), ("instagram", "178000000")})
        self.assertFalse(unique_job_only)

    def test_concurrent_job_creation_for_one_plan_is_single_use(self):
        plan = self.plan(topic="One job only")
        self.service.ready(plan.channel_id, plan.plan_id)
        barrier = threading.Barrier(2)

        def create():
            barrier.wait()
            try:
                return self.service.create_job(plan.channel_id, plan.plan_id)
            except autopilot.InvalidTransition:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(sum(item is not None for item in results), 1)

    def test_concurrent_workers_can_claim_a_queued_job_only_once(self):
        job = self.ready_job(topic="Single worker lease")
        self.service.store.queue(job.job_id)
        barrier = threading.Barrier(2)

        def claim(task_id):
            barrier.wait()
            try:
                return self.service.store.claim(job.job_id, task_id,
                    f"{autopilot.socket.gethostname()}:{os.getpid()}")
            except autopilot.InvalidTransition:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["task-one", "task-two"]))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(self.service.store.get_job(job.job_id).attempt_count, 1)

    def test_manual_runner_uses_pinned_params_suppresses_upload_and_checks_duration(self):
        job = self.ready_job(topic="Runner topic")
        self.service.store.queue(job.job_id)
        state = MemoryState()

        def fake_start(task_id, params, suppress_auto_upload=False):
            self.assertTrue(suppress_auto_upload)
            self.assertEqual(params.video_subject, "Runner topic")
            self.assertEqual(params.asmr_categories, ["kinetic_sand", "soap"])
            state.update_task(task_id, state=const.TASK_STATE_COMPLETE, progress=100,
                              videos=["/tmp/generated.mp4"], combined_videos=[])

        with patch.object(autopilot.sm, "state", state), \
             patch.object(autopilot.task_service, "start", side_effect=fake_start) as start, \
             patch.object(autopilot, "probe_video_duration", return_value=28.0), \
             patch.object(self.service, "qa_report", return_value={"passed": True, "checks": {"artifacts": True}}):
            self.service._run_job(job.job_id)
        start.assert_called_once()
        finished = self.service.store.get_job(job.job_id)
        self.assertEqual(finished.state, "generated")
        self.assertEqual(finished.actual_duration_seconds, 28.0)
        self.assertEqual(finished.output_artifacts, ["/tmp/generated.mp4"])
        self.assertEqual(finished.attempt_count, 1)

    def test_manual_video_uses_its_own_settings_and_runs_with_schedule_paused(self):
        profile = self.profiles[0]
        script = self.hindi_script(topic="three hearts")
        plan = self.service.plan(profile.channel_id, ContentPlanCreate(
            topic="ऑक्टोपस के तीन दिल क्यों होते हैं?", script=script,
            min_duration_seconds=18, max_duration_seconds=45,
        ), manual_overrides={
            "target_duration_seconds": 30, "video_aspect": "9:16",
            "stock_video_style": "satisfying_asmr_cutting", "language": "hi",
            "font_name": "Lohit-Devanagari.ttf",
        })
        self.assertEqual(plan.script, script)
        self.service.ready(profile.channel_id, plan.plan_id)
        job = self.service.create_job(profile.channel_id, plan.plan_id)
        self.assertEqual(job.params_snapshot["video_script"], script)
        self.assertEqual(job.params_snapshot["font_name"], "Lohit-Devanagari.ttf")
        self.assertEqual(job.profile_snapshot["publishing_policy"], "manual")
        self.assertEqual(job.target_duration_seconds, 30)
        self.service.set_channel_paused(profile.channel_id, True)
        done = threading.Event()
        service = autopilot.AutopilotService(self.database, runner=lambda _: done.set())
        self.addCleanup(service._executor.shutdown, wait=True)
        queued = service.start_job(job.job_id)
        self.assertEqual(queued.state, "queued")
        self.assertTrue(done.wait(2))

    def test_existing_ready_plan_can_be_reused_as_manual_without_auto_upload(self):
        profile = self.profiles[0]
        plan = self.plan(topic="Octopus ke 3 dil")
        self.service.ready(profile.channel_id, plan.plan_id)
        updated = self.service.profile_store.update(profile.channel_id,
            profile.model_copy(update={"publishing_policy": "private"}), profile.version)
        reused = self.service.prepare_manual_draft(profile.channel_id, plan.plan_id, {
            "target_duration_seconds": 30, "video_aspect": "9:16",
            "stock_video_style": "satisfying_asmr_cutting", "language": "hi",
            "font_name": "Lohit-Devanagari.ttf",
            "bgm_type": "", "bgm_file": "", "bgm_volume": 0.0,
        })
        self.assertEqual(reused.script, plan.script)
        self.assertEqual(reused.profile_version, updated.version)
        job = self.service.create_job(profile.channel_id, reused.plan_id)
        self.assertEqual(job.profile_snapshot["publishing_policy"], "manual")
        self.assertEqual(job.params_snapshot["video_script"], plan.script)
        self.assertEqual(job.params_snapshot["font_name"], "Lohit-Devanagari.ttf")
        self.assertEqual(job.params_snapshot["bgm_type"], "")
        self.assertEqual(job.params_snapshot["bgm_volume"], 0.0)
        with self.assertRaises(autopilot.InvalidTransition):
            self.service.prepare_manual_draft(profile.channel_id, plan.plan_id, {})

    def test_qa_failure_marks_job_failed_and_keeps_the_rendered_artifact(self):
        job = self.ready_job(topic="QA failure")
        self.service.store.queue(job.job_id)
        state = MemoryState()

        def fake_start(task_id, params, suppress_auto_upload=False):
            state.update_task(task_id, state=const.TASK_STATE_COMPLETE, progress=100,
                              videos=["/tmp/qa-failed.mp4"])

        with patch.object(autopilot.sm, "state", state), \
             patch.object(autopilot.task_service, "start", side_effect=fake_start), \
             patch.object(autopilot, "probe_video_duration", return_value=28.0), \
             patch.object(self.service, "qa_report", return_value={"passed": False, "checks": {"audio_track": False}}):
            self.service._run_job(job.job_id)
        failed = self.service.store.get_job(job.job_id)
        self.assertEqual(failed.state, "failed")
        self.assertIn("audio_track", failed.failure_details)
        self.assertEqual(failed.output_artifacts, ["/tmp/qa-failed.mp4"])

    def test_qa_checks_media_dimensions_audio_subtitles_and_material_references(self):
        from unittest.mock import MagicMock
        job = self.ready_job(topic="QA gates")
        self.service.store.queue(job.job_id)
        self.service.store.claim(job.job_id, "qa-task", f"{autopilot.socket.gethostname()}:{os.getpid()}")
        self.service.store.transition(job.job_id, "generated",
            output_artifacts_json='["/tmp/final-1.mp4", "/tmp/combined-1.mp4"]',
            actual_duration_seconds=28)
        state = MemoryState()
        state.update_task("qa-task", state=const.TASK_STATE_COMPLETE, audio_file="/tmp/audio.mp3",
                          subtitle_path="/tmp/subtitle.srt", materials=["/tmp/material.mp4"],
                          videos=["/tmp/final-1.mp4"], combined_videos=["/tmp/combined-1.mp4"])
        final_clip = MagicMock(w=1080, h=1920, audio=object())
        combined_clip = MagicMock(w=1080, h=1920, audio=None)
        with patch.object(autopilot.sm, "state", state), \
             patch("app.services.autopilot.os.path.isfile", return_value=True), \
             patch.object(autopilot.video, "_open_video_clip_quietly",
                          side_effect=lambda path, audio: (
                              final_clip if path.endswith("final-1.mp4") else combined_clip
                          )):
            report = self.service.qa_report(job.job_id)
        self.assertTrue(report["passed"])
        self.assertTrue(report["checks"]["orientation"])
        self.assertTrue(report["checks"]["audio_track"])
        self.assertTrue(report["checks"]["subtitles"])

    def test_duration_mismatch_fails_without_discarding_artifact_or_rerendering(self):
        strict_profile = self.profiles[0].model_copy(update={"duration_enforcement": "strict"})
        self.service.profile_store.update(
            strict_profile.channel_id, strict_profile, strict_profile.version
        )
        job = self.ready_job(topic="Wrong duration")
        self.service.store.queue(job.job_id)
        state = MemoryState()

        def fake_start(task_id, params, suppress_auto_upload=False):
            state.update_task(task_id, state=const.TASK_STATE_COMPLETE, progress=100,
                              videos=["/tmp/out-of-range.mp4"])

        with patch.object(autopilot.sm, "state", state), \
             patch.object(autopilot.task_service, "start", side_effect=fake_start), \
             patch.object(autopilot, "probe_video_duration", return_value=34.0):
            self.service._run_job(job.job_id)
        failed = self.service.store.get_job(job.job_id)
        self.assertEqual(failed.state, "failed")
        self.assertIn("no automatic rerender", failed.failure_details)
        self.assertEqual(failed.output_artifacts, ["/tmp/out-of-range.mp4"])

    def test_flexible_autopilot_keeps_out_of_range_render_and_marks_metadata(self):
        job = self.ready_job(topic="Flexible duration")
        self.assertEqual(job.duration_enforcement, "flexible")
        self.service.store.queue(job.job_id)
        state = MemoryState()

        def fake_start(task_id, params, suppress_auto_upload=False):
            state.update_task(task_id, state=const.TASK_STATE_COMPLETE, progress=100,
                              videos=["/tmp/out-of-range-flexible.mp4"])

        with patch.object(autopilot.sm, "state", state), \
             patch.object(autopilot.task_service, "start", side_effect=fake_start), \
             patch.object(autopilot, "probe_video_duration", return_value=34.0), \
             patch.object(self.service, "qa_report", return_value={"passed": True, "checks": {}}):
            self.service._run_job(job.job_id)
        generated = self.service.store.get_job(job.job_id)
        self.assertEqual(generated.state, "generated")
        self.assertEqual(generated.actual_duration_seconds, 34.0)
        self.assertTrue(generated.duration_outside_requested_range)

    def test_failed_mpt_generation_is_recorded_for_manual_retry(self):
        job = self.ready_job(topic="Generation failure")
        self.service.store.queue(job.job_id)
        state = MemoryState()

        def fake_start(task_id, params, suppress_auto_upload=False):
            state.update_task(task_id, state=const.TASK_STATE_FAILED, progress=30,
                              failed_stage="materials", error="provider unavailable")

        with patch.object(autopilot.sm, "state", state), \
             patch.object(autopilot.task_service, "start", side_effect=fake_start):
            self.service._run_job(job.job_id)
        failed = self.service.store.get_job(job.job_id)
        self.assertEqual(failed.state, "failed")
        self.assertIn("provider unavailable", failed.failure_details)
        self.assertEqual(failed.attempt_count, 1)
        self.assertTrue(failed.mpt_task_id)

    def test_failed_pipeline_is_persisted_and_retry_keeps_snapshot(self):
        calls = threading.Event()

        def fake_runner(job_id):
            calls.set()

        service = autopilot.AutopilotService(self.database, runner=fake_runner)
        self.addCleanup(service._executor.shutdown, wait=True)
        job = self.ready_job(topic="Retry exact snapshot")
        service.store.queue(job.job_id)
        service.store.claim(job.job_id, "first-task", f"{autopilot.socket.gethostname()}:{os.getpid()}")
        service.store.transition(job.job_id, "failed", failure_details="provider timeout")
        retried = service.retry_job(job.job_id)
        self.assertTrue(calls.wait(2))
        self.assertEqual(retried.state, "queued")
        self.assertEqual(service.store.get_job(job.job_id).params_snapshot, job.params_snapshot)

    def test_audio_recovery_creates_new_job_with_saved_voice_and_keeps_failed_snapshot(self):
        profile = self.service.profile_store.get(self.profiles[0].channel_id)
        profile = self.service.profile_store.update(
            profile.channel_id,
            profile.model_copy(update={"voice_name": "hi-IN-Chirp3-HD-Iapetus"}), profile.version,
        )
        job = self.ready_job(topic="Voice recovery").model_copy(update={"mpt_task_id": "audio-failed-task"})
        self.service.store.queue(job.job_id)
        self.service.store.claim(job.job_id, job.mpt_task_id,
                                 f"{autopilot.socket.gethostname()}:{os.getpid()}")
        self.service.store.transition(job.job_id, "failed", failure_details="TTS failed")
        profile = self.service.profile_store.get(job.channel_id)
        updated = self.service.profile_store.update(
            profile.channel_id, profile.model_copy(update={"voice_name": "gemini:Puck-Upbeat"}), profile.version
        )
        state = MemoryState()
        state.update_task(job.mpt_task_id, state=const.TASK_STATE_FAILED, progress=20, failed_stage="audio")
        completed = threading.Event()
        self.service._runner = lambda _job_id: completed.set()

        with patch.object(autopilot.sm, "state", state):
            candidate = self.service.failed_manual_audio_job(job.channel_id, job.topic)
            self.assertEqual(candidate.job_id, job.job_id)
            self.assertIsNone(self.service.failed_manual_audio_job(job.channel_id, "Different topic"))
            replacement = self.service.retry_with_current_channel_settings(job.job_id)
        self.assertTrue(completed.wait(2))
        self.assertEqual(replacement.state, "queued")
        self.assertNotEqual(replacement.job_id, job.job_id)
        self.assertEqual(replacement.profile_snapshot["voice_name"], updated.voice_name)
        self.assertEqual(self.service.store.get_job(job.job_id).profile_snapshot["voice_name"],
                         job.profile_snapshot["voice_name"])

    def test_audio_recovery_rejects_same_voice(self):
        profile = self.service.profile_store.get(self.profiles[0].channel_id)
        self.service.profile_store.update(
            profile.channel_id, profile.model_copy(update={"voice_name": "gemini:Puck-Upbeat"}), profile.version
        )
        job = self.ready_job(topic="Voice recovery unchanged").model_copy(
            update={"mpt_task_id": "audio-failed-same-voice"}
        )
        self.service.store.queue(job.job_id)
        self.service.store.claim(job.job_id, job.mpt_task_id,
                                 f"{autopilot.socket.gethostname()}:{os.getpid()}")
        self.service.store.transition(job.job_id, "failed", failure_details="TTS failed")
        state = MemoryState()
        state.update_task(job.mpt_task_id, state=const.TASK_STATE_FAILED, progress=20, failed_stage="audio")
        with patch.object(autopilot.sm, "state", state), self.assertRaisesRegex(
            autopilot.AutopilotError, "save a different channel voice"
        ):
            self.service.retry_with_current_channel_settings(job.job_id)

    def test_cancel_queued_job_and_reject_cancel_after_worker_claim(self):
        job = self.ready_job(topic="Cancellation")
        self.service.store.queue(job.job_id)
        cancelled = self.service.cancel_job(job.job_id)
        self.assertEqual(cancelled.state, "cancelled")
        second = self.ready_job(topic="Cannot stop active")
        self.service.store.queue(second.job_id)
        self.service.store.claim(second.job_id, "mpt-task", f"{autopilot.socket.gethostname()}:{os.getpid()}")
        with self.assertRaises(autopilot.InvalidTransition):
            self.service.cancel_job(second.job_id)

    def test_recurring_slot_is_materialized_once_and_paused_channels_do_not_run(self):
        from datetime import datetime, timezone
        profile = self.profiles[0].model_copy(update={
            "publications_per_week": 1, "schedule_times_local": ["09:00"],
            "schedule_weekdays": [0], "topic_preferences": ["science and nature facts"],
        })
        profile = self.service.profile_store.update(profile.channel_id, profile, profile.version)
        self.service._runner = lambda _job_id: None
        with patch.object(
            autopilot.llm, "generate_topic",
            return_value="चंद्रमा क्षितिज के पास बड़ा क्यों दिखता है?",
        ), patch.object(
            autopilot.llm, "generate_script",
            return_value=self.hindi_script(topic="moon"),
        ):
            first = self.service.tick_scheduler(datetime(2026, 10, 19, 9, 5, tzinfo=timezone.utc))
            second = self.service.tick_scheduler(datetime(2026, 10, 19, 9, 6, tzinfo=timezone.utc))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        slots = self.service.store.list_slots(profile.channel_id)
        self.assertEqual(sum(slot["state"] == "queued" for slot in slots), 1)
        self.assertFalse(any(slot["state"] == "failed" for slot in slots))

    def test_scheduler_recovers_missed_slots_after_1_4_and_24_hour_outages(self):
        from datetime import datetime, timezone

        cases = (
            ("one hour", datetime(2035, 1, 1, 10, 0, tzinfo=timezone.utc),
             datetime(2035, 1, 1, 8, 59, tzinfo=timezone.utc), "2035-01-01T09:00:00+00:00"),
            ("four hours", datetime(2035, 1, 1, 13, 0, tzinfo=timezone.utc),
             datetime(2035, 1, 1, 8, 59, tzinfo=timezone.utc), "2035-01-01T09:00:00+00:00"),
            ("twenty four hours", datetime(2035, 1, 2, 10, 0, tzinfo=timezone.utc),
             datetime(2035, 1, 1, 9, 59, tzinfo=timezone.utc), "2035-01-02T09:00:00+00:00"),
        )
        for label, now, previous_tick, missed_time in cases:
            with self.subTest(outage=label), TemporaryDirectory() as temporary:
                database = Path(temporary) / "autopilot.sqlite3"
                service = autopilot.AutopilotService(database, runner=lambda _job_id: None)
                profile = service.profile_store.create(ChannelProfile(
                    display_name=f"Recovery {label}", language="hi", niche="Hindi facts",
                    topic_preferences=["nature"], timezone="UTC", publications_per_week=7,
                    schedule_times_local=["09:00"], schedule_weekdays=list(range(7)),
                    generation_lead_hours=48, ready_buffer_target=1,
                    publishing_policy="manual", auto_public_enabled=False,
                ))
                service.store.set_scheduler_tick(profile.channel_id, previous_tick)
                topic_variants = iter(("Jupiter storms", "Octopus hearts", "Coral reefs"))
                with patch.object(
                    service, "_topic_for_slot",
                    side_effect=lambda _profile, slot, _style=None: f"{next(topic_variants)} {slot.isoformat()}",
                ), patch.object(
                    autopilot.llm, "generate_script",
                    side_effect=lambda **kwargs: self.hindi_script(topic=kwargs["video_subject"]),
                ):
                    service.tick_scheduler(now)

                slots = service.store.list_slots(profile.channel_id, limit=100)
                missed = next(slot for slot in slots if slot["slot_utc"] == missed_time)
                self.assertEqual(missed["state"], "skipped")
                self.assertIn("rescheduled to", missed["details"])
                recovery = next(slot for slot in slots if slot["recovery_of_slot_id"] == missed["slot_id"])
                recovered_at = datetime.fromisoformat(recovery["slot_utc"])
                self.assertGreater(recovered_at, now)
                self.assertIn(recovered_at.weekday(), range(7))
                self.assertEqual((recovered_at.hour, recovered_at.minute), (9, 0))
                recovered_job = service.store.get_job(recovery["job_id"])
                self.assertEqual(recovered_job.channel_id, profile.channel_id)
                self.assertEqual(recovered_job.intended_publication_slot, recovered_at)
                self.assertEqual(recovered_job.profile_snapshot["publishing_policy"], "manual")
                self.assertFalse(recovered_job.profile_snapshot["auto_public_enabled"])
                service.close(wait=True)

    def test_missed_recovery_link_and_work_survive_scheduler_restart(self):
        from datetime import datetime, timezone

        with TemporaryDirectory() as temporary:
            database = Path(temporary) / "autopilot.sqlite3"
            now = datetime(2035, 1, 1, 13, 0, tzinfo=timezone.utc)
            service = autopilot.AutopilotService(database, runner=lambda _job_id: None)
            profile = service.profile_store.create(ChannelProfile(
                display_name="Restart recovery", language="hi", niche="Hindi facts",
                topic_preferences=["nature"], timezone="UTC", publications_per_week=1,
                schedule_times_local=["09:00"], schedule_weekdays=[0],
                generation_lead_hours=48, ready_buffer_target=1,
                publishing_policy="manual", auto_public_enabled=False,
            ))
            service.store.set_scheduler_tick(
                profile.channel_id, datetime(2035, 1, 1, 8, 59, tzinfo=timezone.utc),
            )
            topic_variants = iter(("Moon tides", "Octopus hearts", "Coral reefs"))
            with patch.object(
                service, "_topic_for_slot",
                side_effect=lambda _profile, slot, _style=None: f"{next(topic_variants)} {slot.isoformat()}",
            ), patch.object(
                autopilot.llm, "generate_script",
                side_effect=lambda **kwargs: self.hindi_script(topic=kwargs["video_subject"]),
            ):
                service.tick_scheduler(now)
            service.close(wait=True)

            reopened = autopilot.AutopilotService(database, runner=lambda _job_id: None)
            try:
                before = reopened.store.list_slots(profile.channel_id, limit=100)
                missed = next(slot for slot in before if slot["slot_utc"] == "2035-01-01T09:00:00+00:00")
                recovery = next(slot for slot in before if slot["recovery_of_slot_id"] == missed["slot_id"])
                reopened.tick_scheduler(now)
                after = reopened.store.list_slots(profile.channel_id, limit=100)
                self.assertEqual(
                    sum(slot["recovery_of_slot_id"] == missed["slot_id"] for slot in after), 1,
                )
                self.assertEqual(reopened.store.get_job(recovery["job_id"]).intended_publication_slot,
                                 datetime.fromisoformat(recovery["slot_utc"]))
            finally:
                reopened.close(wait=True)

    def test_pause_state_survives_scheduler_cursor_migration(self):
        profile = self.profiles[0]
        self.service.set_channel_paused(profile.channel_id, True, "operator pause")
        reopened = autopilot.AutopilotService(self.database, runner=lambda _job_id: None)
        try:
            self.assertTrue(reopened.schedule_state(profile.channel_id)["paused"])
        finally:
            reopened.close(wait=True)

    def test_missed_recovery_reuses_completed_video_when_upload_never_started(self):
        from datetime import datetime, timezone

        with TemporaryDirectory() as temporary:
            database = Path(temporary) / "autopilot.sqlite3"
            now = datetime(2035, 1, 1, 13, 0, tzinfo=timezone.utc)
            missed_at = datetime(2035, 1, 1, 9, 0, tzinfo=timezone.utc)
            artifact = Path(temporary) / "completed.mp4"
            artifact.write_bytes(b"offline test video")
            service = autopilot.AutopilotService(database, runner=lambda _job_id: None)
            profile = service.profile_store.create(ChannelProfile(
                display_name="Reuse missed video", language="hi", niche="Hindi facts",
                topic_preferences=["nature"], timezone="UTC", publications_per_week=7,
                schedule_times_local=["09:00"], schedule_weekdays=list(range(7)),
                generation_lead_hours=48, ready_buffer_target=1,
                publishing_policy="manual", auto_public_enabled=False,
            ))
            service.store.set_scheduler_tick(
                profile.channel_id, datetime(2035, 1, 1, 8, 59, tzinfo=timezone.utc),
            )
            plan = service.plan(profile.channel_id, ContentPlanCreate(
                topic="Missed original topic", script=self.hindi_script(topic="missed"),
                min_duration_seconds=25, max_duration_seconds=30,
                intended_publication_slot=missed_at,
            ))
            ready = service.ready(profile.channel_id, plan.plan_id, True)
            job = service.create_job(profile.channel_id, ready.plan_id)
            service.store.queue(job.job_id)
            service.store.claim(job.job_id, "completed-task", "test-worker")
            service.store.transition(
                job.job_id, "generated", output_artifacts_json=json.dumps([str(artifact)]),
            )
            with service.store._connection() as conn:
                conn.execute(
                    "INSERT INTO job_qa VALUES (?, 'qa_passed', '{}', NULL, NULL, ?)",
                    (job.job_id, autopilot._now()),
                )
            missed_id = f"{profile.channel_id}:{missed_at.isoformat()}"
            service.store.claim_slot(missed_id, profile.channel_id, missed_at.isoformat(), "test-owner")
            service.store.finish_slot(missed_id, "queued", job.job_id)

            with patch.object(service, "reconcile_publications"):
                service.tick_scheduler(now)

            recovery = next(slot for slot in service.store.list_slots(profile.channel_id, 100)
                            if slot["recovery_of_slot_id"] == missed_id)
            self.assertEqual(recovery["job_id"], job.job_id)
            self.assertEqual(service.store.get_job(job.job_id).intended_publication_slot,
                             datetime.fromisoformat(recovery["slot_utc"]))
            self.assertEqual(service.store.get_job(job.job_id).output_artifacts, [str(artifact)])
            service.close(wait=True)

    def test_uncertain_missed_upload_does_not_create_replacement_work(self):
        from datetime import datetime, timezone
        from uuid import uuid4

        now = datetime(2035, 1, 1, 13, 0, tzinfo=timezone.utc)
        missed_at = datetime(2035, 1, 1, 9, 0, tzinfo=timezone.utc)
        profile = self.profiles[0].model_copy(update={
            "timezone": "UTC", "publications_per_week": 1,
            "schedule_times_local": ["09:00"], "schedule_weekdays": [0],
            "publishing_policy": "manual", "auto_public_enabled": False,
        })
        profile = self.service.profile_store.update(profile.channel_id, profile, profile.version)
        self.service.store.set_scheduler_tick(
            profile.channel_id, datetime(2035, 1, 1, 8, 59, tzinfo=timezone.utc),
        )
        plan = self.service.plan(profile.channel_id, ContentPlanCreate(
            topic="Uncertain missed upload", script=self.hindi_script(topic="uncertain"),
            min_duration_seconds=25, max_duration_seconds=30,
            intended_publication_slot=missed_at,
        ))
        ready = self.service.ready(profile.channel_id, plan.plan_id, True)
        job = self.service.create_job(profile.channel_id, ready.plan_id)
        missed_id = f"{profile.channel_id}:{missed_at.isoformat()}"
        self.service.store.claim_slot(missed_id, profile.channel_id, missed_at.isoformat(), "test-owner")
        self.service.store.finish_slot(missed_id, "skipped", details="missed during downtime; recovery pending")
        now_text = autopilot._now()
        with self.service.store._connection() as conn:
            conn.execute(
                "INSERT INTO publications (publication_id,job_id,channel_id,profile_version,idempotency_key,"
                "provider_request_id,state,attempt_count,created_at,updated_at,publisher_backend,next_check_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'unknown', 1, ?, ?, 'youtube_direct', ?)",
                (str(uuid4()), job.job_id, profile.channel_id, job.profile_version,
                 f"retry-{job.job_id}", f"request-{job.job_id}", now_text, now_text,
                 datetime(2035, 1, 1, 14, 0, tzinfo=timezone.utc).isoformat()),
            )
        with patch.object(self.service, "reconcile_publications"), patch.object(
            self.service, "_topic_for_slot", side_effect=AssertionError("uncertain upload must block new work"),
        ):
            self.service.tick_scheduler(now)
        self.assertFalse(any(slot["recovery_of_slot_id"] == missed_id
                             for slot in self.service.store.list_slots(profile.channel_id, 100)))

    def test_schedule_slot_lease_prevents_concurrent_claim_and_recovers_after_expiry(self):
        slot_id = f"{self.profiles[0].channel_id}:2030-01-01T09:00:00+00:00"
        self.assertTrue(self.service.store.claim_slot(slot_id, self.profiles[0].channel_id,
                                                      "2030-01-01T09:00:00+00:00", "worker-a"))
        self.assertFalse(self.service.store.claim_slot(slot_id, self.profiles[0].channel_id,
                                                       "2030-01-01T09:00:00+00:00", "worker-b"))
        with self.service.store._connection() as conn:
            conn.execute("UPDATE autopilot_slots SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE slot_id=?", (slot_id,))
        self.assertTrue(self.service.store.claim_slot(slot_id, self.profiles[0].channel_id,
                                                      "2030-01-01T09:00:00+00:00", "worker-b"))

    def test_three_daily_kolkata_slots_fill_buffer_and_persist_style_rotation(self):
        from datetime import datetime, timezone
        profile = self.profiles[0].model_copy(update={
            "timezone": "Asia/Kolkata", "publications_per_week": 21,
            "schedule_times_local": ["09:00", "14:00", "20:00"],
            "schedule_weekdays": list(range(7)),
            "generation_lead_hours": 48, "ready_buffer_target": 6,
            "visual_style_rotation": ["relevant_stock", "savage_animated_explainer"],
            "topic_preferences": ["Hindi science facts"],
        })
        profile = self.service.profile_store.update(profile.channel_id, profile, profile.version)
        self.service._runner = lambda _job_id: None
        topics = iter([
            "Moonlight tides along coastlines", "Coral gardens feed reef ecosystems",
            "Jupiter winds cross giant storms", "Honey lasts through dry winters",
            "Octopus arms sense underwater terrain", "Venus turns opposite most planets",
        ])
        topic_prompts = []
        def draft_topic(**kwargs):
            topic_prompts.append(kwargs)
            return next(topics)

        with patch.object(autopilot.llm, "generate_topic",
                          side_effect=draft_topic), \
             patch.object(autopilot.llm, "generate_script",
                          side_effect=lambda **kwargs: (
                              f"This narration explains {kwargs['video_subject']} with distinct science details."
                          )):
            created = self.service.tick_scheduler(
                datetime(2035, 1, 1, 2, 0, tzinfo=timezone.utc)
            )
        self.assertEqual(len(created), 6)
        expected_guidance = [
            f"{autopilot.visual_style.preset_for(style).idea_guidance} "
            f"{autopilot.visual_style.preset_for(style).visual_guidance}"
            for style in profile.visual_style_rotation
        ] * 3
        self.assertEqual([item["style_guidance"] for item in topic_prompts], expected_guidance)
        jobs = self.service.store.list_jobs(profile.channel_id, limit=20)
        self.assertEqual(sum(job.state in {"queued", "generating"} for job in jobs), 6)
        ordered_slots = sorted(
            (slot for slot in self.service.store.list_slots(profile.channel_id, limit=100)
             if slot["state"] == "queued"),
            key=lambda slot: slot["slot_utc"],
        )
        assigned = [slot["visual_style"] for slot in ordered_slots]
        self.assertEqual(assigned, ["relevant_stock", "savage_animated_explainer"] * 3)
        self.assertEqual(self.service.schedule_state(profile.channel_id)["buffer"]["in_progress"], 6)
        self.assertEqual(self.service.tick_scheduler(
            datetime(2035, 1, 1, 2, 1, tzinfo=timezone.utc)
        ), [])

    def test_buffer_reports_six_qa_passed_future_jobs(self):
        from datetime import datetime, timezone
        from datetime import timedelta

        now = datetime(2035, 1, 1, tzinfo=timezone.utc)
        examples = [
            ("lunar phase produces tides", "Lunar gravity moves ocean water around Earth coastlines."),
            ("coral reefs host microscopic algae", "Corals shelter algae that provide reef animals with energy."),
            ("jupiter spins rapidly despite its size", "Jupiter completes one rotation in roughly ten hours."),
            ("honey persists in dry storage", "Low water content helps honey resist spoilage for long periods."),
            ("octopus neurons extend into arms", "An octopus processes touch through neurons distributed across its arms."),
            ("venus rotates in reverse direction", "Venus spins east to west unlike most planets in our system."),
        ]
        for index, (topic, script) in enumerate(examples):
            plan = self.service.plan(self.profiles[0].channel_id, ContentPlanCreate(
                topic=topic, script=script,
                intended_publication_slot=now + timedelta(days=index + 1),
            ))
            ready = self.service.ready(plan.channel_id, plan.plan_id, True)
            job = self.service.create_job(plan.channel_id, ready.plan_id)
            self.service.store.queue(job.job_id)
            self.service.store.claim(job.job_id, f"task-{index}",
                                     f"{autopilot.socket.gethostname()}:{os.getpid()}")
            self.service.store.transition(job.job_id, "generated", output_artifacts_json='["/tmp/short.mp4"]')
            with self.service.store._connection() as conn:
                conn.execute("INSERT INTO job_qa VALUES (?, 'qa_passed', '{}', NULL, NULL, ?)",
                             (job.job_id, autopilot._now()))
        self.assertEqual(self.service.store.buffer_counts(
            self.profiles[0].channel_id, now, target=6
        ), {"in_progress": 0, "validated": 6, "target": 6, "remaining": 0})

    def test_transient_retry_predicate_is_narrow_and_retry_limit_is_configurable(self):
        self.assertTrue(autopilot._transient_generation_failure(
            "ConnectionError: temporary failure in name resolution"
        ))
        self.assertFalse(autopilot._transient_generation_failure(
            "failed QA: video is missing an audio track"
        ))

    def test_scheduler_retries_transient_topic_planning_failure(self):
        from datetime import datetime, timedelta, timezone
        profile = self.profiles[0]
        current = datetime.now(timezone.utc)
        next_slot = current + timedelta(minutes=2)
        profile = profile.model_copy(update={
            "timezone": "UTC", "publications_per_week": 1,
            "schedule_times_local": [next_slot.strftime("%H:%M")],
            "schedule_weekdays": [next_slot.weekday()],
            "generation_lead_hours": 24, "ready_buffer_target": 1,
            "provider_retry_limit": 2, "topic_preferences": ["nature facts"],
        })
        profile = self.service.profile_store.update(profile.channel_id, profile, profile.version)
        self.service._runner = lambda _job_id: None
        with patch.object(autopilot.llm, "generate_topic", side_effect=[
            ConnectionError("temporary failure in name resolution"),
            "A distinct fact about ocean currents",
        ]), patch.object(autopilot.llm, "generate_script",
                         return_value="Ocean currents move warm water between distant regions."):
            self.assertEqual(self.service.tick_scheduler(current), [])
            slot = self.service.store.list_slots(profile.channel_id)[0]
            self.assertEqual(slot["state"], "failed")
            self.assertEqual(slot["attempt_count"], 1)
            with self.service.store._connection() as conn:
                conn.execute(
                    "UPDATE autopilot_slots SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE slot_id=?",
                    (slot["slot_id"],),
                )
            second = self.service.tick_scheduler(current + timedelta(seconds=31))
        self.assertEqual(len(second), 1)
        retried = next(item for item in self.service.store.list_slots(profile.channel_id)
                       if item["slot_id"] == slot["slot_id"])
        self.assertEqual(retried["state"], "queued")
        self.assertEqual(retried["attempt_count"], 2)

    def test_scheduler_retries_only_due_transient_jobs_within_profile_limit(self):
        from datetime import datetime, timedelta, timezone
        profile = self.profiles[0].model_copy(update={"provider_retry_limit": 1})
        profile = self.service.profile_store.update(profile.channel_id, profile, profile.version)
        plan = self.service.plan(profile.channel_id, ContentPlanCreate(
            topic="transient network retry eligibility",
            script="The ocean contains salt dissolved in its water.",
            intended_publication_slot=datetime.now(timezone.utc) + timedelta(days=1),
        ))
        job = self.service.create_job(profile.channel_id,
                                      self.service.ready(plan.channel_id, plan.plan_id, True).plan_id)
        self.service.store.queue(job.job_id)
        self.service.store.claim(job.job_id, "retry-task",
                                 f"{autopilot.socket.gethostname()}:{os.getpid()}")
        self.service.store.transition(job.job_id, "failed",
            failure_details="ConnectionError: temporary failure in name resolution")
        with self.service.store._connection() as conn:
            conn.execute("UPDATE production_jobs SET updated_at=? WHERE job_id=?",
                         ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), job.job_id))
        with patch.object(self.service, "start_job", return_value=job) as start:
            result = self.service.tick_scheduler()
        start.assert_called_once_with(job.job_id)
        self.assertIn({"job_id": job.job_id, "retry": 1}, result)

    def test_upload_is_routed_using_the_immutable_job_account_and_stable_request_id(self):
        from unittest.mock import MagicMock
        job = self.ready_job(topic="Pinned account publication")
        self.service.store.queue(job.job_id)
        self.service.store.claim(job.job_id, "task", f"{autopilot.socket.gethostname()}:{os.getpid()}")
        self.service.store.transition(job.job_id, "generated", output_artifacts_json='["/tmp/video.mp4"]', actual_duration_seconds=28)
        with self.service.store._connection() as conn:
            conn.execute("INSERT INTO job_qa VALUES (?, 'qa_passed', '{}', NULL, NULL, ?)",
                         (job.job_id, autopilot._now()))
        uploader = MagicMock()
        uploader.upload_video.return_value = {"success": True, "request_id": "provider-request", "results": {"youtube": {"success": True, "video_id": "yt123", "url": "https://youtube.test/yt123"}}}
        with patch.object(self.service, "qa_report", return_value={"passed": True, "checks": {}}) as fresh_qa, \
             patch.object(autopilot, "UploadPostService", return_value=uploader) as uploader_class, \
             patch.object(autopilot.config, "app", {"upload_post_api_key": "local-secret",
                                                       "upload_post_enabled": True}):
            uploader.connected_platform.return_value = {"username": "yt-account-1", "handle": "MPT Shorts"}
            result = self.service.publish_job(job.job_id)
        fresh_qa.assert_not_called()
        self.assertEqual(result["state"], "published")
        account = uploader_class.call_args.args[0]
        self.assertEqual(account["upload_post_username"], "account-a")
        request_id = uploader.upload_video.call_args.kwargs["request_id"]
        self.assertEqual(len(request_id), 32)
        with self.service.store._connection() as conn:
            receipt = conn.execute("SELECT * FROM publications WHERE job_id=?", (job.job_id,)).fetchone()
        self.assertEqual(receipt["provider_video_id"], "yt123")
        self.assertEqual(receipt["provider_url"], "https://youtube.test/yt123")

    def test_upload_post_uses_each_pinned_channels_audience_setting(self):
        from unittest.mock import MagicMock

        jobs = []
        for index, (profile, made_for_kids) in enumerate(zip(self.profiles, (True, False))):
            updated = self.service.profile_store.update(
                profile.channel_id,
                profile.model_copy(update={"youtube_made_for_kids": made_for_kids}),
                profile.version,
            )
            plan = self.service.plan(updated.channel_id, ContentPlanCreate(
                topic=f"Audience profile {index}",
                script=self.hindi_script(topic=f"audience-{index}"),
                min_duration_seconds=25, max_duration_seconds=30,
            ))
            ready = self.service.ready(updated.channel_id, plan.plan_id)
            job = self.service.create_job(updated.channel_id, ready.plan_id)
            self.service.store.queue(job.job_id)
            self.service.store.claim(
                job.job_id, f"task-{index}",
                f"{autopilot.socket.gethostname()}:{os.getpid()}",
            )
            self.service.store.transition(
                job.job_id, "generated", output_artifacts_json='["/tmp/video.mp4"]',
                actual_duration_seconds=28,
            )
            with self.service.store._connection() as conn:
                conn.execute(
                    "INSERT INTO job_qa VALUES (?, 'qa_passed', '{}', NULL, NULL, ?)",
                    (job.job_id, autopilot._now()),
                )
            jobs.append(job)

        uploader = MagicMock()
        uploader.connected_platform.side_effect = lambda _platform, username, *_args: {
            "username": username, "handle": username,
        }
        uploader.upload_video.side_effect = [
            {"success": True, "request_id": f"request-{index}",
             "results": {"youtube": {"success": True, "video_id": f"video-{index}"}}}
            for index in range(2)
        ]
        with patch.object(self.service, "qa_report", return_value={"passed": True, "checks": {}}), \
             patch.object(autopilot, "UploadPostService", return_value=uploader), \
             patch.object(autopilot.config, "app", {
                 "upload_post_api_key": "local-secret", "upload_post_enabled": True,
                 "upload_post_youtube_made_for_kids": True,
             }):
            for job in jobs:
                self.service.publish_job(job.job_id, platform="youtube")

        declarations = [call.kwargs["youtube_extra"]["selfDeclaredMadeForKids"]
                        for call in uploader.upload_video.call_args_list]
        self.assertEqual(declarations, [True, False])

    def test_youtube_and_instagram_publications_are_independent_destinations(self):
        from unittest.mock import MagicMock
        job = self.ready_job(topic="Publish separately to YouTube and Instagram")
        self.service.store.queue(job.job_id)
        self.service.store.claim(job.job_id, "task", f"{autopilot.socket.gethostname()}:{os.getpid()}")
        self.service.store.transition(job.job_id, "generated", output_artifacts_json='["/tmp/video.mp4"]', actual_duration_seconds=28)
        with self.service.store._connection() as conn:
            conn.execute("INSERT INTO job_qa VALUES (?, 'approved', '{}', 'operator', ?, ?)",
                         (job.job_id, autopilot._now(), autopilot._now()))
        uploader = MagicMock()
        uploader.connected_platform.side_effect = lambda platform, *_args: {
            "instagram": {"username": "ig-account-42", "handle": "mpt.reels"},
            "youtube": {"username": "yt-account-12", "handle": "MPT Shorts"},
        }.get(platform)
        uploader.upload_video.side_effect = [
            {"success": True, "request_id": "instagram-request",
             "results": {"instagram": {"success": True, "publish_id": "ig-123",
                                          "url": "https://instagram.com/reel/ig-123/"}}},
            {"success": True, "request_id": "youtube-request",
             "results": {"youtube": {"success": True, "video_id": "yt-123",
                                       "url": "https://youtube.test/yt-123"}}},
        ]
        with patch.object(self.service, "qa_report", return_value={"passed": True, "checks": {}}), \
             patch.object(self.service, "_validate_instagram_video"), \
             patch.object(autopilot, "UploadPostService", return_value=uploader), \
             patch.object(autopilot.config, "app", {
                 "upload_post_api_key": "local-secret", "upload_post_enabled": True,
                 "upload_post_username": "account-a",
             }):
            instagram = self.service.publish_job(job.job_id, platform="instagram")
            youtube = self.service.publish_job(job.job_id, platform="youtube")
            repeat = self.service.publish_job(job.job_id, platform="instagram")

        self.assertEqual(instagram["state"], "published")
        self.assertEqual(youtube["state"], "published")
        self.assertEqual(repeat["state"], "published")
        self.assertEqual(uploader.upload_video.call_count, 2)
        with self.service.store._connection() as conn:
            records = conn.execute(
                "SELECT platform,destination_account_id,provider_video_id,provider_url FROM publications WHERE job_id=?",
                (job.job_id,),
            ).fetchall()
        self.assertEqual({(row["platform"], row["destination_account_id"]) for row in records},
                         {("instagram", "ig-account-42"), ("youtube", "yt-account-12")})
        self.assertIn(("ig-123", "https://instagram.com/reel/ig-123/"),
                      {(row["provider_video_id"], row["provider_url"]) for row in records})

    def test_uncertain_publication_is_reconciled_before_any_retry(self):
        from unittest.mock import MagicMock
        job = self.ready_job(topic="Reconcile before retry")
        self.service.store.queue(job.job_id)
        self.service.store.claim(job.job_id, "task", f"{autopilot.socket.gethostname()}:{os.getpid()}")
        self.service.store.transition(job.job_id, "generated", output_artifacts_json='["/tmp/video.mp4"]', actual_duration_seconds=28)
        with self.service.store._connection() as conn:
            conn.execute("INSERT INTO job_qa VALUES (?, 'approved', '{}', 'operator', ?, ?)", (job.job_id, autopilot._now(), autopilot._now()))
            conn.execute("INSERT INTO publications (publication_id,job_id,channel_id,profile_version,account_username,idempotency_key,provider_request_id,state,attempt_count,created_at,updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'unknown', 1, ?, ?)",
                         ("pub", job.job_id, job.channel_id, job.profile_version, "account-a", "stable", "stable", autopilot._now(), autopilot._now()))
        uploader = MagicMock()
        uploader.check_status.return_value = {"status": "processing"}
        with patch.object(self.service, "qa_report", return_value={"passed": True, "checks": {}}), \
             patch.object(autopilot, "UploadPostService", return_value=uploader), \
             patch.object(autopilot.config, "app", {"upload_post_api_key": "local-secret",
                                                       "upload_post_enabled": True}):
            uploader.connected_platform.return_value = {"username": "yt-account-1"}
            with self.assertRaisesRegex(autopilot.AutopilotError, "no retry was submitted"):
                self.service.publish_job(job.job_id)
        uploader.upload_video.assert_not_called()
        uploader.check_status.assert_called_once_with("stable")

    def test_scheduled_publication_uses_pinned_local_time_and_profile_timezone(self):
        from datetime import datetime, timezone
        from unittest.mock import MagicMock
        profile = self.profiles[0]
        profile = self.service.profile_store.update(profile.channel_id,
            profile.model_copy(update={"publishing_policy": "scheduled_public", "auto_public_enabled": True,
                                       "timezone": "Asia/Kolkata"}), profile.version)
        plan = self.service.plan(profile.channel_id, ContentPlanCreate(
            topic="Scheduled public fact", script=self.hindi_script(),
            intended_publication_slot=datetime(2035, 1, 1, 9, 0, tzinfo=timezone.utc),
            min_duration_seconds=25, max_duration_seconds=30,
        ))
        ready = self.service.ready(plan.channel_id, plan.plan_id, True)
        job = self.service.create_job(plan.channel_id, ready.plan_id)
        self.service.store.queue(job.job_id)
        self.service.store.claim(job.job_id, "task", f"{autopilot.socket.gethostname()}:{os.getpid()}")
        self.service.store.transition(job.job_id, "generated", output_artifacts_json='["/tmp/video.mp4"]', actual_duration_seconds=28)
        with self.service.store._connection() as conn:
            conn.execute("INSERT INTO job_qa VALUES (?, 'approved', '{}', 'operator', ?, ?)", (job.job_id, autopilot._now(), autopilot._now()))
        from app.models.autopilot import ContentQualityReview
        self.service.store.record_content_review(job.job_id, "operator", ContentQualityReview(
            originality_status="original", rights_status="documented",
            notes="Narration adds original explanation and visuals have reviewed license evidence.",
            rights_evidence=[{"asset_key": "fixture.mp4", "evidence": "https://example.org/license"}]))
        uploader = MagicMock()
        uploader.upload_video.return_value = {"success": True, "request_id": "provider-request", "job_id": "scheduled-provider-job", "results": {"youtube": {"success": True}}}
        with patch.object(self.service, "qa_report", return_value={"passed": True, "technical_checks_passed": True,
                                                                    "repetition_warnings": [], "checks": {}}), \
             patch.object(autopilot, "UploadPostService", return_value=uploader), \
             patch.object(autopilot.config, "app", {"upload_post_api_key": "local-secret",
                                                       "upload_post_enabled": True}), \
             patch.object(autopilot.sm.state, "get_task", return_value={
                 "fact_verification_state": "source_verified",
                 "fact_verification_script_hash": hashlib.sha256(job.script.encode("utf-8")).hexdigest(),
             }):
            uploader.connected_platform.return_value = {"username": "yt-account-1", "handle": "MPT Shorts"}
            result = self.service.publish_job(job.job_id)
        self.assertEqual(result["state"], "scheduled")
        kwargs = uploader.upload_video.call_args.kwargs
        self.assertEqual(kwargs["scheduled_date"], "2035-01-01T14:30:00")
        self.assertEqual(kwargs["timezone_name"], "Asia/Kolkata")
        self.assertEqual(kwargs["external_id"], job.job_id)

    def test_direct_publisher_persists_pinned_destination_and_never_reuploads_receipt(self):
        from cryptography.fernet import Fernet
        from app.services.youtube_direct import OAUTH_SCOPES, YouTubeDirectPublisher
        profile = self.profiles[0]
        with patch.dict(os.environ, {"MPT_YOUTUBE_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
            connection_id = self.service.youtube_connections.save_connection(
                profile.channel_id, {"id": "UC-direct", "snippet": {"title": "Direct channel"}},
                "refresh-token", list(OAUTH_SCOPES))
            plan = self.plan(profile, "Direct publishing fact")
            ready = self.service.ready(plan.channel_id, plan.plan_id)
            job = self.service.create_job(profile.channel_id, ready.plan_id)
            pinned = dict(job.profile_snapshot)
            pinned.update({"publishing_backend": "youtube_direct", "youtube_connection_id": connection_id,
                           "youtube_channel_id": "UC-direct", "publishing_policy": "private"})
            artifact = Path(self.temp.name) / "private-short.mp4"
            artifact.write_bytes(b"video")
            self.service.store.queue(job.job_id)
            self.service.store.claim(job.job_id, "task", f"{autopilot.socket.gethostname()}:{os.getpid()}")
            self.service.store.transition(job.job_id, "generated",
                output_artifacts_json=__import__("json").dumps([str(artifact)]), actual_duration_seconds=28)
            with self.service.store._connection() as conn:
                conn.execute("UPDATE production_jobs SET profile_snapshot_json=? WHERE job_id=?",
                             (__import__("json").dumps(pinned), job.job_id))
            with self.service.store._connection() as conn:
                conn.execute("INSERT INTO job_qa VALUES (?, 'approved', '{}', 'operator', ?, ?)",
                             (job.job_id, autopilot._now(), autopilot._now()))
            publisher = YouTubeDirectPublisher(self.service.youtube_connections)
            receipt = {"id": "youtube-video", "snippet": {"channelId": "UC-direct"},
                       "status": {"privacyStatus": "private"}}

            def upload(**kwargs):
                kwargs["on_session"]("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=fake")
                return receipt

            with patch.object(self.service, "qa_report", return_value={"passed": True}), \
                 patch.object(publisher, "publish", side_effect=upload) as send:
                self.service.youtube_publisher = publisher
                first = self.service.publish_job(job.job_id)
                second = self.service.publish_job(job.job_id)
            self.assertEqual(first["video_id"], "youtube-video")
            self.assertEqual(second["state"], "published")
            self.assertEqual(send.call_count, 1)
            with self.service.store._connection() as conn:
                row = conn.execute("SELECT * FROM publications WHERE job_id=?", (job.job_id,)).fetchone()
            self.assertEqual(row["publisher_backend"], "youtube_direct")
            self.assertEqual(row["youtube_channel_id"], "UC-direct")
            stored = bytes(row["resumable_session_url"])
            self.assertNotIn(b"upload_id=fake", stored)
            session, legacy, unverified = self.service.youtube_connections.decode_session(
                stored, send.call_args.kwargs["session_context"])
            self.assertEqual(session, "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=fake")
            self.assertFalse(legacy)
            self.assertFalse(unverified)
            with self.service.store._connection() as conn:
                conn.execute("UPDATE publications SET resumable_session_url=? WHERE job_id=?",
                             ("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=legacy", job.job_id))
                legacy_row = conn.execute("SELECT * FROM publications WHERE job_id=?", (job.job_id,)).fetchone()
            legacy_context = send.call_args.kwargs["session_context"]
            legacy_url, legacy_unverified = self.service._load_direct_session(
                legacy_row, job, connection_id, "UC-direct", str(artifact), legacy_context)
            self.assertTrue(legacy_unverified)
            with self.service.store._connection() as conn:
                migrated_row = conn.execute("SELECT resumable_session_url FROM publications WHERE job_id=?", (job.job_id,)).fetchone()
            migrated_url, was_legacy, still_unverified = self.service.youtube_connections.decode_session(
                migrated_row["resumable_session_url"], legacy_context)
            self.assertEqual(legacy_url, migrated_url)
            self.assertFalse(was_legacy)
            self.assertTrue(still_unverified)

    def test_direct_uncertain_upload_without_session_is_never_blindly_retried(self):
        profile = self.profiles[0]
        plan = self.plan(profile, "Uncertain direct upload")
        ready = self.service.ready(plan.channel_id, plan.plan_id)
        job = self.service.create_job(profile.channel_id, ready.plan_id)
        pinned = dict(job.profile_snapshot)
        pinned.update({"publishing_backend": "youtube_direct", "youtube_connection_id": "old-connection",
                       "youtube_channel_id": "UC-pinned", "publishing_policy": "private"})
        artifact = Path(self.temp.name) / "uncertain-short.mp4"
        artifact.write_bytes(b"video")
        self.service.store.queue(job.job_id)
        self.service.store.claim(job.job_id, "task", f"{autopilot.socket.gethostname()}:{os.getpid()}")
        self.service.store.transition(job.job_id, "generated", output_artifacts_json=__import__("json").dumps([str(artifact)]),
                                      actual_duration_seconds=28)
        with self.service.store._connection() as conn:
            conn.execute("UPDATE production_jobs SET profile_snapshot_json=? WHERE job_id=?",
                         (__import__("json").dumps(pinned), job.job_id))
            conn.execute("INSERT INTO job_qa VALUES (?, 'approved', '{}', 'operator', ?, ?)",
                         (job.job_id, autopilot._now(), autopilot._now()))
            conn.execute("INSERT INTO publications (publication_id,job_id,channel_id,profile_version,account_username,idempotency_key,provider_request_id,state,attempt_count,created_at,updated_at,publisher_backend,youtube_connection_id,youtube_channel_id) VALUES (?, ?, ?, ?, NULL, ?, ?, 'unknown', 1, ?, ?, 'youtube_direct', ?, ?)",
                         ("uncertain-direct", job.job_id, job.channel_id, job.profile_version, "stable", "stable",
                          autopilot._now(), autopilot._now(), "old-connection", "UC-pinned"))
        with patch.object(self.service, "qa_report", return_value={"passed": True}), \
             patch.object(self.service.youtube_connections, "get_connection", return_value={
                 "connection_id": "old-connection", "local_channel_id": job.channel_id,
                 "youtube_channel_id": "UC-pinned"}), \
             patch.object(self.service.youtube_publisher, "publish") as upload:
            with self.assertRaisesRegex(autopilot.AutopilotError, "no resumable session"):
                self.service.publish_job(job.job_id)
        upload.assert_not_called()

    def test_documented_rights_review_must_map_evidence_to_each_asset_key(self):
        from app.models.autopilot import ContentQualityReview
        job = self.ready_job(topic="Per-asset rights evidence")
        self.service.store.queue(job.job_id)
        self.service.store.claim(job.job_id, "task", f"{autopilot.socket.gethostname()}:{os.getpid()}")
        self.service.store.transition(job.job_id, "generated", output_artifacts_json='["mock-video.mp4"]',
                                      actual_duration_seconds=28)
        review = ContentQualityReview(
            originality_status="original", rights_status="documented",
            notes="I checked the license terms for each source asset.",
            rights_evidence=[{"asset_key": "asset-1", "evidence": "https://example.org/license/1"}],
        )
        with patch.object(self.service, "qa_report", return_value={"technical_checks_passed": True}), \
             patch.object(self.service, "_material_sources_for_job", return_value=[
                 {"asset_id": "asset-1"}, {"asset_id": "asset-2"}]), \
             self.assertRaisesRegex(autopilot.AutopilotError, "every retained video asset"):
            self.service.review_content_quality(job.job_id, "operator", review)


if __name__ == "__main__":
    unittest.main()
