import unittest

from app.services import visual_style


class TestVisualStyle(unittest.TestCase):
    def test_explicit_animated_explainer_is_local_first(self):
        mode, reason = visual_style.initial_selection(
            visual_style.SAVAGE_ANIMATED_EXPLAINER,
            "ISS और पृथ्वी दोनों लगातार गिर रहे हैं।",
            "ISS में गुरुत्वाकर्षण",
        )
        self.assertEqual(mode, visual_style.SAVAGE_ANIMATED_EXPLAINER)
        self.assertIn("local no-stock fallback", reason)

    def test_auto_keeps_unknown_and_abstract_topics_on_local_explainer(self):
        examples = (
            ("अज्ञात सिद्धांत का असर बाद में समझ आता है।", "क्वांटम प्रभाव"),
            ("तितलियाँ धूप में पंख फैलाकर ताप लेती हैं।", "Butterfly thermoregulation"),
            ("चार्ट में अर्थव्यवस्था की दिशा दिखती है।", "Economic direction"),
        )
        for script, topic in examples:
            with self.subTest(topic=topic):
                mode, reason = visual_style.initial_selection(visual_style.AUTO, script, topic)
                self.assertEqual(mode, visual_style.SAVAGE_ANIMATED_EXPLAINER)
                self.assertIn("no semantic stock-fit evidence", reason)

    def test_auto_rejects_low_quality_jelly_and_reports_asmr_fallback(self):
        # Ordinary jelly metadata is not an approved ASMR review; no coverage means
        # AUTO must fail rather than treating it as an eligible alternate.
        with self.assertRaisesRegex(ValueError, "No suitable footage passed"):
            visual_style.decide_after_preflight(
                visual_style.AUTO, visual_style.RELEVANT_STOCK,
                relevant_coverage=0, asmr_coverage=0, required_coverage=25,
                preferred_failure="No butterfly stock passed relevance review.",
            )
        mode, reason = visual_style.decide_after_preflight(
            visual_style.AUTO, visual_style.RELEVANT_STOCK,
            relevant_coverage=0, asmr_coverage=30, required_coverage=25,
            preferred_failure="No butterfly stock passed relevance review.",
        )
        self.assertEqual(mode, visual_style.ASMR_CUTTING)
        self.assertIn("fell back", reason)
        self.assertIn("No butterfly stock passed relevance review", reason)

    def test_explicit_modes_never_substitute_the_other_mode(self):
        with self.assertRaisesRegex(ValueError, "Relevant Stock coverage is insufficient"):
            visual_style.decide_after_preflight(
                visual_style.RELEVANT_STOCK, visual_style.RELEVANT_STOCK,
                relevant_coverage=0, asmr_coverage=40, required_coverage=25,
            )
        with self.assertRaisesRegex(ValueError, "ASMR Cutting coverage is insufficient"):
            visual_style.decide_after_preflight(
                visual_style.ASMR_CUTTING, visual_style.ASMR_CUTTING,
                relevant_coverage=40, asmr_coverage=0, required_coverage=25,
            )


if __name__ == "__main__":
    unittest.main()
