#!/usr/bin/env python3
"""Public event publication contracts: discoverability, dates and data bounds."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from flask import Flask

import ib_research_source_pages as pages
from ib_research_sources import sync_public_archive


def _event(index=1, **changes):
    result = {
        "id": f"{index:024x}", "symbol": "NVDA", "bank": "Morgan Stanley",
        "action": "upgrade", "action_label": "上调评级", "event_date": "2026-08-16",
        "date_basis": "reported_date_unconfirmed", "old_rating": "Equal-weight",
        "new_rating": "Overweight", "price_target": "180", "previous_price_target": "160",
        "source_type": "verified_headline", "source_label": "公开新闻标题规则抽取",
        "source_title": f"Morgan Stanley upgrades NVDA ({index})",
        "source_url": f"https://example.com/news/{index}", "quality_flags": [],
        "first_seen_at": "2026-11-22T04:00:00Z", "last_seen_at": "2026-11-22T04:00:00Z",
        "published_at": "2026-11-22T04:00:00Z", "modified_at": "2026-11-22T04:00:00Z",
    }
    result.update(changes)
    return result


def _report():
    return {
        "meta": {"generated_at": "2026-11-22T04:00:00Z"},
        "summary": {"Rating_Changes": [{
            "Bank": "Morgan Stanley", "Symbol": "NVDA", "Action": "upgrade",
            "Old_Rating": "Equal-weight", "New_Rating": "Overweight", "Price_Target": "180",
            "Time": "2026-08-16", "_Source": "verified_headline",
            "_Headline": "Morgan Stanley upgrades NVDA", "_Source_URL": "https://example.com/news/1",
        }]},
        "raw": {"news": []},
    }


def _article(html):
    for script in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        parsed = json.loads(script)
        if parsed.get("@type") == "Article":
            return parsed
    raise AssertionError("No Article structured data")


class SourcePagesTest(unittest.TestCase):
    def setUp(self):
        self.origin = patch.dict(os.environ, {"IB_RESEARCH_PUBLIC_ORIGIN": "https://example.org"})
        self.origin.start()
        self.addCleanup(self.origin.stop)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, PUBLICATION_EVENTS=[_event()])
        pages.register_source_routes(self.app)
        self.client = self.app.test_client()

    def test_server_rendered_pagination_links_cover_all_events(self):
        events = [_event(index) for index in range(1, 102)]
        self.app.config["PUBLICATION_EVENTS"] = events
        paths = pages.event_paths(events)
        self.assertEqual(len(paths), 104)
        self.assertIn("/events/page/3/", paths)
        self.assertNotIn("/events/page/1/", paths)
        seen = set()
        for path, expected_count in (("/events/", 50), ("/events/page/2/", 50), ("/events/page/3/", 1)):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            links = re.findall(r'href="(/events/[0-9a-f]{24}/)"', html)
            self.assertEqual(len(links), expected_count)
            seen.update(links)
            self.assertIn(f'rel="canonical" href="https://example.org{path}"', html)
        self.assertEqual(len(seen), 101)
        self.assertIn('rel="next" href="/events/page/2/"', self.client.get("/events/").get_data(as_text=True))
        self.assertIn('rel="prev" href="/events/"', self.client.get("/events/page/2/").get_data(as_text=True))

    def test_empty_archive_is_useful_but_noindex(self):
        self.app.config["PUBLICATION_EVENTS"] = []
        with patch.object(pages, "get_latest_report", side_effect=AssertionError("Snapshot must not load report")):
            response = self.client.get("/events/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("等待首批可回查事件", response.get_data(as_text=True))
        self.assertIn("noindex", response.headers["X-Robots-Tag"])
        self.assertIn('content="noindex,follow"', response.get_data(as_text=True))
        self.assertEqual(pages.source_sitemap_entries([]), [])
        self.assertEqual(pages.event_paths([]), ["/events/"])

    def test_missing_pages_and_ids_do_not_generate_content(self):
        for path in ("/events/page/0/", "/events/page/2/", "/events/not-an-id/", f"/events/{99:024x}/"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        redirect = self.client.get("/events/page/1/")
        self.assertEqual(redirect.status_code, 308)
        self.assertEqual(redirect.headers["Location"], "/events/")

    def test_detail_preserves_sources_dates_and_backfill_caveats(self):
        self.app.config["PUBLICATION_EVENTS"] = [_event(
            date_basis="first_seen", quality_flags=["inferred_event_date", "pt_backfilled", "old_rating_backfilled", "new_rating_inferred", "yahoo_backfilled"]
        )]
        html = self.client.get(f"/events/{1:024x}/").get_data(as_text=True)
        self.assertIn('href="https://example.com/news/1"', html)
        self.assertIn("首次发现日期（事件日期未确认）", html)
        self.assertIn("目标价由后续证据补全", html)
        self.assertIn("原评级由更早的历史记录回填", html)
        self.assertIn("新评级含推断", html)
        self.assertIn("部分字段由 Yahoo 结构化记录补全", html)
        self.assertIn("不代表本站取得或核验了投行完整原始研报", html)
        self.assertIn("不提供逐字段出处映射", html)
        schema = _article(html)
        self.assertEqual(schema["datePublished"], "2026-11-22T04:00:00+00:00")
        self.assertEqual(schema["dateModified"], "2026-11-22T04:00:00+00:00")
        self.assertNotEqual(schema["datePublished"][:10], "2026-08-16")
        self.assertEqual(schema["citation"]["url"], "https://example.com/news/1")

    def test_unknown_publication_dates_are_omitted_not_invented(self):
        self.app.config["PUBLICATION_EVENTS"] = [_event(published_at="", modified_at="not-a-date")]
        html = self.client.get(f"/events/{1:024x}/").get_data(as_text=True)
        schema = _article(html)
        self.assertNotIn("datePublished", schema)
        self.assertNotIn("dateModified", schema)
        self.assertNotIn("2026-08-30", html)
        self.assertIn("未记录", html)
        entries = dict(pages.source_sitemap_entries(self.app.config["PUBLICATION_EVENTS"]))
        self.assertEqual(entries[f"/events/{1:024x}/"], "")
        # A known original publication date cannot stand in for an unknown
        # substantive modification time on a newer, unarchived projection.
        entries = dict(pages.source_sitemap_entries([_event(modified_at="")]))
        self.assertEqual(entries[f"/events/{1:024x}/"], "")
        entries = dict(pages.source_sitemap_entries([_event(), _event(2, modified_at="")]))
        self.assertEqual(entries["/events/"], "")

    def test_feed_is_xml_and_uses_publication_not_event_date(self):
        self.app.config["PUBLICATION_EVENTS"] = [_event(index) for index in range(1, 56)]
        response = self.client.get("/feed.xml")
        self.assertEqual(response.mimetype, "application/rss+xml")
        self.assertIn("public", response.headers["Cache-Control"])
        feed = ET.fromstring(response.data)
        items = feed.findall("./channel/item")
        self.assertEqual(len(items), 50)
        self.assertTrue(items[0].findtext("link").startswith("https://example.org/events/"))
        self.assertEqual(items[0].findtext("pubDate"), "Sun, 22 Nov 2026 04:00:00 GMT")
        self.assertNotIn("Aug", items[0].findtext("pubDate"))
        self.assertIn("不等于投行原始研报", items[0].findtext("description"))
        self.app.config["PUBLICATION_EVENTS"] = [_event(published_at="", modified_at="")]
        item = ET.fromstring(self.client.get("/feed.xml").data).find("./channel/item")
        self.assertIsNone(item.find("pubDate"))

    def test_public_json_is_a_whitelist_without_raw_or_private_fields(self):
        self.app.config["PUBLICATION_EVENTS"] = [_event(
            raw={"api_key": "PRIVATE_VALUE"}, api_key="PRIVATE_VALUE", notes="PRIVATE_VALUE",
            source_url="https://example.com/news?api_key=PRIVATE_VALUE",
        )]
        response = self.client.get("/data/events.json")
        self.assertEqual(response.mimetype, "application/json")
        payload = response.get_json()
        self.assertEqual(payload["count"], 1)
        event = payload["events"][0]
        self.assertEqual(set(event), set(pages.PUBLIC_EVENT_FIELDS) | {"url"})
        self.assertEqual(event["source_url"], "")
        self.assertNotIn("PRIVATE_VALUE", response.get_data(as_text=True))
        self.assertNotIn("raw", event)
        self.assertIn("public", response.headers["Cache-Control"])

    def test_untrusted_text_cannot_escape_html_or_json_ld(self):
        malicious = '</script><script>alert("XSS")</script>'
        self.app.config["PUBLICATION_EVENTS"] = [_event(
            bank=malicious, source_title=malicious, source_label=malicious,
            source_url="javascript:alert(1)", quality_flags=[malicious],
        )]
        html = self.client.get(f"/events/{1:024x}/").get_data(as_text=True)
        self.assertNotIn(malicious, html)
        self.assertNotIn('href="javascript:', html)
        self.assertIn("&lt;/script&gt;", html)
        self.assertEqual(_article(html)["headline"], malicious + " · NVDA · 上调评级")
        ET.fromstring(self.client.get("/feed.xml").data)
        self.client.get("/data/events.json").get_json()

    def test_fixed_snapshot_avoids_report_reads_and_mutations(self):
        self.app.config["PUBLICATION_REPORT"] = {"meta": {"quality_status": "degraded"}}
        with patch.object(pages, "get_latest_report", side_effect=AssertionError("Unexpected report read")), \
             patch.object(pages, "get_public_events", side_effect=AssertionError("Unexpected archive read")), \
             patch("ib_research_fetcher.run_fetch", side_effect=AssertionError("Unexpected fetch")), \
             patch("ib_research_sources.sync_public_archive", side_effect=AssertionError("Unexpected archive write")):
            for path in ("/events/", f"/events/{1:024x}/", "/feed.xml", "/data/events.json"):
                self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_report_snapshot_is_passed_to_read_only_projection(self):
        del self.app.config["PUBLICATION_EVENTS"]
        report = {"meta": {"quality_status": "degraded"}}
        self.app.config["PUBLICATION_REPORT"] = report
        with patch.object(pages, "get_latest_report", side_effect=AssertionError("Unexpected report read")), \
             patch.object(pages, "get_public_events", return_value=[_event()]) as read:
            self.assertEqual(self.client.get("/events/").status_code, 200)
        read.assert_called_once_with(report, cache_dir=pages.CACHE_DIR)

    def test_fixed_snapshot_normalizes_once_and_replacement_invalidates_view(self):
        self.app.config["PUBLICATION_EVENTS"] = [_event(index) for index in range(1, 101)]
        with patch.object(pages, "_normalized_events", wraps=pages._normalized_events) as normalize, \
             patch.object(pages, "_public_event", wraps=pages._public_event) as project:
            for index in range(1, 11):
                self.assertEqual(self.client.get(f"/events/{index:024x}/").status_code, 200)
            self.assertEqual(normalize.call_count, 1)
            self.assertEqual(project.call_count, 100)
            # A new list with the same length is a different snapshot.
            replacement = [_event(index) for index in range(101, 201)]
            self.app.config["PUBLICATION_EVENTS"] = replacement
            self.assertEqual(self.client.get(f"/events/{101:024x}/").status_code, 200)
            self.assertEqual(self.client.get(f"/events/{1:024x}/").status_code, 404)
            self.assertEqual(normalize.call_count, 2)
            self.assertIs(self.app.config["PUBLICATION_EVENT_VIEW"]["_input"], replacement)
            self.app.config["PUBLICATION_EVENTS"] = []
            response = self.client.get("/events/")
            self.assertIn("等待首批可回查事件", response.get_data(as_text=True))
            self.assertEqual(self.client.get(f"/events/{101:024x}/").status_code, 404)
            self.assertEqual(normalize.call_count, 3)

    def test_snapshot_origin_change_does_not_reuse_old_absolute_urls(self):
        first = self.client.get("/data/events.json").get_json()
        self.assertTrue(first["events"][0]["url"].startswith("https://example.org/"))
        with patch.dict(os.environ, {"IB_RESEARCH_PUBLIC_ORIGIN": "https://new.example.org"}):
            second = self.client.get("/data/events.json").get_json()
        self.assertTrue(second["events"][0]["url"].startswith("https://new.example.org/"))

    def test_live_requests_do_not_reuse_a_previous_requests_view(self):
        del self.app.config["PUBLICATION_EVENTS"]
        self.app.config["PUBLICATION_REPORT"] = {}
        with patch.object(pages, "get_public_events", side_effect=[[_event(1)], [_event(2)]]) as read:
            self.assertEqual(self.client.get(f"/events/{1:024x}/").status_code, 200)
            self.assertEqual(self.client.get(f"/events/{2:024x}/").status_code, 200)
        self.assertEqual(read.call_count, 2)
        self.assertNotIn("PUBLICATION_EVENT_VIEW", self.app.config)

    def test_archived_event_survives_latest_report_rotation_and_gets_are_read_only(self):
        del self.app.config["PUBLICATION_EVENTS"]
        with tempfile.TemporaryDirectory() as folder:
            events = sync_public_archive(_report(), folder, now="2026-11-22T04:00:00Z")
            self.assertEqual(len(events), 1)
            event_id = events[0]["id"]
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(folder).iterdir() if p.is_file()}
            self.app.config["PUBLICATION_REPORT"] = {"meta": {"quality_status": "degraded"}, "summary": {}, "raw": {}}
            with patch.object(pages, "CACHE_DIR", folder), \
                 patch("ib_research_fetcher.run_fetch", side_effect=AssertionError("Unexpected fetch")), \
                 patch("ib_research_sources.sync_public_archive", side_effect=AssertionError("Unexpected archive write")):
                for path in ("/events/", f"/events/{event_id}/", "/feed.xml", "/data/events.json"):
                    self.assertEqual(self.client.get(path).status_code, 200, path)
            after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(folder).iterdir() if p.is_file()}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
