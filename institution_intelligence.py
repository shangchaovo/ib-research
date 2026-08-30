#!/usr/bin/env python3
"""Institution-view aggregation and post-call market reaction analysis.

The module is intentionally deterministic.  It turns public sell-side actions
(rating changes, reiterations and price-target changes) into an auditable view
of what an institution said and how the covered stock traded afterwards.  It
does *not* infer proprietary positions, inventory or trading intent.
"""
from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any, Iterable, Optional


TRACKED_INSTITUTIONS: dict[str, dict[str, Any]] = {
    "goldman-sachs": {
        "name": "Goldman Sachs",
        "aliases": ("goldman", "goldman sachs"),
        "description": "高盛公开卖方评级、目标价调整及其随后市场反应。",
    },
    "morgan-stanley": {
        "name": "Morgan Stanley",
        "aliases": ("morgan stanley",),
        "description": "摩根士丹利公开卖方评级、目标价调整及其随后市场反应。",
    },
    "jpmorgan": {
        "name": "JPMorgan",
        "aliases": ("jpmorgan", "j.p. morgan", "jp morgan"),
        "description": "摩根大通公开卖方评级、目标价调整及其随后市场反应。",
    },
    "bank-of-america": {
        "name": "Bank of America",
        "aliases": ("bank of america", "bofa", "b of a securities"),
        "description": "美国银行公开卖方评级、目标价调整及其随后市场反应。",
    },
    "citi": {
        "name": "Citi",
        "aliases": ("citi", "citigroup"),
        "description": "花旗公开卖方评级、目标价调整及其随后市场反应。",
    },
    "ubs": {
        "name": "UBS",
        "aliases": ("ubs",),
        "description": "瑞银公开卖方评级、目标价调整及其随后市场反应。",
    },
    "deutsche-bank": {
        "name": "Deutsche Bank",
        "aliases": ("deutsche bank",),
        "description": "德意志银行公开卖方评级、目标价调整及其随后市场反应。",
    },
    "barclays": {
        "name": "Barclays",
        "aliases": ("barclays",),
        "description": "巴克莱公开卖方评级、目标价调整及其随后市场反应。",
    },
    "wells-fargo": {
        "name": "Wells Fargo",
        "aliases": ("wells fargo", "wells fargo & company"),
        "description": "富国银行公开卖方评级、目标价调整及其随后市场反应。",
    },
    "hsbc": {
        "name": "HSBC",
        "aliases": ("hsbc", "hsbc holdings plc"),
        "description": "汇丰公开卖方评级、目标价调整及其随后市场反应。",
    },
    "nomura": {
        "name": "Nomura",
        "aliases": ("nomura",),
        "description": "野村公开卖方评级、目标价调整及其随后市场反应。",
    },
    "jefferies": {
        "name": "Jefferies",
        "aliases": ("jefferies",),
        "description": "杰富瑞公开卖方评级、目标价调整及其随后市场反应。",
    },
    "evercore-isi": {
        "name": "Evercore ISI",
        "aliases": ("evercore isi", "evercore isi group", "evercore"),
        "description": "Evercore ISI 公开卖方评级、目标价调整及其随后市场反应。",
    },
    "piper-sandler": {
        "name": "Piper Sandler",
        "aliases": ("piper sandler",),
        "description": "Piper Sandler 公开卖方评级、目标价调整及其随后市场反应。",
    },
    "raymond-james": {
        "name": "Raymond James",
        "aliases": ("raymond james",),
        "description": "Raymond James 公开卖方评级、目标价调整及其随后市场反应。",
    },
    "bernstein": {
        "name": "Bernstein",
        "aliases": ("bernstein", "bernstein research", "sanford c. bernstein"),
        "description": "Bernstein 公开卖方评级、目标价调整及其随后市场反应。",
    },
}


_ALIAS_TO_SLUG = {
    alias.casefold(): slug
    for slug, profile in TRACKED_INSTITUTIONS.items()
    for alias in profile["aliases"]
}

