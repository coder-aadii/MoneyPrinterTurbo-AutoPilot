import json
import re
import types as pytypes
import unittest
from unittest.mock import MagicMock, Mock, patch

from app.services import source_research


class TestSourceResearch(unittest.TestCase):
    def setUp(self):
        for target, replacement in (
            ("_cache_read", Mock(return_value=None)),
            ("_cache_write", Mock()),
            ("_robots_permission", Mock(return_value=(True, 0.0))),
            ("_wait_for_search_slot", Mock()),
            ("_wait_for_fetch_slot", Mock()),
        ):
            patcher = patch.object(source_research, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _response(*, body=b"", content_type="text/html; charset=utf-8", url=""):
        response = MagicMock()
        response.status_code = 200
        response.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        response.content = body
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.iter_content.return_value = [body]
        response.encoding = "utf-8"
        response.url = url
        response.raise_for_status.return_value = None
        return response

    @patch.dict("os.environ", {"MPT_FACT_RESEARCH_API_KEY": "mock-key"})
    def test_bounded_retrieval_keeps_only_trusted_candidates_and_requires_human_decision(self):
        search = self._response(content_type="application/json", url="https://api.search.brave.com")
        search.json.return_value = {"web": {"results": [
            {"title": "Trusted page", "url": "https://science.example.org/fact", "description": "The source describes the claim."},
            {"title": "Independent page", "url": "https://lab.example.net/fact", "description": "Second source."},
        ]}}
        page = self._response(
            body=(b"<html><body><nav>Do not use navigation text</nav><article><p>"
                  b"The source page itself describes the complete supported fact in substantial detail. "
                  b"This visible paragraph is long enough to serve as retrieved source evidence."
                  b"</p><script>hidden false evidence</script></article></body></html>"),
            url="https://science.example.org/fact",
        )
        page2 = self._response(
            body=(b"<article><p>An independent source page contains corroborating details about the described "
                  b"supported fact, in visible body text for source verification.</p></article>"),
            url="https://lab.example.net/fact",
        )
        with patch.object(source_research.config, "app", {
            "fact_research_provider": "brave_search",
            "fact_research_trusted_domains": ["example.org", "example.net"],
            "fact_research_max_claims": 2,
        }), patch.object(source_research, "_robots_permission", return_value=(True, 0.0)), \
                patch.object(source_research.requests, "get", side_effect=[search, page, page2]) as get:
            report = source_research.research_script("A supported fact is described by the source.")
        self.assertEqual(len(report["claims"]), 1)
        claim = report["claims"][0]
        self.assertEqual([item["url"] for item in claim["evidence"]], [
            "https://science.example.org/fact", "https://lab.example.net/fact",
        ])
        self.assertIn("complete supported fact", claim["evidence"][0]["excerpt"])
        self.assertNotIn("hidden false evidence", claim["evidence"][0]["excerpt"])
        self.assertEqual(claim["evidence"][0]["source_type"], "retrieved_page_content")
        self.assertEqual(claim["review_status"], "human_review_required")
        self.assertEqual(get.call_count, 3)
        reviewed = source_research.review_claims(report, [{
            "claim_id": claim["claim_id"], "status": "verified",
            "evidence_ids": [claim["evidence"][0]["evidence_id"]],
            "notes": "I opened this source and confirmed it supports the complete narration claim.",
        }])
        self.assertEqual(reviewed["state"], "human_verified")

    def test_unconfigured_research_is_unavailable_not_machine_verified(self):
        with patch.object(source_research.config, "app", {"fact_research_provider": "invalid"}), \
                patch.dict("os.environ", {}, clear=True):
            report = source_research.research_script("This is a factual sentence that needs review.")
        self.assertEqual(report["state"], "unavailable")
        self.assertEqual(report["claims"][0]["review_status"], "human_review_required")
        self.assertEqual(report["claims"][0]["status"], "unverified")

    def test_source_fetch_preserves_trailing_slash_redirect_target(self):
        redirect = self._response(url="https://science.example.org/fact")
        redirect.status_code = 301
        redirect.headers["Location"] = "/fact/"
        body = self._response(
            body=("<article><p>This page body contains enough visible factual source text "
                  "to be extracted after following the canonical trailing slash redirect safely.</p></article>").encode(),
            url="https://science.example.org/fact/",
        )
        with patch.object(source_research.requests, "get", side_effect=[redirect, body]) as get:
            url, excerpt = source_research._fetch_trusted_page(
                "https://science.example.org/fact", ("example.org",),
            )
        self.assertEqual(url, "https://science.example.org/fact")
        self.assertIn("page body contains enough visible factual source text", excerpt)
        self.assertEqual([call.args[0] for call in get.call_args_list], [
            "https://science.example.org/fact", "https://science.example.org/fact/",
        ])

    def test_claims_cannot_be_automatically_verified_without_evidence_or_review_notes(self):
        report = {"claims": [{"claim_id": "claim-1", "claim": "Claim", "evidence": []}]}
        with self.assertRaisesRegex(ValueError, "require selected retrieved evidence"):
            source_research.review_claims(report, [{
                "claim_id": "claim-1", "status": "verified", "notes": "Looks right to me."
            }])

    def test_source_verifier_requires_two_trusted_domains_and_cited_evidence(self):
        with patch.object(source_research.config, "app", {
            "fact_research_trusted_domains": ["example.org", "example.net"],
        }):
            report = {
                "provider": "brave_search", "state": "needs_human_review", "claims": [{
                    "claim_id": "claim-1", "claim": "A supported claim.",
                    "status": "candidate_evidence", "evidence": [
                        {"evidence_id": "source-1", "url": "https://science.example.org/a",
                         "source_type": "retrieved_page_content", "excerpt": "The actual visible source page text. " * 8},
                        {"evidence_id": "source-2", "url": "https://lab.example.net/b",
                         "source_type": "retrieved_page_content", "excerpt": "Independent visible source page text. " * 8},
                        {"evidence_id": "source-3", "url": "https://other.example.org/c",
                         "source_type": "retrieved_page_content", "excerpt": "Another page from the same publisher root. " * 8},
                    ],
                }],
            }
            unsupported = source_research.verify_report(
                report,
                verifier=lambda _prompt: '{"results":[{"claim_id":"claim-1",'
                '"verdict":"supported","evidence_ids":["source-1"],"rationale":"One source"}]}',
            )
            self.assertEqual(unsupported["state"], "unsupported")

            one_domain = source_research.verify_report(
                report,
                verifier=lambda _prompt: '{"results":[{"claim_id":"claim-1",'
                '"verdict":"supported","evidence_ids":["source-1","source-3"],'
                '"rationale":"These pages share one publisher root."}]}',
            )
            self.assertEqual(one_domain["state"], "unsupported")

            verified = source_research.verify_report(
                report,
                verifier=lambda _prompt: '{"results":[{"claim_id":"claim-1",'
                '"verdict":"supported","evidence_ids":["source-1","source-2"],'
                '"rationale":"Both independent excerpts support the claim."}]}',
            )
        self.assertEqual(verified["state"], "source_verified")
        self.assertEqual(verified["claims"][0]["verified_evidence_ids"], ["source-1", "source-2"])

    def test_search_snippets_or_untrusted_model_citations_cannot_verify_a_claim(self):
        report = {
            "provider": "brave_search", "state": "needs_human_review", "claims": [{
                "claim_id": "claim-1", "claim": "Siberian tit song predicts tomorrow's weather.",
                "status": "candidate_evidence", "evidence": [
                    {"evidence_id": "snippet-1", "url": "https://science.example.org/a",
                     "excerpt": "Search snippet that appears to support the claim. " * 3},
                    {"evidence_id": "snippet-2", "url": "https://facts.example.net/b",
                     "excerpt": "Another search result description, not fetched page text. " * 3},
                ],
            }],
        }
        verifier = Mock(return_value='{"results":[{"claim_id":"claim-1",'
                                    '"verdict":"supported","evidence_ids":["snippet-1","snippet-2"]}]}')
        with patch.object(source_research.config, "app", {
            "fact_research_trusted_domains": ["example.org", "example.net"],
        }):
            result = source_research.verify_report(report, verifier=verifier)
        self.assertEqual(result["state"], "unsupported")
        verifier.assert_not_called()

    @patch.dict("os.environ", {"MPT_FACT_RESEARCH_API_KEY": "mock-key"})
    def test_nasa_venus_fact_uses_fetched_page_bodies_and_passes_automatically(self):
        search = self._response(content_type="application/json", url="https://api.search.brave.com")
        search.json.return_value = {"web": {"results": [
            {"title": "NASA Venus Facts", "url": "https://science.nasa.gov/venus/venus-facts/",
             "description": "Misleading search snippet that omits the day/year comparison."},
            {"title": "Venus Facts", "url": "https://www.britannica.com/place/Venus-planet",
             "description": "A search snippet that is not used for verification."},
        ]}}
        nasa_body = ("<html><body><nav>Navigation and unrelated menu</nav><article><h1>Venus</h1><p>"
                     "A day on Venus is 243 Earth days long, longer than its year, which takes 225 Earth days. "
                     "This is due to the planet's very slow rotation. NASA article index 7, unrelated value 1990.</p>"
                     "<p>Other unrelated mission count: 12.</p></article></body></html>").encode()
        britannica_body = ("<html><body><article><p>Venus rotates once every 243 Earth days. "
                           "It revolves around the Sun every 225 Earth days, making its rotation period "
                           "longer than its orbital period. Other unrelated facts include 96 percent and 2.</p>"
                           "</article></body></html>").encode()
        pages = [
            self._response(body=nasa_body, url="https://science.nasa.gov/venus/venus-facts/"),
            self._response(body=britannica_body, url="https://www.britannica.com/place/Venus-planet"),
        ]
        claim = "Venus takes longer to rotate once than to orbit the Sun."
        captured = {}

        def verify(prompt):
            captured["prompt"] = prompt
            evidence_ids = re.findall(r'"evidence_id": "([^"]+)"', prompt)
            return json.dumps({"results": [{
                "claim_id": source_research._unsupported(claim)["claim_id"],
                "verdict": "supported", "evidence_ids": evidence_ids,
                "rationale": "Both fetched page bodies state rotation exceeds orbital period.",
            }]})

        with patch.object(source_research.config, "app", {
            "fact_research_provider": "brave_search",
            "fact_research_trusted_domains": ["nasa.gov", "britannica.com"],
            "fact_research_max_claims": 2,
        }), patch.object(source_research, "_robots_permission", return_value=(True, 0.0)), \
                patch.object(source_research.requests, "get", side_effect=[search, *pages]) as get:
            result = source_research.verify_script(claim, verifier=verify)
        self.assertEqual(result["state"], "source_verified")
        self.assertEqual(get.call_count, 3)
        self.assertIn("243 Earth days long", captured["prompt"])
        self.assertIn("revolves around the Sun every 225 Earth days", captured["prompt"])
        self.assertEqual(result["claims"][0]["status"], "source_supported")
        self.assertNotIn("Misleading search snippet", captured["prompt"])
        self.assertNotIn("search snippet that is not used", captured["prompt"])

    @patch.dict("os.environ", {"MPT_FACT_RESEARCH_API_KEY": "mock-key"})
    def test_bird_weather_claim_is_rejected_from_source_page_content(self):
        search = self._response(content_type="application/json", url="https://api.search.brave.com")
        search.json.return_value = {"web": {"results": [
            {"title": "Birdsong study", "url": "https://www.science.org/doi/bird-song",
             "description": "This search snippet falsely says Siberian tits predict tomorrow's weather."},
            {"title": "Bird calls", "url": "https://www.nature.com/articles/bird-calls",
             "description": "Another snippet repeats the unsupported weather claim."},
        ]}}
        pages = [
            self._response(body=("<article><p>Researchers examined how rainfall and seasonal conditions "
                                 "can correlate with song variation in several bird species. The article "
                                 "does not report Siberian tits forecasting the next day's weather.</p></article>").encode(),
                           url="https://www.science.org/doi/bird-song"),
            self._response(body=("<article><p>Bird calls can vary by species, context, and local conditions. "
                                 "This source contains no finding that Siberian tit song predicts tomorrow's "
                                 "weather.</p></article>").encode(),
                           url="https://www.nature.com/articles/bird-calls"),
        ]
        claim = (
            "साइबेरियन टिट के गीत में छिपे पैटर्न वैज्ञानिकों ने पकड़े हैं – दबाव गिरने पर "
            "वह तेज़ और ऊंचे स्वर में गाती है, दबाव बढ़ने पर धीमे और गहरे स्वर में।"
        )
        seen = {}

        def verifier(prompt):
            seen["prompt"] = prompt
            return json.dumps({"results": [{
                "claim_id": source_research._unsupported(claim)["claim_id"],
                "verdict": "insufficient", "evidence_ids": [],
                "rationale": "The retrieved article bodies discuss other song correlations, not forecasts.",
            }]})

        with patch.object(source_research.config, "app", {
            "fact_research_provider": "brave_search",
            "fact_research_trusted_domains": ["science.org", "nature.com"],
        }), patch.object(source_research, "_robots_permission", return_value=(True, 0.0)), \
                patch.object(source_research.requests, "get", side_effect=[search, *pages]):
            result = source_research.verify_script(claim, verifier=verifier)
        self.assertEqual(result["state"], "unsupported")
        self.assertIn("does not report Siberian tits", seen["prompt"])
        self.assertNotIn("falsely says Siberian tits", seen["prompt"])

    def test_unsupported_bird_weather_topic_without_retrieved_evidence_fails_closed(self):
        report = {
            "provider": "brave_search", "state": "needs_human_review", "claims": [{
                "claim_id": "claim-bird-weather", "claim": "Siberian tit song predicts tomorrow's weather.",
                "status": "unsupported", "evidence": [],
            }],
        }
        result = source_research.verify_report(report)
        self.assertEqual(result["state"], "unsupported")

    def test_gemini_grounding_uses_only_google_grounding_metadata_urls(self):
        response = pytypes.SimpleNamespace(candidates=[pytypes.SimpleNamespace(
            grounding_metadata=pytypes.SimpleNamespace(grounding_chunks=[
                pytypes.SimpleNamespace(web=pytypes.SimpleNamespace(
                    uri="https://science.nasa.gov/venus/venus-facts/", title="NASA Venus Facts")),
                pytypes.SimpleNamespace(web=pytypes.SimpleNamespace(
                    uri="https://science.nasa.gov/venus/venus-facts/", title="Duplicate")),
            ])
        )], text="Untrusted model-authored fake source https://fake.invalid/source")
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.models.generate_content.return_value = response
        with patch.object(source_research.config, "app", {
            "gemini_api_key": "test-key", "fact_research_gemini_model": "gemini-2.5-flash",
        }), patch.object(source_research, "_cache_read", return_value=0), \
                patch.object(source_research, "_cache_write") as cache_write, \
                patch("google.genai.Client", return_value=client) as client_factory:
            results = source_research._search_gemini_grounding("A day on Venus exceeds its year.")
        self.assertEqual([item["url"] for item in results], ["https://science.nasa.gov/venus/venus-facts/"])
        self.assertEqual(results[0]["provider"], "gemini_search_grounding")
        self.assertNotIn("fake.invalid", json.dumps(results))
        client_factory.assert_called_once_with(api_key="test-key")
        request = client.models.generate_content.call_args.kwargs
        self.assertEqual(request["model"], "gemini-2.5-flash")
        self.assertTrue(request["config"].tools[0].google_search is not None)
        cache_write.assert_called_once()

    def test_gemini_grounding_urls_flow_through_existing_page_body_verification_input(self):
        rows = [
            source_research._normalize_search_result(
                {"url": "https://science.nasa.gov/venus/venus-facts/", "title": "NASA"},
                "gemini_search_grounding", 1),
            source_research._normalize_search_result(
                {"url": "https://www.esa.int/Science_Exploration/Space_Science/Venus", "title": "ESA"},
                "gemini_search_grounding", 2),
        ]
        pages = [
            self._response(body=("<article><p>NASA states a Venus day lasts 243 Earth days and its year lasts "
                                 "225 Earth days, so its rotation takes longer than its solar orbit.</p></article>").encode(),
                           url=rows[0]["url"]),
            self._response(body=("<article><p>ESA independently states that a Venus rotation is 243 Earth days "
                                 "while the planet orbits the Sun every 225 Earth days.</p></article>").encode(),
                           url=rows[1]["url"]),
        ]
        captured = {}

        def verifier(prompt):
            captured["prompt"] = prompt
            evidence_ids = re.findall(r'"evidence_id": "([^"]+)"', prompt)
            return json.dumps({"results": [{
                "claim_id": source_research._unsupported(
                    "Venus takes longer to rotate once than to orbit the Sun."
                )["claim_id"],
                "verdict": "supported", "evidence_ids": evidence_ids,
                "rationale": "Both fetched agency page bodies state 243 versus 225 Earth days.",
            }]})

        with patch.object(source_research.config, "app", {
            "fact_research_provider": "gemini_search_grounding",
            "gemini_api_key": "test-key",
            "fact_research_trusted_domains": ["nasa.gov", "esa.int"],
        }), patch.object(source_research, "_search_provider", return_value=(rows, False)), \
                patch.object(source_research.requests, "get", side_effect=pages), \
                patch.object(source_research, "_robots_permission", return_value=(True, 0.0)):
            result = source_research.verify_script(
                "Venus takes longer to rotate once than to orbit the Sun.", verifier=verifier)
        self.assertEqual(result["state"], "source_verified")
        self.assertIn("NASA states a Venus day lasts 243", captured["prompt"])
        self.assertIn("ESA independently states", captured["prompt"])
        self.assertNotIn("search_snippet", captured["prompt"])

    def test_gemini_grounding_daily_cap_blocks_before_request(self):
        with patch.object(source_research.config, "app", {"gemini_api_key": "test-key"}), \
                patch.object(source_research, "_cache_read", return_value=20), \
                patch("google.genai.Client") as client:
            with self.assertRaisesRegex(RuntimeError, "daily safety limit"):
                source_research._search_gemini_grounding("Any factual claim")
        client.assert_not_called()

    def test_gemini_selection_gets_free_ddgs_fallback_without_explicit_fallback(self):
        with patch.object(source_research.config, "app", {
            "fact_research_provider": "gemini_search_grounding",
            "gemini_api_key": "test-key",
        }):
            primary, providers, _endpoint = source_research._research_provider_settings()
        self.assertEqual(primary, "gemini_search_grounding")
        self.assertEqual(providers, ["gemini_search_grounding", "ddgs"])

    def test_search_result_normalization_preserves_fetch_slash_and_dedupes_by_canonical_url(self):
        rows = source_research._dedupe_search_results([
            source_research._normalize_search_result(
                {"url": "https://science.nasa.gov/venus/venus-facts/?utm_source=search"}, "ddgs", 1),
            source_research._normalize_search_result(
                {"url": "https://science.nasa.gov/venus/venus-facts"}, "ddgs", 2),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://science.nasa.gov/venus/venus-facts/")

    def test_search_claim_adds_one_primary_source_query_when_independent_domain_missing(self):
        nasa = source_research._normalize_search_result(
            {"url": "https://science.nasa.gov/venus/venus-facts/", "title": "NASA"}, "ddgs", 1)
        esa = source_research._normalize_search_result(
            {"url": "https://www.esa.int/Science_Exploration/Space_Science/Venus", "title": "ESA"}, "ddgs", 1)
        with patch.object(source_research, "_search_provider", side_effect=[([nasa], False), ([esa], False)]) as search:
            rows, attempts = source_research._search_claim("Venus rotates slower than it orbits.", "ddgs", ["ddgs"], "")
        self.assertEqual({row["source_domain"] for row in rows}, {"science.nasa.gov", "www.esa.int"})
        self.assertEqual([item["query_variant"] for item in attempts], ["claim", "primary_sources"])
        self.assertIn("site:esa.int", search.call_args_list[1].args[0])

    def test_research_timeout_returns_unavailable_without_retrying_remaining_claims(self):
        with patch.object(source_research, "research_health", return_value={"ok": True}), \
                patch.object(source_research, "_search_claim",
                             side_effect=source_research.ResearchTimeout("budget expired")) as search:
            report = source_research.research_script(
                "First factual sentence. Second factual sentence.", timeout_seconds=0.2,
            )
        self.assertEqual(report["state"], "unavailable")
        self.assertTrue(report["timed_out"])
        self.assertEqual(search.call_count, 1)
        self.assertTrue(all(item["status"] in {"unavailable", "not_researched"}
                            for item in report["claims"]))

    def test_ddgs_uses_search_engines_and_skips_non_search_wiki_backends(self):
        with patch("ddgs.DDGS") as ddgs:
            ddgs.return_value.__enter__.return_value.text.return_value = [{
                "title": "NASA", "href": "https://science.nasa.gov/venus/venus-facts/", "body": "snippet",
            }]
            rows = source_research._search_ddgs("Venus rotation period")
        self.assertEqual(rows[0]["source_domain"], "science.nasa.gov")
        self.assertEqual(rows[0]["provider"], "ddgs")
        kwargs = ddgs.return_value.__enter__.return_value.text.call_args.kwargs
        self.assertEqual(kwargs["backend"], "duckduckgo,brave,startpage,mojeek,yahoo")


if __name__ == "__main__":
    unittest.main()
