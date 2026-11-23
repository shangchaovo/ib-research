"""Publication invariants: attribution, durability, dates and public field boundaries."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ib_research_sources import (
    ARCHIVE_FILENAME,
    event_id,
    get_public_events,
    load_public_archive,
    project_events,
    public_report,
    safe_source_url,
    source_label,
    sync_public_archive,
)


NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def change(**overrides):
    result = {
        "Bank": "Morgan Stanley", "Symbol": "NVDA", "Action": "upgrade",
        "Time": "2026-11-20", "Old_Rating": "Equal-weight", "New_Rating": "Overweight",
        "Price_Target": "$200", "_Source": "verified_headline",
        "_Headline": "Morgan Stanley upgrades NVIDIA to Overweight",
        "_Source_URL": "https://news.example.com/research/nvda-upgrade",
    }
    result.update(overrides)
    return result


def report(changes=None, **extra):
    result = {
        "meta": {"generated_at": "2026-11-22T11:00:00", "rating_window_days": 30},
        "summary": {"Rating_Changes": [change()] if changes is None else changes},
        "raw": {"news": []},
    }
    result.update(extra)
    return result


class PublicEventProjectionTest(unittest.TestCase):
    def test_identity_is_stable_across_mutable_fields_and_aliases(self):
        original = change(Bank="J.P. Morgan", Symbol="nvda")
        enriched = change(Bank="JPMorgan", Price_Target="$240", Old_Rating="Neutral", _Headline="Updated headline", _Source_URL="https://publisher.example.com/updated")
        self.assertEqual(event_id(original), event_id(enriched))
        self.assertRegex(event_id(original), r"^[0-9a-f]{24}$")
        self.assertNotEqual(event_id(original), event_id({**original, "Time": "2026-11-21"}))
        self.assertNotEqual(event_id(original), event_id({**original, "Action": "downgrade"}))

    def test_only_traceable_extractor_records_are_projected(self):
        cases = [
            change(_Source=""), change(_Source="llm_inferred"),
            change(_Source="yahoo"), change(_Source="yahoo_structured"),
            change(_Source="kr_headline"), change(_Source="av_consensus"),
            change(_Headline=""), change(_Source_URL=""), change(Time="bad-date"),
            change(Bank=""), change(Symbol="<bad>"), change(Action="buy_everything"),
            change(New_Rating=None),
        ]
        self.assertEqual(project_events(report(cases)), [])
        projected = project_events(report([change(), change(Time="2026-11-21", _Source="verified_summary")]))
        self.assertEqual(len(projected), 2)
        self.assertEqual(projected[0]["source_label"], "公开新闻摘要规则抽取")
        self.assertNotIn("published_at", projected[0])
        self.assertNotIn("modified_at", projected[0])

    def test_exact_title_backfill_is_preserved_without_exposing_raw_news(self):
        original = change(_Source_URL="")
        good = {"title": original["_Headline"], "url": "https://news.example.com/evidence", "private": "RAW_SENTINEL"}
        wrong = {"title": original["_Headline"] + " elsewhere", "url": "https://news.example.com/wrong"}
        data = report([original], raw={"news": [wrong, good]})
        output = project_events(data)
        self.assertEqual(output[0]["source_url"], good["url"])
        self.assertNotIn("RAW_SENTINEL", json.dumps(output))
        self.assertEqual(project_events(report([original], raw={"news": [wrong]})), [])

    def test_date_precision_and_field_inference_are_disclosed(self):
        data = report([change(Time=None, _First_Seen="2026-11-20", _Time_Inferred=True,
                              _Quality_Flags=["old_rating_backfilled", "new_rating_inferred", "PRIVATE_FLAG_SENTINEL"])])
        event = project_events(data)[0]
        self.assertEqual(event["event_date"], "2026-11-20")
        self.assertEqual(event["date_basis"], "first_seen")
        self.assertEqual(event["quality_flags"], ["inferred_event_date", "new_rating_inferred", "old_rating_backfilled"])
        self.assertEqual(project_events(report())[0]["date_basis"], "reported_date_unconfirmed")

    def test_source_labels_do_not_overclaim_verification(self):
        for tag in ("verified_headline", "verified_summary", "yahoo", "yahoo_structured", "kr_headline", "llm_inferred", "av_consensus", "", "unknown", None, {}):
            with self.subTest(tag=tag):
                label = source_label(tag)
                self.assertIsInstance(label, str)
                self.assertTrue(label)
                self.assertNotEqual(label, "已验证")
        self.assertIn("推断", source_label("kr_headline"))
        self.assertIn("未标明", source_label("unknown"))

    def test_rejects_unsafe_or_private_citation_urls(self):
        blocked = [
            "javascript:alert(1)", "data:text/html,foo", "/source", "//example.com/a",
            "https://name:secret@example.com/a", "https://localhost/a",
            "https://127.0.0.1/a", "https://10.0.0.8/a", "http://[::1]/a",
            "https://192.168.0.1/a", "https://service.local/a", "https://service.internal/a",
            "https://2130706433/a", "https://127.1/a", "https://example.com/with space",
            "https://example.com/\nfoo", "https://example.com\\@evil.com/a",
            "https://example.com:999999/a", "https://example.com/a?api_key=SECRET",
            "https://example.com/a?access_token=SECRET", "https://example.com/a?%74oken=SECRET",
            {"url": "https://example.com"}, None,
        ]
        for url in blocked:
            with self.subTest(url=url):
                self.assertEqual(safe_source_url(url), "")
        for url in ("https://example.com/article?utm_source=feed", "http://news.example.org/a", "https://8.8.8.8/a", "https://例子.中国/a"):
            self.assertEqual(safe_source_url(url), url)
        self.assertEqual(project_events(report([change(_Source_URL="https://localhost/private")])), [])

    def test_invalid_dates_and_actions_never_generate_ids(self):
        for invalid in ("2026-02-30", "today", "2026-11-20injected", "", None):
            self.assertEqual(event_id(change(Time=invalid)), "")
        self.assertEqual(event_id(change(Action="inject")), "")
        self.assertEqual(event_id({}), "")
        self.assertEqual(event_id(None), "")

    def test_non_dict_shapes_and_json_summary_fail_closed(self):
        for data in ({"summary": "not-json"}, {"summary": []}, {"summary": {"Rating_Changes": {"bad": "object"}}}, None):
            self.assertEqual(project_events(data), [])
        data = report()
        data["summary"] = json.dumps(data["summary"])
        self.assertEqual(len(project_events(data)), 1)


class PublicEventArchiveTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cache = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_archive_survives_rolling_window_and_retains_source_link(self):
        original = change(_Source_URL="")
        data = report([original], raw={"news": [{"title": original["_Headline"], "url": "https://example.com/source"}]})
        first = sync_public_archive(data, self.cache, NOW)
        self.assertEqual(first[0]["source_url"], "https://example.com/source")
        # Thirty days later both the headline lookup and active event disappear.
        later = sync_public_archive(report([]), self.cache, NOW + timedelta(days=45))
        self.assertEqual(later, first)
        self.assertEqual(load_public_archive(self.cache), first)
        # A current raw record without a link cannot erase its archived evidence.
        self.assertEqual(get_public_events(report([original]), self.cache), first)

    def test_publication_time_and_modified_time_only_follow_real_changes(self):
        initial = sync_public_archive(report(), self.cache, NOW)[0]
        repeated = sync_public_archive(report(), self.cache, NOW + timedelta(days=1))[0]
        self.assertEqual(initial["published_at"], "2026-11-22T12:00:00Z")
        self.assertEqual(initial["event_date"], "2026-11-20")
        self.assertEqual(initial["published_at"], repeated["published_at"])
        self.assertEqual(initial["modified_at"], repeated["modified_at"])
        self.assertNotEqual(initial["last_seen_at"], repeated["last_seen_at"])
        updated = sync_public_archive(report([change(Price_Target="$220")]), self.cache, NOW + timedelta(days=2))[0]
        self.assertEqual(initial["id"], updated["id"])
        self.assertEqual(initial["published_at"], updated["published_at"])
        self.assertEqual(updated["modified_at"], "2026-11-24T12:00:00Z")

    def test_read_only_merge_never_writes_or_invents_dates(self):
        empty_cache = self.cache / "not-created"
        current = get_public_events(report(), empty_cache)[0]
        self.assertNotIn("published_at", current)
        self.assertFalse(empty_cache.exists())
        sync_public_archive(report(), self.cache, NOW)
        path = self.cache / ARCHIVE_FILENAME
        before = path.read_bytes()
        changed = get_public_events(report([change(Price_Target="$250")]), self.cache)[0]
        self.assertEqual(changed["price_target"], "$250")
        self.assertIn("published_at", changed)
        self.assertNotIn("modified_at", changed)
        self.assertEqual(path.read_bytes(), before)

    def test_bootstrap_reads_existing_history_and_retries_bad_files_safely(self):
        old = report([change(Time="2026-08-01")])
        (self.cache / "report_2026-08-01.json").write_text(json.dumps(old), encoding="utf-8")
        bad = self.cache / "report_2026-08-02.json"
        bad.write_text("{broken", encoding="utf-8")
        with self.assertLogs("ib_research_sources", level="WARNING"):
            events = sync_public_archive(report(), self.cache, NOW)
        self.assertEqual(len(events), 2)
        payload = json.loads((self.cache / ARCHIVE_FILENAME).read_text())
        self.assertFalse(payload["bootstrapped"])
        # Correct the archived old event; a historical retry must not revert it.
        with self.assertLogs("ib_research_sources", level="WARNING"):
            sync_public_archive(report([change(Time="2026-08-01", Price_Target="$300")]), self.cache, NOW + timedelta(hours=1))
        bad.write_text(json.dumps(report([change(Time="2026-08-02")])), encoding="utf-8")
        events = sync_public_archive(report([]), self.cache, NOW + timedelta(hours=2))
        self.assertEqual(len(events), 3)
        old_event = next(event for event in events if event["event_date"] == "2026-08-01")
        self.assertEqual(old_event["price_target"], "$300")
        self.assertTrue(json.loads((self.cache / ARCHIVE_FILENAME).read_text())["bootstrapped"])

    def test_invalid_archive_is_not_silently_replaced(self):
        path = self.cache / ARCHIVE_FILENAME
        path.write_text("{BROKEN_PRIVATE_SENTINEL", encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            sync_public_archive(report(), self.cache, NOW)
        self.assertEqual(path.read_bytes(), before)
        with self.assertLogs("ib_research_sources", level="WARNING"):
            current = get_public_events(report(), self.cache)
        self.assertEqual(len(current), 1)
        self.assertNotIn("BROKEN_PRIVATE_SENTINEL", json.dumps(current))

    def test_failed_atomic_replace_leaves_previous_archive(self):
        sync_public_archive(report(), self.cache, NOW)
        path = self.cache / ARCHIVE_FILENAME
        before = path.read_bytes()
        with patch("ib_research_sources.os.replace", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                sync_public_archive(report([change(Time="2026-11-21")]), self.cache, NOW + timedelta(hours=1))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.cache.glob(".public-events-*.tmp")), [])

    def test_archive_write_and_read_keep_unknown_fields_private(self):
        candidate = change(_Secret="EVENT_SENTINEL", credentials={"key": "CREDENTIAL_SENTINEL"})
        data = report([candidate], raw={"news": [], "private": "RAW_SENTINEL"}, meta={"run_id": "META_SENTINEL"})
        sync_public_archive(data, self.cache, NOW)
        path = self.cache / ARCHIVE_FILENAME
        for sentinel in ("EVENT_SENTINEL", "CREDENTIAL_SENTINEL", "RAW_SENTINEL", "META_SENTINEL"):
            self.assertNotIn(sentinel, path.read_text())
        payload = json.loads(path.read_text())
        payload["events"][0]["internal"] = "STORED_SENTINEL"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertNotIn("STORED_SENTINEL", json.dumps(load_public_archive(self.cache)))


class PublicReportTest(unittest.TestCase):
    def test_recursive_allowlist_removes_internal_fields_and_bad_shapes(self):
        data = report([change(_private="EVENT_SENTINEL")])
        data["meta"].update({"run_id": "META_SENTINEL", "analysis_provider": "PROVIDER_SENTINEL", "error": "META_ERROR_SENTINEL"})
        data["raw"]["private"] = "RAW_SENTINEL"
        data["internal"] = "TOP_SENTINEL"
        data["summary"].update({
            "Metadata": {"Institution": "FResearch", "Date": "2026-11-20", "Primary_Assets_Covered": ["NVDA"], "api_key": "NESTED_SENTINEL"},
            "Core_Thesis": "模型概述。",
            "Statistical_Evidence": "模型整理的依据。",
            "Tail_Risks": ["样本不足。", {"bad": "RISK_SENTINEL"}],
            "Asset_Targets": [{"Asset": "NVDA", "Target_Price": "$200", "_Source": "av_consensus", "private": "TARGET_SENTINEL", "_Consensus": {"secret": "CONSENSUS_SENTINEL"}}],
            "error": "SUMMARY_ERROR_SENTINEL", "_debug": "DEBUG_SENTINEL",
        })
        original = copy.deepcopy(data)
        clean = public_report(data)
        text = json.dumps(clean, ensure_ascii=False)
        self.assertNotIn("SENTINEL", text)
        self.assertEqual(clean["summary"]["Rating_Changes"][0]["_Source_Label"], "公开新闻标题规则抽取")
        self.assertEqual(clean["summary"]["Core_Thesis"], "模型概述。")
        self.assertEqual(set(clean), {"meta", "summary", "publication_note"})
        self.assertEqual(data, original)

    def test_public_report_handles_unlabelled_model_records_and_nonfinite_values(self):
        data = report([change(_Source="", _Source_URL="https://localhost/private")])
        data["meta"]["rating_event_count"] = float("nan")
        data["meta"]["data_sources"] = [{"secret": "NESTED_SENTINEL"}, "finnhub_recommendations", "SOURCE_SENTINEL"]
        data["summary"]["Core_Thesis"] = {"secret": "DICT_SENTINEL"}
        clean = public_report(data)
        self.assertEqual(clean["summary"]["Rating_Changes"][0]["_Source"], "unattributed")
        self.assertEqual(clean["summary"]["Rating_Changes"][0]["_Source_URL"], "")
        self.assertNotIn("Core_Thesis", clean["summary"])
        self.assertNotIn("rating_event_count", clean["meta"])
        self.assertNotIn("SENTINEL", json.dumps(clean, allow_nan=False))


if __name__ == "__main__":
    unittest.main()