_POSITIVE_RATINGS = (
    "strong buy",
    "conviction buy",
    "overweight",
    "outperform",
    "market outperform",
    "positive",
    "buy",
)
_NEGATIVE_RATINGS = (
    "strong sell",
    "underweight",
    "underperform",
    "reduce",
    "negative",
    "sell",
)

ACTION_LABELS = {
    "upgrade": "上调评级",
    "downgrade": "下调评级",
    "initiate": "首次覆盖",
    "reiterate": "重申评级",
    "price_target_raise": "上调目标价",
    "price_target_cut": "下调目标价",
}


def institution_slug(name: Any) -> str:
    """Return the allow-listed institution slug, or an empty string."""
    normalized = " ".join(str(name or "").strip().casefold().split())
    if not normalized:
        return ""
    direct = _ALIAS_TO_SLUG.get(normalized)
    if direct:
        return direct
    # Accept common suffixes without allowing arbitrary programmatic pages.
    for alias, slug in sorted(_ALIAS_TO_SLUG.items(), key=lambda item: len(item[0]), reverse=True):
        if normalized.startswith(alias + " "):
            suffix = normalized[len(alias):].strip()
            if suffix in {"research", "securities", "group", "capital markets"}:
                return slug
    return ""


def canonical_institution(name: Any) -> str:
    slug = institution_slug(name)
    return TRACKED_INSTITUTIONS[slug]["name"] if slug else ""


def _rating_stance(rating: Any) -> str:
    normalized = " ".join(str(rating or "").strip().casefold().replace("_", " ").split())
    if any(token in normalized for token in _POSITIVE_RATINGS):
        return "bullish"
    if any(token in normalized for token in _NEGATIVE_RATINGS):
        return "bearish"
    return "neutral"


def classify_stance(event: dict[str, Any]) -> str:
    """Classify the direction of the *published action*, not hidden intent."""
    action = str(event.get("Action") or "").strip().casefold()
    if action in {"upgrade", "price_target_raise"}:
        return "bullish"
    if action in {"downgrade", "price_target_cut"}:
        return "bearish"
    return _rating_stance(event.get("New_Rating"))


def _date(value: Any) -> str:
    raw = str(value or "").strip()
    for candidate in (raw[:10], raw[:8]):
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return ""


def _float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:
        return None
    return number


def _history_rows(raw: Any) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    if isinstance(raw, dict):
        iterable: Iterable[Any] = ({"date": key, "close": value} for key, value in raw.items())
    elif isinstance(raw, list):
        iterable = raw
    else:
        iterable = ()
    seen: dict[str, float] = {}
    for item in iterable:
        if not isinstance(item, dict):
            continue
        day = _date(item.get("date") or item.get("Date"))
        close = _float(item.get("close") if "close" in item else item.get("Close"))
        if day and close is not None:
            seen[day] = close
    rows.extend(sorted(seen.items()))
    return rows


def _horizon_return(
    rows: list[tuple[str, float]], event_date: str, sessions: int
) -> Optional[dict[str, Any]]:
    before = [row for row in rows if row[0] < event_date]
    after = [row for row in rows if row[0] >= event_date]
    if not before or len(after) < sessions:
        return None
    base_date, base_close = before[-1]
    end_date, end_close = after[sessions - 1]
    return {
        "base_date": base_date,
        "base_close": round(base_close, 4),
        "end_date": end_date,
        "end_close": round(end_close, 4),
        "return": round(end_close / base_close - 1, 6),
    }


