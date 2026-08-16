#!/usr/bin/env python3
"""
外资投行研报数据获取模块 v2
聚焦: Goldman Sachs, Morgan Stanley, JPMorgan, BofA, Citi, UBS, Deutsche Bank, Barclays
排除: 中国本土投行 (中金、中信、华泰等)

v2 改进:
- JSON Mode: Kimi API 使用 response_format={"type":"json_object"}，彻底消除解析失败
- 智能限流: Finnhub 60 RPM 限流保护，Alpha Vantage 调用合并省配额
- 增量更新: 数据哈希对比，无变化时跳过 Kimi API 调用
- 历史趋势: 读取最近 7 天报告，生成投行情绪变化分析
- 降级恢复: Kimi 失败时自动复用前一天的 summary
"""
import argparse
import fcntl
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests
from email.utils import parsedate_to_datetime
from urllib3.exceptions import InsecureRequestWarning

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple, Callable

# Qwen fallback（阿里云百炼，国内直连）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from env_loader import load_dotenv, get_proxy, ssl_verify, request_proxy_modes

load_dotenv()

try:
    from llm_fallback import call_qwen as _call_qwen, is_quota_error as _is_quota_error
    _HAS_QWEN = True
except ImportError:
    _HAS_QWEN = False

# === 配置 ===
FINNHUB_TOKEN = os.environ.get("FINNHUB_API_KEY", "")
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
TWELVEDATA_KEY = os.environ.get("TWELVEDATA_API_KEY", "")
MARKETDATA_KEY = os.environ.get("MARKETDATA_API_KEY", "")
KIMI_API_KEY = os.environ.get("KIMI_API_KEY", "")
KIMI_URL = (
    os.environ.get("IB_RESEARCH_KIMI_URL", "").strip()
    or "https://api.kimi.com/coding/v1/messages"
)
# 长文本投行研报默认使用 K3；cron 可通过非敏感环境变量受控覆盖。
KIMI_MODEL = os.environ.get("IB_RESEARCH_KIMI_MODEL", "").strip() or "k3"
# 默认优先使用 Kimi（已配置 API key 且未显式关闭时）。Kimi 的 response_format=json_object
# 对结构化提取更稳定；OpenClaw fallback 仅作为额度/服务不可用时的备份。
IB_RESEARCH_USE_KIMI = os.environ.get(
    "IB_RESEARCH_USE_KIMI",
    "1" if KIMI_API_KEY else "0",
).strip().lower() not in {"0", "false", "no", "off"}
OPENCLAW_BIN = (
    os.environ.get("OPENCLAW_BIN", "").strip()
    or shutil.which("openclaw")
    or os.path.expanduser("~/.npm-global/bin/openclaw")
)
IB_RESEARCH_FALLBACK_MODEL = (
    os.environ.get("IB_RESEARCH_FALLBACK_MODEL", "").strip()
    or "openai/gpt-5.5"
)
OPENCLAW_NODE_DIR = os.environ.get("OPENCLAW_NODE_DIR", "").strip()
# 仅在显式配置 HTTPS_PROXY/HTTP_PROXY 时走代理；不再默认连本机 1082。
PROXY = get_proxy()
SSL_VERIFY = ssl_verify()
if not SSL_VERIFY:
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

CACHE_DIR = os.environ.get("IB_RESEARCH_CACHE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ib_research"))
os.makedirs(CACHE_DIR, exist_ok=True)

# 复用 TCP 连接的 requests Session（线程安全用于 GET/POST）
_http_session = requests.Session()
_http_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
})
_direct_http_session = requests.Session()
_direct_http_session.trust_env = False
_direct_http_session.headers.update(_http_session.headers)

# 关注的外资投行列表
TARGET_BANKS = [
    "Goldman Sachs", "Morgan Stanley", "JPMorgan", "J.P. Morgan",
    "Bank of America", "BofA", "Citigroup", "Citi",
    "UBS", "Deutsche Bank", "Barclays", "Wells Fargo",
    "Credit Suisse", "HSBC", "Nomura", "Jefferies",
    "Evercore ISI", "Piper Sandler", "Raymond James",
    "Bernstein", "Bernstein Research", "Sanford C. Bernstein",
]

# 关注的热门股票
# 监控股票池：以 AI 算力 + 存储/HBM + 半导体设备 + AI 网络为主线（primary）。
# 该列表是单一入口，Finnhub 评级 / Yahoo 回填 / Google·Finnhub 新闻 / AV 共识都跟随它。
# 移除了原偏防守/非 AI 的 NFLX/UBER/COIN 与宽基 ETF(ARKK/SPY/QQQ/IWM——指数与 ETF 无个股评级)。
HOT_SYMBOLS = [
    # ── AI 算力 / 半导体 ──
    "NVDA", "AMD", "AVGO", "MRVL", "INTC", "TSM", "ARM",
    # ── 存储 / HBM(NAND·DRAM 涨价主线)──
    "MU", "WDC", "STX", "SNDK",
    # ── 半导体设备 ──
    "ASML", "AMAT", "LRCX", "KLAC",
    # ── AI 网络 / 服务器 / 云基础设施 ──
    "ANET", "DELL", "CRWV", "VRT",
    # ── AI 平台 / 软件 / 云巨头 ──
    "MSFT", "GOOGL", "AMZN", "META", "ORCL", "IBM", "PLTR", "SNOW",
    # ── AI 应用 / 龙头 ──
    "AAPL", "TSLA", "CRM",
]
# 原池(备查):AAPL MSFT GOOGL AMZN META TSLA NVDA AMD INTC NFLX CRM ORCL IBM UBER COIN PLTR ARKK SPY QQQ IWM

STATE_FILE = os.path.join(CACHE_DIR, "fetch_state.json")
FETCH_LOCK_FILE = os.path.join(CACHE_DIR, ".fetch.lock")
RATING_LEDGER_FILE = os.path.join(CACHE_DIR, "rating_changes_ledger.json")
FAST_REFRESH_INTERVAL_MIN = 240
# 注：历史上曾有 IB_RESEARCH_FAST_AI_MIN_RATINGS / FAST_AI_MIN_RATING_ITEMS 用作
# “累计 N 条评级才分析”的门槛；该机制已移除，快速刷新固定每 4 小时分析一次。
FAST_AI_RETRY_INTERVAL_MIN = FAST_REFRESH_INTERVAL_MIN
FAST_BATCH_MAX_ANALYZED_KEYS = 2000
RSS_MAX_AGE_DAYS = 14
RATING_CHANGES_MAX_AGE_DAYS = 30
KIMI_SUMMARY_TIMEOUT_SECONDS = int(os.environ.get("KIMI_SUMMARY_TIMEOUT_SECONDS", "375"))

# 分析师评级关键词
RATING_KEYWORDS = [
    "upgrade", "downgrade", "price target", "overweight", "underweight",
    "reiterat", "initiat", "coverage initiated", "coverage", "outperform", "underperform",
    "raised to", "lowered to", "maintain", "boost", "cut", "lift", "slash",
]
HIGH_PRIORITY_KEYWORDS = ["upgrade", "downgrade", "price target", "reiterat", "initiat"]

# === 限流器 ===
class RateLimiter:
    """简单滑动窗口限流器（线程安全，供 Finnhub 并行抓取共用）"""
    def __init__(self, max_calls: int, window_seconds: int):
        self.max_calls = max_calls
        self.window = window_seconds
        self.calls: List[float] = []
        self._lock = threading.Lock()

    def acquire(self):
        """阻塞直到获得一个令牌"""
        with self._lock:
            now = time.time()
            cutoff = now - self.window
            self.calls = [t for t in self.calls if t > cutoff]
            if len(self.calls) >= self.max_calls:
                sleep_time = self.calls[0] - cutoff
                if sleep_time > 0:
                    print(f"     [RateLimit] 等待 {sleep_time:.1f}s...")
                    time.sleep(sleep_time)
                    cutoff = time.time() - self.window
                    self.calls = [t for t in self.calls if t > cutoff]
            self.calls.append(time.time())


# Finnhub 免费版: 60 calls/min
finnhub_limiter = RateLimiter(max_calls=55, window_seconds=60)


def _extract_kimi_text(result: dict) -> Optional[str]:
    """从 Kimi API 响应中提取文本内容（兼容 thinking + text 多块结构）"""
    if not isinstance(result, dict):
        return None
    if "content" in result and isinstance(result["content"], list):
        # kimi-for-coding 强制 thinking，text 块通常在 thinking 之后
        for item in result["content"]:
            if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                return item["text"]
        # 诊断：没有找到 text 块时打印结构摘要
        types = [item.get("type") if isinstance(item, dict) else type(item).__name__ for item in result["content"]]
        stop_reason = result.get("stop_reason")
        usage = result.get("usage", {})
        print(f"     [WARN] Kimi 响应无 text 块; content_types={types}, stop_reason={stop_reason}, usage={usage}")
    if "choices" in result and isinstance(result["choices"], list):
        try:
            return result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            pass
    return None


def _parse_kimi_json(content: str) -> Optional[Dict]:
    """从 Kimi / OpenClaw fallback 返回的文本中提取并解析 JSON。
    优先处理 markdown 代码块；若不存在，则通过花括号匹配提取第一个合法 JSON 对象。
    """
    if not content:
        return None

    # 1. markdown 代码块
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif "```" in content:
        content = content.split("```")[1].split("```")[0].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # 2. 从文本中定位第一个合法 JSON 对象（花括号匹配，跳过无效片段）
    start = content.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escape = False
        end = None
        for i, ch in enumerate(content[start:], start):
            if in_string:
                if escape:
                    escape = False
                    continue
                if ch == "\\":
                    escape = True
                    continue
                if ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            return None
        try:
            return json.loads(content[start:end + 1])
        except json.JSONDecodeError:
            start = content.find("{", start + 1)
    return None


def _http_request(
    method: str,
    url: str,
    payload: dict = None,
    headers: dict = None,
    as_text: bool = False,
    timeout: int = 30,
    retries: int = 3,
) -> Any:
    """统一 HTTP 请求：代理/直连双路、指数退避重试"""
    req_headers = dict(headers) if headers else {}

    last_err = None
    for attempt in range(retries):
        mode_err = None
        for proxies, mode in request_proxy_modes(PROXY):
            resp = None
            session = _http_session if proxies else _direct_http_session
            request_timeout = timeout
            if method.upper() == "POST":
                request_timeout = max(10, int(timeout * (0.8 if proxies else 0.2)))
            try:
                if method.upper() == "POST":
                    resp = session.post(
                        url, json=payload, headers=req_headers, proxies=proxies, timeout=request_timeout, verify=SSL_VERIFY
                    )
                else:
                    resp = session.get(
                        url, headers=req_headers, proxies=proxies, timeout=timeout, verify=SSL_VERIFY
                    )
                resp.raise_for_status()
                return resp.text if as_text else resp.json()
            except json.JSONDecodeError as e:
                mode_err = e
                print(f"     [WARN] {method} {mode} JSON decode failed: {e}")
                continue
            except Exception as e:
                mode_err = e
                if resp is not None:
                    print(f"     [WARN] {method} {mode} failed: status={resp.status_code} err={e} body={resp.text[:500]}")
                    if resp.status_code in (400, 401, 403, 404):
                        print(f"     [WARN] HTTP {resp.status_code} 不可重试，跳过该数据源")
                        return "" if as_text else {}
                else:
                    print(f"     [WARN] {method} {mode} failed: {e}")
                continue
        last_err = mode_err if mode_err else Exception("All modes failed")
        if attempt < retries - 1:
            wait = 2 ** attempt  # 指数退避: 1s, 2s, 4s
            print(f"     [Retry {attempt+1}/{retries}] {url[:60]}... 等待 {wait}s: {last_err}")
            time.sleep(wait)
    print(f"[WARN] HTTP {method} failed after {retries} retries: {url[:80]}... error={last_err}")
    return "" if as_text else {}


def _http_get(url: str, timeout: int = 30, retries: int = 3) -> dict:
    """带代理和重试的 HTTP GET（返回 JSON）"""
    return _http_request("GET", url, timeout=timeout, retries=retries)


def _http_get_text(url: str, timeout: int = 30, retries: int = 3) -> str:
    """带代理和重试的 HTTP GET（返回原始文本，用于 RSS 等非 JSON 接口）"""
    return _http_request("GET", url, as_text=True, timeout=timeout, retries=retries)


def _http_post_json(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    """带代理/直连双路重试的 HTTP POST（返回 JSON）"""
    return _http_request("POST", url, payload=payload, headers=headers, timeout=timeout, retries=1)


def _http_post_json_once(
    url: str, payload: dict, headers: dict, timeout: int = 120
) -> dict:
    """快速批次只走一次代理请求，避免超时后重复提交同一计费任务。"""
    proxies = {"http": PROXY, "https": PROXY} if PROXY else None
    session = _http_session if proxies else _direct_http_session
    try:
        response = session.post(
            url,
            json=payload,
            headers=dict(headers),
            proxies=proxies,
            timeout=timeout,
            verify=SSL_VERIFY,
        )
        response.raise_for_status()
        return response.json()
    except Exception as error:
        print(f"     [WARN] 快速批次单次 POST 失败: {error}")
        return {}


def _openclaw_runtime_env() -> Dict[str, str]:
    """为后台任务补齐 OpenClaw 所需的兼容 Node PATH。"""
    env = os.environ.copy()
    node_dirs = [
        OPENCLAW_NODE_DIR,
        "/opt/homebrew/opt/node/bin",
        "/usr/local/opt/node/bin",
        os.path.dirname(shutil.which("node") or ""),
    ]
    for node_dir in node_dirs:
        if not node_dir:
            continue
        node_bin = os.path.join(node_dir, "node")
        if os.path.isfile(node_bin) and os.access(node_bin, os.X_OK):
            current_path = env.get("PATH", "")
            env["PATH"] = (
                node_dir
                if not current_path
                else node_dir + os.pathsep + current_path
            )
            break
    return env


def _call_openclaw_analysis_fallback(prompt: str, timeout: int = 750) -> dict:
    """Kimi 额度/服务不可用时，通过 OpenClaw 的 data agent 调用备用模型。"""
    if not OPENCLAW_BIN or not os.path.exists(OPENCLAW_BIN):
        print("     [WARN] OpenClaw fallback 不可用：找不到 openclaw CLI")
        return {}

    prompt_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", prefix="ib_research_prompt_", suffix=".txt",
            delete=False,
        ) as handle:
            handle.write(prompt)
            prompt_path = handle.name

        run_token = re.sub(
            r"[^A-Za-z0-9-]+", "-",
            os.environ.get("IB_RESEARCH_RUN_ID", "") or str(int(time.time())),
        )[:32]
        command = [
            OPENCLAW_BIN,
            "agent",
            "--agent", "data",
            "--session-key", f"agent:data:ib-research-fallback-{run_token}",
            "--model", IB_RESEARCH_FALLBACK_MODEL,
            "--message-file", prompt_path,
            "--thinking", "low",
            "--json",
            "--timeout", str(timeout),
        ]
        completed = subprocess.run(
            command,
            cwd=os.path.dirname(__file__),
            env=_openclaw_runtime_env(),
            capture_output=True,
            text=True,
            timeout=timeout + 30,
            check=False,
        )
        if completed.returncode != 0:
            detail = " ".join((completed.stderr or completed.stdout or "").split())
            print(
                "     [WARN] OpenClaw fallback 调用失败: "
                f"exit={completed.returncode} {detail[:300]}"
            )
            return {}

        raw = completed.stdout.strip()
        try:
            response = json.loads(raw)
        except json.JSONDecodeError:
            json_start = raw.find("{")
            if json_start < 0:
                print("     [WARN] OpenClaw fallback 未返回 JSON")
                return {}
            try:
                response = json.loads(raw[json_start:])
            except json.JSONDecodeError as exc:
                print(f"     [WARN] OpenClaw fallback JSON 解析失败: {exc}")
                return {}

        payloads = response.get("result", {}).get("payloads", [])
        text = next(
            (
                item.get("text", "")
                for item in payloads
                if isinstance(item, dict) and item.get("text")
            ),
            "",
        )
        if response.get("status") != "ok" or not text:
            print(
                "     [WARN] OpenClaw fallback 响应不完整: "
                f"status={response.get('status')}"
            )
            return {}
        return {
            "choices": [{"message": {"content": text}}],
            "_provider": IB_RESEARCH_FALLBACK_MODEL,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"     [WARN] OpenClaw fallback 异常: {exc}")
        return {}
    finally:
        if prompt_path:
            try:
                os.unlink(prompt_path)
            except FileNotFoundError:
                pass


def _fetch_single_recommendation(sym: str) -> Optional[Dict]:
    """获取单只股票的推荐数据（用于并行）"""
    finnhub_limiter.acquire()
    url = f"https://finnhub.io/api/v1/stock/recommendation?symbol={sym}&token={FINNHUB_TOKEN}"
    data = _http_get(url)
    if data and isinstance(data, list) and len(data) > 0:
        latest = data[0]
        latest["symbol"] = sym
        return latest
    return None


def fetch_finnhub_recommendations(symbols: List[str]) -> List[Dict]:
    """从 Finnhub 获取分析师推荐数据（并行 + 限流）"""
    if not FINNHUB_TOKEN:
        print("[WARN] FINNHUB_API_KEY not set, skipping finnhub recommendations")
        return []

    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch_single_recommendation, sym): sym for sym in symbols}
        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)
    return results


