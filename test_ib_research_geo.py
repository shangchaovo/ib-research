#!/usr/bin/env python3
"""SEO/GEO 公开页与爬虫入口的回归测试。"""
import json
import unittest
from unittest.mock import patch

from ib_research_fetcher import HOT_SYMBOLS, _build_verified_asset_targets
from ib_research_geo import (
    COMPARE_PAIRS,
    LEARN_PAGES,
    RESEARCH_LENS_PAGES,
    TOPIC_PAGES,
    _plausible_targets,
    compare_slug,
    sitemap_xml,
)
from ib_research_server import _generate_html, app


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
                    "_Source": "verified_headline",
                    "_Headline": "Morgan Stanley upgrades NVDA",
                    "_Source_URL": "https://example.com/n",
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
        "market_context": {
            "benchmark": "SPY",
            "history": {
                "NVDA": [
                    {"date": "2026-08-14", "close": 100},
                    {"date": "2026-08-17", "close": 104},
                    {"date": "2026-08-18", "close": 105},
                    {"date": "2026-08-19", "close": 106},
                    {"date": "2026-08-20", "close": 108},
                    {"date": "2026-08-21", "close": 110},
                ],
                "SPY": [
                    {"date": "2026-08-14", "close": 100},
                    {"date": "2026-08-17", "close": 100.5},
                    {"date": "2026-08-18", "close": 101},
                    {"date": "2026-08-19", "close": 101.5},
                    {"date": "2026-08-20", "close": 102},
                    {"date": "2026-08-21", "close": 102.5},
                ],
            },
        },
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
        self.assertIn("<loc>https://fresearch.cc.cd/topics/price-targets/</loc>", body)
        self.assertIn("<loc>https://fresearch.cc.cd/institutions/</loc>", body)
        self.assertIn("<loc>https://fresearch.cc.cd/institutions/morgan-stanley/</loc>", body)
        self.assertIn("<loc>https://fresearch.cc.cd/reactions/</loc>", body)
        self.assertIn("<loc>https://fresearch.cc.cd/about/</loc>", body)
        self.assertNotIn("<loc>https://fresearch.cc.cd/topics/ai/</loc>", body)
        self.assertNotIn("<loc>https://fresearch.cc.cd/learn/", body)
        # 只有模板文案、没有事件/目标价/新闻的股票页不进 sitemap，
        # 但页面本身仍可访问并保留站内链接。
        self.assertNotIn("<loc>https://fresearch.cc.cd/stocks/amd/</loc>", body)
        self.assertEqual(self.client.get("/stocks/amd/").status_code, 200)

    def test_homepage_has_indexable_metadata(self):
        response = self.client.get(
            "/",
            headers={"CF-Connecting-IP": "203.0.113.10"},
            environ_base={"REMOTE_ADDR": "203.0.113.10"},
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("<title>投行目标价与研报观点追踪", html)
        self.assertIn('name="description"', html)
        self.assertIn(
            'name="google-site-verification" content="s8mvn7tXPvT_q4BHyD2tZAXPFuj3xdUSksDjR6BCj1g"',
            html,
        )
        self.assertIn('rel="canonical"', html)
        self.assertIn("application/ld+json", html)
        self.assertIn("<h1", html)
        self.assertIn("投行目标价", html)
        self.assertIn("机构公开倾向与观点蒸馏", html)
        self.assertIn("机构观点后的市场反应", html)
        self.assertIn('href="/institutions/"', html)
        self.assertIn('href="/reactions/"', html)
        self.assertIn("不推断未披露的持仓或交易意图", html)
        self.assertIn('href="/stocks/nvda/"', html)
        self.assertNotIn("noindex", html.lower())
        self.assertIn("index, follow", response.headers.get("X-Robots-Tag", "").lower())
        self.assertIn("public", response.headers.get("Cache-Control", ""))
        self.assertNotEqual(response.headers.get("Pragma"), "no-cache")

    def test_software_schema_only_on_about_page(self):
        """首页主体是事件集合，挂 SoftwareApplication 属于类型错配。"""
        home = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("SoftwareApplication", home)
        self.assertIn("CollectionPage", home)
        about = self.client.get("/about/").get_data(as_text=True)
        self.assertIn("SoftwareApplication", about)

    def test_stock_page_is_unique_and_structured(self):
        response = self.client.get("/stocks/nvda/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("NVIDIA", html)
        self.assertIn("英伟达", html)
        self.assertIn("Morgan Stanley", html)
        self.assertIn("本页要点", html)
        self.assertIn("机构观点整合", html)
        self.assertIn("观点发布后的市场反应", html)
        self.assertIn("已验证来源", html)
        self.assertIn("application/ld+json", html)
        self.assertIn('rel="canonical" href="https://fresearch.cc.cd/stocks/nvda/"', html)
        self.assertIn("/methodology/", html)
        # 枚举必须落成中文标签，不能把 price_target_raise 这种原值拼进正文。
        self.assertNotIn("price_target_raise", html)
        self.assertNotIn("对 NVDA 的 upgrade", html)

    def test_stock_page_leads_with_price_versus_consensus(self):
        html = self.client.get("/stocks/nvda/").get_data(as_text=True)
        self.assertIn('class="consensus"', html)
        self.assertIn("最新收盘", html)
        self.assertIn("一致目标价", html)
        self.assertIn("隐含空间", html)
        # 首屏顺序：共识卡片要排在评级明细表之前。
        self.assertLess(html.index('class="consensus"'), html.index("外资评级与目标价动作"))
        # 机构观点不再每条复读同一个前缀。
        self.assertEqual(html.count("近 30 日公开倾向"), 0)
        self.assertIn('class="viewpoint-list"', html)

    def test_consensus_range_drops_upstream_outliers(self):
        # 实测上游标题解析会吐出截断价（$31）和串标的的价（$1,150）。
        targets = [("A", 300.0), ("B", 320.0), ("C", 350.0), ("D", 31.0), ("E", 1150.0)]
        kept, dropped = _plausible_targets(targets)
        self.assertEqual(dropped, 2)
        self.assertEqual([price for _, price in kept], [300.0, 320.0, 350.0])

    def test_consensus_range_keeps_small_samples_intact(self):
        targets = [("A", 300.0), ("B", 1200.0)]
        kept, dropped = _plausible_targets(targets)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(kept), 2)

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
            "/topics/price-targets/",
            "/institutions/",
            "/institutions/morgan-stanley/",
            "/reactions/",
            "/llms.txt",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            body = response.get_data(as_text=True)
            self.assertTrue(body.strip(), path)

    def test_legacy_learn_and_compare_pages_redirect_to_core_research(self):
        expected = {
            "/learn/": "/methodology/",
            "/learn/pe-ratio/": "/methodology/",
            "/learn/price-target/": "/topics/price-targets/",
            "/compare/": "/stocks/",
            f"/compare/{compare_slug(*COMPARE_PAIRS[0])}/": "/stocks/",
        }
        for path, target in expected.items():
            response = self.client.get(path)
            self.assertEqual(response.status_code, 308, path)
            self.assertEqual(response.headers["Location"], target, path)

    def test_legacy_industry_topics_redirect_to_research_lenses(self):
        response = self.client.get("/topics/ai/")
        self.assertEqual(response.status_code, 308)
        self.assertEqual(response.headers["Location"], "/topics/institution-views/")

    def test_noncanonical_paths_redirect_to_trailing_slash(self):
        for path in ("/stocks", "/stocks/nvda", "/institutions", "/reactions"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 308, path)
            self.assertTrue(response.headers["Location"].endswith(path + "/"), path)

    def test_homepage_ships_one_page_of_rows_plus_full_ledger_island(self):
        report = _empty_report()
        template = report["summary"]["Rating_Changes"][0]
        report["summary"]["Rating_Changes"] = [
            {**template, "Time": f"2026-08-{(index % 28) + 1:02d}", "Price_Target": str(180 + index)}
            for index in range(65)
        ]
        report["summary"]["Rating_Changes"].append(
            {**template, "Symbol": "ZZZZ", "Time": "2026-08-15", "Price_Target": "42"}
        )
        html = _generate_html(report)

        # 只有首屏一页进入 DOM；完整账本以预渲染字符串放在 JSON 岛里。
        body = html.split('id="ratingTableBody"', 1)[1].split("</tbody>", 1)[0]
        self.assertEqual(body.count('class="rating-row"'), 30)
        self.assertIn('id="ratingLedger"', html)
        ledger = json.loads(
            html.split('id="ratingLedger">', 1)[1]
            .split("</script>", 1)[0]
            .replace("\\u003c", "<")
            .replace("\\u0026", "&")
        )
        self.assertEqual(len(ledger), 66)
        self.assertEqual(sum(item["t"] for item in ledger), 65)
        self.assertTrue(all(item["h"].startswith("<tr class=\"rating-row\"") for item in ledger))
        self.assertTrue(any(item["s"] == "ZZZZ" and item["t"] == 0 for item in ledger))

        # 首屏默认关注池，非关注池事件不出现在服务端渲染的那一页里。
        self.assertNotIn('data-symbol="ZZZZ"', body)
        self.assertIn("universe: 'tracked'", html)
        self.assertIn("近 30 天 · 关注池 · 共 65 条", html)
        self.assertIn("关注池 65", html)
        self.assertIn("全部事件 66", html)
        self.assertIn("完整保留近 30 日 66 条", html)
        self.assertIn("其中关注池 65 条", html)
        self.assertIn('id="ratingPagination"', html)
        self.assertIn("pageSize: 30", html)

    def test_rating_rows_drop_the_pending_source_column_noise(self):
        report = _empty_report()
        template = report["summary"]["Rating_Changes"][0]
        report["summary"]["Rating_Changes"] = [
            {**template, "_Source": "", "_Headline": "", "Time": "2026-08-20"},
            {**template, "_Source": "yahoo_structured", "Time": "2026-08-21"},
        ]
        html = _generate_html(report)
        self.assertNotIn("待补来源", html)
        self.assertIn("evidence-verified", html)
        # 原/新评级合并成一列后，独立的箭头列不再存在。
        self.assertNotIn('<td class="arrow">', html)
        self.assertIn('class="rating-arrow"', html)

    def test_target_cards_require_external_evidence_and_tracked_symbol(self):
        self.assertEqual(
            _build_verified_asset_targets(
                [
                    {"Asset": "NVDA", "Target_Price": "$999"},
                    {"Asset": "NOTINPOOL", "Target_Price": "$50"},
                ],
                [],
                {},
                [],
            ),
            [],
        )
        targets = _build_verified_asset_targets(
            [{"Asset": "NVDA", "Target_Price": "$999"}],
            [{"Symbol": "NVDA", "Price_Target": "$200", "Bank": "JPMorgan", "Time": "2026-08-30"}],
            {},
            [],
        )
        self.assertEqual(targets[0]["Target_Price"], "$200")
        self.assertEqual(targets[0]["_Source"], "verified_headline")

    def test_catalog_stays_curated(self):
        self.assertLessEqual(len(HOT_SYMBOLS), 40)
        self.assertLessEqual(len(TOPIC_PAGES), 8)
        self.assertEqual(len(RESEARCH_LENS_PAGES), 4)
        self.assertLessEqual(len(LEARN_PAGES), 12)
        xml = sitemap_xml(_empty_report())
        self.assertLess(xml.count("<url>"), 80)


if __name__ == "__main__":
    unittest.main()