def _event_reaction(
    event: dict[str, Any],
    market_history: dict[str, Any],
    benchmark: str,
) -> dict[str, Any]:
    event_date = _date(event.get("Time") or event.get("_First_Seen"))
    symbol = str(event.get("Symbol") or "").strip().upper()
    stance = classify_stance(event)
    stock_rows = _history_rows(market_history.get(symbol))
    benchmark_rows = _history_rows(market_history.get(benchmark))
    returns: dict[str, Optional[float]] = {}
    excess_returns: dict[str, Optional[float]] = {}
    windows: dict[str, dict[str, Any]] = {}

    for sessions in (1, 5, 20):
        key = f"{sessions}d"
        stock_result = _horizon_return(stock_rows, event_date, sessions)
        benchmark_result = _horizon_return(benchmark_rows, event_date, sessions)
        returns[key] = stock_result["return"] if stock_result else None
        if stock_result:
            windows[key] = {
                "base_date": stock_result["base_date"],
                "end_date": stock_result["end_date"],
            }
        excess_returns[key] = (
            round(
                (1 + stock_result["return"]) / (1 + benchmark_result["return"]) - 1,
                6,
            )
            if stock_result and benchmark_result
            else None
        )

    evaluated_horizon = ""
    evaluated_return: Optional[float] = None
    for key in ("20d", "5d", "1d"):
        candidate = excess_returns.get(key)
        if candidate is not None:
            evaluated_horizon = key
            evaluated_return = candidate
            break

    alignment = "pending"
    if stance == "neutral" and evaluated_return is not None:
        alignment = "neutral"
    elif evaluated_return is not None:
        directional = 1 if stance == "bullish" else -1
        signed = directional * evaluated_return
        if signed >= 0.01:
            alignment = "aligned"
        elif signed <= -0.01:
            alignment = "diverged"
        else:
            alignment = "mixed"

    labels = {
        "aligned": "市场随后同向",
        "diverged": "公开观点未获市场确认",
        "mixed": "市场反应不明确",
        "neutral": "中性动作",
        "pending": "等待足够交易日",
    }
    return {
        "returns": returns,
        "excess_returns": excess_returns,
        "windows": windows,
        "evaluated_horizon": evaluated_horizon,
        "alignment": alignment,
        "alignment_label": labels[alignment],
    }


def _tone(bullish: int, bearish: int) -> tuple[str, float]:
    directional = bullish + bearish
    if not directional:
        return "中性", 0.0
    score = (bullish - bearish) / directional
    if score >= 0.2:
        return "偏多", round(score, 3)
    if score <= -0.2:
        return "偏空", round(score, 3)
    return "分歧", round(score, 3)


def _interpret_institution(
    tone: str,
    evaluated: int,
    aligned: int,
    diverged: int,
    *,
    symbols: Optional[list[str]] = None,
    median_signed_excess_5d: Optional[float] = None,
) -> str:
    """给单个机构写一句带具体数字的结论。

    早先所有落在 35%~65% 一致率区间的机构共用同一句模板文案，一页上会出现
    十几张卡片说完全相同的话——对读者没信息量，对 GEO 是纯重复内容。
    现在把一致率、样本数、覆盖标的与 5 日中位超额收益写进句子里。超额收益按
    看多/看空方向取过符号，否则一家多空混做的机构会出现「71% 同向但超额
    收益 -6%」这种自相矛盾的句子。
    """
    coverage = ""
    if symbols:
        head = "、".join(symbols[:3])
        coverage = (
            f"覆盖 {head} 等 {len(symbols)} 只标的；" if len(symbols) > 3 else f"覆盖 {head}；"
        )
    if not evaluated:
        return f"{coverage}已有公开观点记录，尚缺足够交易日数据验证后续反应。"
    decisive = aligned + diverged
    if not decisive:
        return f"{coverage}公开观点后的相对走势幅度较小，{evaluated} 个样本均未形成明确验证。"

    rate = aligned / decisive
    excess = ""
    if median_signed_excess_5d is not None:
        excess = f"，5 日按方向计的相对 SPY 超额收益中位数 {median_signed_excess_5d:+.1%}"
    stat = f"{decisive} 个可判定样本中 {aligned} 次同向（{rate:.0%}）{excess}"
    if rate >= 0.65:
        return f"{coverage}公开观点{tone}，{stat}，方向与随后相对走势较一致。"
    if rate <= 0.35:
        stance = "偏多" if tone == "偏多" else "偏谨慎" if tone == "偏空" else tone
        return f"{coverage}公开观点{stance}，{stat}，多次未获随后价格确认。"
    return f"{coverage}公开观点{tone}，{stat}，验证结果分化。"