def fetch_yahoo_enrichment(symbols: List[str]) -> Dict[str, Dict]:
    """用 Yahoo Finance 补充分析师评级 + 基本面（PE/目标价/52周区间）。

    与 Finnhub recommendations 互补：Finnhub 提供趋势，Yahoo 提供目标价和基本面。
    失败静默降级（返回部分结果），不阻塞主流程。
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from collectors.yahoo_provider import YahooProvider
    except Exception as e:
        print(f"     [WARN] Yahoo provider 不可用: {e}")
        return {}

    yp = YahooProvider()
    out: Dict[str, Dict] = {}
    for sym in symbols:
        try:
            recs = yp.recommendations(sym)
            fund = yp.fundamentals(sym)
            upgrades = yp.upgrades_downgrades(sym, days=90, limit=6)
            entry: Dict[str, Any] = {}
            if recs:
                entry["yahoo_recs"] = recs
            if fund:
                entry["yahoo_fundamentals"] = {
                    "pe_trailing": fund.get("pe_trailing"),
                    "target_mean_price": fund.get("target_mean_price"),
                    "recommendation": fund.get("recommendation"),
                    "week52_high": fund.get("week52_high"),
                    "week52_low": fund.get("week52_low"),
                    "analyst_count": fund.get("analyst_count"),
                }
            if upgrades:
                entry["yahoo_upgrades"] = upgrades
            if entry:
                out[sym] = entry
        except Exception:
            continue
    print(f"     Yahoo 增强: {len(out)}/{len(symbols)} 只获取到评级/基本面")
    return out


def _fetch_single_news(sym: str, from_date: str, to_date: str) -> List[Dict]:
    """获取单只股票的新闻（用于并行；去重在汇总阶段完成）"""
    finnhub_limiter.acquire()
    url = (f"https://finnhub.io/api/v1/company-news?symbol={sym}"
           f"&from={from_date}&to={to_date}&token={FINNHUB_TOKEN}")
    data = _http_get(url)
    if not data or not isinstance(data, list):
        return []

    local_results = []
    for news in data:
        headline = news.get("headline", "")
        summary = news.get("summary", "")
        text = f"{headline} {summary}"

        matched_bank = _match_bank(text)
        if not matched_bank:
            continue

        news_url = news.get("url", "")

        dt_unix = news.get("datetime", 0)
        time_published = ""
        if dt_unix:
            try:
                dt = datetime.fromtimestamp(dt_unix)
                time_published = dt.strftime("%Y%m%dT%H%M%S")
            except Exception:
                pass

        relevance = _rating_relevance_score(headline, summary)
        local_results.append({
            "bank": matched_bank,
            "title": headline,
            "summary": summary,
            "url": news_url,
            "source": news.get("source", ""),
            "time_published": time_published,
            "overall_sentiment": 0,
            "tickers": [{"ticker": sym}],
            "_relevance": relevance,
        })
    return local_results


def fetch_finnhub_news(symbols: List[str]) -> List[Dict]:
    """从 Finnhub 获取公司新闻，筛选含投行名称的标题（并行 + 限流）"""
    if not FINNHUB_TOKEN:
        return []

    from_date = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    to_date = datetime.now().strftime("%Y-%m-%d")
    results = []

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(_fetch_single_news, sym, from_date, to_date): sym
            for sym in symbols
        }
        for future in as_completed(futures):
            results.extend(future.result())

    deduped = []
    seen_urls = set()
    for item in results:
        news_url = item.get("url") or ""
        if news_url:
            if news_url in seen_urls:
                continue
            seen_urls.add(news_url)
        deduped.append(item)

    deduped.sort(key=lambda x: x.get("_relevance", 0), reverse=True)
    print(f"     Finnhub 新闻补充源: {len(deduped)} 条匹配")
    return deduped


def _rating_relevance_score(title: str, summary: str) -> int:
    """计算新闻与分析师评级的相关度分数"""
    text = f"{title} {summary}".lower()
    score = 0
    for kw in HIGH_PRIORITY_KEYWORDS:
        if kw in text:
            score += 3
    for kw in RATING_KEYWORDS:
        if kw in text:
            score += 1
    return score


def _match_bank(text: str) -> Optional[str]:
    """从文本中匹配目标投行名称"""
    text_lower = text.lower()
    for bank in TARGET_BANKS:
        if bank.lower() in text_lower:
            return bank
    return None


def fetch_alpha_vantage_news(topics: str = "technology,finance,financial_markets", limit: int = 1000) -> List[Dict]:
    """从 Alpha Vantage 获取新闻（合并主题，减少 API 调用次数）"""
    if not ALPHA_VANTAGE_KEY:
        return []

    url = (f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT"
           f"&topics={topics}&apikey={ALPHA_VANTAGE_KEY}&limit={limit}")
    data = _http_get(url, timeout=60)
    feed = data.get("feed", []) if isinstance(data, dict) else []
    print(f"     Alpha Vantage 返回 {len(feed)} 条新闻")

    results = []
    for item in feed:
        title = item.get("title", "")
        summary = item.get("summary", "")

        matched_bank = _match_bank(f"{title} {summary}")
        if not matched_bank:
            continue

        relevance = _rating_relevance_score(title, summary)
        results.append({
            "bank": matched_bank,
            "title": title,
            "summary": summary,
            "url": item.get("url", ""),
            "source": item.get("source", ""),
            "time_published": item.get("time_published", ""),
            "overall_sentiment": item.get("overall_sentiment_score", 0),
            "tickers": item.get("ticker_sentiment", []),
            "_relevance": relevance,
        })

    results.sort(key=lambda x: x.get("_relevance", 0), reverse=True)
    return results


def fetch_seeking_alpha_news(symbols: List[str]) -> List[Dict]:
    """从 Seeking Alpha 获取新闻（补充源，无需API key）"""
    results = []

    for sym in symbols[:5]:
        url = f"https://seekingalpha.com/api/v3/symbols/{sym}/news"
        data = _http_get(url, timeout=30)
        articles = data.get("data", []) if isinstance(data, dict) else []

        for article in articles:
            attrs = article.get("attributes", {})
            title = attrs.get("title", "")
            summary = attrs.get("summary", "") or ""

            matched_bank = _match_bank(f"{title} {summary}")
            if not matched_bank:
                continue

            relevance = _rating_relevance_score(title, summary)
            results.append({
                "bank": matched_bank,
                "title": title,
                "summary": summary,
                "url": f"https://seekingalpha.com{article.get('links', {}).get('self', '')}" if article.get('links') else "",
                "source": "Seeking Alpha",
                "time_published": attrs.get("publishOn", "").replace("-", "").replace(":", "").replace(" ", "T"),
                "overall_sentiment": 0,
                "tickers": [{"ticker": sym}],
                "_relevance": relevance,
            })

    results.sort(key=lambda x: x.get("_relevance", 0), reverse=True)
    print(f"     Seeking Alpha 新闻: {len(results)} 条匹配")
    return results


# === 已验证事件提取（regex ground-truth，与 LLM 无关） ===
# 匹配 Benzinga / TheFly 等标准格式：
#   "UBS Maintains Buy on Netflix, Lowers Price Target to $115"
#   "Evercore ISI Group Maintains Outperform on Netflix, Lowers Price Target to $100"
#   "HSBC downgrades IBM to Reduce"
#   "JPMorgan Upgrades Apple to Overweight"
_VERIFIED_ACTION_MAP = {
    "maintains": "reiterate",
    "reiterates": "reiterate",
    "reaffirms": "reiterate",
    "keeps": "reiterate",
    "upgrades": "upgrade",
    "upgrade": "upgrade",
    "upgraded": "upgrade",
    "downgrades": "downgrade",
    "downgrade": "downgrade",
    "downgraded": "downgrade",
    "initiates": "initiate",
    "initiated": "initiate",
    "initiates coverage on": "initiate",
    "initiated coverage on": "initiate",
    "assumes": "initiate",
    "raises": "price_target_raise",
    "lifts": "price_target_raise",
    "boosts": "price_target_raise",
    "lowers": "price_target_cut",
    "cuts": "price_target_cut",
    "slashes": "price_target_cut",
    "reduces": "price_target_cut",
    "adjusts": "price_target_cut",
    "tweaks": "price_target_cut",
}
_VERIFIED_ACTION_RE = "|".join(sorted(_VERIFIED_ACTION_MAP, key=len, reverse=True))
_VERIFIED_EVENT_PATTERN = re.compile(
    r"^(?P<bank>[A-Z][\w&.' ]{1,40}?)\s+(?P<action>" + _VERIFIED_ACTION_RE + r")(?P<rest>.*)$",
    re.IGNORECASE,
)
_VERIFIED_PT_RE = re.compile(
    r"(?:price target|pt)\s+(?:on\s+[\w&.'’\- ]+?\s+)?(?:(?:to|from)\s+\$?[\d,.]+\s+)*?(?:to\s+)?\$?(?P<pt>[\d,.]+)",
    re.IGNORECASE,
)
_VERIFIED_BANK_PREFIXES = (
    # 目标投行全称（TARGET_BANKS 无法覆盖 "Evercore ISI Group" 之类带后缀名称）
    "goldman", "morgan stanley", "jpmorgan", "j.p. morgan", "bank of america", "bofa",
    "citi", "ubs", "deutsche bank", "barclays", "wells fargo", "credit suisse", "hsbc",
    "nomura", "jefferies", "evercore", "piper sandler", "raymond james", "bernstein",
    "sanford", "mizuho", "keybanc", "wedbush", "truis", "stifel", "bmo", "rbc", "td cowen",
    "loop capital", "guggenheim", "oppenheimer", "cantor", "needham", "roth",
    "susquehanna", "wolf", "daiwa", "macquarie", "benchmark", "citizens",
)
_RATING_WORDS = {
    "buy", "sell", "hold", "neutral", "overweight", "underweight", "outperform",
    "underperform", "equal-weight", "equal weight", "market perform", "market outperform",
    "sector perform", "sector weight", "reduce", "strong buy", "conviction buy", "positive",
    "negative", "mixed", "in-line", "peer perform", "speculative buy", "moderate buy",
}
_PT_RAISE_WORDS = {"raises", "lifts", "boosts"}
_PT_CUT_WORDS = {"lowers", "cuts", "slashes", "reduces"}
# 全大写词但绝不是股票代码的（英文词 / 金融缩写）
_TICKER_BLOCKLIST = {
    "PT", "CEO", "CFO", "COO", "EPS", "IPO", "ETF", "AI", "US", "USA", "GDP", "CPI",
    "FED", "SEC", "DOJ", "FCC", "FDA", "THE", "AND", "FOR", "NEW", "TOP", "BEST",
    "WORST", "WHY", "HOW", "WHAT", "NOW", "SAYS", "AFTER", "AMID", "OVER", "WITH",
    "THIS", "THAT", "THEY", "THEIR", "STOCK", "STOCKS", "SHARE", "SHARES", "PRICE",
    "TARGET", "RATING", "RATINGS", "BUY", "SELL", "HOLD", "HERE", "INTO", "FROM",
    "JUST", "LIKE", "MORE", "MOST", "LESS", "THAN", "THEN", "WHEN", "WILL", "WOULD",
    "COULD", "SHOULD", "ABOUT", "STILL", "EVEN", "MUCH", "MANY", "SOME", "VERY",
}
_EXCHANGE_TICKER_RE = re.compile(r"\((?:NASDAQ|NYSE|NYSEAMERICAN|AMEX|TSX|LSE|HK)[:：]\s*([A-Z]{1,6})\)", re.IGNORECASE)
_PAREN_TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)\s*$")
_TRAILING_TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)")


def _resolve_event_symbol(item: Dict, company_name: str, title: str) -> str:
    """按可信度从高到低解析事件的股票代码；解析不出则返回空串（丢弃事件）。

    1. 新闻自带的 tickers 字段（Finnhub/AV 结构化数据）
    2. 标题中的交易所前缀 "(NASDAQ:NFLX)" / "(NYSE:IBM)"
    3. 公司名首词在原文中全大写（NVDA/RPM/PPG 这类真代码写法）
    4. 知名公司名前缀映射（netflix → NFLX、jpmorgan chase → JPM）
    """
    tickers = item.get("tickers") or []
    if tickers and isinstance(tickers, list):
        candidate = str(tickers[0].get("ticker", "")).upper()
        if 1 <= len(candidate) <= 5:
            return candidate

    m = _EXCHANGE_TICKER_RE.search(title)
    if m:
        return m.group(1).upper()

    # 公司名首词全大写（原文大小写，防止 "Their" 被当成 THEIR）
    raw_first = company_name.strip().split()[0] if company_name.strip() else ""
    raw_first = raw_first.strip(",;:")
    if (2 <= len(raw_first) <= 5
            and re.fullmatch(r"[A-Z][A-Z0-9.\-]{1,4}", raw_first)
            and raw_first not in _TICKER_BLOCKLIST):
        return raw_first

    # 知名公司名：最长前缀匹配（"JPMorgan Chase & Co. ..." → jpmorgan chase → JPM）
    cleaned = company_name.strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    best = ""
    for name, sym in _COMPANY_NAME_TO_SYMBOL.items():
        if cleaned.startswith(name) and len(name) > len(best):
            best = name
    if best:
        return _COMPANY_NAME_TO_SYMBOL[best]

    return ""

# 从新闻文本中误抓的全大写英文单词（不是真实股票代码）
_TICKER_BLOCKLIST = {
    "THE", "AND", "FOR", "NEW", "TOP", "BEST", "WORST", "WHY", "HOW", "WHAT",
    "THEIR", "THEM", "THEY", "THIS", "THAT", "WITH", "FROM", "INTO", "OVER",
    "PT", "EPS", "IPO", "CEO", "CFO", "AI", "US", "UK", "EU", "GDP", "CPI",
    "FED", "SEC", "ETF", "PE", "ROE",
}


def _extract_verified_rating_events(news: List[Dict]) -> List[Dict]:
    """用正则直接从新闻标题提取结构化评级事件，作为不依赖 LLM 的事实源。

    每条事件带 _Source="verified_headline"，用于与 LLM 提取结果交叉验证。
    只接受标题以已知投行名开头、且包含标准评级动词的条目。
    """
    events: Dict[Tuple[str, str, str, str], Dict] = {}
    today = datetime.now().strftime("%Y%m%d")
    for item in news or []:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        m = _VERIFIED_EVENT_PATTERN.match(title)
        if not m:
            continue
        bank_raw = m.group("bank").strip()
        if not any(bank_raw.lower().startswith(p) for p in _VERIFIED_BANK_PREFIXES):
            continue
        verb = m.group("action").lower()
        action = _VERIFIED_ACTION_MAP[verb]
        rest = (m.group("rest") or "").strip()

        # 评级：maintains Buy on X / downgrades X to Reduce
        # 摘要兜底："cut its price target to $191 from $231"、"raised its price target ... to $366 from $260"
        rating = None
        company_name = ""
        mr = re.match(r"^([\w\- ]+?)\s+on\s+(.+)$", rest, re.IGNORECASE)
        mt = re.match(r"^(.+?)\s+to\s+([\w\- ]+?)(?:[,.]|$)", rest, re.IGNORECASE)
        if verb in ("upgrades", "upgrade", "upgraded", "downgrades", "downgrade", "downgraded",
                    "initiates", "initiated", "assumes") and mt:
            company_name = mt.group(1).strip()
            candidate = mt.group(2).strip()
            rating = candidate if candidate.lower() in _RATING_WORDS else None
        elif mr:
            candidate = mr.group(1).strip()
            if candidate.lower() in _RATING_WORDS:
                rating = candidate
                company_name = mr.group(2).strip()
            else:
                # "Raises Price Target on Netflix ..." / "Adjusts Price Target on IBM to $250"
                rest2 = mr.group(2).strip()
                if candidate.lower() == "price target":
                    company_name = rest2
                else:
                    mo = re.match(r"^price target\s+on\s+(.+)$", rest2, re.IGNORECASE)
                    if mo:
                        company_name = mo.group(1).strip()
                    else:
                        company_name = rest

        # 目标价：支持 "Price Target to $115"、"PT $220"、"Price Target from $291 to $250"
        pt_match = _VERIFIED_PT_RE.search(title)
        pt = pt_match.group("pt") if pt_match else None
        direction = ""
        old_pt = None
        if not pt or True:
            ms = re.search(
                r"(?:raises?|raised|cuts?|cut|lowers?|lowered|lifts?|lifted|boosts?|boosted)\s+"
                r"(?:its\s+|the\s+|their\s+)?price target(?:\s+on\s+[\w&.'’\- ]+?)?\s+"
                r"to\s+\$?([\d,.]+)(?:\s+from\s+\$?([\d,.]+))?",
                str(item.get("summary") or ""), re.IGNORECASE,
            )
            if ms:
                if not pt:
                    pt = ms.group(1)
                old_pt = ms.group(2)
                if not direction:
                    direction = "raises" if re.match(r"(?i)raises?|raised|lifts?|lifted|boosts?|boosted", ms.group(0)) else "lowers"
        # 标题 "to $250 From $291" 也可以回填旧目标价
        if old_pt is None:
            mf2 = re.search(r"to\s+\$?[\d,.]+\s+from\s+\$?([\d,.]+)", title, re.IGNORECASE)
            if mf2:
                old_pt = mf2.group(1)

        # 明确的方向动词优先于动词本身（"Maintains Buy, Lowers PT" 记为 price_target_cut）
        # "to $250 From $291" 说明是下调；"to $300 From $250" 说明是上调
        if not direction:
            md = re.search(r"\b(raises|lowers|cuts|lifts|boosts|slashes)\s+(?:the\s+)?price target", title, re.IGNORECASE)
            if md:
                direction = md.group(1).lower()
        if direction and pt and action in ("reiterate",):
            action = "price_target_raise" if direction in _PT_RAISE_WORDS else "price_target_cut"
        elif action == "price_target_cut" and pt:
            # adjusts/动词默认 cut，但若匹配到 from→to 数值方向则纠正
            mf = re.search(r"to\s+\$?([\d,.]+)\s+from\s+\$?([\d,.]+)", title, re.IGNORECASE)
            if mf:
                new_pt, prev_pt = mf.group(1), mf.group(2)
                try:
                    if float(new_pt.replace(",", "")) > float(prev_pt.replace(",", "")):
                        action = "price_target_raise"
                except ValueError:
                    pass
        # upgrade/downgrade 动词遇到 PT 变化时保留评级动作，但方向信息也有价值

        # 从 tickers / 交易所前缀 / 原文全大写 / 公司名映射推导股票代码
        symbol = _resolve_event_symbol(item, company_name, title)
        if not symbol:
            continue

        ts = str(item.get("time_published", ""))[:8]
        event_date = ts if ts and ts <= today else today
        # 用 headline 里的投行名为准（比 RSS 分类更可靠）
        bank = _preferred_bank_name(_canonical_bank_name(bank_raw))
        key = (bank.casefold(), symbol, action, event_date)
        event = {
            "Bank": bank,
            "Symbol": symbol,
            "Action": action,
            "New_Rating": rating,
            "Old_Rating": None,
            "Price_Target": f"${pt}" if pt else None,
            "Time": datetime.strptime(event_date, "%Y%m%d").strftime("%Y-%m-%d"),
            "_Source": "verified_headline",
            "_Headline": title,
        }
        if old_pt:
            event["_Old_Price_Target"] = f"${old_pt}"
        existing = events.get(key)
        if existing is None or _rating_change_score(event) > _rating_change_score(existing):
            events[key] = event
    return list(events.values())


_PT_DIRECTION_WORDS = (
    "raises", "raised", "raise", "lowers", "lowered", "lower", "cuts", "cut",
    "lifts", "lifted", "lift", "boosts", "boosted", "boost", "slashes", "slashed",
    "hikes", "hiked", "reduces", "reduced", "doubles", "doubled",
)
_PT_DIRECTION_RE = "|".join(_PT_DIRECTION_WORDS)
# 通用「投行 + 方向动词 + 目标价」结构，覆盖比原窄版更多的措辞：
#   "Citi lowered its target to $235 from $400"
#   "Baird doubled its price target on AMD stock to $1,250"
#   "JPMorgan raises price target to $240 From $200"
_SUMMARY_PT_RE = re.compile(
    r"(?P<bank>[A-Z][\w&.'’\-]{1,40}?)\s+(?:analyst\s+[\w. ]+?\s+)?"
    r"(?P<direction>" + _PT_DIRECTION_RE + r")\s+(?:its\s+|the\s+|their\s+)?"
    r"(?:price\s+)?target(?:\s+on\s+(?P<on>[\w&.'’\- ]+?))?\s+to\s+\$(?P<new_pt>[\d,.]+?)(?=[^\d,.]|$)"
    r"(?:\s+from\s+\$(?P<old_pt>[\d,.]+?)(?=[^\d,.]|$))?",
    re.IGNORECASE,
)


def _clean_pt_digits(value: Optional[str]) -> Optional[str]:
    """把正则抓到的目标价数字收敛为干净字符串：去首尾逗号/点，空则 None。"""
    if value is None:
        return None
    s = str(value).strip().strip(",.")
    # 形如 1,250 / 235 / 13.5 / 9.00
    if not re.fullmatch(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?", s):
        return None
    return s
# 摘要首句里的「投行 maintains/reiterates ... rating」，用于给同摘要 PT 事件补评级。
_SUMMARY_RATING_RE = re.compile(
    r"(?P<bank>[A-Z][\w&.'’\- ]{1,40}?)\s+(?:analyst\s+[\w. ]+?\s+)?"
    r"(?P<verb>maintains|reiterates|reaffirms|keeps)\s+(?P<rest>[\w&.'’\-(),/ ]+?)"
    r"\s+(?:with\s+an?\s+|at\s+|to\s+)(?P<rating>[\w\- ]+?)(?:\s+rating|\s*,|\s+and\s|\.|$)",
    re.IGNORECASE,
)


def _extract_pt_events_from_summaries(news: List[Dict]) -> List[Dict]:
    """从 Benzinga/聚合新闻摘要提取「投行 X 把目标价调到 $N」结构（不依赖 LLM）。

    摘要正文格式规整，可补出标题正则抓不到的 old→new 目标价。事件带
    _Source="verified_summary"，与标题提取结果在账本里按 key 合并。
    """
    events: Dict[Tuple[str, str, str, str], Dict] = {}
    today = datetime.now().strftime("%Y%m%d")
    for item in news or []:
        text = (item.get("summary") or "").strip()
        if not text:
            continue
        title = item.get("title") or ""

        ts = str(item.get("time_published", ""))[:8]
        event_date = ts if ts and ts <= today else today

        # 摘要首句里的「投行维持/重申某评级」，给同摘要 PT 事件补评级用。
        rating_by_bank: Dict[str, str] = {}
        for rm in _SUMMARY_RATING_RE.finditer(text):
            rb = _canonical_bank_name(rm.group("bank").strip())
            rr = rm.group("rating").strip()
            if rr.lower() in _RATING_WORDS:
                rating_by_bank.setdefault(rb.casefold(), rr)

        for m in _SUMMARY_PT_RE.finditer(text):
            bank_raw = m.group("bank").strip()
            if not any(bank_raw.lower().startswith(p) for p in _VERIFIED_BANK_PREFIXES):
                continue
            new_pt = _clean_pt_digits(m.group("new_pt"))
            old_pt = _clean_pt_digits(m.group("old_pt"))
            if not new_pt:
                continue
            # 股票代码：优先本事件「target on X」片段，再退回整篇新闻的解析。
            on_text = (m.group("on") or "").strip()
            symbol = _resolve_event_symbol(item, on_text, on_text)
            if not symbol:
                symbol = _resolve_event_symbol(item, on_text or title, title)
            if not symbol:
                continue

            direction = m.group("direction").lower()
            if direction in _PT_RAISE_WORDS or direction in (
                "raise", "raised", "hike", "hiked", "hikes", "double", "doubled", "doubles",
            ):
                pt_action = "price_target_raise"
            elif direction in _PT_CUT_WORDS or direction in (
                "cut", "slash", "slashed", "slashes", "reduce", "reduced", "reduces",
            ):
                pt_action = "price_target_cut"
            elif old_pt:
                try:
                    pt_action = (
                        "price_target_raise"
                        if float(new_pt.replace(",", "")) > float(old_pt.replace(",", ""))
                        else "price_target_cut"
                    )
                except ValueError:
                    continue
            else:
                continue
            bank = _preferred_bank_name(_canonical_bank_name(bank_raw))
            rating = rating_by_bank.get(bank.casefold())
            key = (bank.casefold(), symbol, pt_action, event_date)
            event = {
                "Bank": bank,
                "Symbol": symbol,
                "Action": pt_action,
                "New_Rating": rating,
                "Old_Rating": None,
                "Price_Target": f"${new_pt}",
                "Time": datetime.strptime(event_date, "%Y%m%d").strftime("%Y-%m-%d"),
                "_Source": "verified_summary",
            }
            if old_pt:
                event["_Old_Price_Target"] = f"${old_pt}"
            existing = events.get(key)
            if existing is None or _rating_change_score(event) > _rating_change_score(existing):
                events[key] = event
    return list(events.values())


def _backfill_missing_price_targets(changes: List[Dict], verified_events: List[Dict]) -> int:
    """用标题/摘要正则验证事件，回填 LLM 事件中缺失的目标价/评级。

    LLM 抓的评级事件常因「软标题」（如 "UBS resets AMD stock target"）没有具体
    数字而缺 Price_Target；但同一条新闻的摘要里往往有 "lowered its target to $235"
    这类规整结构，已被 _extract_pt_events_from_summaries / _extract_verified_rating_events
    抓到。这里按 (投行, 股票, 动作) 匹配，把已验证的目标价/评级回填进缺字段的事件。

    只填「空」字段，绝不覆盖 LLM 已给出的值；返回回填的字段数。
    """
    # 索引已验证事件：broad_key(投行, 股票, 动作) -> 事件
    by_broad: Dict[Tuple[str, str, str], Dict] = {}
    for e in verified_events or []:
        if not isinstance(e, dict):
            continue
        bank = _canonical_bank_name(e.get("Bank"))
        symbol = str(e.get("Symbol", "") or "").strip().upper()
        action = str(e.get("Action", "") or "").strip().lower()
        if not bank or not symbol or not action:
            continue
        if not (_clean_field(e.get("Price_Target")) or _clean_field(e.get("New_Rating"))):
            continue
        key = (bank.casefold(), symbol, action)
        current = by_broad.get(key)
        if current is None or _rating_change_score(e) > _rating_change_score(current):
            by_broad[key] = e

    filled = 0
    for c in changes or []:
        if not isinstance(c, dict):
            continue
        # 只补真正缺目标价的事件；评级缺失也一并尝试。
        need_pt = not _clean_field(c.get("Price_Target"))
        need_rating = not _clean_field(c.get("New_Rating"))
        if not (need_pt or need_rating):
            continue
        bank = _canonical_bank_name(c.get("Bank"))
        symbol = str(c.get("Symbol", "") or "").strip().upper()
        action = str(c.get("Action", "") or "").strip().lower()
        if not bank or not symbol:
            continue
        # 优先同动作匹配；PT 类动作（raise/cut）方向不同也允许，交给数值方向兜底。
        match = by_broad.get((bank.casefold(), symbol, action))
        if match is None and action in ("price_target_raise", "price_target_cut"):
            for alt in ("price_target_raise", "price_target_cut"):
                match = by_broad.get((bank.casefold(), symbol, alt))
                if match:
                    break
        if match is None:
            continue
        if need_pt and _clean_field(match.get("Price_Target")):
            c["Price_Target"] = _format_price_target(_clean_field(match["Price_Target"]))
            filled += 1
            # 若标题/摘要里有旧目标价，顺带存下（账本合并与展示都会用）。
            if match.get("_Old_Price_Target") and not c.get("_Old_Price_Target"):
                c["_Old_Price_Target"] = match["_Old_Price_Target"]
        if need_rating and _clean_field(match.get("New_Rating")):
            c["New_Rating"] = _clean_field(match["New_Rating"])
            filled += 1
        if filled:
            c.setdefault("_Quality_Flags", [])
            if "pt_backfilled" not in c["_Quality_Flags"]:
                c["_Quality_Flags"] = sorted(set(c["_Quality_Flags"]) | {"pt_backfilled"})
    return filled


def _backfill_old_ratings(changes: List[Dict]) -> int:
    """用同一「投行 + 股票」在本批事件里更早的评级，回填旧评级 / 顺带补新评级。

    数据源几乎不直接发布「旧评级」，OLD 栏因此长期偏空，看起来像是抓取失败。
    但 30 天账本里同一投行对同一股票往往有更早一条事件，其评级就是本次变动前
    的评级。这里按 (bank, symbol) 分组、按时间升序处理每条：
      1. Old_Rating 空 → 回填为上一条的评级（上一条缺 New_Rating 时沿用其 Old_Rating
         以保持链条）；与本次 New_Rating 相同则不留（避免 reiterate 出现无意义配对）。
      2. New_Rating 仍为空、且 Old_Rating 是刚推断出的延续评级 → 把它顺到 New_Rating，
         因为投行只是调价没改评级，避免出现「OLD 有值 / NEW 空」的怪行。
    只填空、不覆盖已给出的值。返回回填的字段总数（含旧评级与新评级）。
    """
    # (bank, symbol) -> 按时间升序的事件列表
    groups: Dict[Tuple[str, str], List[Dict]] = {}
    for c in changes or []:
        if not isinstance(c, dict):
            continue
        bank = _canonical_bank_name(c.get("Bank"))
        symbol = str(c.get("Symbol", "") or "").strip().upper()
        if not bank or not symbol:
            continue
        groups.setdefault((bank.casefold(), symbol), []).append(c)

    filled_old = 0
    filled_new = 0
    for group in groups.values():
        group.sort(key=lambda x: _extract_change_time(x))
        prev_rating: Optional[str] = None
        for c in group:
            new_rating = _clean_field(c.get("New_Rating"))
            old_rating = _clean_field(c.get("Old_Rating"))
            flags = set(c.get("_Quality_Flags", []) or [])
            inferred_old = False
            # 1) 回填旧评级：上一条事件的评级即本次变动前的评级。
            if old_rating is None and prev_rating and prev_rating != new_rating:
                c["Old_Rating"] = prev_rating
                old_rating = prev_rating
                inferred_old = True
                filled_old += 1
                flags.add("old_rating_backfilled")
            # 2) 新评级为空时：若旧评级是从账本推断出的延续评级，说明投行只是调价
            #    没改评级，把它顺到新评级，避免出现「OLD 有值 / NEW 空」的怪行。
            if new_rating is None and inferred_old and old_rating:
                c["New_Rating"] = old_rating
                new_rating = old_rating
                filled_new += 1
                flags.add("new_rating_inferred")
            if flags:
                c["_Quality_Flags"] = sorted(flags)
            # 沿链推进：本条的“旧”优先用真实/回填的 Old_Rating，否则用 New_Rating。
            prev_rating = old_rating or new_rating or prev_rating
    return filled_old + filled_new


_YAHOO_ACTION_MAP = {
    "up": "upgrade",
    "down": "downgrade",
    "init": "initiate",
    "reit": "reiterate",
    "main": "reiterate",
}


def _yahoo_pt_change_action(u: Dict) -> str:
    """由 Yahoo 目标价 prior→current 推断调价方向；无变化/缺值回退到动作映射。"""
    cur, prior = u.get("pt_current"), u.get("pt_prior")
    if cur and prior:
        if cur > prior:
            return "price_target_raise"
        if cur < prior:
            return "price_target_cut"
    return _YAHOO_ACTION_MAP.get(str(u.get("action", "")).lower(), "reiterate")


def _fetch_yahoo_rating_events(symbols: List[str], days: int = 30, limit: int = 10) -> List[Dict]:
    """把 Yahoo upgrades_downgrades 转成标准评级事件（_Source="yahoo"）。

    Yahoo 的 Firm/from_grade/to_grade/pt_current/pt_prior 字段齐全，是补全评级表
    OLD/NEW/TARGET 三栏最干净的来源（还能覆盖 Finnhub 评级接口给不到旧评级的盲区）。
    仅返回 HOT_SYMBOLS（美股）；韩股等 Yahoo 无评级数据的代码会拿到空列表自然跳过。
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from collectors.yahoo_provider import YahooProvider
    except Exception as exc:
        print(f"     [WARN] Yahoo provider 不可用，跳过 Yahoo 评级回填: {exc}")
        return []

    yp = YahooProvider()
    events: List[Dict] = []
    for sym in symbols or []:
        sym = str(sym or "").strip().upper()
        if not sym or "." in sym:  # 跳过非纯美股代码（韩股 000660.KS 之类）
            continue
        try:
            rows = yp.upgrades_downgrades(sym, days=days, limit=limit)
        except Exception:
            continue
        for u in rows or []:
            if not isinstance(u, dict):
                continue
            firm = _canonical_bank_name(u.get("firm"))
            if not firm:
                continue
            date = str(u.get("date") or "")[:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                continue
            to_grade = _canonical_rating(u.get("to_grade"))
            from_grade = _canonical_rating(u.get("from_grade"))
            pt_cur = u.get("pt_current")
            pt_prior = u.get("pt_prior")
            # 评级没变化时（reiterate/纯调价），Yahoo 的 from_grade==to_grade，把它也
            # 作为旧评级带进来，让 OLD→NEW 能配上对（如 Overweight→Overweight）。
            event = {
                "Bank": _preferred_bank_name(firm),
                "Symbol": sym,
                "Action": _yahoo_pt_change_action(u),
                "New_Rating": to_grade,
                "Old_Rating": from_grade,
                "Price_Target": f"${pt_cur:g}" if pt_cur else None,
                "Time": date,
                "_Source": "yahoo",
            }
            if pt_prior and pt_prior != pt_cur:
                event["_Old_Price_Target"] = f"${pt_prior:g}"
            events.append(event)
    return events


def _backfill_from_yahoo(changes: List[Dict], symbols: List[str]) -> int:
    """用 Yahoo 评级事件回填评级表中缺失的 OLD/NEW/TARGET（只填空、不覆盖）。

    先按 (bank, symbol, action, date) 精确回填到对应事件；Yahoo 里账本没有的新事件
    追加进列表（它们自带完整 from→to 评级与目标价），交给后续账本合并去重。
    """
    yahoo_events = _fetch_yahoo_rating_events(symbols or HOT_SYMBOLS)
    if not yahoo_events:
        return 0

    # 精确索引：key -> 事件
    def _key(bank, symbol, action, date):
        return (str(bank).casefold(), str(symbol).upper(), str(action).lower(), date)

    by_exact: Dict[Tuple[str, str, str, str], Dict] = {}
    for e in yahoo_events:
        date = _extract_change_time(e)
        if not date:
            continue
        date = datetime.strptime(date, "%Y%m%d").strftime("%Y-%m-%d")
        k = _key(_canonical_bank_name(e["Bank"]), e["Symbol"], e["Action"], date)
        cur = by_exact.get(k)
        if cur is None or _rating_change_score(e) > _rating_change_score(cur):
            by_exact[k] = e

    matched_keys = set()
    filled = 0
    for c in changes or []:
        if not isinstance(c, dict):
            continue
        date = _extract_change_time(c)
        if not date:
            continue
        date = datetime.strptime(date, "%Y%m%d").strftime("%Y-%m-%d")
        bank = _canonical_bank_name(c.get("Bank"))
        symbol = str(c.get("Symbol", "") or "").strip().upper()
        action = str(c.get("Action", "") or "").strip().lower()
        if not bank or not symbol:
            continue
        # 先同动作精确匹配；调价类允许 raise/cut 互换（方向以 Yahoo 数值为准）。
        match = by_exact.get(_key(bank, symbol, action, date))
        if match is None and action in ("price_target_raise", "price_target_cut", "reiterate"):
            for alt in ("price_target_raise", "price_target_cut", "reiterate"):
                m2 = by_exact.get(_key(bank, symbol, alt, date))
                if m2:
                    match = m2
                    break
        if match is None:
            continue
        matched_keys.add(_key(_canonical_bank_name(match["Bank"]), match["Symbol"], match["Action"], date))
        flags = set(c.get("_Quality_Flags", []) or [])
        before = filled
        if not _clean_field(c.get("Old_Rating")) and _clean_field(match.get("Old_Rating")):
            c["Old_Rating"] = _clean_field(match["Old_Rating"])
            filled += 1
        if not _clean_field(c.get("New_Rating")) and _clean_field(match.get("New_Rating")):
            c["New_Rating"] = _clean_field(match["New_Rating"])
            filled += 1
        if not _clean_field(c.get("Price_Target")) and _clean_field(match.get("Price_Target")):
            c["Price_Target"] = _clean_field(match["Price_Target"])
            filled += 1
        if match.get("_Old_Price_Target") and not c.get("_Old_Price_Target"):
            c["_Old_Price_Target"] = match["_Old_Price_Target"]
        if filled > before:
            flags.add("yahoo_backfilled")
            c["_Quality_Flags"] = sorted(flags)

    # Yahoo 里账本没有的新事件：直接补进来（多为 Yahoo 独有的调价记录）。
    existing_keys = set()
    for c in changes or []:
        if not isinstance(c, dict):
            continue
        date = _extract_change_time(c)
        if not date:
            continue
        date = datetime.strptime(date, "%Y%m%d").strftime("%Y-%m-%d")
        existing_keys.add(_key(_canonical_bank_name(c.get("Bank")), str(c.get("Symbol","")).upper(), str(c.get("Action","")).lower(), date))
    appended = 0
    for k, e in by_exact.items():
        if k in existing_keys or k in matched_keys:
            continue
        ne = dict(e)
        ne["_Quality_Flags"] = ["yahoo_backfilled"]
        changes.append(ne)
        existing_keys.add(k)
        appended += 1
    if appended:
        print(f"     Yahoo 补充新评级事件: {appended} 条")
    return filled


_COMPANY_NAME_TO_SYMBOL = {
    "netflix": "NFLX", "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA",
    "tesla": "TSLA", "amazon": "AMZN", "alphabet": "GOOGL", "google": "GOOGL",
    "meta": "META", "meta platforms": "META", "intel": "INTC", "amd": "AMD",
    "advanced micro devices": "AMD", "ibm": "IBM", "international business machines": "IBM",
    "oracle": "ORCL", "salesforce": "CRM", "uber": "UBER", "coinbase": "COIN",
    "palantir": "PLTR", "micron": "MU", "broadcom": "AVGO", "qualcomm": "QCOM",
    "texas instruments": "TXN", "servicenow": "NOW", "adobe": "ADBE", "shopify": "SHOP",
    "snowflake": "SNOW", "crowdstrike": "CRWD", "datadog": "DDOG", "mongodb": "MDB",
    "spotify": "SPOT", "disney": "DIS", "boeing": "BA", "caterpillar": "CAT",
    "exxon": "XOM", "chevron": "CVX", "jpmorgan": "JPM", "visa": "V", "mastercard": "MA",
    "paypal": "PYPL", "square": "XYZ", "block": "XYZ", "robinhood": "HOOD",
    "tsmc": "TSM", "taiwan semiconductor": "TSM", "asml": "ASML", "arm": "ARM",
    "super micro": "SMCI", "dell": "DELL", "hp": "HPQ", "cisco": "CSCO",
    "western digital": "WDC", "seagate": "STX", "sandisk": "SNDK", "marvell": "MRVL",
    "arista": "ANET", "arista networks": "ANET", "applied materials": "AMAT",
    "lam research": "LRCX", "kla": "KLAC", "kla corp": "KLAC",
    "coreweave": "CRWV", "circle": "CRCL", "reddit": "RDDT", "ast spacemobile": "ASTS",
    "oklo": "OKLO", "nuscale": "SMR", "ge vernova": "GEV", "vertiv": "VRT",
    "eli lilly": "LLY", "novo nordisk": "NVO", "unitedhealth": "UNH", "nike": "NKE",
    "starbucks": "SBUX", "mcdonald": "MCD", "walmart": "WMT", "costco": "COST",
    "home depot": "HD", "lockheed": "LMT", "rtx": "RTX", "northrop": "NOC",
}


def _safe_int(value: Any, default: int = 0) -> int:
    """容错整数解析：Alpha Vantage 在缺数据时会返回 '-' / 'None' / 空串等，
    直接 int() 会抛 ValueError 进而中断整次完整刷新。无法解析时回退 default。"""
    if value is None:
        return default
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", "-", "--", "n/a", "na"):
        return default
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def fetch_alpha_vantage_overviews(symbols: List[str]) -> Dict[str, Dict]:
    """Alpha Vantage OVERVIEW：官方分析师共识（目标价均值 + 评级分布）。

    免费版限 25 calls/day，因此只抓当日报告中出现过目标价的股票，
    每次完整刷新最多 20 只，并在函数内做当日磁盘缓存。
    单个符号失败（瞬时限流）只跳过该符号，缓存已有的部分。
    """
    if not ALPHA_VANTAGE_KEY:
        return {}
    cache_path = os.path.join(CACHE_DIR, f"av_overview_{datetime.now().strftime('%Y-%m-%d')}.json")
    cached: Dict[str, Dict] = {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    overviews: Dict[str, Dict] = dict(cached)
    fetched = 0
    for sym in symbols[:20]:
        if sym in overviews and overviews[sym].get("analyst_target_price"):
            continue
        if fetched >= 20:
            break
        url = (f"https://www.alphavantage.co/query?function=OVERVIEW"
               f"&symbol={urllib.parse.quote(sym)}&apikey={ALPHA_VANTAGE_KEY}")
        data = _http_get(url, timeout=30, retries=2)
        fetched += 1
        if not isinstance(data, dict) or not data.get("Symbol"):
            # 限流瞬时失败时跳过该符号但保留已抓到的数据
            continue
        overviews[str(data["Symbol"]).upper()] = {
            "symbol": str(data["Symbol"]).upper(),
            "analyst_target_price": _clean_field(data.get("AnalystTargetPrice")),
            "strong_buy": _safe_int(data.get("AnalystRatingStrongBuy")),
            "buy": _safe_int(data.get("AnalystRatingBuy")),
            "hold": _safe_int(data.get("AnalystRatingHold")),
            "sell": _safe_int(data.get("AnalystRatingSell")),
            "strong_sell": _safe_int(data.get("AnalystRatingStrongSell")),
            "week_52_high": _clean_field(data.get("52WeekHigh")),
            "week_52_low": _clean_field(data.get("52WeekLow")),
            "pe_ratio": _clean_field(data.get("PERatio")),
            "eps": _clean_field(data.get("EPS")),
            "market_cap": _clean_field(data.get("MarketCapitalization")),
        }
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(overviews, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"     [WARN] AV overview 缓存写入失败: {e}")
    print(f"     Alpha Vantage 共识数据: {len(overviews)} 只")
    return overviews


def _parse_price(value: Any) -> Optional[float]:
    """从 '$115', '1,150.5', '115' 等格式解析数字价格。"""
    if value is None:
        return None
    s = str(value).strip().replace("$", "").replace(",", "").rstrip("kK")
    try:
        num = float(s)
    except ValueError:
        return None
    if str(value).strip().lower().endswith("k"):
        num *= 1000
    return num if num > 0 else None


def _build_verified_asset_targets(
    llm_targets: List[Dict],
    verified_events: List[Dict],
    overviews: Dict[str, Dict],
    news: Optional[List[Dict]] = None,
) -> List[Dict]:
    """重建资产目标卡片：每只股票一张，以已验证数据为准。

    优先级：
    1. AV 官方共识目标价（_Source="av_consensus"）
    2. 正则提取的最新投行目标价（_Source="verified_headline"）
    3. LLM 目标价仅在能与时价/共识交叉验证（≤2x）时保留（_Source="llm_inferred"）
    """
    # 每只股票取最新一条 verified headline 事件
    verified_by_symbol: Dict[str, Dict] = {}
    for event in sorted(verified_events, key=lambda e: str(e.get("Time", "")), reverse=True):
        if event.get("Price_Target"):
            verified_by_symbol.setdefault(event["Symbol"], event)

    current_price_by_symbol: Dict[str, Optional[float]] = {}
    for sym, ov in overviews.items():
        current_price_by_symbol[sym] = _parse_price(ov.get("analyst_target_price"))

    merged: Dict[str, Dict] = {}
    for item in llm_targets or []:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("Asset") or "").strip().upper()
        if not sym:
            continue
        llm_price = _parse_price(item.get("Target_Price"))
        overview = overviews.get(sym)
        consensus_price = _parse_price((overview or {}).get("analyst_target_price"))
        verified_event = verified_by_symbol.get(sym)
        verified_price = _parse_price((verified_event or {}).get("Price_Target"))

        # 交叉验证：LLM 价格与共识/已验证价格偏差超过 2x 视为幻觉，丢弃
        reference_price = consensus_price or verified_price
        llm_plausible = (
            llm_price is not None
            and (reference_price is None or 0.5 <= llm_price / reference_price <= 2.0)
        )

        # LLM 推断的卡片要求当日原始新闻里确实提到该股票，防止历史幻觉沉淀
        sym_mentioned = True
        if news:
            sym_mentioned = any(
                sym in f"{n.get('title','')} {n.get('summary','')}"
                or any(str(t.get("ticker", "")).upper() == sym for t in (n.get("tickers") or []) if isinstance(t, dict))
                for n in news
            )

        target = {
            "Asset": sym,
            "Target_Price": None,
            "Timeframe": _clean_field(item.get("Timeframe")),
            "Technical_Levels": item.get("Technical_Levels") or [],
            "_Source": None,
            "_Consensus": None,
            "_Verified_PT": None,
            "_Verified_Bank": None,
        }

        if consensus_price is not None:
            target["Target_Price"] = f"${consensus_price:g}"
            target["_Source"] = "av_consensus"
            target["_Consensus"] = overview
        elif verified_price is not None:
            target["Target_Price"] = f"${verified_price:g}"
            target["_Source"] = "verified_headline"
        elif llm_plausible and sym_mentioned and llm_price is not None:
            target["Target_Price"] = f"${llm_price:g}"
            target["_Source"] = "llm_inferred"

        if verified_event is not None:
            target["_Verified_PT"] = verified_event.get("Price_Target")
            target["_Verified_Bank"] = verified_event.get("Bank")

        # 三个来源都没有任何价格信息时，卡片无意义
        if target["Target_Price"] is None and target["_Verified_PT"] is None:
            continue
        # 同一只股票保留信息最完整的一张卡
        existing = merged.get(sym)
        if existing is None or (target["_Source"] == "av_consensus" and existing["_Source"] != "av_consensus"):
            merged[sym] = target

    # verified headline 中出现但 LLM 完全没提到的股票，也补一张卡
    for sym, event in verified_by_symbol.items():
        if sym in merged:
            continue
        overview = overviews.get(sym)
        consensus_price = _parse_price((overview or {}).get("analyst_target_price"))
        merged[sym] = {
            "Asset": sym,
            "Target_Price": f"${consensus_price:g}" if consensus_price else event.get("Price_Target"),
            "Timeframe": None,
            "Technical_Levels": [],
            "_Source": "av_consensus" if consensus_price else "verified_headline",
            "_Consensus": overview,
            "_Verified_PT": event.get("Price_Target"),
            "_Verified_Bank": event.get("Bank"),
        }

    result = list(merged.values())
    result.sort(key=lambda t: (0 if t["_Source"] == "av_consensus" else 1, t["Asset"]))
    return result


def _llm_symbol_drift_issues(
    llm_targets: List[Dict],
    verified_events: List[Dict],
    overviews: Dict[str, Dict],
) -> List[str]:
    """质检：LLM 目标价与已验证价格偏差 >2x 时给出告警（不阻断）。"""
    issues = []
    verified_pt = {e["Symbol"]: _parse_price(e.get("Price_Target")) for e in verified_events if e.get("Price_Target")}
    consensus_pt = {s: _parse_price(o.get("analyst_target_price")) for s, o in overviews.items()}
    for item in llm_targets or []:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("Asset") or "").upper()
        llm_price = _parse_price(item.get("Target_Price"))
        if not sym or llm_price is None:
            continue
        ref = consensus_pt.get(sym) or verified_pt.get(sym)
        if ref and not (0.5 <= llm_price / ref <= 2.0):
            issues.append(
                f"LLM 目标价疑似幻觉: {sym} LLM=${llm_price:g} vs 已验证≈${ref:g}"
            )
    return issues


