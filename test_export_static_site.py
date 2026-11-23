"""Offline publication tests: privacy, permanent URLs, and complete snapshots."""
from __future__ import annotations

import copy
from contextlib import ExitStack
from datetime import date, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import export_static_site as exporter
import ib_research_server as server
import ib_research_sources as sources


ORIGIN = "https://publication.example.com"
PRIVATE_SENTINEL = "PRIVATE-RAW-AND-INTERNAL-STATE-DO-NOT-PUBLISH"
REFRESH_SENTINEL = "OWNER-REFRESH-SECRET-DO-NOT-PUBLISH"


def _report(count: int = 1, *, day_offset: int = 0) -> dict:
    changes = []
    for offset in range(count):
        day = date(2026, 8, 1) + timedelta(days=offset + day_offset)
        changes.append({
            "Bank": "Morgan Stanley", "Symbol": "NVDA", "Action": "upgrade",
            "Time": day.isoformat(), "New_Rating": "Overweight", "Old_Rating": "Equal-weight",
            "Price_Target": "180", "_Source": "verified_headline",
            "_Headline": f"Morgan Stanley upgrades NVDA & sets target on {day.isoformat()}",
            "_Source_URL": f"https://news.example.com/nvda/{day.isoformat()}",
            "Private_Internal_Notes": {"nested": PRIVATE_SENTINEL},
        })
    return {
        "meta": {
            "generated_at": "2026-11-22T10:00:00+00:00", "analysis_status": "ok",
            "quality_status": "ok", "data_sources": ["public_news_rss"],
            "rating_event_count": count, "run_id": PRIVATE_SENTINEL,
        },
        "summary": {
            "Metadata": {"Date": "2026-11-22", "Institution": "FResearch", "Primary_Assets_Covered": ["NVDA"]},
            "Core_Thesis": "公开测试概述，属于模型整理。", "Rating_Changes": changes,
            "Asset_Targets": [], "Tail_Risks": [],
            "Private_Internal_Notes": PRIVATE_SENTINEL,
        },
        "raw": {"news": [], "reddit_posts": [{"private_body": PRIVATE_SENTINEL}], "private": PRIVATE_SENTINEL},
        "unknown_future_field": {"nested": PRIVATE_SENTINEL},
    }


class StaticPublicationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.output = self.root / "published"

    def _build(self, report: dict, *, allow_empty: bool = False) -> dict:
        # Independent module imports of get_latest_report must not cause a second
        # read while an export is pinned to PUBLICATION_REPORT.
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "IB_RESEARCH_PUBLIC_ORIGIN": ORIGIN,
                "IB_RESEARCH_REFRESH_TOKEN": REFRESH_SENTINEL,
            }))
            stack.enter_context(patch("ib_research_fetcher.CACHE_DIR", str(self.cache)))
            stack.enter_context(patch("ib_research_server.CACHE_DIR", str(self.cache)))
            stack.enter_context(patch("ib_research_server._logo_cache_dir", str(self.cache / "logos")))
            reader = stack.enter_context(patch("ib_research_fetcher.get_latest_report", return_value=copy.deepcopy(report)))
            for name in (
                "ib_research_server.get_latest_report",
                "ib_research_geo._load_latest_report",
                "ib_research_source_pages.get_latest_report",
                "ib_research_fetcher.run_fetch",
                "ib_research_server.run_fetch",
                "ib_research_server._queue_logo_fetch",
            ):
                stack.enter_context(patch(name, side_effect=AssertionError(f"unexpected read/fetch: {name}")))
            sync = stack.enter_context(patch("ib_research_sources.sync_public_archive", wraps=sources.sync_public_archive))
            manifest = exporter.build_static_site(self.output, allow_empty=allow_empty)
            reader.assert_called_once_with()
            sync.assert_called_once()
            return manifest

    def _json(self, relative: str) -> dict:
        return json.loads((self.output / relative).read_text(encoding="utf-8"))

    def test_export_is_closed_safe_and_uses_one_report(self):
        report = _report(52)
        manifest = self._build(report)
        self.assertEqual(manifest["events"], 52)
        self.assertEqual(manifest["origin"], ORIGIN)
        self.assertEqual(manifest["files"], sum(path.is_file() for path in self.output.rglob("*")))
        self.assertTrue((self.output / "events/page/2/index.html").is_file())

        data = self._json("data/events.json")
        self.assertEqual(data["count"], 52)
        event_ids = {event["id"] for event in data["events"]}
        for event in data["events"]:
            detail = self.output / "events" / event["id"] / "index.html"
            self.assertTrue(detail.is_file())
            html = detail.read_text(encoding="utf-8")
            self.assertIn(f'rel="canonical" href="{event["url"]}"', html)
            self.assertNotIn('"datePublished":"2026-08-30"', html)

        sitemap = ET.parse(self.output / "sitemap.xml")
        sitemap_paths = []
        for location in sitemap.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
            parsed = urlsplit(location.text)
            self.assertEqual(f"{parsed.scheme}://{parsed.netloc}", ORIGIN)
            sitemap_paths.append(parsed.path)
            self.assertTrue((self.output / exporter._page_target(parsed.path)).is_file(), parsed.path)
        self.assertTrue({f"/events/{event_id}/" for event_id in event_ids}.issubset(sitemap_paths))

        feed = ET.parse(self.output / "feed.xml")
        items = feed.findall("./channel/item")
        self.assertTrue(items)
        for item in items:
            link = item.findtext("link")
            self.assertEqual(item.findtext("guid"), link)
            self.assertTrue((self.output / exporter._page_target(urlsplit(link).path)).is_file())
            self.assertTrue(item.findtext("pubDate"))

        self.assertEqual(self._json("api/ib-research.json")["data"], sources.public_report(report))
        self.assertFalse((self.output / "api/ib-research-raw.json").exists())
        self.assertNotIn("/api/ib-research/raw", (self.output / "_redirects").read_text())
        headers = (self.output / "_headers").read_text()
        self.assertIn("/stocks/amd/\n  X-Robots-Tag: noindex, follow", headers)
        self.assertIn("Cache-Control: no-cache, max-age=0, must-revalidate", headers)
        for path in self.output.rglob("*"):
            if path.is_file():
                body = path.read_bytes()
                self.assertNotIn(PRIVATE_SENTINEL.encode(), body, str(path))
                self.assertNotIn(REFRESH_SENTINEL.encode(), body, str(path))

    def test_second_build_retains_aged_out_event_url_and_dates(self):
        self._build(_report())
        first = self._json("data/events.json")["events"][0]
        second_report = _report(day_offset=100)
        second_report["meta"]["generated_at"] = "2026-11-23T10:00:00+00:00"
        self._build(second_report)
        current = {item["id"]: item for item in self._json("data/events.json")["events"]}
        self.assertEqual(len(current), 2)
        self.assertIn(first["id"], current)
        retained = current[first["id"]]
        self.assertEqual(retained["url"], first["url"])
        self.assertEqual(retained["published_at"], first["published_at"])
        self.assertEqual(retained["modified_at"], first["modified_at"])
        self.assertTrue((self.output / exporter._page_target(urlsplit(first["url"]).path)).is_file())

    def test_empty_report_gate_preserves_previous_snapshot(self):
        self.output.mkdir()
        (self.output / "old.txt").write_text("previous complete publication")
        empty = {"meta": {"generated_at": "", "quality_status": "degraded"}, "summary": {"Rating_Changes": []}, "raw": {}}
        with self.assertRaisesRegex(RuntimeError, "empty or unavailable"):
            self._build(empty)
        self.assertEqual((self.output / "old.txt").read_text(), "previous complete publication")
        self.assertFalse((self.cache / sources.ARCHIVE_FILENAME).exists())

    def test_explicit_empty_fixture_is_supported(self):
        empty = {"meta": {"generated_at": "", "quality_status": "degraded"}, "summary": {}, "raw": {}}
        manifest = self._build(empty, allow_empty=True)
        self.assertEqual(manifest["events"], 0)
        self.assertTrue((self.output / "events/index.html").is_file())
        self.assertEqual(self._json("data/events.json")["events"], [])
        self.assertIn("/events/\n  X-Robots-Tag: noindex, follow", (self.output / "_headers").read_text())

    def test_render_failure_preserves_output_and_restores_config(self):
        self.output.mkdir()
        (self.output / "old.txt").write_text("previous complete publication")
        original_write = exporter._write

        def fail_on_feed(root, relative, body):
            if relative == "/feed.xml":
                raise RuntimeError("injected feed write failure")
            return original_write(root, relative, body)

        with patch.dict(server.app.config, {
            "PUBLICATION_REPORT": {"before": "report"},
            "PUBLICATION_EVENTS": [{"before": "events"}],
            "PUBLICATION_EVENT_VIEW": {"before": "view"},
            "PUBLICATION_DISABLE_LOGO_FETCH": False,
        }), patch.object(exporter, "_write", side_effect=fail_on_feed):
            with self.assertRaisesRegex(RuntimeError, "injected feed"):
                self._build(_report())
            self.assertEqual(server.app.config["PUBLICATION_REPORT"], {"before": "report"})
            self.assertEqual(server.app.config["PUBLICATION_EVENTS"], [{"before": "events"}])
            self.assertEqual(server.app.config["PUBLICATION_EVENT_VIEW"], {"before": "view"})
            self.assertIs(server.app.config["PUBLICATION_DISABLE_LOGO_FETCH"], False)
            self.assertEqual(server._page_cache, {})
        self.assertEqual((self.output / "old.txt").read_text(), "previous complete publication")
        self.assertFalse(list(self.root.glob(".cloudflare-dist-*")))

    def test_directory_swap_failure_rolls_back_previous_output(self):
        self.output.mkdir()
        (self.output / "old.txt").write_text("previous complete publication")
        original_replace = Path.replace

        def fail_new_commit(path, target):
            if path.name.startswith(".cloudflare-dist-") and Path(target) == self.output:
                raise OSError("injected directory commit failure")
            return original_replace(path, target)

        with patch.object(Path, "replace", new=fail_new_commit):
            with self.assertRaisesRegex(OSError, "injected directory commit"):
                self._build(_report())
        self.assertEqual((self.output / "old.txt").read_text(), "previous complete publication")
        self.assertFalse(list(self.root.glob(".cloudflare-previous-*")))

    def test_static_headers_keep_feed_types_and_logo_security(self):
        rules = {}
        current = None
        for line in exporter._headers_file().splitlines():
            if line.startswith("/"):
                current = line
                rules[current] = {}
            elif line.startswith("  ") and current:
                key, value = line.strip().split(":", 1)
                rules[current][key] = value.strip()
        self.assertEqual(rules["/feed.xml"]["Content-Type"], "application/rss+xml; charset=utf-8")
        self.assertEqual(rules["/feed.xml"]["X-Robots-Tag"], "noindex, follow")
        self.assertEqual(rules["/data/events.json"]["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(rules["/data/events.json"]["X-Robots-Tag"], "noindex, follow")
        self.assertEqual(rules["/events/*"]["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(rules["/sitemap.xml"]["Content-Type"], "application/xml; charset=utf-8")
        self.assertEqual(rules["/assets/logos/*"]["Content-Security-Policy"], "script-src 'none'")
        self.assertNotIn("immutable", rules["/og-image.png"]["Cache-Control"])


if __name__ == "__main__":
    unittest.main()
