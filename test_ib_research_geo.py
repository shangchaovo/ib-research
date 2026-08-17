#!/usr/bin/env python3
"""SEO/GEO 公开页与爬虫入口的回归测试。"""
import unittest
from unittest.mock import patch

from ib_research_fetcher import HOT_SYMBOLS
from ib_research_geo import COMPARE_PAIRS, LEARN_PAGES, TOPIC_PAGES, compare_slug, sitemap_xml
from ib_research_server import app


def _empty_report():
    return {
        "meta": {"generated_at": "2026-08-17T12:00:00", "data_sources": ["finnhub"]},
        "summary": {
            "Metadata": {"Date": "2026-08-17", "Institution": "FResearch", "Primary_Assets_Covered": ["NVDA"]},
            "Core_Thesis": "测试用核心观点。",
            "Rating_Changes": [
                {
                    "Symbol": "NVDA",
                    "Bank": "Morgan Stanley",
                    "Action": "upgrade",
                    "Old_Rating": "Equal-weight",
                    "New_Rating": "Overweight",
                    "Price_Target": "180",
                    "Time": "2026-08-16",
                }
            ],
            "Asset_Targets": [
                {
                    "Asset": "NVDA",
                    "Target_Price": "180",
                    "_Source": "verified_headline",
                    "_Verified_PT": "180",
                    "_Verified_Bank": "Morgan Stanley",
                }
            ],
            "Tail_Risks": [],
        },
        "raw": {"news": [{"title": "Morgan Stanley upgrades NVDA", "tickers": [{"ticker": "NVDA"}], "url": "https://example.com/n", "source": "test"}]},
    }


class GeoPagesTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.report_patch = patch("ib_research_server.get_latest_report", side_effect=_empty_report)
        self.geo_patch = patch("ib_research_geo.get_latest_report", side_effect=_empty_report)
        self.report_patch.start()
        self.geo_patch.start()

    def tearDown(self):
        self.report_patch.stop()
        self.geo_patch.stop()

    def test_robots_allows_search_bots(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("User-agent: Googlebot", body)
        self.assertIn("User-agent: OAI-SearchBot", body)
        self.assertIn("Allow: /", body)
        self.assertIn("User-agent: GPTBot", body)
        self.assertIn("Disallow: /", body)
        self.assertIn("Disallow: /api/", body)
        self.assertIn("Sitemap: https://fresearch.cc.cd/sitemap.xml", body)
        self.assertNotIn("noindex", body.lower())

    def test_sitemap_lists_core_urls(self):
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("<loc>https://fresearch.cc.cd/</loc>", body)
        self.assertIn("<loc>https://fresearch.cc.cd/stocks/nvda/</loc>", body)
        self.assertIn("<loc>https://fresearch.cc.cd/topics/ai/</loc>", body)
        self.assertIn("<loc>https://fresearch.cc.cd/learn/pe-ratio/</loc>", body)
        self.assertIn("<loc>https://fresearch.cc.cd/about/</loc>", body)
        for ticker in HOT_SYMBOLS:
            self.assertIn(f"/stocks/{ticker.lower()}/", body)

    def test_homepage_has_indexable_metadata(self):
        response = self.client.get(
            "/",
            headers={"CF-Connecting-IP": "203.0.113.10"},
            environ_base={"REMOTE_ADDR": "203.0.113.10"},
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("<title>FResearch - AI金融研究平台", html)
        self.assertIn('name="description"', html)
        self.assertIn('rel="canonical"', html)
        self.assertIn("application/ld+json", html)
        self.assertIn("<h1", html)
        self.assertIn("AI 金融", html)
        self.assertIn('href="/stocks/nvda/"', html)
        self.assertNotIn("noindex", html.lower())
        self.assertIn("index, follow", response.headers.get("X-Robots-Tag", "").lower())
        self.assertIn("public", response.headers.get("Cache-Control", ""))
        self.assertNotEqual(response.headers.get("Pragma"), "no-cache")

    def test_stock_page_is_unique_and_structured(self):
        response = self.client.get("/stocks/nvda/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("NVIDIA", html)
        self.assertIn("英伟达", html)
        self.assertIn("Morgan Stanley", html)
        self.assertIn("Key Takeaways", html)
        self.assertIn("application/ld+json", html)
        self.assertIn('rel="canonical" href="https://fresearch.cc.cd/stocks/nvda/"', html)
        self.assertIn("/methodology/", html)

    def test_unknown_symbol_is_404(self):
        response = self.client.get("/stocks/not-a-real-ticker/")
        self.assertEqual(response.status_code, 404)

    def test_api_is_noindex(self):
        response = self.client.get("/api/ib-research")
        self.assertEqual(response.status_code, 200)
        self.assertIn("noindex", response.headers.get("X-Robots-Tag", ""))

    def test_trust_and_learn_pages(self):
        for path in (
            "/about/",
            "/methodology/",
            "/ai-methodology/",
            "/sources/",
            "/disclosures/",
            "/ai-info/",
            "/learn/pe-ratio/",
            "/topics/ai/",
            f"/compare/{compare_slug(*COMPARE_PAIRS[0])}/",
            "/llms.txt",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            body = response.get_data(as_text=True)
            self.assertTrue(body.strip(), path)

    def test_catalog_stays_curated(self):
        self.assertLessEqual(len(HOT_SYMBOLS), 40)
        self.assertLessEqual(len(TOPIC_PAGES), 8)
        self.assertLessEqual(len(LEARN_PAGES), 12)
        xml = sitemap_xml(_empty_report())
        self.assertLess(xml.count("<url>"), 80)


if __name__ == "__main__":
    unittest.main()
