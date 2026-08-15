"""Yahoo Finance 共享数据层（yfinance 封装）。

提供美股行情、新闻、基本面、财报日历、分析师评级。
带代理配置 + TTL 内存缓存 + 失败静默降级，供 collector / monitor 复用。

用法：
    from scripts.collectors.yahoo_provider import YahooProvider
    yp = YahooProvider()
    quote = yp.quote("AAPL")            # {price, change_pct, ...}
    news = yp.news("AAPL", limit=5)     # [{title, publisher, link, published}, ...]
    fund = yp.fundamentals("AAPL")      # {pe, market_cap, target_price, ...}
    cal = yp.earnings_calendar("AAPL")  # {earnings_date, eps_estimate, ...}
    recs = yp.recommendations("AAPL")   # {strong_buy, buy, hold, sell, strong_sell}
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Optional

import pandas as pd

PROXY = "http://127.0.0.1:1082"


def _safe_float(v) -> Optional[float]:
    try:
        import pandas as pd
        if v is None or (isinstance(v, float) and v != v) or pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def _setup_proxy():
    os.environ.setdefault("HTTP_PROXY", PROXY)
    os.environ.setdefault("HTTPS_PROXY", PROXY)


class _TTLCache:
    def __init__(self):
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: int):
        item = self._store.get(key)
        if item and (time.time() - item[0]) < ttl:
            return item[1]
        return None

    def set(self, key: str, value: Any):
        self._store[key] = (time.time(), value)


class YahooProvider:
    """Yahoo Finance 数据提供者（线程无关，按需实例化）。"""

    # 不同数据类型的缓存 TTL（秒）：新闻/基本面变化慢，行情变化快
    TTL_QUOTE = 300        # 5 min
    TTL_NEWS = 1800        # 30 min
    TTL_FUNDAMENTALS = 21600  # 6 h
    TTL_CALENDAR = 21600   # 6 h
    TTL_RECS = 21600       # 6 h

    def __init__(self):
        _setup_proxy()
        self._cache = _TTLCache()
        self._tickers: dict[str, Any] = {}

    def _ticker(self, symbol: str):
        if symbol not in self._tickers:
            import yfinance as yf
            self._tickers[symbol] = yf.Ticker(symbol)
        return self._tickers[symbol]

    # ── 行情 ──
    def quote(self, symbol: str) -> Optional[dict]:
        key = f"quote:{symbol}"
        cached = self._cache.get(key, self.TTL_QUOTE)
        if cached is not None:
            return cached
        try:
            fi = self._ticker(symbol).fast_info
            price = getattr(fi, "last_price", None)
            prev = getattr(fi, "previous_close", None)
            if price is None:
                return None
            out = {
                "symbol": symbol,
                "price": round(float(price), 2),
                "change_pct": round((price - prev) / prev, 4) if prev else None,
                "prev_close": round(float(prev), 2) if prev else None,
                "currency": getattr(fi, "currency", "USD"),
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._cache.set(key, out)
            return out
        except Exception:
            return None

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        return {s: q for s in symbols if (q := self.quote(s))}

    # ── 新闻 ──
    def news(self, symbol: str, limit: int = 5) -> list[dict]:
        key = f"news:{symbol}:{limit}"
        cached = self._cache.get(key, self.TTL_NEWS)
        if cached is not None:
            return cached
        try:
            raw = self._ticker(symbol).news or []
            out = []
            for n in raw[:limit]:
                c = n.get("content", n)
                title = c.get("title")
                if not title:
                    continue
                provider = c.get("provider") or {}
                link = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
                out.append({
                    "title": title,
                    "publisher": provider.get("displayName") or "Yahoo Finance",
                    "link": link,
                    "published": c.get("pubDate") or c.get("displayTime"),
                    "summary": (c.get("summary") or c.get("description") or "")[:240],
                })
            self._cache.set(key, out)
            return out
        except Exception:
            return []

    # ── 基本面 ──
    def fundamentals(self, symbol: str) -> Optional[dict]:
        key = f"fund:{symbol}"
        cached = self._cache.get(key, self.TTL_FUNDAMENTALS)
        if cached is not None:
            return cached
        try:
            info = self._ticker(symbol).info or {}
            if not info:
                return None
            out = {
                "symbol": symbol,
                "name": info.get("longName") or info.get("shortName"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "market_cap": info.get("marketCap"),
                "pe_trailing": info.get("trailingPE"),
                "pe_forward": info.get("forwardPE"),
                "price_to_book": info.get("priceToBook"),
                "week52_high": info.get("fiftyTwoWeekHigh"),
                "week52_low": info.get("fiftyTwoWeekLow"),
                "dividend_yield": info.get("dividendYield"),
                "recommendation": info.get("recommendationKey"),
                "analyst_count": info.get("numberOfAnalystOpinions"),
                "target_mean_price": info.get("targetMeanPrice"),
            }
            self._cache.set(key, out)
            return out
        except Exception:
            return None

    # ── 财报日历 ──
    def earnings_calendar(self, symbol: str) -> Optional[dict]:
        key = f"cal:{symbol}"
        cached = self._cache.get(key, self.TTL_CALENDAR)
        if cached is not None:
            return cached
        try:
            cal = self._ticker(symbol).calendar
            if not cal:
                return None
            earnings_date = cal.get("Earnings Date")
            if isinstance(earnings_date, list) and earnings_date:
                earnings_date = earnings_date[0].isoformat()
            out = {
                "symbol": symbol,
                "earnings_date": earnings_date,
                "eps_estimate": cal.get("Earnings Average"),
                "revenue_estimate": cal.get("Revenue Average"),
                "ex_dividend_date": (cal.get("Ex-Dividend Date") or "").isoformat() if cal.get("Ex-Dividend Date") else None,
            }
            self._cache.set(key, out)
            return out
        except Exception:
            return None

    # ── 分析师评级 ──
    def recommendations(self, symbol: str) -> Optional[dict]:
        key = f"recs:{symbol}"
        cached = self._cache.get(key, self.TTL_RECS)
        if cached is not None:
            return cached
        try:
            rec = self._ticker(symbol).recommendations
            if rec is None or rec.empty:
                return None
            latest = rec.iloc[0]
            out = {
                "symbol": symbol,
                "strong_buy": int(latest.get("strongBuy", 0)),
                "buy": int(latest.get("buy", 0)),
                "hold": int(latest.get("hold", 0)),
                "sell": int(latest.get("sell", 0)),
                "strong_sell": int(latest.get("strongSell", 0)),
            }
            total = sum(out[k] for k in ["strong_buy", "buy", "hold", "sell", "strong_sell"])
            out["total"] = total
            out["buy_ratio"] = round((out["strong_buy"] + out["buy"]) / total, 3) if total else None
            self._cache.set(key, out)
            return out
        except Exception:
            return None

    # ── 机构评级行动（投行上调/下调/首次覆盖/维持） ──
    def upgrades_downgrades(self, symbol: str, days: int = 90, limit: int = 8) -> list[dict]:
        """近 N 天机构评级行动：{firm, action, to_grade, from_grade, pt_current, pt_prior, date}"""
        key = f"updown:{symbol}:{days}"
        cached = self._cache.get(key, self.TTL_RECS)
        if cached is not None:
            return cached
        try:
            ud = self._ticker(symbol).upgrades_downgrades
            if ud is None or ud.empty:
                return []
            if ud.index.tz is not None:
                cutoff = pd.Timestamp.now(tz=ud.index.tz) - pd.Timedelta(days=days)
            else:
                cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
            recent = ud[ud.index >= cutoff]
            out = []
            for dt, row in recent.head(limit).iterrows():
                pt_cur = row.get("currentPriceTarget")
                pt_prior = row.get("priorPriceTarget")
                out.append({
                    "date": dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10],
                    "firm": row.get("Firm"),
                    "action": row.get("Action"),           # up / down / main / init / reit
                    "to_grade": row.get("ToGrade") or None,
                    "from_grade": row.get("FromGrade") or None,
                    "pt_current": float(pt_cur) if pd.notna(pt_cur) and pt_cur else None,
                    "pt_prior": float(pt_prior) if pd.notna(pt_prior) and pt_prior else None,
                })
            self._cache.set(key, out)
            return out
        except Exception:
            return []

    # ── 机构持仓（13F 大股东 + 合计持股比例） ──
    def institutional(self, symbol: str, top: int = 5) -> Optional[dict]:
        """{pct_institutions, pct_insiders, holders_count, top_holders: [{holder, pct_held, pct_change, value}]}"""
        key = f"inst:{symbol}:{top}"
        cached = self._cache.get(key, self.TTL_FUNDAMENTALS)
        if cached is not None:
            return cached
        try:
            t = self._ticker(symbol)
            out: dict[str, Any] = {"symbol": symbol}
            info = t.info or {}
            out["pct_institutions"] = _safe_float(info.get("heldPercentInstitutions"))
            out["pct_insiders"] = _safe_float(info.get("heldPercentInsiders"))
            ih = t.institutional_holders
            if ih is not None and not ih.empty:
                holders = []
                for _, row in ih.head(top).iterrows():
                    holders.append({
                        "holder": row.get("Holder"),
                        "pct_held": _safe_float(row.get("pctHeld")),
                        "pct_change": _safe_float(row.get("pctChange")),
                        "value": _safe_float(row.get("Value")),
                    })
                out["top_holders"] = holders
                out["holders_count"] = len(ih)
                increasing = sum(1 for h in holders if (h.get("pct_change") or 0) > 0.05)
                out["top_increasing"] = increasing
            if not out.get("top_holders") and out.get("pct_institutions") is None:
                return None
            self._cache.set(key, out)
            return out
        except Exception:
            return None


if __name__ == "__main__":
    import json
    yp = YahooProvider()
    print("QUOTE:", json.dumps(yp.quote("AAPL"), ensure_ascii=False))
    print("NEWS[0]:", json.dumps(yp.news("AAPL", 2)[0] if yp.news("AAPL", 2) else None, ensure_ascii=False)[:300])
    print("FUND:", json.dumps(yp.fundamentals("AAPL"), ensure_ascii=False)[:300])
    print("CAL:", json.dumps(yp.earnings_calendar("AAPL"), ensure_ascii=False))
    print("RECS:", json.dumps(yp.recommendations("AAPL"), ensure_ascii=False))
