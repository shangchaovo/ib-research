#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault(
    "IB_RESEARCH_CACHE_DIR",
    tempfile.mkdtemp(prefix="ib-research-test-"),
)

from ib_research_fetcher import RateLimiter, _extract_verified_rating_events, _parse_kimi_json
from ib_research_health import _aware_generated_at, _extract_latest_news_date
from collectors.yahoo_provider import YahooProvider


class ParseKimiJsonTests(unittest.TestCase):
    def test_plain_object(self):
        payload = _parse_kimi_json('{"Core_Thesis": "ok", "Rating_Changes": []}')
        self.assertEqual(payload["Core_Thesis"], "ok")

    def test_markdown_fence(self):
        payload = _parse_kimi_json(
            'prefix\n```json\n{"Core_Thesis": "from-fence"}\n```\n'
        )
        self.assertEqual(payload["Core_Thesis"], "from-fence")

    def test_embedded_object_with_braces_in_string(self):
        payload = _parse_kimi_json(
            'note {ignored}\n{"Core_Thesis": "has {braces} inside", "n": 1}\ntrailing'
        )
        self.assertEqual(payload["Core_Thesis"], "has {braces} inside")
        self.assertEqual(payload["n"], 1)

    def test_empty_and_invalid(self):
        self.assertIsNone(_parse_kimi_json(""))
        self.assertIsNone(_parse_kimi_json("no json here"))


class VerifiedEventTests(unittest.TestCase):
    def test_upgrade_with_price_target(self):
        events = _extract_verified_rating_events(
            [
                {
                    "title": "JPMorgan Upgrades Apple to Overweight, Raises Price Target to $250",
                    "summary": "JPMorgan raised its price target to $250 from $210",
                    "time_published": "20260816T120000",
                    "tickers": [{"ticker": "AAPL"}],
                }
            ]
        )
        self.assertTrue(events)
        event = events[0]
        self.assertEqual(event["Action"], "upgrade")
        self.assertEqual(event["New_Rating"], "Overweight")
        self.assertEqual(event["Price_Target"], "$250")
        self.assertEqual(event["Symbol"], "AAPL")


class RateLimiterTests(unittest.TestCase):
    def test_concurrent_acquire_stays_within_window(self):
        limiter = RateLimiter(max_calls=8, window_seconds=1)
        hits = []

        def worker():
            limiter.acquire()
            hits.append(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(hits), 8)
        self.assertLessEqual(len(limiter.calls), 8)


class HealthDateTests(unittest.TestCase):
    def test_extract_latest_news_date(self):
        latest = _extract_latest_news_date(
            {
                "news": [
                    {"time_published": "20260810T010000"},
                    {"time_published": "20260816T090000"},
                    {"time_published": "bad"},
                ]
            }
        )
        self.assertEqual(latest.isoformat(), "2026-08-16")

    def test_naive_generated_at_is_local_not_utc(self):
        local_now = datetime.now().astimezone().replace(microsecond=0)
        parsed = _aware_generated_at(local_now.replace(tzinfo=None).isoformat())
        age = abs(
            (
                datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
            ).total_seconds()
        )
        self.assertLess(age, 5)


class YahooProviderTests(unittest.TestCase):
    def test_init_does_not_inject_local_proxy(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HTTPS_PROXY", None)
            os.environ.pop("HTTP_PROXY", None)
            YahooProvider()
            self.assertNotEqual(os.environ.get("HTTPS_PROXY", ""), "http://127.0.0.1:1082")
            self.assertIsNone(os.environ.get("HTTPS_PROXY"))


if __name__ == "__main__":
    unittest.main()
