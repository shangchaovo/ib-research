#!/usr/bin/env python3
import unittest

from institution_intelligence import (
    build_institution_intelligence,
    classify_stance,
    institution_slug,
)


def _history(values):
    dates = [
        "2026-08-03",
        "2026-08-04",
        "2026-08-05",
        "2026-08-06",
        "2026-08-07",
        "2026-08-10",
        "2026-08-11",
    ]
    return [{"date": day, "close": value} for day, value in zip(dates, values)]


class InstitutionIntelligenceTest(unittest.TestCase):
    def test_institution_aliases_are_allow_listed(self):
        self.assertEqual(institution_slug("J.P. Morgan"), "jpmorgan")
        self.assertEqual(institution_slug("BofA Securities"), "bank-of-america")
        self.assertEqual(institution_slug("not a real institution"), "")

    def test_stance_uses_action_and_rating_direction(self):
        self.assertEqual(classify_stance({"Action": "price_target_raise"}), "bullish")
        self.assertEqual(classify_stance({"Action": "price_target_cut"}), "bearish")
        self.assertEqual(
            classify_stance({"Action": "reiterate", "New_Rating": "Underweight"}),
            "bearish",
        )

    def test_builds_market_alignment_from_relative_returns(self):
        changes = [
            {
                "Bank": "Morgan Stanley",
                "Symbol": "NVDA",
                "Action": "upgrade",
                "New_Rating": "Overweight",
                "Price_Target": "$200",
                "Time": "2026-08-04",
                "_Headline": "Morgan Stanley upgrades NVIDIA",
            }
        ]
        market = {
            "NVDA": _history([100, 102, 105, 107, 109, 110, 111]),
            "SPY": _history([100, 100.5, 101, 101.5, 102, 102.5, 103]),
        }
        result = build_institution_intelligence(
            changes,
            market,
            as_of="2026-08-11T12:00:00",
            universe=["NVDA"],
        )
        event = result["events"][0]
        self.assertEqual(event["reaction"]["alignment"], "aligned")
        self.assertGreater(event["reaction"]["excess_returns"]["5d"], 0.05)
        institution = result["institutions"][0]
        self.assertEqual(institution["tone"], "偏多")
        self.assertEqual(institution["alignment_rate"], 1.0)
        self.assertEqual(result["market_reaction_coverage"]["evaluated"], 1)
        self.assertEqual(result["market_reaction_coverage"]["pending"], 0)
        self.assertEqual(result["market_reaction_coverage"]["neutral"], 0)

    def test_does_not_invent_reaction_without_history(self):
        result = build_institution_intelligence(
            [{
                "Bank": "Goldman Sachs",
                "Symbol": "AAPL",
                "Action": "downgrade",
                "New_Rating": "Sell",
                "Time": "2026-08-04",
            }],
            {},
            universe=["AAPL"],
        )
        self.assertEqual(result["events"][0]["reaction"]["alignment"], "pending")
        self.assertEqual(result["market_reaction_coverage"]["pending"], 1)
        self.assertIn("尚缺足够交易日", result["institutions"][0]["interpretation"])
        self.assertIn("不能证明", result["methodology"]["boundary"])

    def test_filters_events_outside_curated_universe(self):
        result = build_institution_intelligence(
            [{
                "Bank": "UBS",
                "Symbol": "NOTINPOOL",
                "Action": "upgrade",
                "Time": "2026-08-04",
            }],
            {},
            universe=["NVDA"],
        )
        self.assertEqual(result["events"], [])
        self.assertEqual(result["institutions"], [])

    def test_interpretation_carries_institution_specific_numbers(self):
        """两家机构不能拿到同一句模板文案。"""
        changes = [
            {
                "Bank": "Morgan Stanley",
                "Symbol": "NVDA",
                "Action": "upgrade",
                "New_Rating": "Overweight",
                "Time": "2026-08-04",
            },
            {
                "Bank": "UBS",
                "Symbol": "AAPL",
                "Action": "downgrade",
                "New_Rating": "Sell",
                "Time": "2026-08-04",
            },
        ]
        market = {
            "NVDA": _history([100, 102, 105, 107, 109, 110, 111]),
            "AAPL": _history([100, 99, 98, 97, 96, 95, 94]),
            "SPY": _history([100, 100.5, 101, 101.5, 102, 102.5, 103]),
        }
        result = build_institution_intelligence(
            changes, market, as_of="2026-08-11T12:00:00", universe=["NVDA", "AAPL"]
        )
        texts = {item["interpretation"] for item in result["institutions"]}
        self.assertEqual(len(texts), 2)
        for item in result["institutions"]:
            self.assertIn(item["symbols"][0], item["interpretation"])
            self.assertIn("可判定样本", item["interpretation"])

    def test_signed_excess_follows_call_direction(self):
        """看空且随后跑输大盘 -> 按方向计的超额收益应为正。"""
        result = build_institution_intelligence(
            [{
                "Bank": "Goldman Sachs",
                "Symbol": "AAPL",
                "Action": "downgrade",
                "New_Rating": "Sell",
                "Time": "2026-08-04",
            }],
            {
                "AAPL": _history([100, 99, 98, 97, 96, 95, 94]),
                "SPY": _history([100, 100.5, 101, 101.5, 102, 102.5, 103]),
            },
            as_of="2026-08-11T12:00:00",
            universe=["AAPL"],
        )
        institution = result["institutions"][0]
        self.assertLess(institution["median_excess_5d"], 0)
        self.assertGreater(institution["median_signed_excess_5d"], 0)
        self.assertEqual(institution["alignment_rate"], 1.0)

    def test_neutral_calls_are_not_counted_as_pending(self):
        result = build_institution_intelligence(
            [{
                "Bank": "JPMorgan",
                "Symbol": "NVDA",
                "Action": "reiterate",
                "New_Rating": "Neutral",
                "Time": "2026-08-04",
            }],
            {
                "NVDA": _history([100, 101, 102, 103, 104, 105, 106]),
                "SPY": _history([100, 100.5, 101, 101.5, 102, 102.5, 103]),
            },
            universe=["NVDA"],
        )
        coverage = result["market_reaction_coverage"]
        self.assertEqual(coverage["neutral"], 1)
        self.assertEqual(coverage["pending"], 0)
        self.assertEqual(coverage["evaluated"], 0)


if __name__ == "__main__":
    unittest.main()