def fetch_reddit_discussions() -> List[Dict]:
    """从 Reddit 获取热门讨论（本地缓存）"""
    results = []
    reddit_files = [
        os.path.expanduser("~/.openclaw/workspace/reddit_top_posts.json"),
    ]

    for rf in reddit_files:
        if not os.path.exists(rf):
            continue
        try:
            with open(rf, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 支持两种格式：list 直接是 posts，或 dict 有 data.children
            if isinstance(data, list):
                posts = data
            elif isinstance(data, dict):
                posts = data.get("data", {}).get("children", [])
                posts = [p.get("data", p) for p in posts]
            else:
                posts = []

            for post_data in posts:
                if not isinstance(post_data, dict):
                    continue
                title = post_data.get("title", "")
                text = f"{title} {post_data.get('selftext', '')}".lower()

                # 检查是否涉及目标股票
                mentioned_symbols = []
                for sym in HOT_SYMBOLS:
                    if sym.lower() in text or f"${sym.lower()}" in text:
                        mentioned_symbols.append(sym)

                if mentioned_symbols:
                    results.append({
                        "bank": "Reddit社区",
                        "title": title,
                        "summary": post_data.get("selftext", "")[:300] if post_data.get("selftext") else "",
                        "url": post_data.get("url", ""),
                        "source": "Reddit",
                        "time_published": datetime.now().strftime("%Y%m%dT%H%M%S"),
                        "overall_sentiment": 0,
                        "tickers": [{"ticker": s} for s in mentioned_symbols],
                        "_relevance": 1,
                    })
        except Exception as e:
            print(f"     [WARN] Reddit 读取失败: {e}")

    print(f"     Reddit 讨论: {len(results)} 条匹配")
    return results


def summarize_reddit_with_kimi(posts: List[Dict]) -> Dict[str, Any]:
    """使用 Kimi API 总结 Reddit 讨论，提取重要观点和情绪分析"""
    if not posts:
        return {"summary": "暂无 Reddit 数据", "sentiment": "neutral", "key_points": []}

    # 构建输入文本
    posts_text = "\n\n".join([
        f"[{i+1}] {p.get('title', '')}\n{p.get('summary', '')[:200]}"
        for i, p in enumerate(posts[:15])
    ])

    prompt = f"""你是一位社交媒体情绪分析师。请分析以下 Reddit 讨论，提取重要观点和情绪。

## Reddit 讨论内容
{posts_text}

## 分析要求
1. **重要观点总结**：用3-5句话总结讨论中最值得关注的投资观点
2. **情绪分析**：判断整体情绪（看多/看空/中性），并说明原因
3. **热门股票**：列出被讨论最多的股票及原因
4. **风险提示**：指出讨论中提到的潜在风险

## 输出格式
严格输出如下JSON：
{{
  "summary": "重要观点总结（3-5句话）",
  "sentiment": "bullish/bearish/neutral",
  "sentiment_score": 0.7,
  "key_points": ["观点1", "观点2", "观点3"],
  "hot_stocks": ["AAPL: 原因", "NVDA: 原因"],
  "risks": ["风险1", "风险2"]
}}
"""

    headers = {
        "Authorization": f"Bearer {KIMI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": KIMI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 6000,
        "response_format": {"type": "json_object"},
    }

    result = {}
    if IB_RESEARCH_USE_KIMI and KIMI_API_KEY:
        result = _http_post_json(KIMI_URL, payload, headers, timeout=60)
    if result:
        try:
            content = _extract_kimi_text(result)
            if content is None:
                print(f"     [WARN] Reddit 总结响应结构异常: {str(result)[:200]}")
            else:
                parsed = _parse_kimi_json(content)
                if parsed:
                    return {
                        "summary": parsed.get("summary", "无法生成总结"),
                        "sentiment": parsed.get("sentiment", "neutral"),
                        "sentiment_score": parsed.get("sentiment_score", 0.5),
                        "key_points": parsed.get("key_points", []),
                        "hot_stocks": parsed.get("hot_stocks", []),
                        "risks": parsed.get("risks", []),
                    }
        except Exception as e:
            print(f"     [WARN] Reddit 总结解析失败: {e}")

    # Kimi 失败时尝试 Qwen fallback
    if _HAS_QWEN:
        try:
            print("     [Reddit] Kimi 不可用，切换 Qwen...")
            qwen_text, _ = _call_qwen(
                [{"role": "user", "content": prompt}],
                max_tokens=6000, temperature=0.3, timeout=60,
            )
            parsed_q = _parse_kimi_json(qwen_text)
            if parsed_q:
                return {
                    "summary": parsed_q.get("summary", "无法生成总结"),
                    "sentiment": parsed_q.get("sentiment", "neutral"),
                    "sentiment_score": parsed_q.get("sentiment_score", 0.5),
                    "key_points": parsed_q.get("key_points", []),
                    "hot_stocks": parsed_q.get("hot_stocks", []),
                    "risks": parsed_q.get("risks", []),
                    "_provider": "qwen",
                }
        except Exception as qe:
            print(f"     [WARN] Reddit Qwen fallback 也失败: {qe}")

    return {"summary": "分析服务暂时不可用", "sentiment": "neutral", "key_points": []}


def fetch_google_news_rss(symbols: List[str]) -> List[Dict]:
    """从 Google News RSS 获取新闻（免费，无需API key）"""
    results = []

    # 构建查询：投行名称 + 股票代码
    query = " OR ".join([f'"{b}"' for b in TARGET_BANKS[:6]])
    query += " OR " + " OR ".join(symbols[:5])
    query += ' (upgrade OR downgrade OR "price target" OR rating)'

    url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    data = _http_get_text(url, timeout=30)

    if not data:
        print("     Google News RSS: 0 条")
        return results

    try:
        root = ET.fromstring(data)
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            description = item.findtext("description", "")

            matched_bank = _match_bank(f"{title} {description}")
            if not matched_bank:
                continue

            relevance = _rating_relevance_score(title, description)
            pub_date = item.findtext("pubDate", "")
            # 解析 RSS date
            time_published = ""
            try:
                dt = datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %Z")
                time_published = dt.strftime("%Y%m%dT%H%M%S")
            except ValueError:
                pass

            results.append({
                "bank": matched_bank,
                "title": title,
                "summary": description[:300] if description else "",
                "url": item.findtext("link", ""),
                "source": item.findtext("source", "Google News"),
                "time_published": time_published,
                "overall_sentiment": 0,
                "tickers": [],
                "_relevance": relevance,
            })
    except Exception as e:
        print(f"     [WARN] Google News 解析失败: {e}")

    results.sort(key=lambda x: x.get("_relevance", 0), reverse=True)
    print(f"     Google News RSS: {len(results)} 条匹配")
    return results


def _strip_html(raw: str) -> str:
    """移除 HTML 标签"""
    if not raw:
        return ""
    return re.sub(r"<[^>]+>", "", raw)


def _parse_rss_date(pub_date: str) -> Optional[str]:
    """将 RSS pubDate 转为 YYYYMMDDTHHMMSS"""
    if not pub_date:
        return ""
    try:
        dt = parsedate_to_datetime(pub_date)
        return dt.strftime("%Y%m%dT%H%M%S")
    except Exception:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(pub_date, fmt).strftime("%Y%m%dT%H%M%S")
        except ValueError:
            continue
    return ""


def _classify_news(title: str, summary: str, source_label: str) -> Optional[Tuple[str, int]]:
    """判断新闻是否与分析师评级相关，返回 (bank_or_source, relevance)"""
    text = f"{title} {summary}"
    bank = _match_bank(text)
    relevance = _rating_relevance_score(title, summary)
    text_lower = text.lower()
    has_symbol = any(
        sym.lower() in text_lower or f"${sym.lower()}" in text_lower
        for sym in HOT_SYMBOLS
    )

    # SemiAnalysis: 半导体行业权威独立研究，只要提到关注标的就纳入
    if "semianalysis" in source_label.lower():
        if has_symbol:
            return "SemiAnalysis", 4
        return None

    if bank:
        # Bernstein 在半导体/科技领域被视为顶级卖方，显著加权
        if "bernstein" in bank.lower():
            return bank, relevance + 6
        return bank, relevance + 2

    if has_symbol:
        has_rating = any(kw in text_lower for kw in RATING_KEYWORDS)
        if has_rating:
            return source_label, max(relevance, 1)
    return None


def _parse_rss_items(xml_text: str, label: str, max_items: int = 100) -> List[Dict]:
    """通用 RSS 解析器"""
    results = []
    if not xml_text:
        return results
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        print(f"     [WARN] {label} RSS XML 解析失败: {e}")
        return results

    count = 0
    for item in root.iter("item"):
        if count >= max_items:
            break
        title = ""
        description = ""
        link = ""
        pub_date = ""
        source = label
        for child in item:
            tag = child.tag.split("}")[-1]
            text = (child.text or "").strip()
            if tag == "title":
                title = text
            elif tag == "description":
                description = _strip_html(text)
            elif tag == "link":
                link = text
            elif tag == "pubDate":
                pub_date = text
            elif tag == "source":
                source = text or label
        classification = _classify_news(title, description, source)
        if not classification:
            continue
        bank, relevance = classification
        count += 1
        results.append({
            "bank": bank,
            "title": title,
            "summary": description[:400],
            "url": link,
            "source": source,
            "time_published": _parse_rss_date(pub_date),
            "overall_sentiment": 0,
            "tickers": [],
            "_relevance": relevance,
        })
    return results


def _fetch_rss_feed(url: str, label: str, max_items: int = 100) -> List[Dict]:
    """获取并解析单个 RSS 源"""
    print(f"     获取 {label} ...")
    xml_text = _http_get_text(url, timeout=25, retries=2)
    if not xml_text:
        print(f"     [WARN] {label} 返回空")
        return []
    items = _parse_rss_items(xml_text, label, max_items)
    print(f"     {label}: {len(items)} 条匹配")
    return items


def fetch_yahoo_finance_rss(symbols: List[str] = None, max_items: int = 200) -> List[Dict]:
    """Yahoo Finance 股票头条 RSS（按 symbol）"""
    symbols = symbols or HOT_SYMBOLS
    query = ",".join(symbols)
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={urllib.parse.quote(query)}"
    return _fetch_rss_feed(url, "Yahoo Finance", max_items)


def fetch_seeking_alpha_rss(max_items: int = 100) -> List[Dict]:
    """Seeking Alpha 主 RSS 流"""
    return _fetch_rss_feed("https://seekingalpha.com/feed.xml", "Seeking Alpha RSS", max_items)


def fetch_benzinga_rss(max_items: int = 100) -> List[Dict]:
    """Benzinga 主 RSS 流"""
    return _fetch_rss_feed("https://www.benzinga.com/feed", "Benzinga", max_items)


def fetch_barrons_rss(max_items: int = 100) -> List[Dict]:
    """Barron's 最新新闻 RSS"""
    return _fetch_rss_feed("https://www.barrons.com/news/latest-news?format=rss", "Barron's", max_items)


def fetch_marketwatch_rss(max_items: int = 100) -> List[Dict]:
    """MarketWatch 头条 RSS"""
    return _fetch_rss_feed("https://www.marketwatch.com/rss/topstories", "MarketWatch", max_items)


def fetch_zacks_rss(max_items: int = 80) -> List[Dict]:
    """Zacks 升级/降级 RSS"""
    results = []
    for type_code, label in [("2", "Zacks Upgrades"), ("3", "Zacks Downgrades")]:
        url = f"https://www.zacks.com/rss/market-commentary.php?type={type_code}"
        results.extend(_fetch_rss_feed(url, label, max_items // 2))
    return results


def fetch_semianalysis_rss(max_items: int = 60) -> List[Dict]:
    """SemiAnalysis 半导体独立研究 RSS"""
    return _fetch_rss_feed("https://semianalysis.com/feed/", "SemiAnalysis", max_items)


def fetch_streetinsider_rss(max_items: int = 100) -> List[Dict]:
    """StreetInsider 分析师评级 RSS"""
    return _fetch_rss_feed(
        "https://www.streetinsider.com/rss/rss.php?feed=Analyst+Ratings",
        "StreetInsider",
        max_items,
    )


def _compute_data_hash(recommendations: List[Dict], news: List[Dict]) -> str:
    """计算数据指纹，用于增量更新检测"""
    # 纳入来源、发布时间与摘要片段，避免标题相同但内容已更新时漏分析。
    rec_sig = sorted([
        f"{r.get('symbol')}:{r.get('buy',0)}:{r.get('hold',0)}:{r.get('sell',0)}"
        for r in recommendations
    ])
    news_sig = sorted([
        ":".join([
            str(n.get("bank", "")),
            str(n.get("time_published", "")),
            str(n.get("url", "")),
            str(n.get("title", "")),
            str(n.get("summary", ""))[:240],
        ])
        for n in news
    ])
    combined = json.dumps({"rec": rec_sig, "news": news_sig}, ensure_ascii=False)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]


def _analysis_payload_error(summary: Any) -> Optional[str]:
    """验证 Kimi 分析载荷的最小结构，防止空对象被当成成功。"""
    if not isinstance(summary, dict):
        return "summary 不是对象"
    if summary.get("error"):
        return str(summary.get("error"))
    if not isinstance(summary.get("Core_Thesis"), str) or not summary.get("Core_Thesis", "").strip():
        return "缺少 Core_Thesis"
    if not isinstance(summary.get("Metadata"), dict):
        return "Metadata 格式异常"
    if not isinstance(summary.get("Rating_Changes"), list):
        return "Rating_Changes 格式异常"
    for field in ("Asset_Targets", "Tail_Risks"):
        if field in summary and not isinstance(summary[field], list):
            return f"{field} 格式异常"
    return None


def _extract_valid_summary(path: str) -> Optional[Dict]:
    """从报告文件中提取结构完整的 summary，失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            report = json.load(f)
        summary = _load_summary(report)
        if summary and _analysis_payload_error(summary) is None:
            return summary
    except Exception:
        pass
    return None


def _load_summary(report: dict) -> Optional[Dict]:
    """从 report 中提取并解析 summary（支持 dict 或 JSON 字符串）"""
    summary = report.get("summary", {})
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except Exception:
            return None
    return summary if isinstance(summary, dict) else None


def _load_previous_summary() -> Optional[Dict]:
    """加载前一天的 summary 作为降级备份"""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    cache_path = os.path.join(CACHE_DIR, f"report_{yesterday}.json")
    summary = _extract_valid_summary(cache_path)
    if summary:
        return summary

    # 尝试找最近的有效报告
    files = sorted(
        [f for f in os.listdir(CACHE_DIR) if f.startswith("report_") and f.endswith(".json")],
        reverse=True,
    )
    for f in files[:3]:
        summary = _extract_valid_summary(os.path.join(CACHE_DIR, f))
        if summary:
            return summary
    return None


def _load_historical_reports(days: int = 7) -> List[Dict]:
    """加载最近 N 天的报告，用于趋势分析"""
    reports = []
    files = sorted(
        [f for f in os.listdir(CACHE_DIR) if f.startswith("report_") and f.endswith(".json")],
        reverse=True,
    )
    for f in files[:days]:
        try:
            with open(os.path.join(CACHE_DIR, f), "r", encoding="utf-8") as fh:
                report = json.load(fh)
                reports.append(report)
        except Exception:
            continue
    return reports


def _analyze_trends(reports: List[Dict]) -> Dict[str, Any]:
    """分析历史趋势：投行情绪变化、连续评级追踪"""
    if len(reports) < 2:
        return {"note": "历史数据不足，无法分析趋势"}

    # 每份日报都包含完整账本，必须按事件 key 跨报告去重后再统计。
    unique_changes: Dict[Tuple[str, str, str, str], Dict] = {}
    for r in reports:
        summary = _load_summary(r)
        if not summary:
            continue
        generated = str(r.get("meta", {}).get("generated_at", ""))[:10]
        for raw_change in summary.get("Rating_Changes", []) or []:
            if not isinstance(raw_change, dict):
                continue
            c = dict(raw_change)
            if not _extract_change_time(c) and generated:
                c["_First_Seen"] = generated
            key = _make_change_key(c)
            if key is None:
                continue
            existing = unique_changes.get(key)
            if existing is None or _rating_change_score(c) > _rating_change_score(existing):
                unique_changes[key] = c
    all_changes = list(unique_changes.values())

    # 按 (Bank, Symbol, Action) 聚合，检测连续动作
    action_history: Dict[Tuple[str, str, str], List[str]] = {}
    for c in all_changes:
        key = (
            _canonical_bank_name(c.get("Bank")),
            str(c.get("Symbol", "")).upper(),
            str(c.get("Action", "")).lower(),
        )
        event_date = _extract_change_time(c)
        date = datetime.strptime(event_date, "%Y%m%d").strftime("%Y-%m-%d") if event_date else ""
        if key not in action_history:
            action_history[key] = []
        if date and date not in action_history[key]:
            action_history[key].append(date)

    # 找出连续 2 天以上出现的动作
    persistent_signals = []
    for (bank, sym, action), dates in action_history.items():
        if len(dates) >= 2:
            persistent_signals.append({
                "bank": bank,
                "symbol": sym,
                "action": action,
                "dates": sorted(dates, reverse=True),
                "persistence_days": len(dates),
            })

    # 按出现天数排序
    persistent_signals.sort(key=lambda x: x["persistence_days"], reverse=True)

    # 统计各投行近期看多/看空倾向
    bank_sentiment: Dict[str, Dict[str, int]] = {}
    for c in all_changes:
        bank = _canonical_bank_name(c.get("Bank")) or "Unknown"
        action = c.get("Action", "").lower()
        if bank not in bank_sentiment:
            bank_sentiment[bank] = {"bullish": 0, "bearish": 0, "neutral": 0}
        if action in ("upgrade", "initiat"):
            bank_sentiment[bank]["bullish"] += 1
        elif action in ("downgrade",):
            bank_sentiment[bank]["bearish"] += 1
        else:
            bank_sentiment[bank]["neutral"] += 1

    return {
        "analysis_period_days": len(reports),
        "total_rating_changes": len(all_changes),
        "persistent_signals": persistent_signals[:10],
        "bank_sentiment": bank_sentiment,
    }


def _filter_by_age(items: List[Dict], days: int, date_field: str = "time_published") -> List[Dict]:
    """按日期字段过滤，只保留最近 N 天的条目"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    return [it for it in items if (it.get(date_field, "") or "")[:8] >= cutoff]


def _extract_change_time(c: Dict) -> str:
    """把事件日期统一转成 YYYYMMDD；无发布时间时使用首次记录日期。"""
    for raw_value in (c.get("Time", ""), c.get("_First_Seen", "")):
        value = str(raw_value or "").strip()
        match = re.match(r"^(\d{4})[-/]?(\d{2})[-/]?(\d{2})", value)
        if not match:
            continue
        compact = "".join(match.groups())
        try:
            parsed = datetime.strptime(compact, "%Y%m%d")
        except ValueError:
            continue
        if parsed.date() > (datetime.now() + timedelta(days=1)).date():
            continue
        return parsed.strftime("%Y%m%d")
    return ""


_BANK_ALIASES = {
    "bofa": "Bank of America",
    "bofa securities": "Bank of America",
    "bank of america": "Bank of America",
    "bank of america securities": "Bank of America",
    "j.p. morgan": "JPMorgan",
    "jp morgan": "JPMorgan",
    "jpmorgan": "JPMorgan",
    "jpmorgan chase": "JPMorgan",
    "evercore isi group": "Evercore ISI",
    "evercore isi": "Evercore ISI",
    "evercore": "Evercore ISI",
    "sanford c. bernstein": "Bernstein",
    "sanford bernstein": "Bernstein",
    # 中文译名归一，防止与英文条目重复
    "高盛": "Goldman Sachs",
    "摩根士丹利": "Morgan Stanley",
    "摩根大通": "JPMorgan",
    "美国银行": "Bank of America",
    "美银证券": "Bank of America",
    "花旗": "Citigroup",
    "瑞银": "UBS",
    "德意志银行": "Deutsche Bank",
    "巴克莱": "Barclays",
    "富国银行": "Wells Fargo",
    "汇丰": "HSBC",
    "野村": "Nomura",
    "杰富瑞": "Jefferies",
    "伯恩斯坦": "Bernstein",
    "派珀桑德勒": "Piper Sandler",
    "瑞穗证券": "Mizuho",
    "瑞穗": "Mizuho",
    "evercore isi group": "Evercore ISI",
    "evercore isi": "Evercore ISI",
    "jpmorgan chase & co.": "JPMorgan",
    "jpmorgan chase & co": "JPMorgan",
    "bank of america securities": "Bank of America",
    "sanford c. bernstein": "Bernstein",
    "citigroup": "Citigroup",
    "citi": "Citigroup",
    # Yahoo Finance 公司名变体（upgrades_downgrades 的 Firm 字段）
    "jp morgan": "JPMorgan",
    "j.p. morgan": "JPMorgan",
    "roth capital": "Roth Capital",
    "roth capital partners": "Roth Capital",
    "roth mkm": "Roth Capital",
    "truist securities": "Truist",
    "truist": "Truist",
    "rbc capital": "RBC",
    "rbc capital markets": "RBC",
    "rbc": "RBC",
    "cantor fitzgerald": "Cantor",
    "cantor fitzgerald & co.": "Cantor",
    "wells fargo securities": "Wells Fargo",
    "piper sandler & co.": "Piper Sandler",
    "raymond james financial": "Raymond James",
    "goldman sachs group": "Goldman Sachs",
    "morgan stanley & co.": "Morgan Stanley",
    "ubs group": "UBS",
    "ubs securities": "UBS",
    "barclays capital": "Barclays",
    "deutsche bank ag": "Deutsche Bank",
    "hsbc holdings": "HSBC",
    "hsbc securities": "HSBC",
    "clsa": "CLSA",
    "jefferies llc": "Jefferies",
    "jefferies & company": "Jefferies",
}

# 合并同一事件的英文/中文名称时，优先展示英文名
_BANK_PREFERRED_NAME = {
    "摩根士丹利": "Morgan Stanley",
    "美国银行": "Bank of America",
    "美银证券": "Bank of America",
    "高盛": "Goldman Sachs",
    "花旗": "Citigroup",
    "富国银行": "Wells Fargo",
    "汇丰": "HSBC",
    "派珀桑德勒": "Piper Sandler",
    "杰富瑞": "Jefferies",
    "瑞银": "UBS",
    "巴克莱": "Barclays",
    "伯恩斯坦": "Bernstein",
    "摩根大通": "JPMorgan",
    "德意志银行": "Deutsche Bank",
    "野村": "Nomura",
    "瑞穗证券": "Mizuho",
    "瑞穗": "Mizuho",
}

# Kimi 偶尔把评级也翻译成中文；评级统一保留英文以便前端着色
_RATING_ALIASES = {
    "买入": "Buy",
    "强力买入": "Strong Buy",
    "持有": "Hold",
    "卖出": "Sell",
    "中性": "Neutral",
    "增持": "Overweight",
    "超配": "Overweight",
    "超重": "Overweight",
    "减持": "Underweight",
    "低配": "Underweight",
    "跑赢大盘": "Outperform",
    "跑输大盘": "Underperform",
    "市场表现": "Market Perform",
    "同步大盘": "Market Perform",
}


def _canonical_bank_name(value: Any) -> str:
    bank = str(value or "").strip()
    return _BANK_ALIASES.get(bank.casefold(), bank)


# ── 韩股评级(SK Hynix / 三星)─────────────────────────────────────────
# 韩国券商:韩文/别名 → 英文规范展示名。Google News 韩语标题里证券公司常以
# 「XX증권」(XX证券)出现,这里归一成可读的英文名,并加上 KR 标签便于辨识。
_KR_BROKER_ALIASES = {
    "kb": "KB Securities", "kb증권": "KB Securities",
    "nh": "NH Investment", "nh투자": "NH Investment", "nh투자증권": "NH Investment",
    "한국투자": "Korea Investment", "한투": "Korea Investment", "한국투자증권": "Korea Investment",
    "미래에셋": "Mirae Asset", "미래에셋증권": "Mirae Asset",
    "삼성": "Samsung Securities", "삼성증권": "Samsung Securities",
    "키움": "Kiwoom", "키움증권": "Kiwoom",
    "신한": "Shinhan", "신한투자": "Shinhan", "신한투자증권": "Shinhan",
    "하나": "Hana", "하나증권": "Hana",
    "메리츠": "Meritz", "메리츠증권": "Meritz",
    "대신": "Daishin", "대신증권": "Daishin",
    "유안타": "Yuanta", "유안타증권": "Yuanta",
    "교보": "Kyobo", "교보증권": "Kyobo",
    "ibk": "IBK", "ibk투자": "IBK", "ibk투자증권": "IBK",
    "sk": "SK Securities", "sk증권": "SK Securities",
    "현대차": "Hyundai Motor Sec", "현대차증권": "Hyundai Motor Sec",
    "한화": "Hanwha", "한화투자": "Hanwha", "한화투자증권": "Hanwha",
    "bnk": "BNK", "bnk투자": "BNK", "bnk투자증권": "BNK",
    "db금융": "DB Financial", "db금융투자": "DB Financial",
    "유진": "Eugene", "유진투자": "Eugene", "유진투자증권": "Eugene",
    "부국": "Bookook", "부국증권": "Bookook",
    "신영": "Shinyoung", "신영증권": "Shinyoung",
    "케이프": "CAPE", "케이프투자증권": "CAPE",
    "이베스트": "eBest", "이베스트투자증권": "eBest",
}
# 韩语评级词 → 英文(标题出现时才用;多数在正文,故常为 None 交给 Kimi/回填)
_KR_RATING_WORDS = {
    "매수": "Buy", "적극매수": "Strong Buy", "중립": "Neutral", "홀드": "Hold",
    "비중확대": "Overweight", "비중축소": "Underweight", "매도": "Sell",
    "유지": None,  # 「维持」只表示动作,不是评级
}
# 韩股监控:Yahoo 代码 → (谷歌韩语查询词, 展示用代码, 名称)
_KR_STOCKS = {
    "000660.KS": {"q": "SK하이닉스", "display": "SKHYNIX", "name": "SK Hynix"},
    "005930.KS": {"q": "삼성전자", "display": "SAMSUNG", "name": "Samsung Electronics"},
}
# 韩元→美元 近似汇率(仅用于展示参考;目标价原始单位是 만원=万韩元)
_KRW_PER_USD = 1390.0


def _krw_man_to_usd(man_str: str) -> Optional[str]:
    """把「N만원」(N万韩元)目标价转成约 $X,便于与美股目标价同列展示。失败返回 None。"""
    try:
        man = float(str(man_str).replace(",", ""))
    except (TypeError, ValueError):
        return None
    krw = man * 10000.0
    usd = krw / _KRW_PER_USD
    return f"${usd:,.0f}"


def _kr_current_price_man(yahoo_sym: str) -> Optional[float]:
    """抓韩股 Yahoo 现价并换算成「만원」(万韩元),用于判断目标价方向。失败返回 None。"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from collectors.yahoo_provider import YahooProvider
        q = YahooProvider().quote(yahoo_sym) or {}
        price = q.get("price") or q.get("prev_close")
        if price:
            return float(price) / 10000.0  # 韩元 → 만원
    except Exception:
        pass
    return None


def fetch_korean_rating_news() -> List[Dict]:
    """抓 Google News 韩语 RSS 里 SK Hynix / 三星的券商评级新闻,转成标准评级事件。

    这是覆盖韩股评级的唯一可用源(Yahoo/Finnhub 免费版对韩股均无评级数据)。
    韩语标题里「证券公司(XX증권) + 目标价(N만원)」很规整,可正则抽取;评级
    (매수 등)多在正文,标题抽不到时留 None,交给 Kimi 分析或账本回填。
    事件 _Source="kr_headline",挂 Yahoo 代码(000660.KS 等),目标价附 USD 参考值。
    目标价方向参照 Yahoo 现价判断(高于现价=上调,低于=下调),只保留有明确券商
    或明确目标价的有效行;同一券商同一股票出现多个目标价时合并成区间(185~420만)。
    """
    pt_re = re.compile(r"(?:목표주가|목표가)\s*['\"]?(\d[\d,\.]*)만원")
    pt_fallback = re.compile(r"(\d[\d,\.]*)만원")
    broker_re = re.compile(r"([A-Za-z가-힣]{1,10}?)\s*증권")
    _kr_broker_block = {"증권가", "충격의", "시각", "리포트", "전망", "극과", "극", "고개",
                        "제시한", "국내", "두고", "고수한", "엇갈린", "모으는", "나왔다",
                        "상향", "하향", "유지", "잡았다", "꼽은", "던진", "팔아", "말아"}

    # (bank_key, yahoo_sym) -> 聚合事件
    aggregated: Dict[Tuple[str, str], Dict] = {}
    for yahoo_sym, meta in _KR_STOCKS.items():
        cur_man = _kr_current_price_man(yahoo_sym)
        q = f"{meta['q']} 목표주가 투자의견"
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
               + "&hl=ko&gl=KR&ceid=KR:ko")
        data = _http_get_text(url, timeout=30)
        if not data:
            continue
        try:
            root = ET.fromstring(data)
        except Exception:
            continue
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            if not title or meta["q"] not in title:
                continue
            pub = (item.findtext("pubDate") or "").strip()
            event_date = _parse_rss_date(pub) or datetime.now().strftime("%Y-%m-%d")

            bm = broker_re.search(title)
            broker = None
            if bm:
                cand = bm.group(1).strip()
                if cand and cand not in _kr_broker_block:
                    # 只认别名表里的真券商;不认识的「XX증권」多为动词/形容词误匹配,丢弃
                    broker = _KR_BROKER_ALIASES.get(cand.casefold()) or _KR_BROKER_ALIASES.get(cand)
            pm = pt_re.search(title) or pt_fallback.search(title)
            pt_man = None
            if pm:
                try:
                    pt_man = float(pm.group(1).replace(",", ""))
                except ValueError:
                    pt_man = None
            # 只保留有效行:有明确券商,或有明确目标价
            if not broker and pt_man is None:
                continue
            rating = None
            for kw, en in _KR_RATING_WORDS.items():
                if kw in title and en:
                    rating = en
                    break
            bank_key = (broker or "韩国券商").casefold()
            key = (bank_key, yahoo_sym)
            agg = aggregated.get(key)
            if agg is None:
                agg = {
                    "Bank": broker or "韩国券商",
                    "Symbol": yahoo_sym,
                    "New_Rating": rating,
                    "Old_Rating": None,
                    "Time": event_date,
                    "_Source": "kr_headline",
                    "_Symbol_Display": meta["display"],
                    "_kr_targets": [],   # 收集该券商该股的所有目标价(만원)
                    "_kr_headlines": [],
                    "_kr_has_reiterate": False,
                }
                aggregated[key] = agg
            if rating and not agg["New_Rating"]:
                agg["New_Rating"] = rating
            if pt_man is not None:
                agg["_kr_targets"].append(pt_man)
            agg["_kr_headlines"].append(title)
            if "유지" in title:
                agg["_kr_has_reiterate"] = True
            if event_date > agg["Time"]:
                agg["Time"] = event_date

    # 汇总:同一券商同一股票的多个目标价合并成区间,并按现价判断方向
    events: List[Dict] = []
    for (bank_key, yahoo_sym), agg in aggregated.items():
        targets = sorted(set(agg.pop("_kr_targets")))
        cur_man = _kr_current_price_man(yahoo_sym)
        agg.pop("_kr_headlines", None)
        has_reiterate = agg.pop("_kr_has_reiterate")
        if targets:
            lo, hi = targets[0], targets[-1]
            fmt = lambda v: (f"{v:g}")
            pt_core = f"₩{fmt(lo)}만" if lo == hi else f"₩{fmt(lo)}~{fmt(hi)}만"
            usd = _krw_man_to_usd(str(hi if lo == hi else hi))
            agg["Price_Target"] = pt_core + (f" (~{usd})" if usd and lo == hi else "")
            # 方向:用最高目标价对现价;无现价则默认上调(有目标价新闻多为看多)
            if cur_man:
                agg["Action"] = "price_target_cut" if hi < cur_man else "price_target_raise"
            else:
                agg["Action"] = "price_target_raise"
        else:
            agg["Price_Target"] = None
            agg["Action"] = "reiterate" if has_reiterate or not agg["New_Rating"] else "upgrade"
        events.append(agg)
    return events


def _preferred_bank_name(bank: str, candidate: Optional[str] = None) -> str:
    """为规范后的机构名选择展示名：中文名一律替换为英文规范名。"""
    canonical = _canonical_bank_name(bank)
    preferred = _BANK_PREFERRED_NAME.get(canonical)
    if preferred:
        return preferred
    for name in (bank, candidate):
        if name and not _is_ascii(name) and _BANK_PREFERRED_NAME.get(str(name).strip()):
            return _BANK_PREFERRED_NAME[str(name).strip()]
    return canonical


def _is_ascii(text: str) -> bool:
    return all(ord(ch) < 128 for ch in text)


def _canonical_rating(value: Any) -> Optional[str]:
    rating = _clean_field(value)
    if rating is None:
        return None
    return _RATING_ALIASES.get(rating, rating)


def _merge_rating_changes(existing: List[Dict], new_changes: List[Dict]) -> List[Dict]:
    """合并评级事件：同日同动作去重，不同日期保留，并滑出超期条目。"""
    events: Dict[Tuple[str, str, str, str], Dict] = {}
    inferred_date_by_signal: Dict[Tuple[str, str, str], str] = {}
    for c in list(existing or []) + list(new_changes or []):
        if not isinstance(c, dict):
            continue
        event = dict(c)
        event["Bank"] = _canonical_bank_name(event.get("Bank"))
        event["Symbol"] = str(event.get("Symbol", "") or "").strip().upper()
        event["Action"] = str(event.get("Action", "") or "").strip().lower()
        action = str(event.get("Action", "") or "").strip().lower()
        broad_key = (event["Bank"].casefold(), event["Symbol"], event["Action"])
        if not _extract_change_time({"Time": event.get("Time")}):
            event["_First_Seen"] = inferred_date_by_signal.get(
                broad_key,
                event.get("_First_Seen") or datetime.now().strftime("%Y-%m-%d"),
            )
            event["_Time_Inferred"] = True
        if event.get("_Time_Inferred"):
            inferred_date = _extract_change_time(event)
            if inferred_date:
                inferred_date_by_signal.setdefault(
                    broad_key,
                    datetime.strptime(inferred_date, "%Y%m%d").strftime("%Y-%m-%d"),
                )

        quality_flags = set(event.get("_Quality_Flags", []) or [])
        if action in ("upgrade", "downgrade") and not _clean_field(event.get("New_Rating")):
            quality_flags.add("missing_new_rating")
        if not _extract_change_time({"Time": event.get("Time")}):
            quality_flags.add("inferred_event_date")
        if quality_flags:
            event["_Quality_Flags"] = sorted(quality_flags)

        key = _make_change_key(event)
        if key is None:
            continue
        current = events.get(key)
        if current is None:
            events[key] = event
            continue

        merged = dict(current)
        merged["Bank"] = _preferred_bank_name(current.get("Bank"), event.get("Bank"))
        for field in ("Symbol", "Action", "New_Rating", "Old_Rating", "Price_Target", "Time"):
            if _clean_field(event.get(field)):
                merged[field] = event[field]
        first_seen = [
            str(x.get("_First_Seen", ""))
            for x in (current, event)
            if x.get("_First_Seen")
        ]
        last_seen = [
            str(x.get("_Last_Seen", ""))
            for x in (current, event)
            if x.get("_Last_Seen")
        ]
        flags = set(current.get("_Quality_Flags", []) or []) | set(event.get("_Quality_Flags", []) or [])
        if _clean_field(merged.get("New_Rating")):
            flags.discard("missing_new_rating")
        if _extract_change_time({"Time": merged.get("Time")}):
            flags.discard("inferred_event_date")
        if first_seen:
            merged["_First_Seen"] = min(first_seen)
        if last_seen:
            merged["_Last_Seen"] = max(last_seen)
        if flags:
            merged["_Quality_Flags"] = sorted(flags)
        else:
            merged.pop("_Quality_Flags", None)
        if not _extract_change_time({"Time": merged.get("Time")}) and (current.get("_Time_Inferred") or event.get("_Time_Inferred")):
            merged["_Time_Inferred"] = True
        else:
            merged.pop("_Time_Inferred", None)
        events[key] = merged

    # 最终归一：银行展示名与评级语言统一
    for event in events.values():
        event["Bank"] = _preferred_bank_name(event.get("Bank"))
        for rating_field in ("New_Rating", "Old_Rating"):
            canonical = _canonical_rating(event.get(rating_field))
            if canonical:
                event[rating_field] = canonical

    # 时间窗口过滤（兼容 Time 字段多种格式）
    cutoff = (datetime.now() - timedelta(days=RATING_CHANGES_MAX_AGE_DAYS)).strftime("%Y%m%d")
    filtered = [c for c in events.values() if _extract_change_time(c) >= cutoff]
    filtered.sort(
        key=lambda x: (
            _extract_change_time(x),
            str(x.get("Symbol", "")).upper(),
            str(x.get("Bank", "")).casefold(),
        ),
        reverse=True,
    )
    return filtered


def _load_rating_ledger() -> List[Dict]:
    """加载 30 天评级事件账本，并持续用历史日报补全，防止事件丢失。"""
    stored: List[Dict] = []
    try:
        with open(RATING_LEDGER_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, list):
            stored = loaded
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    bootstrapped: List[Dict] = []
    for report in _load_historical_reports(days=RATING_CHANGES_MAX_AGE_DAYS):
        summary = _load_summary(report)
        if isinstance(summary, dict):
            report_date = str(report.get("meta", {}).get("generated_at", ""))[:10]
            historical_changes = []
            for change in summary.get("Rating_Changes", []) or []:
                if not isinstance(change, dict):
                    continue
                candidate = dict(change)
                if not _extract_change_time(candidate) and report_date:
                    candidate["_First_Seen"] = report_date
                    candidate["_Time_Inferred"] = True
                historical_changes.append(candidate)
            bootstrapped = _merge_rating_changes(
                bootstrapped, historical_changes
            )
    # 历史报告先进入，独立账本后进入；字段冲突时以较新的账本记录为准。
    return _merge_rating_changes(bootstrapped, stored)


def _save_rating_ledger(changes: List[Dict]) -> None:
    """原子写入评级账本，避免进程中断留下半个 JSON。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    temp_path = f"{RATING_LEDGER_FILE}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(changes, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, RATING_LEDGER_FILE)


def _load_state() -> Dict[str, Any]:
    """加载增量刷新状态"""
    defaults = {
        "last_full_fetch": "",
        "last_fast_fetch": "",
        "last_summary_attempt": "",
        "last_summary_fetch": "",
        "last_summary_hash": "",
        "seen_urls": [],
        "seen_news_keys": [],
        "last_hash": "",
        "daily_news_count": 0,
        "rating_batch_initialized": False,
        "pending_rating_items": [],
        "analyzed_rating_keys": [],
        "last_rating_batch_error": "",
        "last_rating_batch_error_at": "",
    }
    if not os.path.exists(STATE_FILE):
        return defaults
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        defaults.update(state)
        return defaults
    except Exception as e:
        print(f"     [WARN] 读取 state 失败: {e}")
        return defaults


def _save_state(state: Dict[str, Any]) -> None:
    """保存增量刷新状态"""
    try:
        temp_path = f"{STATE_FILE}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, STATE_FILE)
    except Exception as e:
        print(f"     [WARN] 保存 state 失败: {e}")


def _minutes_since(iso_value: str, now: datetime) -> float:
    """返回距 ISO 时间的分钟数；缺失或损坏视为已到期。"""
    if not iso_value:
        return float("inf")
    try:
        return max(0.0, (now - datetime.fromisoformat(iso_value)).total_seconds() / 60)
    except (TypeError, ValueError):
        return float("inf")


def _single_fetch_process(func):
    """共享进程锁：fast 忙时跳过，full 有界等待，避免排队覆盖日报。"""
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        fast = bool(kwargs.get("fast", args[1] if len(args) > 1 else False))
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(FETCH_LOCK_FILE, "a+", encoding="utf-8") as lock_file:
            if fast:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print("  -> 完整刷新正在运行，本轮快速刷新安全跳过")
                    report = _load_today_report() or _load_latest_cached_report() or {
                        "meta": {}, "summary": {}, "raw": {"news": [], "recommendations": []}
                    }
                    skipped = dict(report)
                    skipped["meta"] = dict(report.get("meta", {}))
                    skipped["meta"]["refresh_skipped"] = "busy"
                    return skipped
            else:
                deadline = time.monotonic() + int(os.environ.get("IB_RESEARCH_LOCK_TIMEOUT_SECONDS", "300"))
                while True:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("等待另一项研报刷新完成超时")
                        time.sleep(1)
            try:
                return func(*args, **kwargs)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return wrapped


def _state_seen_urls(state: Dict[str, Any]) -> set:
    return set(state.get("seen_urls", []))


def _state_mark_seen(state: Dict[str, Any], urls: List[str]) -> None:
    seen = _state_seen_urls(state)
    seen.update(u for u in urls if u)
    state["seen_urls"] = list(seen)[-5000:]


def _news_identity(item: Dict[str, Any]) -> str:
    """为无 URL 的 RSS 条目也生成稳定去重 key。"""
    url = str(item.get("url", "") or "").strip()
    if url:
        return f"url:{url}"
    title = re.sub(
        r"\s+", " ", str(item.get("title", "") or "").strip().casefold()
    )
    if not title:
        return ""
    event_date = str(item.get("time_published", "") or "")[:8]
    return f"title:{event_date}:{title}"


def _filter_new_news_items(
    state: Dict[str, Any],
    existing_news: List[Dict],
    candidates: List[Dict],
    cutoff_date: str,
) -> List[Dict]:
    """排除旧闻、跨源重复和无 URL 条目的重复回流。"""
    known = set(state.get("seen_news_keys", []) or [])
    known.update(
        key for key in (_news_identity(item) for item in existing_news or []) if key
    )
    accepted = []
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        published = str(item.get("time_published", "") or "")[:8]
        if published and published < cutoff_date:
            continue
        key = _news_identity(item)
        if not key or key in known:
            continue
        known.add(key)
        accepted.append(item)
    return accepted


def _state_mark_news_seen(state: Dict[str, Any], news: List[Dict]) -> None:
    known = set(state.get("seen_news_keys", []) or [])
    known.update(
        key for key in (_news_identity(item) for item in news or []) if key
    )
    state["seen_news_keys"] = list(known)[-5000:]


def _load_today_report() -> Optional[Dict[str, Any]]:
    """加载今日已生成的报告"""
    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(CACHE_DIR, f"report_{today}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _load_latest_cached_report() -> Optional[Dict[str, Any]]:
    """读取最近一份缓存；跨午夜时不得因今日文件尚未生成而清空页面。"""
    try:
        files = sorted(
            (
                name
                for name in os.listdir(CACHE_DIR)
                if name.startswith("report_") and name.endswith(".json")
            ),
            reverse=True,
        )
    except OSError:
        return None
    for name in files:
        try:
            with open(os.path.join(CACHE_DIR, name), "r", encoding="utf-8") as f:
                report = json.load(f)
            if isinstance(report, dict):
                return report
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _load_previous_summary_fast(today_report: Optional[Dict]) -> Optional[Dict]:
    """快速模式下优先使用今日 summary，否则用最近有效 summary"""
    if today_report:
        summary = _load_summary(today_report)
        if summary and "error" not in summary:
            return summary
    return _load_previous_summary()


def _load_today_reddit_posts() -> List[Dict]:
    today_report = _load_today_report()
    if today_report:
        return today_report.get("raw", {}).get("reddit_posts", []) or []
    return []


def _fetch_fast_sources(seen_urls: set) -> List[Dict]:
    """快速刷新源：抓所有可用的评级内容源，按 URL 去重。

    每 4 小时一档，目标是「尽量全」：除通用 RSS 外，并入有评级价值的
    Google News RSS 与 Finnhub 个股新闻（原仅完整模式）。Yahoo 评级走
    _backfill_from_yahoo（结构化，不在此处）。实测不可用、故不并入的源：
    Barron's(401 反爬)、Seeking Alpha 个股 API(PerimeterX 403)、
    Seeking Alpha / MarketWatch RSS(几乎不含评级内容)。
    """
    new_items = []
    sources = {
        "Yahoo Finance": fetch_yahoo_finance_rss(max_items=80),
        "Benzinga": fetch_benzinga_rss(max_items=60),
        "Zacks": fetch_zacks_rss(max_items=60),
        "StreetInsider": fetch_streetinsider_rss(max_items=60),
        "SemiAnalysis": fetch_semianalysis_rss(max_items=40),
    }
    # 有评级价值的个股级源（轻量、几秒可完成），并入 4h 档提高覆盖
    for label, fn in [("Google News", lambda: fetch_google_news_rss(HOT_SYMBOLS)),
                      ("Finnhub 个股新闻", lambda: fetch_finnhub_news(HOT_SYMBOLS))]:
        try:
            sources[label] = fn()
        except Exception as exc:
            print(f"     [WARN] 快速源 {label} 获取失败，跳过: {type(exc).__name__}: {exc}")
            sources[label] = []
    for label, items in sources.items():
        added = 0
        for item in items:
            url = item.get("url", "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            new_items.append(item)
            added += 1
        if label in ("Google News", "Finnhub 个股新闻"):
            print(f"     快速源 {label}: +{added} 条")
    return new_items


def _fetch_all_rss_sources(seen_urls: set) -> Dict[str, List[Dict]]:
    """完整刷新时拉取的扩展 RSS 源"""
    results = {}
    for label, func, kwargs in [
        ("Yahoo Finance", fetch_yahoo_finance_rss, {"max_items": 150}),
        ("Seeking Alpha RSS", fetch_seeking_alpha_rss, {"max_items": 80}),
        ("Benzinga", fetch_benzinga_rss, {"max_items": 80}),
        ("Barron's", fetch_barrons_rss, {"max_items": 80}),
        ("MarketWatch", fetch_marketwatch_rss, {"max_items": 80}),
        ("Zacks", fetch_zacks_rss, {"max_items": 100}),
        ("StreetInsider", fetch_streetinsider_rss, {"max_items": 100}),
        ("SemiAnalysis", fetch_semianalysis_rss, {"max_items": 60}),
    ]:
        try:
            items = func(**kwargs)
            unique = []
            for item in items:
                url = item.get("url", "")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                unique.append(item)
            results[label] = unique
            print(f"       {label}: +{len(unique)} 条")
        except Exception as e:
            print(f"       [WARN] {label} 获取失败: {e}")
            results[label] = []
    return results


def _backup_report(cache_path: str) -> None:
    """保存报告前自动备份旧版本（保留最近 10 份）"""
    if not os.path.exists(cache_path):
        return
    backup_dir = os.path.join(CACHE_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    base_name = os.path.basename(cache_path)
    timestamp = datetime.now().strftime("%H%M%S")
    backup_path = os.path.join(backup_dir, f"{base_name}.{timestamp}.bak")
    try:
        shutil.copy2(cache_path, backup_path)
        # 只保留最近 10 份备份
        backups = sorted(
            [f for f in os.listdir(backup_dir) if f.startswith(base_name)],
            key=lambda x: os.path.getmtime(os.path.join(backup_dir, x)),
        )
        for old in backups[:-10]:
            os.remove(os.path.join(backup_dir, old))
    except Exception as e:
        print(f"     [WARN] 备份报告失败: {e}")


def _extract_date_range_from_raw(raw_data: Dict, lookback_days: int = 7) -> Optional[str]:
    """从 raw_data 的新闻时间戳提取最近 N 天的实际数据日期范围"""
    news_items = raw_data.get("news", [])
    today = datetime.now().date()
    cutoff = today - timedelta(days=lookback_days - 1)
    dates = set()
    for n in news_items:
        ts = n.get("time_published", "")
        if ts and len(ts) >= 8:
            try:
                d = datetime.strptime(ts[:8], "%Y%m%d").date()
                if d >= cutoff:
                    dates.add(d)
            except ValueError:
                continue
    if not dates:
        return None
    min_date = min(dates)
    max_date = max(dates)
    if min_date == max_date:
        return min_date.strftime("%Y-%m-%d")
    return f"{min_date.strftime('%Y-%m-%d')}至{max_date.strftime('%Y-%m-%d')}"


def _fix_summary_metadata(summary: Dict, raw_data: Dict) -> Dict:
    """用 raw_data 的实际数据修正 summary 中的 Metadata.Date，覆盖 LLM 幻觉"""
    if not isinstance(summary, dict):
        return summary
    actual_date_range = _extract_date_range_from_raw(raw_data)
    if actual_date_range:
        metadata = summary.setdefault("Metadata", {})
        if isinstance(metadata, dict):
            metadata["Date"] = actual_date_range
    return summary


def _clean_field(val: Any, formatter: Optional[Callable[[str], str]] = None) -> Optional[str]:
    """通用字段清洗：过滤 None/空/'null'，可选 formatter"""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() == "null":
        return None
    return formatter(s) if formatter else s


def _format_price_target(s: str) -> str:
    """统一目标价格式：纯数字前加 $"""
    return f"${s}" if re.match(r"^\d+(\.\d+)?[kK]?$", s) else s


def _format_date(s: str) -> Optional[str]:
    """统一日期格式为 YYYY-MM-DD"""
    compact = _extract_change_time({"Time": s})
    if compact:
        return datetime.strptime(compact, "%Y%m%d").strftime("%Y-%m-%d")
    return s  # 无法解析则原样返回，便于人工检查


def _rating_change_score(c: Dict) -> int:
    """评分：信息完整度越高越好"""
    return sum(
        1 for x in (c.get("New_Rating"), c.get("Old_Rating"), c.get("Price_Target"), c.get("Time"))
        if x is not None
    )


def _make_change_key(c: Dict) -> Optional[Tuple[str, str, str, str]]:
    """生成评级事件 key：同投行、股票、动作、日期才视为同一事件。"""
    bank = _canonical_bank_name(c.get("Bank"))
    symbol = str(c.get("Symbol", "") or "").strip().upper()
    action = str(c.get("Action", "") or "").strip().lower()
    event_date = _extract_change_time(c)
    if not bank or not symbol or not action or not event_date:
        return None
    return (bank.casefold(), symbol, action, event_date)


def _change_key_string(c: Dict) -> str:
    key = _make_change_key(c)
    return ":".join(key) if key else ""


def _unique_rating_keys(values: List[Any]) -> List[str]:
    """按原顺序清洗并去重评级事件 key。"""
    seen = set()
    result = []
    for value in values or []:
        key = str(value or "").strip()
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _update_rating_batch_state(
    state: Dict[str, Any],
    baseline_events: List[Dict],
    new_events: List[Dict],
    now: Optional[datetime] = None,
) -> List[Dict]:
    """将新评级事件加入持久化批次，并返回当前待分析队列。

    第一次启用批处理时，把页面中已有事件登记为已分析基线，避免部署后把
    过去 30 天的账本误当成新批次。后续只累计新抓到且未分析的独立事件。
    """
    now = now or datetime.now()
    analyzed_keys = _unique_rating_keys(state.get("analyzed_rating_keys", []))
    if not state.get("rating_batch_initialized"):
        analyzed_keys.extend(
            key
            for key in (_change_key_string(event) for event in baseline_events or [])
            if key
        )
        analyzed_keys = _unique_rating_keys(analyzed_keys)
        state["rating_batch_initialized"] = True

    analyzed_set = set(analyzed_keys)
    pending_by_key: Dict[str, Dict] = {}
    for item in state.get("pending_rating_items", []) or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("_Batch_Key") or _change_key_string(item)).strip()
        if not key or key in analyzed_set or key in pending_by_key:
            continue
        candidate = dict(item)
        candidate["_Batch_Key"] = key
        candidate.setdefault("_Queued_At", now.isoformat())
        pending_by_key[key] = candidate

    for event in new_events or []:
        if not isinstance(event, dict):
            continue
        key = _change_key_string(event)
        if not key or key in analyzed_set or key in pending_by_key:
            continue
        candidate = dict(event)
        candidate["_Batch_Key"] = key
        candidate["_Queued_At"] = now.isoformat()
        pending_by_key[key] = candidate

    pending = sorted(
        pending_by_key.values(),
        key=lambda item: str(item.get("_Queued_At", "")),
    )
    state["pending_rating_items"] = pending
    state["analyzed_rating_keys"] = analyzed_keys[-FAST_BATCH_MAX_ANALYZED_KEYS:]
    return pending


def _mark_rating_batch_analyzed(
    state: Dict[str, Any], analyzed_events: List[Dict]
) -> None:
    """仅在批量分析成功后清空队列并登记已处理事件。"""
    keys = _unique_rating_keys(state.get("analyzed_rating_keys", []))
    for event in list(analyzed_events or []) + list(
        state.get("pending_rating_items", []) or []
    ):
        if not isinstance(event, dict):
            continue
        key = str(event.get("_Batch_Key") or _change_key_string(event)).strip()
        if key:
            keys.append(key)
    state["analyzed_rating_keys"] = _unique_rating_keys(keys)[
        -FAST_BATCH_MAX_ANALYZED_KEYS:
    ]
    state["pending_rating_items"] = []
    state["rating_batch_initialized"] = True
    state["last_rating_batch_error"] = ""
    state["last_rating_batch_error_at"] = ""


def _fast_batch_should_analyze(
    force: bool,
    pending_count: int = 0,
    last_error_at: str = "",
    now: Optional[datetime] = None,
) -> bool:
    """固定 4 小时刷新：不再按“累计 N 条评级”门槛拦截分析。

    只要本轮数据有变化（由调用方通过 data_hash 判断）就会调用一次 Kimi；
    这里只在“上次分析失败后的冷却期”内暂缓重试，避免对持续不可用的模型
    连续空转。pending_count 仅保留参数兼容，不再参与决策。
    """
    if force:
        return True
    if (
        last_error_at
        and _minutes_since(last_error_at, now or datetime.now())
        < FAST_AI_RETRY_INTERVAL_MIN
    ):
        return False
    return True


def _can_reuse_full_analysis(
    force: bool,
    pending_count: int,
    previous_analysis_hash: str,
    data_hash: str,
    previous_summary: Optional[Dict],
) -> bool:
    """只有原始数据与上次实际分析一致且队列为空时才能复用。"""
    return bool(
        not force
        and pending_count == 0
        and previous_analysis_hash
        and previous_analysis_hash == data_hash
        and isinstance(previous_summary, dict)
        and _analysis_payload_error(previous_summary) is None
        and not previous_summary.get("_fallback")
    )


def _normalize_rating_changes(changes: List[Dict], historical_reports: Optional[List[Dict]] = None) -> List[Dict]:
    """规范化 Rating_Changes：去重、回填 null、清洗字段、排序"""
    if not changes:
        return []

    cleaners = {
        "New_Rating": lambda v: _clean_field(v),
        "Old_Rating": lambda v: _clean_field(v),
        "Price_Target": lambda v: _clean_field(v, _format_price_target),
        "Time": lambda v: _clean_field(v, _format_date),
    }

    # 1. 基础清洗
    normalized = []
    for c in changes:
        if not isinstance(c, dict):
            continue
        candidate = dict(c)
        if not _extract_change_time(candidate):
            candidate["_First_Seen"] = datetime.now().strftime("%Y-%m-%d")
            candidate["_Time_Inferred"] = True
        key = _make_change_key(candidate)
        if key is None:
            continue
        event = {
            "Bank": _canonical_bank_name(c.get("Bank")),
            "Symbol": key[1],
            "Action": key[2],
            **{field: cleaner(c.get(field)) for field, cleaner in cleaners.items()},
        }
        if candidate.get("_First_Seen"):
            event["_First_Seen"] = candidate["_First_Seen"]
        if candidate.get("_Time_Inferred"):
            event["_Time_Inferred"] = True
        normalized.append(event)

    # 2. 同一事件（含日期）内去重：保留信息最完整的
    grouped: Dict[Tuple[str, str, str, str], Dict] = {}
    for c in normalized:
        key = _make_change_key(c)
        if key is None:
            continue
        existing = grouped.get(key)
        if existing is None or _rating_change_score(c) > _rating_change_score(existing):
            grouped[key] = c

    deduped = list(grouped.values())

    # 3. 用历史数据回填 null 字段
    if historical_reports:
        history_map: Dict[Tuple[str, str, str, str], Dict] = {}
        for report in historical_reports:
            summary = _load_summary(report)
            if not summary:
                continue
            for hc in summary.get("Rating_Changes", []):
                if not isinstance(hc, dict):
                    continue
                key = _make_change_key(hc)
                if key is None:
                    continue
                existing = history_map.get(key)
                if existing is None or _rating_change_score(hc) > _rating_change_score(existing):
                    history_map[key] = hc

        for c in deduped:
            event_key = _make_change_key(c)
            hc = history_map.get(event_key) if event_key else None
            if not hc:
                continue
            for field, cleaner in cleaners.items():
                if c[field] is None:
                    c[field] = cleaner(hc.get(field))

    # 4. 丢弃仍缺少 New_Rating 的 upgrade/downgrade 事件。
    # 模型提示已要求“无法推断评级时不要输出”，这里再兜底过滤一次，避免健康检查
    # 把“有方向但无评级”的半成品事件当成严重质量问题。
    complete = []
    for c in deduped:
        action = str(c.get("Action", "")).lower()
        if action in ("upgrade", "downgrade") and not c.get("New_Rating"):
            continue
        complete.append(c)

    complete.sort(
        key=lambda x: (
            _extract_change_time(x),
            x.get("Symbol", ""),
            x.get("Bank", "").casefold(),
        ),
        reverse=True,
    )
    return complete


def _parse_summary_analysis_response(result: dict) -> Tuple[Optional[Dict], Optional[str]]:
    """解析并验证主分析或备用模型的结构化响应。"""
    if not result:
        return None, "empty response"
    content = _extract_kimi_text(result)
    if content is None:
        return None, "unable to extract text"
    parsed = _parse_kimi_json(content)
    if parsed is None:
        return None, "unable to extract valid JSON"
    payload_error = _analysis_payload_error(parsed)
    if payload_error:
        return None, payload_error
    return parsed, None


def summarize_with_kimi(raw_data: Dict, previous_summary: Optional[Dict] = None, historical_reports: Optional[List[Dict]] = None, fast: bool = False) -> Dict[str, Any]:
    """生成结构化分析；快速批次严格限制为一次模型请求。"""

    recommendations = raw_data.get("recommendations", [])
    news_items = raw_data.get("news", [])
    pending_rating_events = raw_data.get("pending_rating_events", [])

    # 去重（保留高相关度优先）
    seen_titles = set()
    unique_news = []
    for n in news_items:
        title = n.get("title", "")
        if title and title not in seen_titles:
            seen_titles.add(title)
            unique_news.append(n)

    rating_changes = [n for n in unique_news if n.get("_relevance", 0) >= 3]
    other_news = [n for n in unique_news if n.get("_relevance", 0) < 3]

    top_rating = sorted(rating_changes, key=lambda x: x.get("time_published", ""), reverse=True)[:10 if fast else 20]
    top_other = sorted(other_news, key=lambda x: x.get("time_published", ""), reverse=True)[:5 if fast else 10]

    rating_text = "\n\n".join([
        f"[{i+1}] {n['bank']} | {n['title']}\n"
        f"时间:{n.get('time_published', '')[:8]}\n"
        f"摘要:{n.get('summary', '')[:200]}\n"
        f"标的:{', '.join([t.get('ticker', '') for t in n.get('tickers', [])[:3]])}"
        for i, n in enumerate(top_rating)
    ])
    batch_rating_text = "\n".join(
        f"- {event.get('Time', '')} | {event.get('Bank', '')} | "
        f"{event.get('Action', '')} {event.get('Symbol', '')} | "
        f"新评级={event.get('New_Rating') or 'null'} | "
        f"目标价={event.get('Price_Target') or 'null'} | "
        f"标题={event.get('_Headline', '')}"
        for event in pending_rating_events
        if isinstance(event, dict)
    )

    other_text = "\n\n".join([
        f"[{i+1}] {n['bank']} | {n['title']}\n"
        f"时间:{n.get('time_published', '')[:8]}\n"
        f"摘要:{n.get('summary', '')[:150]}"
        for i, n in enumerate(top_other)
    ])

    rec_text = "\n".join([
        f"- {r['symbol']}: 强买={r.get('strongBuy', 0)}, 买入={r.get('buy', 0)}, "
        f"持有={r.get('hold', 0)}, 卖出={r.get('sell', 0)}, 强卖={r.get('strongSell', 0)} (周期:{r.get('period', '')})"
        for r in recommendations[:10 if fast else 15]
    ])

    # 如果有前一天的 summary，提示 Kimi 保持一致性
    previous_hint = ""
    if previous_summary and isinstance(previous_summary, dict):
        prev_core = previous_summary.get("Core_Thesis", "")[:200]
        prev_changes = previous_summary.get("Rating_Changes", [])[:3]
        if prev_core:
            previous_hint = f"\n\n## 昨日核心观点（供参考一致性）\n{prev_core}\n"
        if prev_changes:
            previous_hint += "昨日评级变化: " + ", ".join([
                f"{c.get('Bank')} {c.get('Action')} {c.get('Symbol')}"
                for c in prev_changes
            ])

    prompt = f"""你是一位顶级市场研究分析师。请从以下输入中提取**具体的分析师评级变化**和**机构核心观点**。\n\n注：SemiAnalysis 等独立研究机构观点与卖方投行评级同等重要，请一并纳入核心观点与风险分析；Bernstein（伯恩斯坦）的观点请作为高权重参考。

## 输入数据

### A. 分析师共识汇总 (Finnhub)
{rec_text if rec_text else "(无共识数据)"}

### B. 近期明确的分析师评级变化（upgrade/downgrade/price target/initiate coverage）
本轮累计、已去重且待统一分析的评级批次：
{batch_rating_text if batch_rating_text else "(完整分析模式，无单独待处理批次)"}

近期高相关度新闻：
{rating_text if rating_text else "(无明确评级变化数据)"}

### C. 其他投行相关新闻与观点
{other_text if other_text else "(无其他新闻)"}
{previous_hint}

## 分析要求

**重点：优先从 B 部分提取具体的分析师评级变化事件。**

1. **近期评级变化 (Rating_Changes)** — 这是最重要的输出
   - 逐条列出近期（最近3天内）具体的 upgrade、downgrade、initiate coverage、reiterate 事件，以及**明确的目标价调整事件**（如标题含 "raises ... price target" / "boosts ... price target" / "cuts ... price target" / "lowers ... price target"）。
   - 动作（Action）可选值：upgrade、downgrade、initiate、reiterate、price_target_raise、price_target_cut。
   - 每条必须包含：投行名称、股票代码、动作、新评级（Buy/Sell/Hold/Overweight/Underweight/Outperform 等；price_target_raise/price_target_cut/reiterate 允许为 null）、如果有目标价也要提取。
   - 格式示例："Goldman Sachs upgrades AAPL to Buy, PT $220"
   - **重要：必须从标题和摘要中主动推断信息。例如标题 "Bank of America upgrades Intel to Buy, PT $135" 应提取为 New_Rating=Buy, Price_Target=$135。只有标题和摘要中确实完全没有相关信息时，才填 null。**
   - **重要：对于 upgrade/downgrade 事件，如果标题和摘要中完全无法推断 New_Rating，则不要输出该事件；reiterate/initiate coverage/price_target_raise/price_target_cut 事件允许 New_Rating 为 null。**
   - **去重：同一家投行对同一股票的同一个动作只保留一条，优先保留信息最完整的一条（有目标价/新旧评级齐全）。**
   - Time 字段必须使用新闻中的实际日期（格式 YYYY-MM-DD），禁止编造年份。

2. **核心宏观与市场观点 (Core_Thesis)**
   - 用3句话总结核心看多/看空逻辑。
   - 提取对关键宏观变量的预期（利率、通胀、GDP）。

3. **量化目标与资产预测 (Asset_Targets)**
   - 精准抓取所有具体价格目标或区间。
   - 提取时间窗口（如"Q3"、"未来6个月"）。

4. **统计依据 (Statistical_Evidence)**
   - 支撑观点的核心数据或模型说明。

5. **尾部风险 (Tail_Risks)**
   - 与当前市场共识最大的分歧点。
   - 可能导致逻辑失效的黑天鹅事件。

## 输出格式

严格输出如下JSON，不要包含任何markdown标记或解释文字。缺失字段填null。

{{
  "Metadata": {{
    "Institution": "涉及的投行机构（逗号分隔）",
    "Date": "数据日期范围",
    "Primary_Assets_Covered": ["AAPL", "NVDA", "SPY"]
  }},
  "Rating_Changes": [
    {{
      "Bank": "投行名称",
      "Symbol": "股票代码",
      "Action": "upgrade/downgrade/initiate/reiterate",
      "New_Rating": "Buy/Sell/Hold/Overweight/Underweight/Outperform/Underperform",
      "Old_Rating": "原评级（如有）",
      "Price_Target": "目标价（如有）",
      "Time": "发布时间"
    }}
  ],
  "Core_Thesis": "核心观点（3句话）",
  "Asset_Targets": [
    {{
      "Asset": "资产代码",
      "Target_Price": "目标价",
      "Timeframe": "时间窗口",
      "Technical_Levels": ["支撑位", "阻力位"]
    }}
  ],
  "Statistical_Evidence": "统计依据",
  "Tail_Risks": ["风险1", "风险2"]
}}

## 重要规则

1. **Rating_Changes 优先**：尽可能多提取具体的评级变化事件，哪怕只有标题信息也要列出。
2. 只基于提供的数据，不要编造。
所有文本字段使用中文（股票代码和评级英文保留）。
4. 输出必须是合法JSON。
"""

    headers = {
        "Authorization": f"Bearer {KIMI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": KIMI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        # K3 会先输出较长 thinking；快速模式也需留足最终 JSON 空间。
        "max_tokens": 16000,
        "response_format": {"type": "json_object"},
    }

    analysis_provider = f"kimi/{KIMI_MODEL}"
    result = {}
    primary_error = "KIMI_API_KEY not set"
    if not IB_RESEARCH_USE_KIMI:
        analysis_provider = IB_RESEARCH_FALLBACK_MODEL
        result = _call_openclaw_analysis_fallback(
            prompt,
            timeout=max(KIMI_SUMMARY_TIMEOUT_SECONDS * 2, 750),
        )
        parsed, primary_error = _parse_summary_analysis_response(result)
        if primary_error:
            print(
                "     低成本主路由不可用 "
                f"({primary_error})，继续备用链..."
            )
    elif KIMI_API_KEY:
        post_json = _http_post_json_once if fast else _http_post_json
        result = post_json(
            KIMI_URL, payload, headers, timeout=KIMI_SUMMARY_TIMEOUT_SECONDS
        )
        parsed, primary_error = _parse_summary_analysis_response(result)
    else:
        parsed = None
        print("     KIMI_API_KEY 未配置，跳过 Kimi 主路由")

    if primary_error:
        # 快速刷新现在就是固定 4 小时的分析节奏，Kimi 不可用时同样走 Qwen /
        # OpenClaw 兜底链（不再像旧设计那样“快速批次不兜底、等下一轮”——那会让
        # Kimi 限额期间每次 fast 都降级，页面长期挂在降级横幅）。

        # ── Qwen fallback（阿里云百炼，国内直连，优先于 OpenClaw agent） ──
        qwen_tried = False
        if _HAS_QWEN:
            try:
                print(
                    "     Kimi 主路由不可用 "
                    f"({primary_error})，切换 Qwen (阿里云百炼)..."
                )
                qwen_text, _ = _call_qwen(
                    [{"role": "user", "content": prompt}],
                    max_tokens=16000,
                    # 低温：分析输出必须是可解析 JSON，降低温度减少偶发的格式
                    # 畸形（否则 Qwen 解析失败会重复调用备用模型）。
                    temperature=0.1,
                    timeout=KIMI_SUMMARY_TIMEOUT_SECONDS,
                )
                qwen_tried = True
                # 尝试解析 Qwen 的 JSON 输出
                parsed_qwen = _parse_kimi_json(qwen_text)
                if parsed_qwen:
                    analysis_provider = "qwen/qwen3.6-plus"
                    parsed = parsed_qwen
                    primary_error = None  # 清除错误标记
                    print(f"     Qwen 分析成功 ({len(qwen_text)} 字符)")
                else:
                    print("     [WARN] Qwen 返回内容无法解析为 JSON")
            except Exception as qwen_exc:
                print(f"     [WARN] Qwen 调用失败: {qwen_exc}")

        if primary_error:
            print(
                "     Kimi 主路由不可用 "
                f"({primary_error})，切换 OpenClaw 备用模型 "
                f"{IB_RESEARCH_FALLBACK_MODEL}..."
            )
            # 仅当 Kimi 与 Qwen 都未能产出可用分析时，才调用 OpenClaw 兜底；
            # 否则会把 Qwen 的成功结果覆盖掉（旧代码此处无条件调用，导致 Qwen
            # 成功时 provider/摘要仍被备用模型覆盖，并白白多调一次模型）。
            result = _call_openclaw_analysis_fallback(
                prompt,
                timeout=max(KIMI_SUMMARY_TIMEOUT_SECONDS * 2, 750),
            )
            analysis_provider = str(
                result.get("_provider", IB_RESEARCH_FALLBACK_MODEL)
            )
            parsed, fallback_error = _parse_summary_analysis_response(result)
            if fallback_error:
                print(
                    "[ERROR] Kimi 主路由与 Qwen / OpenClaw 兜底均失败: "
                    f"primary={primary_error}; fallback={fallback_error}"
                )
                return {
                    "error": (
                        "Kimi primary and Qwen/OpenClaw fallbacks all failed: "
                        f"{fallback_error}"
                    ),
                    "raw_data": raw_data,
                }

    # 规范化 Rating_Changes：去重、回填 null、清洗字段
    historical = historical_reports if historical_reports is not None else _load_historical_reports(days=7)
    parsed["Rating_Changes"] = _normalize_rating_changes(
        parsed.get("Rating_Changes", []), historical
    )
    parsed = _fix_summary_metadata(parsed, raw_data)
    parsed["_analysis_provider"] = analysis_provider
    payload_error = _analysis_payload_error(parsed)
    if payload_error:
        return {"error": f"Kimi normalized summary invalid: {payload_error}"}
    return parsed


def _recent_analyzed_keys_from_reports(reports: List[Dict], hours: int = 48) -> set:
    """从已加载的历史报告中提取最近 N 小时内分析过的变化 key"""
    analyzed_keys = set()
    cutoff = datetime.now() - timedelta(hours=hours)
    for report in reports or []:
        generated_at = report.get("meta", {}).get("generated_at", "")
        if not generated_at:
            continue
        try:
            dt = datetime.fromisoformat(generated_at)
            if dt < cutoff:
                continue
        except ValueError:
            continue
        summary = _load_summary(report)
        if not summary:
            continue
        for c in summary.get("Rating_Changes", []):
            key = _change_key_string(c)
            if key:
                analyzed_keys.add(key)
    return analyzed_keys


def _detect_changes(old_report: Dict, new_report: Dict, recent_reports: List[Dict] = None) -> List[Dict]:
    """对比新旧报告，检测关键变化（排除48h内已分析过的）"""
    changes = []
    analyzed_keys = _recent_analyzed_keys_from_reports(recent_reports, hours=48)

    old_changes = {}
    new_changes = {}

    old_summary = _load_summary(old_report) or {}
    new_summary = _load_summary(new_report) or {}

    for c in old_summary.get("Rating_Changes", []):
        key = _change_key_string(c)
        if key:
            old_changes[key] = c

    for c in new_summary.get("Rating_Changes", []):
        key = _change_key_string(c)
        if key:
            new_changes[key] = c

    # 找出新增的评级变化（排除48h内已分析过的）
    for key, change in new_changes.items():
        if key not in old_changes and key not in analyzed_keys:
            # 检查同一股票同一投行是否有观点变化
            symbol = change.get("Symbol", "")
            bank = change.get("Bank", "")
            old_rating_for_same = None
            for ok, oc in old_changes.items():
                if oc.get("Symbol") == symbol and oc.get("Bank") == bank:
                    old_rating_for_same = oc.get("New_Rating", "")
                    break

            if old_rating_for_same and old_rating_for_same != change.get("New_Rating", ""):
                # 观点发生了变化，高优先级
                changes.append({
                    "type": "rating_changed",
                    "priority": "high",
                    "message": f"{bank} 调整 {symbol} 评级: {old_rating_for_same} -> {change.get('New_Rating', 'N/A')}",
                    "detail": change,
                })
            else:
                # 全新的评级
                changes.append({
                    "type": "new_rating",
                    "priority": "high",
                    "message": f"{bank} {change.get('Action', '')} {symbol} to {change.get('New_Rating', 'N/A')}",
                    "detail": change,
                })

    # 检测目标价变化（同一股票目标价调整）
    old_targets = {f"{t.get('Asset', '')}": t for t in old_summary.get("Asset_Targets", [])}
    new_targets = {f"{t.get('Asset', '')}": t for t in new_summary.get("Asset_Targets", [])}

    for asset, new_target in new_targets.items():
        if asset in old_targets:
            old_price = old_targets[asset].get("Target_Price", "")
            new_price = new_target.get("Target_Price", "")
            if old_price != new_price:
                # 检查48h内是否已分析过该股票的目标价变化
                price_key = f"TARGET:{asset}:{new_price}"
                if price_key not in analyzed_keys:
                    changes.append({
                        "type": "price_target_change",
                        "priority": "medium",
                        "message": f"{asset} 目标价调整: {old_price} -> {new_price}",
                        "detail": {"asset": asset, "old": old_price, "new": new_price},
                    })

    return changes


def analyze_changes_with_kimi(changes: List[Dict], new_report: Dict) -> Dict[str, Any]:
    """使用 Kimi API 实时分析变化的意义"""
    if not changes:
        return {"analysis": "无显著变化", "changes": changes}

    changes_text = "\n".join([
        f"- [{c['priority'].upper()}] {c['message']}"
        for c in changes[:10]
    ])

    core_thesis = new_report.get("summary", {}).get("Core_Thesis", "")

    prompt = f"""你是一位实时市场分析师。以下是最新检测到的投行评级变化，请快速分析其市场意义。

## 检测到的变化
{changes_text}

## 当前市场核心观点
{core_thesis[:300]}

## 分析要求
1. 用1-2句话总结最重要的变化
2. 指出哪个变化最值得关注（高优先级）
3. 简要说明对市场可能的影响
4. 所有文本使用中文

## 输出格式
输出纯文本，不要markdown，不要JSON。格式：
【重要变化】...
【市场影响】...
"""

    headers = {
        "Authorization": f"Bearer {KIMI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": KIMI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4000,
    }

    result = {}
    if IB_RESEARCH_USE_KIMI and KIMI_API_KEY:
        result = _http_post_json(KIMI_URL, payload, headers, timeout=60)
    if result:
        try:
            analysis = _extract_kimi_text(result)
            if analysis is not None:
                return {"analysis": analysis, "changes": changes}
        except Exception:
            pass

    # Kimi 不可用（限额/网络）时，和主分析一样走 Qwen 兜底，避免变化分析长期空缺。
    if _HAS_QWEN:
        try:
            print("     [变化分析] Kimi 不可用，切换 Qwen (阿里云百炼)...")
            qwen_text, _ = _call_qwen(
                [{"role": "user", "content": prompt}],
                max_tokens=2000,
                temperature=0.3,
                timeout=60,
            )
            if qwen_text and qwen_text.strip():
                return {"analysis": qwen_text.strip(), "changes": changes}
        except Exception as qwen_exc:
            print(f"     [WARN] 变化分析 Qwen 兜底失败: {qwen_exc}")

    # 全部失败：返回空分析，让页面回退展示 Core_Thesis，而不是“分析服务暂时不可用”。
    return {"analysis": "", "changes": changes}


def _extend_unique(all_news: List[Dict], items: List[Dict], seen_urls: set) -> int:
    """将 items 中 url 未出现过的条目追加到 all_news，返回新增数量"""
    added = 0
    for item in items:
        url = item.get("url", "")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        all_news.append(item)
        added += 1
    return added


@_single_fetch_process
def run_fetch(force: bool = False, fast: bool = False) -> Dict[str, Any]:
    """主流程：获取数据并生成报告

    Args:
        force: 是否强制重新生成，跳过增量检测
        fast: 是否仅进行快速刷新（只抓 RSS，按 4 小时采集并批量分析）
    """
    mode_label = "快速刷新" if fast else "完整刷新"
    print(f"[{datetime.now()}] 开始 {mode_label} 外资投行研报数据...")

    state = _load_state()
    now = datetime.now()
    prev_report = None
    summary_refreshed = False
    analysis_succeeded = False
    analysis_pending = False
    new_news_count = 0
    pending_rating_count = len(state.get("pending_rating_items", []) or [])
    rating_batch_triggered = False
    rating_batch_succeeded = False
    yahoo_enrichment: Dict[str, Dict] = {}
    # 兜底默认值：即使完整模式的某个采集步骤抛异常，也保证通用后续处理里
    # recommendations / news 已定义，报告仍能落盘（不允许非关键步骤中断整次刷新）。
    recommendations: List[Dict] = []
    news: List[Dict] = []

    if fast:
        # ========== 快速模式：基于今日报告增量更新 ==========
        today_report = _load_today_report() or _load_latest_cached_report()
        recommendations = today_report.get("raw", {}).get("recommendations", []) if today_report else []
        existing_news = list(
            today_report.get("raw", {}).get("news", []) if today_report else []
        )
        all_news = list(existing_news)
        seen_urls = _state_seen_urls(state)
        for n in all_news:
            if n.get("url"):
                seen_urls.add(n["url"])

        cutoff_date = (now - timedelta(days=RSS_MAX_AGE_DAYS)).strftime("%Y%m%d")
        fetched_items = _fetch_fast_sources(seen_urls)
        new_items = _filter_new_news_items(
            state, existing_news, fetched_items, cutoff_date
        )
        new_news_count = len(new_items)
        print(
            f"  -> 快速源候选 {len(fetched_items)} 条，"
            f"去旧闻/重复后新增 {new_news_count} 条"
        )
        all_news.extend(new_items)
        all_news.sort(key=lambda x: x.get("time_published", ""), reverse=True)

        news = [n for n in all_news if n.get("time_published", "")[:8] >= cutoff_date]
        print(f"     过滤 {RSS_MAX_AGE_DAYS} 天前旧数据后: {len(news)} 条")

        data_hash = _compute_data_hash(recommendations, news)
        print(f"  -> 数据指纹: {data_hash}")

        previous_summary = _load_previous_summary_fast(today_report)
        historical = _load_historical_reports(days=RATING_CHANGES_MAX_AGE_DAYS)
        prev_report = today_report

        baseline_events = _load_rating_ledger() + _extract_verified_rating_events(
            existing_news
        )
        new_verified_events = _merge_rating_changes(
            [],
            _extract_verified_rating_events(new_items)
            + _extract_pt_events_from_summaries(new_items),
        )
        pending_rating_items = _update_rating_batch_state(
            state,
            baseline_events=baseline_events,
            new_events=new_verified_events,
            now=now,
        )
        pending_rating_count = len(pending_rating_items)
        # 先落盘队列，再进行任何可能耗时的模型/网络步骤，避免中断丢失水位。
        _save_state(state)
        print(
            "  -> 评级批次: "
            f"本轮新增 {len(new_verified_events)} 条，"
            f"待分析 {pending_rating_count} 条"
        )

        raw_data = {
            "fetch_time": now.isoformat(),
            "recommendations": recommendations,
            "news": news,
            "pending_rating_events": pending_rating_items,
        }
        has_degraded_summary = bool(
            not isinstance(previous_summary, dict)
            or _analysis_payload_error(previous_summary) is not None
            or previous_summary.get("_fallback")
        )
        # 语料（新闻+推荐）相对上次报告无变化，且现有摘要可用时复用，避免空转模型。
        previous_data_hash = str(
            (today_report or {}).get("meta", {}).get("data_hash", "") or ""
        ) or _compute_data_hash(recommendations, existing_news)
        corpus_unchanged = bool(
            not force
            and pending_rating_count == 0
            and not has_degraded_summary
            and previous_data_hash
            and previous_data_hash == data_hash
        )
        # 固定每 4 小时刷新分析：只要语料有变化或摘要降级就调用一次 Kimi，
        # 不再设置“累计 N 条评级”门槛；仅在分析失败后的冷却期内暂缓重试。
        analysis_due = (not corpus_unchanged) and _fast_batch_should_analyze(
            force,
            pending_rating_count,
            state.get("last_rating_batch_error_at", ""),
            now,
        )

        if analysis_due:
            rating_batch_triggered = True
            print("  -> 本轮数据有更新，调用 Kimi 刷新分析（固定 4 小时节奏）...")
            state["last_summary_attempt"] = now.isoformat()
            refreshed = summarize_with_kimi(
                raw_data, previous_summary, historical, fast=True
            )
            if isinstance(refreshed, dict) and "error" not in refreshed:
                summary = refreshed
                summary_refreshed = True
                analysis_succeeded = _analysis_payload_error(summary) is None
                summary["_incremental"] = True
                summary["_note"] = "快速刷新：已分析最新数据"
                if analysis_succeeded:
                    state["last_summary_fetch"] = now.isoformat()
                    state["last_summary_hash"] = data_hash
                    _mark_rating_batch_analyzed(state, pending_rating_items)
                    pending_rating_count = 0
                    rating_batch_succeeded = True
            else:
                fallback_reason = (
                    refreshed.get("error", "Kimi API failed")
                    if isinstance(refreshed, dict)
                    else "Kimi API failed"
                )
                summary = (previous_summary or refreshed or {}).copy()
                # 只有当摘要真的降级（整条 Kimi→Qwen→OpenClaw 链都失败、保留了
                # 陈旧摘要）时才登记错误时间戳并进入冷却；若兜底模型已成功刷新
                # 摘要，则不进入冷却，避免“一次瞬时失败锁死 4 小时分析”。
                summary_is_degraded = bool(
                    summary.get("_fallback") or summary.get("_analysis_error")
                )
                analysis_pending = pending_rating_count > 0 or summary_is_degraded
                if analysis_pending:
                    summary["_analysis_pending"] = True
                summary["_analysis_error"] = fallback_reason
                summary["_note"] = (
                    "快速刷新：本轮分析暂时失败，保留上次摘要和待处理评级"
                )
                if summary_is_degraded:
                    state["last_rating_batch_error"] = fallback_reason
                    state["last_rating_batch_error_at"] = now.isoformat()
                    _save_state(state)
        else:
            summary = (previous_summary or {}).copy()
            summary["_incremental"] = True
            if corpus_unchanged:
                wait_reason = "本轮语料与上次分析一致，复用现有摘要"
                summary.pop("_analysis_pending", None)
                summary.pop("_analysis_error", None)
                summary["_note"] = f"快速刷新：{wait_reason}"
                analysis_succeeded = not has_degraded_summary
            else:
                wait_reason = "上次批量分析失败，处于冷却期，保留上次摘要"
                summary["_note"] = f"快速刷新：{wait_reason}"
                analysis_pending = pending_rating_count > 0 or bool(
                    summary.get("_analysis_error")
                )
                if pending_rating_count > 0:
                    summary["_analysis_pending"] = True
            print(f"  -> {wait_reason}")

        reddit_posts = _load_today_reddit_posts()
        yahoo_enrichment = (
            today_report.get("raw", {}).get("yahoo_enrichment", {}) if today_report else {}
        )
        state["last_fast_fetch"] = now.isoformat()

    else:
        # ========== 完整模式：全量抓取 ==========
        print("  -> 获取 Finnhub 分析师推荐...")
        try:
            recommendations = fetch_finnhub_recommendations(HOT_SYMBOLS) or []
        except Exception as exc:
            recommendations = []
            print(f"     [WARN] Finnhub 推荐获取失败，降级为空: {type(exc).__name__}: {exc}")
        print(f"     获取到 {len(recommendations)} 条推荐数据")

        print("  -> 获取 Yahoo 评级/基本面增强...")
        try:
            yahoo_enrichment = fetch_yahoo_enrichment(HOT_SYMBOLS) or {}
        except Exception as exc:
            yahoo_enrichment = {}
            print(f"     [WARN] Yahoo 增强获取失败，降级为空: {type(exc).__name__}: {exc}")

        print("  -> 获取 Alpha Vantage 新闻...")
        all_news = []
        seen_urls = set()

        try:
            batch = fetch_alpha_vantage_news(topics="technology,finance,financial_markets", limit=1000)
        except Exception as exc:
            batch = []
            print(f"     [WARN] Alpha Vantage 新闻获取失败，降级为空: {type(exc).__name__}: {exc}")
        _extend_unique(all_news, batch, seen_urls)
        print(f"     Alpha Vantage 去重后: {len(all_news)} 条")

        print("  -> 获取 Finnhub 新闻补充源...")
        try:
            finnhub_news = fetch_finnhub_news(HOT_SYMBOLS)
        except Exception as exc:
            finnhub_news = []
            print(f"     [WARN] Finnhub 新闻获取失败，降级为空: {type(exc).__name__}: {exc}")
        _extend_unique(all_news, finnhub_news, seen_urls)
        print(f"     Finnhub 补充后累计: {len(all_news)} 条")

        print("  -> 获取 Seeking Alpha 新闻...")
        try:
            sa_news = fetch_seeking_alpha_news(HOT_SYMBOLS)
        except Exception as exc:
            sa_news = []
            print(f"     [WARN] Seeking Alpha 新闻获取失败，降级为空: {type(exc).__name__}: {exc}")
        _extend_unique(all_news, sa_news, seen_urls)
        print(f"     Seeking Alpha 补充后累计: {len(all_news)} 条")

        print("  -> 获取 Reddit 讨论...")
        try:
            reddit_posts = fetch_reddit_discussions()
        except Exception as exc:
            reddit_posts = []
            print(f"     [WARN] Reddit 讨论获取失败，降级为空: {type(exc).__name__}: {exc}")
        _extend_unique(all_news, reddit_posts, seen_urls)
        print(f"     Reddit 补充后累计: {len(all_news)} 条")

        print("  -> 获取 Google News RSS...")
        try:
            google_news = fetch_google_news_rss(HOT_SYMBOLS)
        except Exception as exc:
            google_news = []
            print(f"     [WARN] Google News RSS 获取失败，降级为空: {type(exc).__name__}: {exc}")
        _extend_unique(all_news, google_news, seen_urls)
        print(f"     Google News 补充后累计: {len(all_news)} 条")

        print("  -> 获取扩展 RSS 数据源...")
        try:
            rss_sources = _fetch_all_rss_sources(seen_urls)
        except Exception as exc:
            rss_sources = {}
            print(f"     [WARN] 扩展 RSS 获取失败，降级为空: {type(exc).__name__}: {exc}")
        for items in rss_sources.values():
            _extend_unique(all_news, items, seen_urls)
        print(f"     扩展 RSS 补充后累计: {len(all_news)} 条")

        all_news.sort(key=lambda x: x.get("time_published", ""), reverse=True)
        cutoff_date = (now - timedelta(days=RSS_MAX_AGE_DAYS)).strftime("%Y%m%d")
        news = [n for n in all_news if n.get("time_published", "")[:8] >= cutoff_date]
        print(f"     过滤 {RSS_MAX_AGE_DAYS} 天前旧数据后: {len(news)} 条")

        data_hash = _compute_data_hash(recommendations, news)
        print(f"  -> 数据指纹: {data_hash}")

        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        yesterday_path = os.path.join(CACHE_DIR, f"report_{yesterday}.json")
        previous_summary = None
        previous_analysis_hash = ""
        prev_report = _load_today_report()

        if prev_report is None and os.path.exists(yesterday_path):
            try:
                with open(yesterday_path, "r", encoding="utf-8") as f:
                    prev_report = json.load(f)
            except Exception:
                pass
        if isinstance(prev_report, dict):
            previous_meta = prev_report.get("meta", {})
            previous_analysis_hash = str(
                previous_meta.get("analysis_data_hash")
                or state.get("last_summary_hash")
                or previous_meta.get("data_hash")
                or ""
            )
            previous_summary = prev_report.get("summary")
            if isinstance(previous_summary, str):
                try:
                    previous_summary = json.loads(previous_summary)
                except json.JSONDecodeError:
                    previous_summary = None

        historical = _load_historical_reports(days=RATING_CHANGES_MAX_AGE_DAYS)

        if _can_reuse_full_analysis(
            force,
            len(state.get("pending_rating_items", []) or []),
            previous_analysis_hash,
            data_hash,
            previous_summary,
        ):
            print("  -> 数据无变化，跳过 Kimi API 调用，复用最近一次分析")
            summary = previous_summary.copy()
            summary.pop("_analysis_pending", None)
            summary.pop("_analysis_error", None)
            summary["_incremental"] = True
            summary["_note"] = "数据无显著变化，分析结果与昨日一致"
            state["last_summary_hash"] = data_hash
            analysis_succeeded = True
        else:
            print("  -> 使用 Kimi API 生成结构化总结...")
            raw_data = {
                "fetch_time": now.isoformat(),
                "recommendations": recommendations,
                "news": news,
                "pending_rating_events": list(
                    state.get("pending_rating_items", []) or []
                ),
            }
            state["last_summary_attempt"] = now.isoformat()
            summary = summarize_with_kimi(raw_data, previous_summary, historical)

            if isinstance(summary, dict) and "error" in summary:
                fallback_reason = summary.get("error", "Kimi API failed")
                print(f"  -> Kimi 调用失败，尝试降级... (错误: {fallback_reason})")
                fallback = previous_summary if (previous_summary and isinstance(previous_summary, dict) and "error" not in previous_summary) else _load_previous_summary()
                if fallback:
                    print("  -> 降级成功：复用前一天的 summary")
                    summary = fallback.copy()
                    summary["_fallback"] = True
                    summary["_fallback_reason"] = fallback_reason
                else:
                    print("  -> 降级失败：无可用历史数据")
            elif _analysis_payload_error(summary) is None:
                summary_refreshed = True
                analysis_succeeded = True
                state["last_summary_fetch"] = now.isoformat()
                state["last_summary_hash"] = data_hash

        state["last_full_fetch"] = now.isoformat()

    # ========== 通用后续处理 ==========
    raw_data = {
        "fetch_time": now.isoformat(),
        "recommendations": recommendations,
        "news": news,
        "yahoo_enrichment": yahoo_enrichment,
    }
    summary = summary if isinstance(summary, dict) else {}
    summary = _fix_summary_metadata(summary, raw_data)

    print(f"  -> 合并 {RATING_CHANGES_MAX_AGE_DAYS} 天评级事件账本...")
    current_rating_changes = []
    for change in summary.get("Rating_Changes", []) or []:
        if not isinstance(change, dict):
            continue
        event = dict(change)
        if summary_refreshed:
            event["_Last_Seen"] = now.strftime("%Y-%m-%d")
        current_rating_changes.append(event)

    # 正则直接从标题/摘要提取已验证评级事件（不依赖 LLM），与 LLM 结果一起进账本
    verified_events = _merge_rating_changes(
        [],
        _extract_verified_rating_events(news)
        + _extract_pt_events_from_summaries(news),
    )
    print(f"     标题/摘要正则验证事件: {len(verified_events)} 条")
    # 先用已验证事件回填 LLM 事件中缺失的目标价/评级（软标题缺数字的情况）
    backfilled = _backfill_missing_price_targets(current_rating_changes, verified_events)
    if backfilled:
        print(f"     回填缺失目标价/评级: {backfilled} 处")
    rating_ledger = _merge_rating_changes(
        _load_rating_ledger(), current_rating_changes + verified_events
    )
    # 用 Yahoo 评级事件(from→to 评级 + 目标价)回填缺失的 OLD/NEW/TARGET，并补充账本外新事件
    try:
        yahoo_filled = _backfill_from_yahoo(rating_ledger, HOT_SYMBOLS)
        if yahoo_filled:
            print(f"     Yahoo 回填缺失评级/目标价: {yahoo_filled} 处")
            rating_ledger = _merge_rating_changes([], rating_ledger)
    except Exception as exc:
        print(f"     [WARN] Yahoo 评级回填失败，跳过: {type(exc).__name__}: {exc}")
    # 韩股(SK Hynix/三星)评级:Google News 韩语 RSS,覆盖 Yahoo/Finnhub 拿不到的韩股
    try:
        kr_events = fetch_korean_rating_news()
        if kr_events:
            rating_ledger = _merge_rating_changes(rating_ledger, kr_events)
            print(f"     韩股评级事件: +{len(kr_events)} 条")
    except Exception as exc:
        print(f"     [WARN] 韩股评级获取失败，跳过: {type(exc).__name__}: {exc}")
    # 用 30 天账本里同投行/同股票的更早评级回填旧评级，让 OLD/NEW 能配上对
    old_filled = _backfill_old_ratings(rating_ledger)
    if old_filled:
        print(f"     回填旧评级: {old_filled} 处")

    # 最终兜底：upgrade/downgrade 仍缺 New_Rating 的事件视为不完整，从输出/账本中剔除。
    # 这些事件通常来自“只有方向、没有评级”的标题，或 LLM 未按提示丢弃的半成品；
    # 保留它们会让健康检查持续报警，且对下游展示没有价值。
    before_filter = len(rating_ledger)
    rating_ledger = [
        event
        for event in rating_ledger
        if not (
            str(event.get("Action", "")).lower() in ("upgrade", "downgrade")
            and not _clean_field(event.get("New_Rating"))
        )
    ]
    dropped = before_filter - len(rating_ledger)
    if dropped:
        print(f"     剔除不完整 upgrade/downgrade 事件: {dropped} 条")

    summary["Rating_Changes"] = rating_ledger
    _save_rating_ledger(rating_ledger)
    print(f"     当前保留 {len(rating_ledger)} 条独立评级事件")

    # 每日完整分析覆盖当前数据后，作为低频评级的兜底并清空待分析批次。
    if not fast and analysis_succeeded:
        _mark_rating_batch_analyzed(state, verified_events)
        pending_rating_count = 0

    # AV 官方分析师共识（目标价均值 + 评级分布），免费版每日限 25 次
    av_overviews: Dict[str, Dict] = {}
    if ALPHA_VANTAGE_KEY and not fast:
        overview_symbols = set()
        for item in summary.get("Asset_Targets", []) or []:
            if isinstance(item, dict) and item.get("Asset"):
                overview_symbols.add(str(item["Asset"]).strip().upper())
        for event in verified_events:
            if event.get("Symbol"):
                overview_symbols.add(event["Symbol"])
        print("  -> 获取 Alpha Vantage 分析师共识...")
        try:
            av_overviews = fetch_alpha_vantage_overviews(sorted(overview_symbols))
        except Exception as exc:
            av_overviews = {}
            print(
                "     [WARN] Alpha Vantage 共识获取失败，降级为空（不影响报告落盘）: "
                f"{type(exc).__name__}: {exc}"
            )
    elif fast:
        cache_path = os.path.join(CACHE_DIR, f"av_overview_{now.strftime('%Y-%m-%d')}.json")
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                av_overviews = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            av_overviews = {}

    # 用已验证数据重建资产目标卡片：每只股票一张，按可信度排序
    summary["Asset_Targets"] = _build_verified_asset_targets(
        summary.get("Asset_Targets", []) or [],
        verified_events,
        av_overviews,
        news,
    )
    summary["_av_overviews"] = av_overviews
    summary["_verified_event_count"] = len(verified_events)

    print("  -> 更新 Reddit 讨论摘要...")
    if fast:
        reddit_summary = {}
        for candidate in [today_report] + list(historical or []):
            if isinstance(candidate, dict) and isinstance(
                candidate.get("reddit_summary"), dict
            ):
                reddit_summary = candidate["reddit_summary"]
                break
        if not reddit_summary:
            reddit_summary = {
                "sentiment": "neutral",
                "summary": "快速采集不单独调用 AI；等待每日完整分析更新。",
                "key_topics": [],
            }
    else:
        try:
            reddit_summary = summarize_reddit_with_kimi(reddit_posts)
        except Exception as exc:
            reddit_summary = {
                "sentiment": "neutral",
                "summary": "Reddit 情绪分析失败，降级为中性。",
                "key_topics": [],
            }
            print(f"     [WARN] Reddit 情绪分析失败，降级: {type(exc).__name__}: {exc}")
    print(f"     Reddit 情绪: {reddit_summary.get('sentiment', 'unknown')}")

    print("  -> 分析历史趋势...")
    historical = _load_historical_reports(days=7)
    today_key = now.strftime("%Y-%m-%d")
    prior_reports = [
        r for r in historical
        if str(r.get("meta", {}).get("generated_at", ""))[:10] != today_key
    ]
    try:
        trends = _analyze_trends(
            [{"meta": {"generated_at": now.isoformat()}, "summary": summary}]
            + prior_reports[:6]
        )
    except Exception as exc:
        trends = {}
        print(f"     [WARN] 历史趋势分析失败，降级为空: {type(exc).__name__}: {exc}")

    change_analysis = None
    if prev_report:
        print("  -> 检测数据变化...")
        try:
            changes = _detect_changes(prev_report, {"summary": summary}, historical)
        except Exception as exc:
            changes = []
            print(f"     [WARN] 数据变化检测失败，降级为空: {type(exc).__name__}: {exc}")
        if changes:
            print(f"     检测到 {len(changes)} 项变化")
            if fast:
                if rating_batch_succeeded:
                    analysis_text = (
                        f"本轮 {len(changes)} 项变化已合并进同一次批量分析。"
                    )
                else:
                    analysis_text = (
                        f"本轮检测到 {len(changes)} 项变化，"
                        "已纳入固定 4 小时分析节奏，暂时保留上次结论。"
                    )
                change_analysis = {
                    "analysis": analysis_text,
                    "changes": changes,
                    "_batched": True,
                }
                print("     快速模式已跳过独立变化分析模型调用")
            else:
                print("  -> 使用 Kimi 实时分析变化...")
                try:
                    change_analysis = analyze_changes_with_kimi(
                        changes, {"summary": summary}
                    )
                except Exception as exc:
                    change_analysis = None
                    print(f"     [WARN] 变化实时分析失败，降级为空: {type(exc).__name__}: {exc}")
                if change_analysis:
                    print(
                        f"     分析完成: "
                        f"{change_analysis.get('analysis', '')[:100]}..."
                    )
        else:
            print("     无显著变化")

    data_sources = [
        "finnhub", "alpha_vantage", "seeking_alpha", "reddit", "google_news",
        "yahoo_finance", "benzinga", "barrons", "marketwatch", "zacks", "streetinsider",
        "semianalysis",
    ]

    if "error" in summary or summary.get("_fallback"):
        analysis_status = "degraded"
    elif analysis_pending:
        analysis_status = "pending"
    elif not analysis_succeeded:
        analysis_status = "degraded"
    else:
        analysis_status = "ok"

    report = {
        "meta": {
            "generated_at": now.isoformat(),
            "data_sources": data_sources,
            "coverage_banks": TARGET_BANKS,
            "excluded": "中国本土投行 (中金、中信、华泰等)",
            "data_hash": data_hash,
            "incremental": not force and state.get("last_hash") == data_hash,
            "refresh_mode": "fast" if fast else "full",
            "analysis_status": analysis_status,
            "analysis_provider": summary.get("_analysis_provider", "cached"),
            "analysis_generated_at": state.get("last_summary_fetch", ""),
            "analysis_data_hash": state.get("last_summary_hash", ""),
            "raw_data_hash": data_hash,
            "rating_window_days": RATING_CHANGES_MAX_AGE_DAYS,
            "rating_event_count": len(rating_ledger),
            "refresh_interval_minutes": FAST_REFRESH_INTERVAL_MIN,
            "new_news_count": new_news_count,
            "pending_rating_count": pending_rating_count,
            "rating_batch_triggered": rating_batch_triggered,
            "rating_batch_succeeded": rating_batch_succeeded,
            "run_id": os.environ.get("IB_RESEARCH_RUN_ID", ""),
        },
        "summary": summary,
        "trends": trends,
        "change_analysis": change_analysis,
        "reddit_summary": reddit_summary,
        "raw": {
            "fetch_time": now.isoformat(),
            "recommendations": recommendations,
            "reddit_posts": reddit_posts,
            "news": news,
        },
    }

    issues = _validate_report(report)
    drift_issues = _llm_symbol_drift_issues(
        summary.get("Asset_Targets", []) or [], verified_events, av_overviews
    )
    for drift in drift_issues:
        print(f"     [WARN] {drift}")
    report["meta"]["quality_status"] = "degraded" if issues else "ok"

    cache_path = os.path.join(CACHE_DIR, f"report_{now.strftime('%Y-%m-%d')}.json")
    _backup_report(cache_path)
    temp_cache_path = f"{cache_path}.tmp"
    with open(temp_cache_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    os.replace(temp_cache_path, cache_path)
    print(f"  -> 报告已保存: {cache_path}")

    _state_mark_seen(state, [n.get("url", "") for n in news])
    _state_mark_news_seen(state, news)
    state["last_hash"] = data_hash
    _save_state(state)

    _ensure_ib_research_server_running()
    if issues:
        print(f"  -> 数据质量警告 ({len(issues)} 项):")
        for issue in issues:
            print(f"     • {issue}")
        _send_alert_if_needed(issues)
    else:
        print("  -> 数据质量检查通过")

    _refresh_dashboard()
    return report


def _ensure_ib_research_server_running():
    """确保 ib_research_server.py 在 8081 端口运行；如未运行则尝试启动"""
    try:
        resp = requests.get(
            "http://localhost:8081/api/ib-research",
            headers={"User-Agent": "ib-research-fetcher/1.0"},
            timeout=1,
        )
        if resp.status_code == 200:
            return
    except Exception:
        pass

    print("  -> IB Research server (8081) 未响应，尝试启动...")
    try:
        server_script = os.path.join(os.path.dirname(__file__), "ib_research_server.py")
        subprocess.Popen(
            [sys.executable, server_script],
            cwd=os.path.dirname(__file__),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print("  -> 已尝试启动 IB Research server")
    except Exception as e:
        print(f"  -> 启动 IB Research server 失败: {e}")


def _validate_report(report: Dict[str, Any]) -> List[str]:
    """数据质量检查，返回问题列表（空列表表示通过）"""
    issues = []
    summary = report.get("summary", {})
    raw = report.get("raw", {})
    meta = report.get("meta", {})
    is_fast = meta.get("refresh_mode") == "fast"

    # 1. Kimi 生成失败
    if isinstance(summary, dict) and not is_fast:
        if "error" in summary:
            issues.append(f"Kimi 分析失败: {summary.get('error', 'unknown')}")
        elif summary.get("_fallback"):
            issues.append(
                "Kimi 分析降级，当前展示历史总结: "
                f"{summary.get('_fallback_reason', 'unknown')}"
            )

    # 2. 新闻数量过少（快速模式阈值更低）
    news_count = len(raw.get("news", []))
    min_news = 1 if is_fast else 5
    if news_count < min_news:
        issues.append(f"新闻数量过少: {news_count} 条（预期 >= {min_news}）")

    # 3. Rating_Changes 为空（增量/快速模式允许）
    is_incremental = meta.get("incremental", False)
    rating_changes = summary.get("Rating_Changes", []) if isinstance(summary, dict) else []
    if not rating_changes and not is_incremental and not is_fast:
        issues.append("未提取到任何评级变化(Rating_Changes)")

    # 4. 数据源检查（完整模式才检查 Finnhub 推荐）
    if not is_fast:
        rec_count = len(raw.get("recommendations", []))
        if rec_count == 0:
            issues.append("Finnhub 推荐数据为空")

    return issues


def _send_alert_if_needed(issues: List[str]) -> None:
    """如有问题，通过 QQ 发送告警"""
    if not issues:
        return

    try:
        from personal_dashboard.scripts.cron_utils import send_qq_message
    except ImportError:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "cron_utils",
                os.path.expanduser("~/.openclaw/workspace/personal-dashboard/scripts/cron_utils.py"),
            )
            cron_utils = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cron_utils)
            send_qq_message = cron_utils.send_qq_message
        except Exception:
            print(f"[WARN] 无法加载 cron_utils，跳过 QQ 告警")
            for issue in issues:
                print(f"  [ALERT] {issue}")
            return

    msg_lines = ["⚠️ 外资投行研报数据异常"]
    msg_lines.extend(f"  • {issue}" for issue in issues)
    msg_lines.append(f"\n时间: {datetime.now().strftime('%m-%d %H:%M')}")
    msg = "\n".join(msg_lines)

    ok = send_qq_message(msg)
    if ok:
        print("  -> QQ 告警已发送")
    else:
        print("  -> QQ 告警发送失败")


def _refresh_dashboard():
    """触发 dashboard 刷新，使新数据立即显示在网页上"""
    try:
        refresh_script = os.path.expanduser(
            "~/.openclaw/workspace/personal-dashboard/scripts/dashboard_refresh.py"
        )
        if os.path.exists(refresh_script):
            import subprocess
            result = subprocess.run(
                [sys.executable, refresh_script, "--source", "ib_research", "--reason", "ib_research_updated"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                print("  -> Dashboard 已刷新")
            else:
                print(f"  -> Dashboard 刷新失败: {result.stderr[:200]}")
        else:
            print(f"  -> Dashboard 刷新脚本不存在: {refresh_script}")
    except Exception as e:
        print(f"  -> Dashboard 刷新异常: {e}")


def get_latest_report() -> Dict[str, Any]:
    """只读取最新缓存；网页访问本身绝不触发抓取或 AI。"""
    report = _load_today_report() or _load_latest_cached_report()
    if report:
        return report
    return {
        "meta": {
            "generated_at": "",
            "analysis_status": "degraded",
            "quality_status": "degraded",
        },
        "summary": {
            "Metadata": {},
            "Rating_Changes": [],
            "Asset_Targets": [],
            "Tail_Risks": [],
            "Core_Thesis": "暂无可用缓存，等待后台采集任务生成首份报告。",
        },
        "raw": {"news": [], "recommendations": [], "reddit_posts": []},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="外资投行研报获取器")
    parser.add_argument("--fast", action="store_true", help="快速刷新模式，仅抓取 RSS 源")
    parser.add_argument("--force", action="store_true", help="强制重新生成，跳过增量检测")
    args = parser.parse_args()

    report = run_fetch(force=args.force, fast=args.fast)
    print("\n=== 报告摘要 ===")
    summary = report.get("summary", {})
    if isinstance(summary, dict):
        if "error" in summary:
            print(f"错误: {summary['error']}")
        else:
            print(f"核心观点: {summary.get('Core_Thesis', 'N/A')[:150]}...")
            changes = summary.get("Rating_Changes", [])
            print(f"评级变化: {len(changes)} 条")
            print(f"刷新模式: {report.get('meta', {}).get('refresh_mode', 'unknown')}")
            trends = report.get("trends", {})
            if trends.get("persistent_signals"):
                print(f"持续信号: {len(trends['persistent_signals'])} 条")
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2)[:500])

    meta = report.get("meta", {})
    if not args.fast and (
        meta.get("analysis_status") != "ok" or meta.get("quality_status") != "ok"
    ):
        print("完整刷新未通过分析/质量检查，返回非零状态供 Cron 告警。")
        sys.exit(1)