def build_institution_intelligence(
    rating_changes: Iterable[dict[str, Any]],
    market_history: Optional[dict[str, Any]] = None,
    *,
    benchmark: str = "SPY",
    as_of: str = "",
    universe: Optional[Iterable[str]] = None,
    max_events: int = 240,
) -> dict[str, Any]:
    """Aggregate public views and compare them with later market performance.

    ``market_history`` is ``{symbol: [{date, close}, ...]}``.  Returns are
    measured from the close immediately before the event to the close after
    1/5/20 trading sessions.  Relative returns subtract the same-window
    benchmark return.
    """
    market_history = market_history if isinstance(market_history, dict) else {}
    allowed_symbols = {str(item).strip().upper() for item in universe or [] if str(item).strip()}
    deduped: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    for raw in rating_changes or []:
        if not isinstance(raw, dict):
            continue
        slug = institution_slug(raw.get("Bank"))
        symbol = str(raw.get("Symbol") or "").strip().upper()
        action = str(raw.get("Action") or "").strip().casefold()
        event_date = _date(raw.get("Time") or raw.get("_First_Seen"))
        if not slug or not symbol or not action or not event_date:
            continue
        if allowed_symbols and symbol not in allowed_symbols:
            continue
        key = (slug, symbol, action, event_date)
        current = deduped.get(key)
        completeness = sum(
            bool(raw.get(field))
            for field in ("New_Rating", "Old_Rating", "Price_Target", "_Headline")
        )
        if current is None or completeness > current[0]:
            deduped[key] = (completeness, dict(raw))

    events: list[dict[str, Any]] = []
    for _, raw in deduped.values():
        slug = institution_slug(raw.get("Bank"))
        institution = TRACKED_INSTITUTIONS[slug]["name"]
        action = str(raw.get("Action") or "").strip().casefold()
        stance = classify_stance(raw)
        event = {
            "institution": institution,
            "institution_slug": slug,
            "symbol": str(raw.get("Symbol") or "").strip().upper(),
            "date": _date(raw.get("Time") or raw.get("_First_Seen")),
            "action": action,
            "action_label": ACTION_LABELS.get(action, action.replace("_", " ")),
            "new_rating": raw.get("New_Rating"),
            "old_rating": raw.get("Old_Rating"),
            "price_target": raw.get("Price_Target"),
            "previous_price_target": raw.get("_Old_Price_Target"),
            "stance": stance,
            "stance_label": {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"}[stance],
            "headline": raw.get("_Headline") or "",
            "source": raw.get("_Source") or "",
            "source_url": raw.get("_Source_URL") or "",
        }
        event["reaction"] = _event_reaction(raw, market_history, benchmark)
        event["session_precision"] = "date_only"
        event["confound_flags"] = []
        events.append(event)

    events.sort(
        key=lambda item: (item["date"], item["institution"], item["symbol"]),
        reverse=True,
    )
    event_density: dict[tuple[str, str], int] = {}
    for event in events:
        density_key = (event["symbol"], event["date"])
        event_density[density_key] = event_density.get(density_key, 0) + 1
    for event in events:
        if event_density[(event["symbol"], event["date"])] > 1:
            event["confound_flags"].append("multiple_same_day_calls")

    groups: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        groups.setdefault(event["institution_slug"], []).append(event)

    institutions: list[dict[str, Any]] = []
    for slug, items in groups.items():
        bullish = sum(item["stance"] == "bullish" for item in items)
        bearish = sum(item["stance"] == "bearish" for item in items)
        neutral = len(items) - bullish - bearish
        tone, tone_score = _tone(bullish, bearish)
        aligned = sum(item["reaction"]["alignment"] == "aligned" for item in items)
        diverged = sum(item["reaction"]["alignment"] == "diverged" for item in items)
        mixed = sum(item["reaction"]["alignment"] == "mixed" for item in items)
        evaluated = aligned + diverged + mixed
        decisive = aligned + diverged
        alignment_rate = round(aligned / decisive, 3) if decisive else None
        excess_5d = [
            item["reaction"]["excess_returns"].get("5d")
            for item in items
            if item["reaction"]["excess_returns"].get("5d") is not None
        ]
        signed_excess_5d = [
            item["reaction"]["excess_returns"]["5d"]
            * (1 if item["stance"] == "bullish" else -1)
            for item in items
            if item["reaction"]["excess_returns"].get("5d") is not None
            and item["stance"] in ("bullish", "bearish")
        ]
        confidence = "高" if evaluated >= 10 else "中" if evaluated >= 5 else "低"
        institutions.append(
            {
                "slug": slug,
                "name": TRACKED_INSTITUTIONS[slug]["name"],
                "description": TRACKED_INSTITUTIONS[slug]["description"],
                "event_count": len(items),
                "bullish": bullish,
                "bearish": bearish,
                "neutral": neutral,
                "tone": tone,
                "tone_score": tone_score,
                "symbols": sorted({item["symbol"] for item in items}),
                "latest_date": items[0]["date"],
                "market_evaluated": evaluated,
                "market_aligned": aligned,
                "market_diverged": diverged,
                "market_mixed": mixed,
                "alignment_rate": alignment_rate,
                "median_excess_5d": round(median(excess_5d), 6) if excess_5d else None,
                "median_signed_excess_5d": (
                    round(median(signed_excess_5d), 6) if signed_excess_5d else None
                ),
                "confidence": confidence,
                "interpretation": _interpret_institution(
                    tone,
                    evaluated,
                    aligned,
                    diverged,
                    symbols=sorted({item["symbol"] for item in items}),
                    median_signed_excess_5d=(
                        round(median(signed_excess_5d), 6) if signed_excess_5d else None
                    ),
                ),
                "latest_events": items[:8],
            }
        )

    institutions.sort(key=lambda item: (item["event_count"], item["latest_date"]), reverse=True)
    bullish_total = sum(item["stance"] == "bullish" for item in events)
    bearish_total = sum(item["stance"] == "bearish" for item in events)
    neutral_total = len(events) - bullish_total - bearish_total
    market_tone, market_score = _tone(bullish_total, bearish_total)
    evaluated_total = sum(item["market_evaluated"] for item in institutions)
    pending_total = sum(item["reaction"]["alignment"] == "pending" for item in events)
    neutral_reaction_total = sum(item["reaction"]["alignment"] == "neutral" for item in events)

    return {
        "version": "1.0",
        "as_of": as_of,
        "benchmark": benchmark,
        "coverage_note": "仅统计当前关注池内、可识别机构与日期的公开卖方动作。",
        "market_sentiment": {
            "label": market_tone,
            "score": market_score,
            "bullish": bullish_total,
            "bearish": bearish_total,
            "neutral": neutral_total,
            "event_count": len(events),
        },
        "institutions": institutions,
        "events": events[:max_events],
        "market_reaction_coverage": {
            "evaluated": evaluated_total,
            "neutral": neutral_reaction_total,
            "pending": pending_total,
            "total": len(events),
        },
        "methodology": {
            "stance": "评级上调/目标价上调记为偏多，下调记为偏空；重申或首次覆盖按公开评级方向分类。",
            "reaction": "从事件前一交易日收盘起，计算其后 1、5、20 个交易日收益，并优先减去 SPY 同期收益。",
            "alignment": "方向与相对收益同向且幅度至少 1% 记为市场确认，反向至少 1% 记为市场未确认；中性动作不强行判定方向。",
            "time_precision": "事件通常只有日期、没有盘前盘后时间，因此反应按日线近似；同一标的同日多机构动作会标记为混杂样本。",
            "boundary": "该结果只能衡量公开观点与随后价格反应，不能证明机构自营盘持仓、出货、抄底或主观意图。",
        },
    }
