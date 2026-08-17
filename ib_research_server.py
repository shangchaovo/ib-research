#!/usr/bin/env python3
"""
外资投行研报总结服务
默认绑定 127.0.0.1:8081（可用 IB_RESEARCH_BIND_HOST 覆盖）；
公网访问经 Cloudflare 隧道 fresearch-dashboard 转发到本机，不直接监听公网网卡。

端点:
  GET /api/ib-research       -> JSON格式的研报总结
  GET /api/ib-research/raw   -> 原始数据
  POST /api/ib-research/refresh -> 手动刷新（需 X-Refresh-Token，见 .env）
  GET /                        -> HTML报告页面
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html import escape
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request as UrlRequest, urlopen
from flask import Flask, jsonify, Response, request

# 将workspace加入路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ib_research_fetcher import run_fetch, get_latest_report, CACHE_DIR, FINNHUB_TOKEN, HOT_SYMBOLS
from ib_research_geo import (
    homepage_footer_nav_html,
    homepage_head_html,
    homepage_nav_html,
    register_geo_routes,
)

app = Flask(__name__)
# 本服务不接收请求体，显式限制防止异常大请求
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

# 全局锁，防止并发刷新
_refresh_lock = threading.Lock()
_logo_fetch_semaphore = threading.BoundedSemaphore(6)
_logo_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="logo-fetch")
_logo_pending_lock = threading.Lock()
_logo_pending = set()
_logo_cache_dir = os.path.join(CACHE_DIR, "logos")
_logo_remote_base = "https://assets.parqet.com/logos/symbol"
_logo_max_bytes = 500_000
_logo_symbol_aliases = {
    "0RP9": ("MT",),
    "BEL FUSE": ("BELFB",),
    "NEXTDECADE": ("NEXT",),
    "TEL2": ("TEL2-B.ST",),
}
_logo_source_overrides = {
    "SPACEX": ("https://www.spacex.com/assets/favicon-spacex.ico",),
    "VOYG": (
        "https://voyagertechnologies.com/wp-content/uploads/2025/03/Favicon_64x64.svg",
    ),
}
_logo_download_hosts = {
    "assets.parqet.com",
    "static2.finnhub.io",
    "www.spacex.com",
    "voyagertechnologies.com",
}
_logo_pending_max = 256  # 待抓取队列上限，防止被随机 symbol 刷爆内存

# —— 页面/数据缓存：以报告文件 mtime 为键，报告更新即自动失效 ——
_page_cache_lock = threading.Lock()
_page_cache: dict = {}

# —— 历史目标价查找缓存（随页面缓存一同失效）——
_hist_pt_cache: dict = {}

# —— 轻量滑动窗口限速器（不引新依赖）——
_rate_lock = threading.Lock()
_rate_buckets: dict = {}


def _client_ip() -> str:
    """优先取 Cloudflare 传来的真实访客 IP。"""
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or "?"


def _rate_limited(scope: str, limit: int, window: int = 60) -> bool:
    """window 秒内超过 limit 次返回 True。"""
    now = time.monotonic()
    key = (scope, _client_ip())
    with _rate_lock:
        hits = _rate_buckets.get(key)
        if hits is None:
            hits = deque()
            _rate_buckets[key] = hits
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            return True
        hits.append(now)
        if len(_rate_buckets) > 10000:  # 兜底防内存膨胀
            _rate_buckets.clear()
    return False


def _report_cache_key() -> tuple:
    """以数据目录内最新报告文件的 (文件名, mtime) 为键；fetcher 写新报告即失效。"""
    latest_name = None
    latest_mtime = 0.0
    try:
        for name in os.listdir(CACHE_DIR):
            if not (name.startswith("report_") and name.endswith(".json")):
                continue
            try:
                mtime = os.path.getmtime(os.path.join(CACHE_DIR, name))
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_name = name
    except OSError:
        pass
    return (latest_name, latest_mtime)


def _get_report_cached() -> dict:
    """按 mtime 键缓存 get_latest_report()，避免每请求重读+重解析 ~800KB JSON。"""
    key = _report_cache_key()
    with _page_cache_lock:
        if _page_cache.get("key") == key and _page_cache.get("report") is not None:
            return _page_cache["report"]
    report = get_latest_report()
    with _page_cache_lock:
        if _page_cache.get("key") != key:
            _page_cache.clear()
            _hist_pt_cache.clear()
            _page_cache["key"] = key
        _page_cache["report"] = report
    return report


def _get_html_cached(can_refresh: bool) -> tuple:
    """机主版/公开版 HTML 分开缓存；命中时不再重复渲染 1.4MB 页面。"""
    key = _report_cache_key()
    # 令牌指纹并入缓存键：轮换令牌后机主版立即失效，不残留旧令牌
    token_fp = hashlib.sha256(
        os.getenv("IB_RESEARCH_REFRESH_TOKEN", "").encode("utf-8")
    ).hexdigest()[:12]
    slot = f"html_owner:{token_fp}" if can_refresh else "html_public"
    with _page_cache_lock:
        if _page_cache.get("key") == key:
            html = _page_cache.get(slot)
            report = _page_cache.get("report")
            if html is not None and report is not None:
                return html, report
    report = _get_report_cached()
    html = _generate_html(report, can_refresh=can_refresh)
    with _page_cache_lock:
        if _page_cache.get("key") != key:
            _page_cache.clear()
            _hist_pt_cache.clear()
            _page_cache["key"] = key
            _page_cache["report"] = report
        _page_cache[slot] = html
    return html, report


def _allowed_logo_symbols() -> frozenset:
    """当前报告真实出现的 ticker 集合（归一化）+ 别名/覆盖表键。

    只有集合内的 symbol 才允许触发上游抓取，公网刷随机 symbol 直接走占位图。
    """
    key = _report_cache_key()
    with _page_cache_lock:
        if _page_cache.get("key") == key and _page_cache.get("logo_symbols") is not None:
            return _page_cache["logo_symbols"]
    symbols = set(_logo_symbol_aliases) | set(_logo_source_overrides)
    report = _get_report_cached()
    summary = report.get("summary", {})
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except Exception:
            summary = {}
    if isinstance(summary, dict):
        for item in summary.get("Asset_Targets", []) or []:
            if isinstance(item, dict):
                # 资产卡片用 Asset 字段(可能是代码或名称，非代码归一化后为空自动跳过)
                normalized = _normalize_logo_symbol(item.get("Asset", ""))
                if normalized:
                    symbols.add(normalized)
        for item in summary.get("Rating_Changes", []) or []:
            if isinstance(item, dict):
                normalized = _normalize_logo_symbol(item.get("Symbol", ""))
                if normalized:
                    symbols.add(normalized)
    result = frozenset(symbols)
    with _page_cache_lock:
        if _page_cache.get("key") == key:
            _page_cache["logo_symbols"] = result
    return result
_site_icon_svg = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#0b0b0c"/>
<path d="M12 47V21h10v26zm15 0V11h10v36zm15 0V27h10v20z" fill="#c9a45c"/>
</svg>"""
_site_icon_png = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAIAAACyr5FlAAABlUlEQVR42u3dwQ2FIBBAwcV4w6qswC6syS6swNpswBtKQGbukq952Xj4YMp5CXgyeQSIA3EgDsSBOBAH4kAciANxgDgQB+JAHIgDcSAOxIE4EAeIA3EgDsSBOBAH4kAciANxwDzaDV/HVnL5up8mhzK+WkEc/yxjtD68cyAOxIE4EAfiQByIA3EgDsQB4kAciANxIA7EgTgQB+JAHCAOxEF0suPNbjOTw24zcdhtJg7EAeJAHIgDcSAOxIE4EAfiQBwgDsSBOAhnn4ezlOv9I87kcP66OJy/Lg7EgTgQB+JAHIgDcSAOEAfiQByIA3EgDsSBOBAH4gBxIA7EgTgQB+JAHIgDcSAOEAfiQByIA3EgDsSBOBAH4kAcIA7EgTgQB+JAHIgDcSAOxAHiQBx0EMdbX7ssWcdvaHdylN+PFSp/VzblvJifeOdAHIgDcSAOxIE4EAfiQBwgDsSBOBAH4kAciANxIA7EAeJAHIgDcSAOxIE4EAfiQByIA8SBOBAH4kAciANxIA7EgThAHIgDcSAOxEFDbndCLrF5TlM/AAAAAElFTkSuQmCC"
)


@app.after_request
def _add_security_headers(response):
    """为公开页面增加浏览器侧纵深防护。"""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    path = request.path or ""
    is_api = path.startswith("/api/")
    is_html = response.mimetype == "text/html"
    if is_html:
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; "
            "img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
    if is_api:
        # API 一律不落缓存、不进索引
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    elif is_html and response.status_code == 200:
        # 公开 HTML 允许 CDN/搜索引擎缓存并索引（route 显式设置的优先）
        response.headers.setdefault(
            "Cache-Control",
            "public, max-age=120, s-maxage=300, stale-while-revalidate=1800",
        )
        response.headers.setdefault(
            "X-Robots-Tag",
            "index, follow, max-image-preview:large, max-snippet:-1",
        )
    elif is_html:
        response.headers.setdefault("X-Robots-Tag", "noindex")
    return response


def _stock_href(symbol: str) -> str:
    """关注池内的股票返回可索引的个股研究页 URL，否则返回空。"""
    ticker = str(symbol or "").strip().upper()
    if ticker in HOT_SYMBOLS:
        return f"/stocks/{ticker.lower()}/"
    return ""


def _load_env():
    """加载环境变量（容忍引号/export 前缀/CRLF）"""
    env_path = os.path.expanduser(os.environ.get("IB_RESEARCH_ENV", "~/.openclaw/workspace/.env"))
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip().lstrip("﻿")
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _request_host() -> str:
    """解析 Host（兼容 [::1]:8081 形式），小写、去端口。"""
    host_header = (request.host or "").strip()
    if host_header.startswith("["):
        return host_header[1:].split("]")[0].lower()
    return host_header.split(":")[0].lower()


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_loopback_request() -> bool:
    try:
        address = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return False
    if address.is_loopback:
        return True
    return bool(address.version == 6 and address.ipv4_mapped and address.ipv4_mapped.is_loopback)


def _is_refresh_authorized_request() -> bool:
    """令牌优先；未配置令牌时仅允许不经过 Cloudflare 且 Host 指向本机的请求。"""
    # 纵深防御：显式跨源的 POST（恶意网页的"localhost 路过式"请求）直接拒绝
    origin = request.headers.get("Origin")
    if origin:
        origin_host = (urlparse(origin).hostname or "").lower()
        if origin_host != _request_host():
            return False

    configured_token = os.getenv("IB_RESEARCH_REFRESH_TOKEN", "").strip()
    if configured_token:
        provided_token = request.headers.get("X-Refresh-Token", "")
        return bool(provided_token) and hmac.compare_digest(provided_token, configured_token)

    # 无令牌回退：loopback + 未经 Cloudflare + Host 白名单（防 DNS 重绑定）
    if request.headers.get("CF-Connecting-IP"):
        return False
    if _request_host() not in _LOCAL_HOSTS:
        return False
    return _is_loopback_request()


def _is_owner_request() -> bool:
    """机主本人从本机浏览器访问：loopback + 未经 Cloudflare + Host 指向本机。

    Host 校验用于防 DNS 重绑定——恶意域名解析到 127.0.0.1 时 Host 不是 localhost，
    此时页面绝不能带刷新按钮和令牌。令牌只会出现在机主版 HTML 里，跨源 fetch
    受同源策略限制读不到它。
    """
    if request.headers.get("CF-Connecting-IP"):
        return False
    if _request_host() not in _LOCAL_HOSTS:
        return False
    return _is_loopback_request()


def _safe_text(value) -> str:
    """把外部/模型数据安全地放入 HTML 文本节点。"""
    return escape(str(value if value is not None else ""), quote=False)


def _safe_attr(value) -> str:
    """把外部/模型数据安全地放入 HTML 属性。"""
    return escape(str(value if value is not None else ""), quote=True)


def _safe_multiline(value) -> str:
    return _safe_text(value).replace("\n", "<br>")


def _normalize_logo_symbol(symbol: str) -> str:
    """把外部 ticker 收敛为可安全请求的 Parqet symbol。"""
    normalized = str(symbol or "").strip().upper().replace(".", "-").replace("/", "-")
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"[^A-Z0-9 _-]", "", normalized).strip()
    return normalized[:32]


def _logo_cache_path(symbol: str) -> str:
    digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()
    return os.path.join(_logo_cache_dir, f"{digest}.svg")


def _fallback_logo_svg(symbol: str) -> bytes:
    label = (symbol[:2] or "?").upper()
    safe_label = escape(label, quote=True)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="{safe_label}">
<rect width="64" height="64" rx="14" fill="#1a1a24"/>
<rect x="1" y="1" width="62" height="62" rx="13" fill="none" stroke="#c9a45c" stroke-opacity=".28"/>
<text x="32" y="39" text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif" font-size="21" font-weight="700" fill="#c9a45c">{safe_label}</text>
</svg>""".encode("utf-8")


def _detect_logo_mimetype(body: bytes) -> Optional[str]:
    lowered = body[:512].lower()
    if b"<svg" in lowered:
        unsafe_markers = (b"<script", b"<foreignobject", b"javascript:", b" onload=")
        if not any(marker in body.lower() for marker in unsafe_markers):
            return "image/svg+xml"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp"
    if body.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def _read_cached_logo(normalized: str) -> Optional[tuple[bytes, str]]:
    cache_path = _logo_cache_path(normalized)
    try:
        with open(cache_path, "rb") as cached:
            body = cached.read(_logo_max_bytes + 1)
        mimetype = _detect_logo_mimetype(body)
        if body and len(body) <= _logo_max_bytes and mimetype:
            return body, mimetype
    except OSError:
        pass
    return None


def _cache_logo_body(normalized: str, body: bytes) -> bool:
    if not body or len(body) > _logo_max_bytes or not _detect_logo_mimetype(body):
        return False
    cache_path = _logo_cache_path(normalized)
    try:
        os.makedirs(_logo_cache_dir, exist_ok=True)
        temp_path = f"{cache_path}.{threading.get_ident()}.tmp"
        with open(temp_path, "wb") as temp_file:
            temp_file.write(body)
        os.replace(temp_path, cache_path)
        return True
    except OSError:
        return False


def _download_logo_body(remote_url: str) -> Optional[bytes]:
    parsed = urlparse(remote_url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or hostname not in _logo_download_hosts:
        return None
    try:
        with _logo_fetch_semaphore:
            upstream_request = UrlRequest(
                remote_url,
                headers={
                    "Accept": "image/svg+xml,image/*;q=0.8",
                    "User-Agent": "fresearch-logo-cache/1.0",
                },
            )
            with urlopen(upstream_request, timeout=5) as upstream:
                body = upstream.read(_logo_max_bytes + 1)
        return body if _detect_logo_mimetype(body) else None
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return None


def _parqet_symbol_candidates(symbol: str) -> list[str]:
    raw = str(symbol or "").strip().upper().replace("/", "-")
    raw = re.sub(r"\s+", " ", raw)
    raw = re.sub(r"[^A-Z0-9 ._-]", "", raw).strip()
    if not raw:
        return []
    candidates = [raw, raw.replace(" ", "-"), raw.replace(".", "-")]
    if "." in raw:
        base = raw.rsplit(".", 1)[0]
        candidates.extend((base, base.replace(" ", "-")))
    return list(dict.fromkeys(candidate[:40] for candidate in candidates if candidate))


def _fetch_parqet_logo(symbol: str) -> Optional[bytes]:
    for candidate in _parqet_symbol_candidates(symbol):
        remote_url = f"{_logo_remote_base}/{quote(candidate, safe='-._')}"
        body = _download_logo_body(remote_url)
        if body is not None:
            return body
    return None


def _finnhub_json(endpoint: str, params: dict) -> dict:
    if not FINNHUB_TOKEN:
        return {}
    query = dict(params)
    query["token"] = FINNHUB_TOKEN
    remote_url = f"https://finnhub.io/api/v1/{endpoint}?{urlencode(query)}"
    try:
        with _logo_fetch_semaphore:
            upstream_request = UrlRequest(
                remote_url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "fresearch-logo-resolver/1.0",
                },
            )
            with urlopen(upstream_request, timeout=6) as upstream:
                payload = upstream.read(1_000_001)
        if not payload or len(payload) > 1_000_000:
            return {}
        decoded = json.loads(payload)
        return decoded if isinstance(decoded, dict) else {}
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return {}


def _compact_company_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _matching_finnhub_results(query: str, results: list) -> list[dict]:
    query_key = _compact_company_key(query)
    matches = []
    for item in results:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "")
        symbol_base = symbol.rsplit(".", 1)[0]
        description = str(item.get("description") or "")
        symbol_match = _compact_company_key(symbol) == query_key
        base_match = _compact_company_key(symbol_base) == query_key
        description_key = _compact_company_key(description)
        description_match = bool(query_key) and description_key.startswith(query_key)
        if symbol_match or base_match or description_match:
            matches.append(item)
    return matches


def _fetch_finnhub_logo(query: str) -> Optional[bytes]:
    # 标准 ticker 先直接查公司档案，避免额外的 search 配额。
    profile = _finnhub_json("stock/profile2", {"symbol": query})
    direct_logo = str(profile.get("logo") or "")
    if direct_logo:
        body = _download_logo_body(direct_logo)
        if body is not None:
            return body

    search = _finnhub_json("search", {"q": query})
    results = search.get("result") if isinstance(search.get("result"), list) else []
    for item in _matching_finnhub_results(query, results):
        candidate_symbol = str(item.get("symbol") or "")
        body = _fetch_parqet_logo(candidate_symbol)
        if body is not None:
            return body
        profile = _finnhub_json("stock/profile2", {"symbol": candidate_symbol})
        logo_url = str(profile.get("logo") or "")
        if logo_url:
            body = _download_logo_body(logo_url)
            if body is not None:
                return body
    return None


def _fetch_and_cache_logo(normalized: str) -> bool:
    """自动多源解析真实 Logo，并以页面中的原始代码为键持久缓存。"""
    for remote_url in _logo_source_overrides.get(normalized, ()):
        body = _download_logo_body(remote_url)
        if body is not None and _cache_logo_body(normalized, body):
            return True

    for candidate in (*_logo_symbol_aliases.get(normalized, ()), normalized):
        body = _fetch_parqet_logo(candidate)
        if body is not None and _cache_logo_body(normalized, body):
            return True

    body = _fetch_finnhub_logo(normalized)
    return bool(body is not None and _cache_logo_body(normalized, body))


def _background_logo_fetch(normalized: str) -> None:
    try:
        _fetch_and_cache_logo(normalized)
    finally:
        with _logo_pending_lock:
            _logo_pending.discard(normalized)


def _queue_logo_fetch(normalized: str) -> None:
    with _logo_pending_lock:
        if normalized in _logo_pending:
            return
        if len(_logo_pending) >= _logo_pending_max:
            return  # 队列已满：放弃回源，本轮用占位图
        _logo_pending.add(normalized)
    _logo_executor.submit(_background_logo_fetch, normalized)


def _get_logo_svg(symbol: str) -> tuple[bytes, str, str]:
    """同源即时返回：缓存命中用真实 logo，否则先用稳定占位图并后台预取。"""
    normalized = _normalize_logo_symbol(symbol)
    if not normalized:
        return _fallback_logo_svg("?"), "image/svg+xml", "fallback"
    cached = _read_cached_logo(normalized)
    if cached is not None:
        body, mimetype = cached
        return body, mimetype, "cache"
    # 白名单闸口：报告里没出现过的 symbol 绝不回源（防公网刷接口耗 Finnhub 配额）
    if normalized in _allowed_logo_symbols():
        _queue_logo_fetch(normalized)
    return _fallback_logo_svg(normalized), "image/svg+xml", "fallback"


def _static_asset_response(body: bytes, mimetype: str) -> Response:
    response = Response(body, mimetype=mimetype)
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    response.set_etag(hashlib.sha256(body).hexdigest())
    return response.make_conditional(request)


@app.route("/favicon.svg")
def favicon_svg():
    return _static_asset_response(_site_icon_svg, "image/svg+xml")


@app.route("/favicon.ico")
@app.route("/apple-touch-icon.png")
def favicon_png():
    return _static_asset_response(_site_icon_png, "image/png")


_og_image_cache: "bytes | None" = None


@app.route("/og-image.png")
def og_image_png():
    """OG 分享预览图(scripts/make_og_image.py 生成, 随 deploy 下发)。"""
    global _og_image_cache
    if _og_image_cache is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "og-image.png")
        try:
            with open(path, "rb") as f:
                _og_image_cache = f.read()
        except OSError:
            return Response("not found", status=404, mimetype="text/plain")
    return _static_asset_response(_og_image_cache, "image/png")


@app.route("/assets/logos/<path:symbol>.svg")
def logo_asset(symbol: str):
    """让浏览器只访问本站；Cloudflare 可缓存所有 logo 和占位图。"""
    if _rate_limited("logo", 120):
        return Response("too many requests", status=429, mimetype="text/plain")
    body, mimetype, source = _get_logo_svg(symbol)
    response = Response(body, mimetype=mimetype)
    response.headers["Cache-Control"] = (
        "public, max-age=86400, s-maxage=604800, stale-while-revalidate=2592000"
        if source != "fallback"
        else "private, no-cache, max-age=0"
    )
    # 上游 SVG 只做了黑名单清洗；禁止脚本执行兜底直接导航场景的存储型 XSS
    response.headers["Content-Security-Policy"] = "script-src 'none'"
    response.set_etag(hashlib.sha256(body).hexdigest())
    return response.make_conditional(request)


def _generate_html(report: dict, can_refresh: bool = False) -> str:
    """生成HTML报告页面 (适配专业量化分析结构)"""
    meta = report.get("meta", {})
    summary = report.get("summary", {})
    generated_at = str(meta.get("generated_at", "N/A"))

    # 处理 summary 可能是字符串或 dict 的情况
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except Exception:
            summary = {}

    # 提取专业结构字段
    metadata = summary.get("Metadata", {}) if isinstance(summary, dict) else {}
    core_thesis = summary.get("Core_Thesis", "暂无数据") if isinstance(summary, dict) else "暂无数据"
    macro_vars = summary.get("Macro_Variables", {}) if isinstance(summary, dict) else {}
    asset_targets = summary.get("Asset_Targets", []) if isinstance(summary, dict) else []
    stat_evidence = summary.get("Statistical_Evidence", "暂无数据") if isinstance(summary, dict) else "暂无数据"
    tail_risks = summary.get("Tail_Risks", []) if isinstance(summary, dict) else []

    institution = metadata.get("Institution", "N/A") if isinstance(metadata, dict) else "N/A"
    date_range = metadata.get("Date", "N/A") if isinstance(metadata, dict) else "N/A"
    assets_covered = metadata.get("Primary_Assets_Covered", []) if isinstance(metadata, dict) else []

    raw = report.get("raw", {})
    news_count = len(raw.get("news", []))
    rec_count = len(raw.get("recommendations", []))
    refresh_mode = str(meta.get("refresh_mode", "full"))
    # 令牌只注入机主版页面；公开访客的 HTML 不含此值
    refresh_token_js = json.dumps(
        os.getenv("IB_RESEARCH_REFRESH_TOKEN", "").strip() if can_refresh else ""
    )
    try:
        pending_rating_count = max(
            0, int(meta.get("pending_rating_count", 0) or 0)
        )
    except (TypeError, ValueError):
        pending_rating_count = 0

    # --- 子渲染函数 ---------------------------------------------------------
    # 配置
    BMC_USERNAME = os.getenv("BMC_USERNAME", "chasetse").strip() or "chasetse"
    default_bmc_url = f"https://www.buymeacoffee.com/{quote(BMC_USERNAME, safe='')}"
    configured_bmc_url = os.getenv("BMC_URL", default_bmc_url).strip()
    parsed_bmc_url = urlparse(configured_bmc_url)
    BMC_URL = (
        configured_bmc_url
        if parsed_bmc_url.scheme.lower() == "https" and parsed_bmc_url.netloc
        else default_bmc_url
    )
    CONTACT_EMAIL = "shangchaoxie888@gmail.com"
    X_PROFILE_URL = "https://x.com/johny_xie"

    def _logo_url(symbol: str) -> str:
        normalized = _normalize_logo_symbol(symbol)
        if not normalized or symbol == "—":
            return ""
        return f'/assets/logos/{quote(normalized, safe="-")}.svg?v=2'

    def _logo_html(symbol: str, size: str = "row") -> str:
        url = _logo_url(symbol)
        if not url:
            return ""
        normalized = _normalize_logo_symbol(symbol)
        safe_url = _safe_attr(url)
        safe_symbol = _safe_attr(symbol)
        safe_initial = _safe_text(symbol[0] if symbol else "?")
        pending_attr = (
            ' data-logo-pending="1"'
            if _read_cached_logo(normalized) is None
            else ""
        )
        if size == "row":
            return f'''<div class="logo-wrap row-logo-wrap">
                <img class="row-logo" src="{safe_url}" alt="" loading="lazy"{pending_attr} onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
                <div class="row-logo-fallback">{safe_initial}</div>
            </div>'''
        return f'''<div class="logo-wrap asset-logo-wrap">
                <img class="asset-logo" src="{safe_url}" alt="{safe_symbol}" loading="lazy"{pending_attr} onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
                <div class="asset-logo-fallback">{safe_initial}</div>
            </div>'''

    def _find_historical_price_target(symbol: str, bank: str, current_time: str) -> Optional[str]:
        """从最近 7 天的报告中查找同一资产/投行的上一个目标价（结果按报告日期缓存）"""
        try:
            current_dt = datetime.strptime(current_time[:10], "%Y-%m-%d")
        except Exception:
            return None
        cache_key = (symbol, bank.lower(), current_time[:10])
        if cache_key in _hist_pt_cache:
            return _hist_pt_cache[cache_key]
        result = None
        for days_back in range(1, 8):
            date = (current_dt - timedelta(days=days_back)).strftime("%Y-%m-%d")
            path = os.path.join(CACHE_DIR, f"report_{date}.json")
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                s = report.get("summary", {})
                if isinstance(s, str):
                    s = json.loads(s)
                for c in s.get("Rating_Changes", []) or []:
                    if not isinstance(c, dict):
                        continue
                    if (str(c.get("Symbol", "")).strip().upper() == symbol and
                        str(c.get("Bank", "")).strip().lower() == bank.lower() and
                        c.get("Price_Target")):
                        result = c.get("Price_Target")
                        break
                if result:
                    break
            except Exception:
                continue
        _hist_pt_cache[cache_key] = result
        return result

    def _is_bernstein(text: str) -> bool:
        return "bernstein" in (text or "").lower()

    def _bank_badge(bank: str) -> str:
        if _is_bernstein(bank):
            return '<span class="bernstein-badge" title="Bernstein（伯恩斯坦）高权重卖方">权威</span>'
        return ''

    def _rating_badge(rating: str) -> str:
        if not rating:
            return '<span class="tag tag-empty" title="来源未公布此项，并非抓取失败">—</span>'
        r = rating.lower()
        safe_rating = _safe_text(rating)
        if any(k in r for k in ("buy", "overweight", "outperform", "strong buy")):
            return f'<span class="tag tag-bull">{safe_rating}</span>'
        if any(k in r for k in ("sell", "underweight", "underperform")):
            return f'<span class="tag tag-bear">{safe_rating}</span>'
        if any(k in r for k in ("hold", "neutral", "equal", "market")):
            return f'<span class="tag tag-neutral">{safe_rating}</span>'
        return f'<span class="tag">{safe_rating}</span>'

    def _action_label(action: str) -> str:
        if not action:
            return ""
        a = action.lower()
        if "raise" in a or "upgrade" in a:
            return '<span class="action-dot action-up"></span>'
        if "cut" in a or "downgrade" in a or "lower" in a:
            return '<span class="action-dot action-down"></span>'
        if "initiate" in a or "new" in a:
            return '<span class="action-dot action-new"></span>'
        return '<span class="action-dot action-flat"></span>'

    def _rating_date(value) -> str:
        """将评级时间归一化为可排序、可筛选的 YYYY-MM-DD。"""
        raw_value = str(value or "").strip()
        match = re.search(r"\d{4}-\d{2}-\d{2}", raw_value)
        if match:
            return match.group(0)
        for fmt in ("%Y/%m/%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(raw_value[:10], fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""

    def _rating_event_key(detail: dict) -> tuple:
        event_date = _rating_date(detail.get("Time")) or _rating_date(detail.get("_First_Seen"))
        return (
            str(detail.get("Bank", "")).strip().casefold(),
            str(detail.get("Symbol", "")).strip().upper(),
            str(detail.get("Action", "")).strip().casefold(),
            event_date,
        )

    def _asset_card(item: dict, price_target_map: dict) -> str:
        asset = item.get("Asset") or "—"
        target = item.get("Target_Price") or "—"
        tf = item.get("Timeframe") or ""
        levels = item.get("Technical_Levels") or []
        levels = [x for x in levels if x]
        levels_str = ", ".join(str(x) for x in levels) if levels else ""
        source = item.get("_Source") or ""
        consensus = item.get("_Consensus") or {}
        verified_pt = item.get("_Verified_PT") or ""
        verified_bank = item.get("_Verified_Bank") or ""

        # 来源徽章
        source_badge = ""
        if source == "av_consensus":
            source_badge = '<span class="source-badge source-consensus">AV 共识</span>'
        elif source == "verified_headline":
            source_badge = '<span class="source-badge source-verified">已验证</span>'
        elif source == "llm_inferred":
            source_badge = '<span class="source-badge source-llm">研报推断</span>'

        # 评级分布条形（AV 共识）
        distribution_html = ""
        sb = int(consensus.get("strong_buy") or 0)
        b = int(consensus.get("buy") or 0)
        h = int(consensus.get("hold") or 0)
        s = int(consensus.get("sell") or 0)
        ss = int(consensus.get("strong_sell") or 0)
        total = sb + b + h + s + ss
        if total > 0:
            bull_pct = round((sb + b) / total * 100)
            hold_pct = round(h / total * 100)
            bear_pct = round((s + ss) / total * 100)
            distribution_html = f"""
            <div class="analyst-dist" title="强买 {sb} / 买入 {b} / 持有 {h} / 卖出 {s} / 强卖 {ss}">
                <div class="dist-bar">
                    <span class="dist-seg dist-bull" style="width:{bull_pct}%"></span>
                    <span class="dist-seg dist-hold" style="width:{hold_pct}%"></span>
                    <span class="dist-seg dist-bear" style="width:{bear_pct}%"></span>
                </div>
                <div class="dist-labels">
                    <span class="dist-bull-t">{sb + b} 买</span>
                    <span class="dist-hold-t">{h} 持</span>
                    <span class="dist-bear-t">{s + ss} 卖</span>
                </div>
            </div>
            """

        change_info = price_target_map.get(str(asset).upper())
        change_html = ""
        if change_info:
            bank = change_info.get("Bank", "") or ""
            action = str(change_info.get("Action", "")).lower()
            time = change_info.get("Time", "") or ""
            current_pt = change_info.get("Price_Target") or target
            # Old_Rating 是评级不是旧目标价；旧目标价只在历史账本里有同机构记录时展示
            old_pt = _find_historical_price_target(str(asset).upper(), bank, time)
            if old_pt == current_pt:
                old_pt = None

            if "raise" in action:
                arrow, color_class, verb = "↑", "change-up", "上调至"
            elif "cut" in action or "lower" in action:
                arrow, color_class, verb = "↓", "change-down", "下调至"
            elif "initiate" in action or "new" in action:
                arrow, color_class, verb = "★", "change-new", "首次覆盖"
            elif "reiterate" in action or "maintain" in action:
                arrow, color_class, verb = "→", "change-flat", "维持"
            else:
                arrow, color_class, verb = "→", "change-flat", "目标价"

            delta_html = ""
            if old_pt and old_pt != "—":
                delta_html = f'<span class="change-old">{_safe_text(old_pt)}</span><span class="change-divider">→</span>'

            new_rating = change_info.get("New_Rating")
            rating_html = f'<span class="change-rating">{_safe_text(new_rating)}</span>' if new_rating else ""

            change_html = f"""
            <div class="asset-change">
                <span class="change-arrow {color_class}">{arrow}</span>
                {rating_html}
                {delta_html}
                <span class="change-new">{verb} {_safe_text(current_pt)}</span>
                <span class="change-bank">{_safe_text(bank)}</span>
                <span class="change-time">{_safe_text(str(time)[:10])}</span>
            </div>
            """
        elif verified_pt and verified_pt != target:
            change_html = f"""
            <div class="asset-change">
                <span class="change-arrow change-flat">→</span>
                <span class="change-new">最新研报 {_safe_text(verified_pt)}</span>
                <span class="change-bank">{_safe_text(verified_bank)}</span>
            </div>
            """

        meta_bits = []
        if tf:
            meta_bits.append(f"时间框架: {_safe_text(tf)}")
        if levels_str:
            meta_bits.append(f"技术位: {_safe_text(levels_str)}")
        if consensus.get("week_52_high") and consensus.get("week_52_low"):
            meta_bits.append(f"52周: {_safe_text(consensus['week_52_low'])}–{_safe_text(consensus['week_52_high'])}")
        meta_html = "".join(f"<div class='asset-meta'>{bit}</div>" for bit in meta_bits)

        return f"""
        <div class="asset-card">
            <div class="asset-card-top">
                {_logo_html(asset, size='asset')}
                {source_badge}
            </div>
            <div class="asset-header">
                {f'<a class="asset-ticker" href="{_safe_attr(_stock_href(asset))}">{_safe_text(asset)}</a>' if _stock_href(asset) else f'<span class="asset-ticker">{_safe_text(asset)}</span>'}
                <span class="asset-target">{_safe_text(target)}</span>
            </div>
            {distribution_html}
            {change_html}
            {meta_html}
        </div>
        """

    def _render_tail_risks(items) -> str:
        if not items:
            return '<p class="empty">暂无尾部风险数据</p>'
        rows = []
        for item in items:
            if isinstance(item, str):
                rows.append(f'<li class="risk-item">{_safe_text(item)}</li>')
            elif isinstance(item, dict):
                text = item.get("Risk") or item.get("Description")
                if text:
                    rendered = _safe_text(text)
                else:
                    rendered = " ".join(
                        f"<b>{_safe_text(k)}:</b> {_safe_text(v)}"
                        for k, v in item.items() if v is not None
                    )
                rows.append(f'<li class="risk-item">{rendered}</li>')
        return f'<ul class="risk-list">{"".join(rows)}</ul>'

    # --- 组装各部分 ---------------------------------------------------------
    # 从 Rating_Changes 构建每个资产最近的目标价变动信息
    price_target_map = {}
    if isinstance(summary, dict):
        for c in summary.get("Rating_Changes", []) or []:
            if not isinstance(c, dict):
                continue
            symbol = str(c.get("Symbol", "")).strip().upper()
            if not symbol or not c.get("Price_Target"):
                continue
            action = str(c.get("Action", "")).lower()
            if "raise" not in action and "cut" not in action and "lower" not in action and "target" not in action:
                continue
            # 保留最新的（列表已是时间倒序）
            if symbol not in price_target_map:
                price_target_map[symbol] = c

    change_analysis = report.get("change_analysis", {}) or {}
    analyzed_changes = list(change_analysis.get("changes", []) or [])
    changes = []
    analysis_text = change_analysis.get("analysis", "") or ""
    # 变化分析失败时，旧报告会存占位串（如“分析服务暂时不可用”）；这类占位串
    # 不应原样展示——视作空，回退到 Core_Thesis，避免评分变更区出现误导文案。
    if analysis_text.strip() in {"分析服务暂时不可用", "无显著变化"}:
        analysis_text = ""

    # 表格以 30 天事件账本为唯一事实源；变化分析只补充优先级和说明。
    if isinstance(summary, dict):
        summary_changes = summary.get("Rating_Changes", []) or []
        priority_by_key = {
            _rating_event_key(ch.get("detail", {}) or {}): ch.get("priority", "normal")
            for ch in analyzed_changes
            if isinstance(ch, dict)
        }
        for c in summary_changes:
            if not isinstance(c, dict):
                continue
            key = _rating_event_key(c)
            changes.append({
                "type": "new_rating",
                "priority": priority_by_key.get(
                    key,
                    "high" if "bernstein" in str(c.get("Bank", "")).lower() else "normal",
                ),
                "message": f"{c.get('Bank', '')} {c.get('Action', '')} {c.get('Symbol', '')} to {c.get('New_Rating', 'N/A')}",
                "detail": c,
            })
        # 取 Core_Thesis 作为变化分析文本
        if not analysis_text:
            analysis_text = summary.get("Core_Thesis", "")
    else:
        changes = analyzed_changes

    # 机构徽章
    bank_bubbles = ""
    coverage_banks = meta.get("coverage_banks", []) or []
    if coverage_banks:
        bank_bubbles = "".join(
            f'<span class="bank-chip">{_safe_text(b)}</span>' for b in coverage_banks[:14]
        )
        if len(coverage_banks) > 14:
            bank_bubbles += f'<span class="bank-chip bank-more">+{len(coverage_banks) - 14}</span>'

    # 资产覆盖标签
    assets_html = ""
    if assets_covered:
        chip_bits = []
        for a in assets_covered:
            href = _stock_href(a)
            label = _safe_text(a)
            if href:
                chip_bits.append(f'<a class="asset-chip" href="{_safe_attr(href)}">{label}</a>')
            else:
                chip_bits.append(f'<span class="asset-chip">{label}</span>')
        assets_html = "".join(chip_bits)

    # 评级变动表格
    changes.sort(
        key=lambda ch: (
            _rating_date((ch.get("detail", {}) or {}).get("Time"))
            or _rating_date((ch.get("detail", {}) or {}).get("_First_Seen"))
        )
        if isinstance(ch, dict) else "",
        reverse=True,
    )
    rating_rows = ""
    if changes:
        for ch in changes:
            d = ch.get("detail", {}) or {}
            symbol = str(d.get("Symbol") or "—")
            bank = str(d.get("Bank") or "—")
            action = str(d.get("Action") or "—")
            old_r = str(d.get("Old_Rating") or "")
            new_r = str(d.get("New_Rating") or "")
            pt = str(d.get("Price_Target") or "—")
            raw_time = str(d.get("Time") or "")
            event_date = _rating_date(raw_time) or _rating_date(d.get("_First_Seen"))
            time = f"{event_date}*" if d.get("_Time_Inferred") else (event_date or raw_time)
            priority = ch.get("priority", "")
            priority_cls = "priority-high" if priority == "high" else "priority-normal"
            rating_rows += f"""
            <tr class="rating-row" data-symbol="{escape(symbol.upper(), quote=True)}" data-date="{escape(event_date, quote=True)}" data-bank="{escape(bank, quote=True)}">
                <td>{_action_label(action)}</td>
                <td>
                    <div class="symbol-cell">
                        {_logo_html(symbol, size='row')}
                        {f'<a class="symbol" href="{_safe_attr(_stock_href(symbol))}">{_safe_text(symbol)}</a>' if _stock_href(symbol) else f'<span class="symbol">{_safe_text(symbol)}</span>'}
                    </div>
                </td>
                <td>{_safe_text(bank)}{_bank_badge(bank)}</td>
                <td class="mono">{_safe_text(action.replace("_", " ").title())}</td>
                <td>{_rating_badge(old_r)}</td>
                <td class="arrow">→</td>
                <td>{_rating_badge(new_r)}</td>
                <td class="mono">{_safe_text(pt) if pt != "—" else '<span class="pt-empty" title="来源未公布目标价，并非抓取失败">—</span>'}</td>
                <td class="mono muted rating-time">{_safe_text(time)}</td>
                <td><span class="{priority_cls}">{_safe_text(str(priority).upper())}</span></td>
            </tr>
            """
        rating_rows += '<tr id="ratingFilterEmpty" hidden><td colspan="10" class="empty-cell">该时间段暂无评级变动</td></tr>'
    else:
        rating_rows = '<tr id="ratingFilterEmpty"><td colspan="10" class="empty-cell">最近 30 天暂无显著评级变动</td></tr>'

    # 资产目标网格（防御性去重：同一只股票只保留第一张卡）
    asset_cards = ""
    seen_assets = set()
    if asset_targets:
        for a in asset_targets:
            if not isinstance(a, dict):
                continue
            sym = str(a.get("Asset") or "").strip().upper()
            if not sym or sym in seen_assets:
                continue
            seen_assets.add(sym)
            asset_cards += _asset_card(a, price_target_map)
    if not asset_cards:
        asset_cards = '<p class="empty">暂无资产目标数据</p>'

    # 宏观变量
    macro_html = ""
    if macro_vars and isinstance(macro_vars, dict):
        macro_items = ""
        for k, v in macro_vars.items():
            if v:
                macro_items += f"""
                <div class="macro-cell">
                    <div class="macro-key">{_safe_text(str(k).replace("_", " ").upper())}</div>
                    <div class="macro-value">{_safe_text(v)}</div>
                </div>"""
        macro_html = f'<div class="macro-grid">{macro_items}</div>' if macro_items else '<p class="empty">暂无宏观数据</p>'
    else:
        macro_html = '<p class="empty">暂无宏观数据</p>'

    # 页脚数据源
    data_sources = meta.get("data_sources", ["finnhub", "alpha_vantage", "seeking_alpha", "reddit", "google_news"])
    sources_html = ", ".join(_safe_text(str(s).title()) for s in data_sources)

    analysis_state = str(meta.get("analysis_status", "")).lower()
    is_degraded = bool(
        isinstance(summary, dict)
        and (summary.get("_fallback") or summary.get("error"))
    ) or analysis_state == "degraded"
    # 固定 4 小时刷新分析，不再设置“累计 N 条评级”门槛；待处理评级只是
    # 下次采集前的瞬态，不单独弹横幅。仅在摘要真正降级时提示。
    banner_title = "分析摘要暂时处于降级模式" if is_degraded else ""
    banner_detail = (
        "页面已保留最近一次可用摘要，并继续展示本轮采集数据；"
        + (
            f"另有 {pending_rating_count} 条待处理评级，将在下次采集时统一分析。"
            if pending_rating_count
            else "系统会在下一个 4 小时周期自动重试。"
        )
    ) if is_degraded else ""
    degraded_banner_html = f"""
        <div class="degraded-banner reveal" role="status">
            <span class="degraded-mark">!</span>
            <div>
                <strong>{banner_title}</strong>
                <span>{banner_detail}</span>
            </div>
        </div>
    """ if is_degraded else ""
    x_icon_svg = (
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" '
        'aria-hidden="true"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 '
        '11.24h-6.66l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 '
        '2.25H8.08l4.713 6.231 5.45-6.231zm-1.161 17.52h1.833L7.084 '
        '4.126H5.117l11.966 15.644z"/></svg>'
    )
    x_profile_link_html = (
        f'<a class="x-icon-link" data-testid="x-profile-link" '
        f'href="{escape(X_PROFILE_URL, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer" title="在 X 上关注 @johny_xie" '
        f'aria-label="X 主页">{x_icon_svg}</a>'
    )
    manual_refresh_html = """
        <button class="refresh-btn" id="refreshBtnFast" onclick="refreshData('fast')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/></svg>
            刷新数据
        </button>
        <button class="refresh-secondary" id="refreshBtnFull" onclick="refreshData('full')">完整重算</button>
    """ if can_refresh else ""

    safe_date_range = _safe_text(date_range)
    safe_institution = _safe_text(institution)
    safe_generated_at = _safe_text(generated_at[:19])
    safe_generated_at_attr = _safe_attr(generated_at)
    safe_refresh_mode = _safe_text(refresh_mode)
    safe_core_thesis = _safe_multiline(core_thesis)
    safe_stat_evidence = _safe_multiline(stat_evidence)
    analysis_panel_html = (
        "<p class='analysis-text panel' style='margin-bottom:18px;'>"
        + _safe_multiline(analysis_text)
        + "</p>"
        if analysis_text else ""
    )

    stock_strip = "".join(
        f'<a class="asset-chip" href="/stocks/{ticker.lower()}/">{_safe_text(ticker)}</a>'
        for ticker in HOT_SYMBOLS
    )
    seo_head_html = homepage_head_html(date_range, generated_at)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
{seo_head_html}
    <link rel="shortcut icon" href="/favicon.ico?v=2" />
    <style>
            :root {{
                --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
                --font-serif: "Iowan Old Style", "Songti SC", "STSong", Georgia, serif;
                --font-mono: "SFMono-Regular", "Cascadia Mono", Consolas, "Liberation Mono", monospace;
                --bg: #08080a;
                --bg-elevated: #0c0c10;
                --surface: #12121a;
                --surface-2: #1a1a24;
                --surface-3: #22222e;
                --border: rgba(232, 230, 227, 0.07);
                --border-strong: rgba(232, 230, 227, 0.15);
                --text: #f2f0ec;
                --text-secondary: #9a9791;
                --text-tertiary: #6b6862;
                --gold: #c9a45c;
                --gold-bright: #d4af37;
                --gold-dim: rgba(201, 164, 92, 0.10);
                --gold-glow: rgba(201, 164, 92, 0.22);
                --bull: #4ade80;
                --bear: #f87171;
                --neutral: #fbbf24;
                --accent: #7dd3fc;
                --shadow-sm: 0 4px 12px rgba(0, 0, 0, 0.25);
                --shadow-md: 0 8px 30px rgba(0, 0, 0, 0.35);
                --shadow-lg: 0 20px 60px rgba(0, 0, 0, 0.45);
                --radius-sm: 8px;
                --radius-md: 14px;
                --radius-lg: 20px;
                --radius-xl: 28px;
                --ease-out: cubic-bezier(0.22, 1, 0.36, 1);
                --ease-spring: cubic-bezier(0.34, 1.56, 0.64, 1);
            }}
            :root[data-theme="light"] {{
                --bg: #f7f5f0;
                --bg-elevated: #ffffff;
                --surface: #ffffff;
                --surface-2: #f2efe9;
                --surface-3: #e8e4dc;
                --border: rgba(30, 25, 18, 0.10);
                --border-strong: rgba(30, 25, 18, 0.18);
                --text: #1e1912;
                --text-secondary: #5c564c;
                --text-tertiary: #8a8378;
                --gold: #8b6914;
                --gold-bright: #a67c00;
                --gold-dim: rgba(139, 105, 20, 0.08);
                --gold-glow: rgba(139, 105, 20, 0.15);
                --bull: #15803d;
                --bear: #b91c1c;
                --neutral: #a16207;
                --accent: #0369a1;
                --shadow-sm: 0 4px 12px rgba(30, 25, 18, 0.08);
                --shadow-md: 0 8px 30px rgba(30, 25, 18, 0.10);
                --shadow-lg: 0 20px 60px rgba(30, 25, 18, 0.12);
            }}
            :root[data-theme="sepia"] {{
                --bg: #f0e9db;
                --bg-elevated: #faf5eb;
                --surface: #faf5eb;
                --surface-2: #e9e0cd;
                --surface-3: #ded3bd;
                --border: rgba(60, 48, 30, 0.12);
                --border-strong: rgba(60, 48, 30, 0.22);
                --text: #2e2418;
                --text-secondary: #5c4d3a;
                --text-tertiary: #8a7660;
                --gold: #6b4c1e;
                --gold-bright: #7d5a24;
                --gold-dim: rgba(107, 76, 30, 0.10);
                --gold-glow: rgba(107, 76, 30, 0.15);
                --bull: #3f6212;
                --bear: #991b1b;
                --neutral: #854d0e;
                --accent: #1e40af;
                --shadow-sm: 0 4px 12px rgba(60, 48, 30, 0.08);
                --shadow-md: 0 8px 30px rgba(60, 48, 30, 0.10);
                --shadow-lg: 0 20px 60px rgba(60, 48, 30, 0.12);
            }}
            :root[data-theme="dark"] {{
                --bg: #08080a;
                --bg-elevated: #0c0c10;
                --surface: #12121a;
                --surface-2: #1a1a24;
                --surface-3: #22222e;
                --border: rgba(232, 230, 227, 0.07);
                --border-strong: rgba(232, 230, 227, 0.15);
                --text: #f2f0ec;
                --text-secondary: #9a9791;
                --text-tertiary: #6b6862;
                --gold: #c9a45c;
                --gold-bright: #d4af37;
                --gold-dim: rgba(201, 164, 92, 0.10);
                --gold-glow: rgba(201, 164, 92, 0.22);
                --bull: #4ade80;
                --bear: #f87171;
                --neutral: #fbbf24;
                --accent: #7dd3fc;
                --shadow-sm: 0 4px 12px rgba(0, 0, 0, 0.25);
                --shadow-md: 0 8px 30px rgba(0, 0, 0, 0.35);
                --shadow-lg: 0 20px 60px rgba(0, 0, 0, 0.45);
            }}
            
            * {{ box-sizing: border-box; }}
            
            html {{ scroll-behavior: smooth; }}
            
            body {{
                margin: 0;
                background: var(--bg);
                color: var(--text);
                font-family: var(--font-sans);
                line-height: 1.6;
                -webkit-font-smoothing: antialiased;
                -moz-osx-font-smoothing: grayscale;
                min-height: 100vh;
            }}
            
            /* Atmospheric background layers */
            body::before {{
                content: "";
                position: fixed;
                inset: 0;
                pointer-events: none;
                z-index: 0;
                background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 400 400' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noiseFilter'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noiseFilter)'/%3E%3C/svg%3E");
                opacity: 0.035;
                mix-blend-mode: overlay;
            }}
            body::after {{
                content: "";
                position: fixed;
                top: -20%;
                left: -10%;
                width: 60vw;
                height: 60vw;
                pointer-events: none;
                z-index: 0;
                background: radial-gradient(circle, var(--gold-glow) 0%, transparent 55%);
                filter: blur(100px);
                opacity: 0.6;
            }}
            
            .wrap {{
                position: relative;
                z-index: 1;
                max-width: 1240px;
                margin: 0 auto;
                padding: 56px 28px 100px;
            }}
            
            /* Typography */
            .serif {{ font-family: var(--font-serif); }}
            .mono {{ font-family: var(--font-mono); }}
            .display {{ font-family: var(--font-serif); }}
            
            /* Reveal animations */
            @keyframes fadeUp {{
                from {{ opacity: 0; transform: translateY(30px); }}
                to {{ opacity: 1; transform: translateY(0); }}
            }}
            @keyframes fadeIn {{
                from {{ opacity: 0; }}
                to {{ opacity: 1; }}
            }}
            @keyframes scaleIn {{
                from {{ opacity: 0; transform: scale(0.96); }}
                to {{ opacity: 1; transform: scale(1); }}
            }}
            @keyframes lineExpand {{
                from {{ transform: scaleX(0); }}
                to {{ transform: scaleX(1); }}
            }}
            @keyframes pulse {{
                0% {{ box-shadow: 0 0 0 0 rgba(74, 222, 128, 0.4); }}
                70% {{ box-shadow: 0 0 0 8px rgba(74, 222, 128, 0); }}
                100% {{ box-shadow: 0 0 0 0 rgba(74, 222, 128, 0); }}
            }}
            @keyframes spin {{ 100% {{ transform: rotate(360deg); }} }}
            
            .reveal {{
                opacity: 0;
                animation: fadeUp 0.8s var(--ease-out) forwards;
            }}
            .reveal-delay-1 {{ animation-delay: 0.06s; }}
            .reveal-delay-2 {{ animation-delay: 0.14s; }}
            .reveal-delay-3 {{ animation-delay: 0.22s; }}
            .reveal-delay-4 {{ animation-delay: 0.30s; }}
            .reveal-delay-5 {{ animation-delay: 0.38s; }}
            
            /* Header */
            .topbar {{
                display: flex;
                align-items: flex-start;
                justify-content: space-between;
                gap: 24px;
                padding-bottom: 24px;
                margin-bottom: 32px;
                border-bottom: 1px solid var(--border);
            }}
            .edition {{
                font-family: "JetBrains Mono", monospace;
                font-size: 11px;
                letter-spacing: 0.22em;
                color: var(--gold);
                text-transform: uppercase;
                font-weight: 500;
            }}
            .edition::before {{
                content: "";
                display: inline-block;
                width: 6px;
                height: 6px;
                border-radius: 50%;
                background: var(--gold);
                margin-right: 10px;
                vertical-align: middle;
                box-shadow: 0 0 10px var(--gold-glow);
            }}
            .refresh-controls {{
                display: flex;
                align-items: center;
                gap: 12px;
                flex-wrap: wrap;
                justify-content: flex-end;
            }}
            .refresh-btn {{
                display: inline-flex;
                align-items: center;
                gap: 8px;
                background: var(--surface);
                color: var(--text-secondary);
                border: 1px solid var(--border);
                padding: 9px 18px;
                border-radius: 999px;
                font-size: 13px;
                font-weight: 500;
                cursor: pointer;
                transition: all 0.25s var(--ease-out);
                font-family: "Bricolage Grotesk", sans-serif;
                box-shadow: var(--shadow-sm);
            }}
            .refresh-btn:hover {{
                border-color: var(--gold);
                color: var(--gold);
                background: var(--gold-dim);
                transform: translateY(-1px);
                box-shadow: 0 6px 20px rgba(201, 164, 92, 0.15);
            }}
            .refresh-btn.spinning svg {{ animation: spin 1s linear infinite; }}
            .refresh-secondary {{
                background: transparent;
                color: var(--text-tertiary);
                border: none;
                font-size: 12px;
                font-weight: 500;
                cursor: pointer;
                padding: 8px 6px;
                font-family: "Bricolage Grotesk", sans-serif;
                transition: color 0.2s ease;
            }}
            .refresh-secondary:hover {{ color: var(--gold); }}
            .auto-refresh-note {{
                display: inline-flex;
                align-items: center;
                gap: 8px;
                font-family: "JetBrains Mono", monospace;
                font-size: 11px;
                color: var(--text-secondary);
                margin-bottom: 20px;
                padding: 6px 12px;
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: 999px;
            }}
            .pulse {{
                width: 6px;
                height: 6px;
                border-radius: 50%;
                background: var(--bull);
                animation: pulse 2s infinite;
            }}
            .update-time {{
                font-family: "JetBrains Mono", monospace;
                font-size: 12px;
                color: var(--text-secondary);
                min-width: 80px;
                text-align: right;
            }}
            
            /* Theme switcher */
            .theme-switcher {{
                display: flex;
                align-items: center;
                gap: 4px;
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: 999px;
                padding: 4px;
            }}
            .theme-btn {{
                background: transparent;
                color: var(--text-secondary);
                border: none;
                width: 34px;
                height: 34px;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                border-radius: 999px;
                cursor: pointer;
                transition: all 0.2s var(--ease-out);
                font-family: "Bricolage Grotesk", sans-serif;
            }}
            .theme-btn svg {{ width: 17px; height: 17px; display: block; }}
            .theme-btn.active {{
                background: var(--gold);
                color: var(--bg);
                box-shadow: 0 2px 8px var(--gold-glow);
            }}
            .theme-btn:hover {{ color: var(--text); }}
            .theme-btn.active:hover {{ color: var(--bg); }}
            
            /* Degraded banner */
            .degraded-banner {{
                display: flex;
                align-items: flex-start;
                gap: 14px;
                margin: 0 0 28px;
                padding: 16px 20px;
                border-radius: var(--radius-md);
                border: 1px solid rgba(251, 191, 36, 0.25);
                background: linear-gradient(135deg, rgba(251, 191, 36, 0.08) 0%, transparent 70%);
                color: var(--text);
                font-size: 13px;
                backdrop-filter: blur(8px);
            }}
            .degraded-banner strong {{ display: block; color: var(--neutral); margin-bottom: 4px; font-weight: 600; }}
            .degraded-banner span:not(.degraded-mark) {{ color: var(--text-secondary); line-height: 1.6; }}
            .degraded-mark {{
                display: inline-flex;
                align-items: center;
                justify-content: center;
                flex: 0 0 auto;
                width: 26px;
                height: 26px;
                border-radius: 50%;
                background: rgba(251, 191, 36, 0.15);
                color: var(--neutral);
                font-weight: 700;
                font-size: 14px;
            }}
            
            /* Hero */
            .hero {{
                display: grid;
                grid-template-columns: 1.3fr 0.7fr;
                gap: 56px;
                align-items: end;
                margin-bottom: 80px;
                position: relative;
            }}
            .hero::before {{
                content: "";
                position: absolute;
                top: -40px;
                left: -40px;
                width: 120px;
                height: 120px;
                border: 1px solid var(--border);
                border-radius: 50%;
                opacity: 0.5;
                pointer-events: none;
            }}
            .hero-title {{
                font-size: clamp(44px, 7vw, 86px);
                line-height: 1.0;
                font-weight: 500;
                letter-spacing: -0.04em;
                margin: 0 0 24px;
                color: var(--text);
            }}
            .hero-title em {{
                font-style: italic;
                color: var(--gold);
                font-weight: 400;
            }}
            .hero-sub {{
                font-size: 16px;
                color: var(--text-secondary);
                max-width: 540px;
                line-height: 1.75;
                margin: 0 0 28px;
            }}
            .meta-grid {{
                display: grid;
                gap: 16px;
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: var(--radius-lg);
                padding: 28px;
                box-shadow: var(--shadow-md);
                position: relative;
                overflow: hidden;
            }}
            .meta-grid::before {{
                content: "";
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 2px;
                background: linear-gradient(90deg, var(--gold), transparent);
                opacity: 0.6;
            }}
            .meta-item {{
                border-left: 2px solid var(--border-strong);
                padding-left: 16px;
            }}
            .meta-label {{
                font-size: 10px;
                text-transform: uppercase;
                letter-spacing: 0.18em;
                color: var(--text-tertiary);
                margin-bottom: 5px;
                font-weight: 600;
            }}
            .meta-value {{
                font-size: 15px;
                color: var(--text);
                font-weight: 500;
            }}
            .meta-value.muted {{ color: var(--text-secondary); }}
            
            /* Bank / asset chips */
            .bank-strip {{
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                margin-top: 18px;
            }}
            .bank-chip {{
                font-size: 11px;
                color: var(--text-secondary);
                border: 1px solid var(--border);
                padding: 5px 12px;
                border-radius: 999px;
                background: var(--surface);
                transition: all 0.2s ease;
            }}
            .bank-chip:hover {{
                border-color: var(--gold);
                color: var(--gold);
                background: var(--gold-dim);
            }}
            .bank-more {{
                color: var(--gold);
                border-color: var(--gold-dim);
                background: var(--gold-dim);
                font-weight: 600;
            }}
            .asset-chip {{
                font-family: "JetBrains Mono", monospace;
                font-size: 12px;
                background: var(--surface-2);
                color: var(--text);
                padding: 5px 10px;
                border-radius: var(--radius-sm);
                border: 1px solid var(--border);
            }}
            
            /* Section */
            .section {{ margin-bottom: 64px; }}
            .section-head {{
                display: flex;
                align-items: baseline;
                justify-content: space-between;
                margin-bottom: 24px;
                padding-bottom: 14px;
                border-bottom: 1px solid var(--border);
                position: relative;
            }}
            .section-head::after {{
                content: "";
                position: absolute;
                bottom: -1px;
                left: 0;
                width: 80px;
                height: 1px;
                background: var(--gold);
                opacity: 0.6;
            }}
            .section-title {{
                font-size: 12px;
                text-transform: uppercase;
                letter-spacing: 0.22em;
                color: var(--gold);
                font-weight: 700;
                font-family: "JetBrains Mono", monospace;
            }}
            .section-count {{
                font-family: "Fraunces", serif;
                font-size: 15px;
                color: var(--text-tertiary);
                font-style: italic;
            }}
            
            /* Panels / cards */
            .panel {{
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: var(--radius-lg);
                padding: 32px;
                transition: all 0.35s var(--ease-out);
                box-shadow: var(--shadow-sm);
                position: relative;
                overflow: hidden;
            }}
            .panel::before {{
                content: "";
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 1px;
                background: linear-gradient(90deg, var(--gold), transparent 60%);
                opacity: 0;
                transition: opacity 0.3s ease;
            }}
            .panel:hover {{
                border-color: var(--border-strong);
                transform: translateY(-3px);
                box-shadow: var(--shadow-md);
            }}
            .panel:hover::before {{ opacity: 0.5; }}
            
            /* Core thesis */
            .thesis {{
                font-size: clamp(22px, 3vw, 32px);
                line-height: 1.55;
                color: var(--text);
                font-weight: 300;
            }}
            .thesis::before {{
                content: "";
                display: block;
                width: 48px;
                height: 3px;
                background: var(--gold);
                margin-bottom: 26px;
                border-radius: 2px;
            }}
            
            /* Logo styles */
            .logo-wrap {{
                display: flex;
                align-items: center;
                justify-content: center;
                overflow: hidden;
            }}
            .symbol-cell {{
                display: flex;
                align-items: center;
                gap: 10px;
            }}
            .row-logo-wrap {{
                width: 28px;
                height: 28px;
                border-radius: 8px;
                background: var(--surface-2);
                border: 1px solid var(--border);
                box-shadow: inset 0 1px 2px rgba(0,0,0,0.1);
            }}
            .row-logo {{
                width: 100%;
                height: 100%;
                object-fit: contain;
                padding: 3px;
            }}
            .row-logo-fallback {{
                display: none;
                width: 100%;
                height: 100%;
                background: var(--gold-dim);
                color: var(--gold);
                font-size: 11px;
                align-items: center;
                justify-content: center;
                font-weight: 700;
            }}
            .asset-logo-wrap {{
                width: 48px;
                height: 48px;
                border-radius: 14px;
                background: var(--surface-2);
                border: 1px solid var(--border);
                box-shadow: inset 0 1px 2px rgba(0,0,0,0.1);
            }}
            .asset-card-top {{
                display: flex;
                align-items: flex-start;
                justify-content: space-between;
                margin-bottom: 16px;
            }}
            .source-badge {{
                display: inline-flex;
                align-items: center;
                font-family: "JetBrains Mono", monospace;
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 0.08em;
                padding: 4px 8px;
                border-radius: 4px;
            }}
            .source-consensus {{
                color: var(--bull);
                background: rgba(74, 222, 128, 0.10);
                border: 1px solid rgba(74, 222, 128, 0.22);
            }}
            .source-verified {{
                color: var(--gold);
                background: var(--gold-dim);
                border: 1px solid rgba(201, 164, 92, 0.25);
            }}
            .source-llm {{
                color: var(--text-tertiary);
                background: var(--surface-2);
                border: 1px solid var(--border);
            }}
            .analyst-dist {{
                margin: 12px 0 4px;
            }}
            .dist-bar {{
                display: flex;
                height: 5px;
                border-radius: 3px;
                overflow: hidden;
                background: var(--surface-2);
            }}
            .dist-seg {{ display: block; height: 100%; }}
            .dist-bull {{ background: var(--bull); }}
            .dist-hold {{ background: var(--neutral); }}
            .dist-bear {{ background: var(--bear); }}
            .dist-labels {{
                display: flex;
                justify-content: space-between;
                margin-top: 6px;
                font-family: "JetBrains Mono", monospace;
                font-size: 10px;
                color: var(--text-tertiary);
            }}
            .dist-bull-t {{ color: var(--bull); }}
            .dist-hold-t {{ color: var(--neutral); }}
            .dist-bear-t {{ color: var(--bear); }}
            .change-rating {{
                font-size: 11px;
                font-weight: 700;
                color: var(--text);
                padding: 2px 8px;
                border-radius: 999px;
                border: 1px solid var(--border-strong);
                background: var(--surface);
            }}
            .asset-logo {{
                width: 70%;
                height: 70%;
                object-fit: contain;
            }}
            .asset-logo-fallback {{
                display: none;
                width: 100%;
                height: 100%;
                background: var(--gold-dim);
                color: var(--gold);
                font-size: 18px;
                font-weight: 700;
                align-items: center;
                justify-content: center;
            }}
            
            /* Bernstein badge */
            .bernstein-badge {{
                display: inline-flex;
                align-items: center;
                font-family: "JetBrains Mono", monospace;
                font-size: 9px;
                font-weight: 700;
                color: var(--bg);
                background: var(--gold);
                padding: 3px 7px;
                border-radius: 4px;
                margin-left: 8px;
                vertical-align: middle;
                letter-spacing: 0.05em;
            }}
            
            /* Rating toolbar */
            .rating-toolbar {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                flex-wrap: wrap;
                gap: 16px;
                margin: 0 0 18px;
            }}
            .rating-toolbar-group {{
                display: flex;
                align-items: center;
                gap: 10px;
                flex-wrap: wrap;
            }}
            .rating-toolbar-label {{
                font-family: "JetBrains Mono", monospace;
                font-size: 10px;
                letter-spacing: 0.12em;
                text-transform: uppercase;
                color: var(--text-tertiary);
            }}
            .rating-segment {{
                display: inline-flex;
                align-items: center;
                padding: 3px;
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: 999px;
                box-shadow: var(--shadow-sm);
            }}
            .rating-control {{
                border: 0;
                border-radius: 999px;
                padding: 7px 13px;
                cursor: pointer;
                color: var(--text-secondary);
                background: transparent;
                font: 500 12px "Bricolage Grotesk", sans-serif;
                transition: all 0.2s var(--ease-out);
            }}
            .rating-control:hover {{ color: var(--text); }}
            .rating-control.active {{
                color: var(--bg);
                background: var(--gold);
                font-weight: 600;
                box-shadow: 0 2px 8px var(--gold-glow);
            }}
            .rating-visible-count {{
                font-family: "JetBrains Mono", monospace;
                font-size: 11px;
                color: var(--text-secondary);
            }}
            
            /* Table */
            .table-wrap {{
                overflow-x: auto;
                border-radius: var(--radius-lg);
                border: 1px solid var(--border);
                background: var(--surface);
                box-shadow: var(--shadow-sm);
            }}
            table {{
                width: 100%;
                border-collapse: separate;
                border-spacing: 0;
                font-size: 14px;
            }}
            thead th {{
                background: var(--surface-2);
                color: var(--text-secondary);
                font-weight: 600;
                text-transform: uppercase;
                font-size: 10px;
                letter-spacing: 0.12em;
                padding: 16px;
                text-align: left;
                white-space: nowrap;
                border-bottom: 1px solid var(--border);
                font-family: "JetBrains Mono", monospace;
                position: sticky;
                top: 0;
                z-index: 2;
            }}
            thead th:first-child {{ border-top-left-radius: var(--radius-lg); }}
            thead th:last-child {{ border-top-right-radius: var(--radius-lg); }}
            tbody tr {{
                border-bottom: 1px solid var(--border);
                transition: background 0.2s ease;
            }}
            tbody tr:last-child {{ border-bottom: none; }}
            tbody tr:hover {{ background: rgba(201, 164, 92, 0.04); }}
            tbody tr.group-start td {{ border-top: 2px solid rgba(201, 164, 92, 0.22); }}
            tbody tr[hidden] {{ display: none; }}
            tbody td {{
                padding: 18px 16px;
                vertical-align: middle;
                border-bottom: 1px solid var(--border);
            }}
            tbody tr:last-child td {{ border-bottom: none; }}
            .symbol {{
                font-family: "JetBrains Mono", monospace;
                font-weight: 700;
                color: var(--text);
                font-size: 15px;
                letter-spacing: -0.02em;
            }}
            .rating-time {{
                white-space: nowrap;
                color: var(--text-secondary);
            }}
            .arrow {{
                color: var(--text-tertiary);
                font-size: 13px;
                padding-left: 4px;
                padding-right: 4px;
            }}
            .tag {{
                display: inline-block;
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 0.03em;
                padding: 5px 11px;
                border-radius: 999px;
                border: 1px solid var(--border);
                color: var(--text-secondary);
                background: rgba(255, 255, 255, 0.03);
            }}
            .tag-bull {{
                color: var(--bull);
                border-color: rgba(74, 222, 128, 0.25);
                background: rgba(74, 222, 128, 0.08);
            }}
            .tag-empty {{
                color: var(--text-tertiary);
                border-style: dashed;
                background: transparent;
                cursor: help;
            }}
            .pt-empty {{
                color: var(--text-tertiary);
                cursor: help;
            }}
            .tag-bear {{
                color: var(--bear);
                border-color: rgba(248, 113, 113, 0.25);
                background: rgba(248, 113, 113, 0.08);
            }}
            .tag-neutral {{
                color: var(--neutral);
                border-color: rgba(251, 191, 36, 0.25);
                background: rgba(251, 191, 36, 0.08);
            }}
            .action-dot {{
                display: inline-block;
                width: 8px;
                height: 8px;
                border-radius: 50%;
            }}
            .action-up {{
                background: var(--bull);
                box-shadow: 0 0 10px rgba(74, 222, 128, 0.5);
            }}
            .action-down {{
                background: var(--bear);
                box-shadow: 0 0 10px rgba(248, 113, 113, 0.5);
            }}
            .action-new {{
                background: var(--accent);
                box-shadow: 0 0 10px rgba(125, 211, 252, 0.5);
            }}
            .action-flat {{
                background: var(--text-tertiary);
            }}
            .priority-high {{
                font-family: "JetBrains Mono", monospace;
                font-size: 9px;
                font-weight: 700;
                color: var(--bear);
                border: 1px solid rgba(248, 113, 113, 0.25);
                padding: 4px 7px;
                border-radius: 4px;
                letter-spacing: 0.05em;
            }}
            .priority-normal {{
                font-family: "JetBrains Mono", monospace;
                font-size: 9px;
                color: var(--text-secondary);
                border: 1px solid var(--border);
                padding: 4px 7px;
                border-radius: 4px;
                letter-spacing: 0.05em;
            }}
            .analysis-text {{
                color: var(--text-secondary);
                line-height: 1.85;
                font-size: 15px;
                white-space: pre-line;
            }}
            
            /* Asset grid */
            .asset-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
                gap: 18px;
            }}
            .asset-card {{
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: var(--radius-md);
                padding: 22px;
                transition: all 0.35s var(--ease-out);
                box-shadow: var(--shadow-sm);
                position: relative;
                overflow: hidden;
            }}
            .asset-card::before {{
                content: "";
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 2px;
                background: linear-gradient(90deg, var(--gold), transparent);
                opacity: 0;
                transition: opacity 0.3s ease;
            }}
            .asset-card:hover {{
                border-color: var(--gold);
                transform: translateY(-4px);
                box-shadow: var(--shadow-md), 0 0 30px var(--gold-glow);
            }}
            .asset-card:hover::before {{ opacity: 0.6; }}
            .asset-header {{
                display: flex;
                justify-content: space-between;
                align-items: baseline;
                margin-bottom: 10px;
            }}
            .asset-ticker {{
                font-family: "JetBrains Mono", monospace;
                font-size: 22px;
                font-weight: 700;
                color: var(--text);
                letter-spacing: -0.02em;
            }}
            .asset-target {{
                font-family: "JetBrains Mono", monospace;
                font-size: 16px;
                color: var(--gold);
                font-weight: 600;
            }}
            .asset-meta {{
                font-size: 12px;
                color: var(--text-secondary);
                margin-top: 8px;
                line-height: 1.5;
            }}
            
            /* Asset target change */
            .asset-change {{
                display: flex;
                align-items: center;
                gap: 8px;
                flex-wrap: wrap;
                margin-top: 12px;
                padding: 10px 12px;
                background: var(--surface-2);
                border: 1px solid var(--border);
                border-radius: var(--radius-sm);
                font-size: 12px;
            }}
            .change-arrow {{
                font-weight: 700;
                font-size: 13px;
            }}
            .change-up {{ color: var(--bull); }}
            .change-down {{ color: var(--bear); }}
            .change-flat {{ color: var(--text-secondary); }}
            .change-new {{ color: var(--accent); }}
            .change-old {{
                color: var(--text-tertiary);
                text-decoration: line-through;
            }}
            .change-new {{
                color: var(--gold);
                font-weight: 600;
            }}
            .change-bank {{ color: var(--text); font-weight: 500; }}
            .change-time {{
                color: var(--text-secondary);
                font-family: "JetBrains Mono", monospace;
            }}
            .change-divider {{ color: var(--text-tertiary); }}
            
            /* Macro grid */
            .macro-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
                gap: 16px;
            }}
            .macro-cell {{
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: var(--radius-md);
                padding: 24px;
                transition: all 0.25s ease;
            }}
            .macro-cell:hover {{
                border-color: var(--border-strong);
                transform: translateY(-2px);
                box-shadow: var(--shadow-sm);
            }}
            .macro-key {{
                font-size: 10px;
                text-transform: uppercase;
                letter-spacing: 0.16em;
                color: var(--text-tertiary);
                margin-bottom: 10px;
                font-weight: 700;
            }}
            .macro-value {{
                font-size: 19px;
                color: var(--text);
                font-weight: 500;
                line-height: 1.4;
            }}
            
            /* Evidence / Risk split */
            .split-grid {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 28px;
            }}
            .evidence-text {{
                color: var(--text);
                line-height: 1.85;
                font-size: 15px;
            }}
            .risk-list {{
                list-style: none;
                padding: 0;
                margin: 0;
            }}
            .risk-item {{
                position: relative;
                padding-left: 24px;
                margin-bottom: 16px;
                color: var(--text);
                line-height: 1.75;
                font-size: 15px;
            }}
            .risk-item::before {{
                content: "▸";
                position: absolute;
                left: 0;
                top: 0;
                color: var(--bear);
                font-weight: 700;
            }}
            .muted {{ color: var(--text-secondary); }}
            .empty {{
                color: var(--text-secondary);
                font-style: italic;
                padding: 16px 0;
                margin: 0;
            }}
            .empty-cell {{
                color: var(--text-secondary);
                text-align: center;
                padding: 40px;
                font-style: italic;
            }}
            
            /* Footer */
            .footer {{
                margin-top: 100px;
                padding-top: 32px;
                border-top: 1px solid var(--border);
                display: flex;
                justify-content: space-between;
                flex-wrap: wrap;
                gap: 16px;
                font-size: 12px;
                color: var(--text-secondary);
                line-height: 1.7;
            }}
            
            /* BMC / Contact panels */
            .bmc-panel {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                flex-wrap: wrap;
                gap: 20px;
                margin-top: 56px;
                padding: 28px 32px;
                border: 1px solid var(--border);
                border-radius: var(--radius-lg);
                background: linear-gradient(135deg, var(--gold-dim) 0%, transparent 60%);
                position: relative;
                overflow: hidden;
            }}
            .bmc-panel::before {{
                content: "";
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 1px;
                background: linear-gradient(90deg, var(--gold), transparent);
                opacity: 0.4;
            }}
            .bmc-title {{
                font-size: 17px;
                font-weight: 600;
                color: var(--text);
            }}
            .bmc-sub {{
                font-size: 13px;
                color: var(--text-secondary);
                margin-top: 5px;
                max-width: 520px;
            }}
            .bmc-disclaimer {{
                font-size: 11px;
                color: var(--text-tertiary);
                margin-top: 9px;
            }}
            .contact-panel {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                flex-wrap: wrap;
                gap: 20px;
                margin-top: 18px;
                padding: 24px 32px;
                border: 1px solid var(--border);
                border-radius: var(--radius-lg);
                background: var(--surface);
                box-shadow: var(--shadow-sm);
            }}
            .contact-title {{
                font-size: 16px;
                font-weight: 600;
                color: var(--text);
            }}
            .contact-sub {{
                font-size: 13px;
                color: var(--text-secondary);
                margin-top: 4px;
            }}
            .contact-btn {{
                display: inline-flex;
                align-items: center;
                gap: 8px;
                color: var(--gold);
                font-family: "JetBrains Mono", monospace;
                font-size: 13px;
                text-decoration: none;
                border: 1px solid var(--border);
                border-radius: 999px;
                padding: 10px 18px;
                transition: all 0.25s var(--ease-out);
                font-weight: 500;
            }}
            .contact-btn:hover {{
                border-color: var(--gold);
                background: var(--gold-dim);
                transform: translateY(-1px);
            }}
            .bmc-btn {{
                display: inline-flex;
                align-items: center;
                gap: 8px;
                background: var(--gold);
                color: var(--bg);
                font-weight: 700;
                padding: 12px 22px;
                border-radius: 999px;
                text-decoration: none;
                transition: all 0.25s var(--ease-out);
                box-shadow: 0 4px 16px var(--gold-glow);
            }}
            .bmc-btn:hover {{
                transform: translateY(-2px);
                box-shadow: 0 8px 28px var(--gold-glow);
                background: var(--gold-bright);
            }}
            
            /* Toast */
            .toast {{
                position: fixed;
                bottom: 32px;
                right: 32px;
                z-index: 50;
                background: var(--surface-2);
                color: var(--text);
                border: 1px solid var(--border-strong);
                padding: 16px 24px;
                border-radius: var(--radius-md);
                font-size: 13px;
                font-weight: 500;
                transform: translateY(140%);
                opacity: 0;
                transition: all 0.4s var(--ease-out);
                box-shadow: var(--shadow-lg);
                max-width: 360px;
                line-height: 1.5;
            }}
            .toast.show {{
                transform: translateY(0);
                opacity: 1;
            }}
            
            /* Responsive */
            @media (max-width: 900px) {{
                .wrap {{ padding: 36px 20px 70px; }}
                .hero {{ grid-template-columns: 1fr; gap: 40px; }}
                .split-grid {{ grid-template-columns: 1fr; }}
                .topbar {{ flex-direction: column; align-items: flex-start; }}
                .refresh-controls {{ justify-content: flex-start; width: 100%; }}
                .update-time {{ text-align: left; flex-basis: 100%; }}
                .section-head {{ flex-direction: column; align-items: flex-start; gap: 6px; }}
                .panel {{ padding: 24px; }}
            }}
            @media (max-width: 640px) {{
                .hero-title {{ font-size: clamp(36px, 12vw, 56px); }}
                .asset-grid {{ grid-template-columns: 1fr; }}
                .rating-toolbar {{ flex-direction: column; align-items: flex-start; }}
                .bmc-panel, .contact-panel {{ flex-direction: column; align-items: flex-start; }}
                table {{ font-size: 13px; }}
                tbody td {{ padding: 14px 12px; }}
            }}
            .site-nav {{
                display: flex;
                flex-wrap: wrap;
                gap: 8px 16px;
                align-items: center;
                margin-bottom: 18px;
            }}
            .site-nav .brand {{
                font-family: var(--font-serif);
                font-size: 20px;
                color: var(--text);
                margin-right: 8px;
                text-decoration: none;
            }}
            .site-nav a {{
                color: var(--text-secondary);
                font-size: 13px;
                text-decoration: none;
            }}
            .site-nav a:hover,
            .site-nav a[aria-current="page"] {{
                color: var(--gold);
            }}
            a.asset-chip,
            a.asset-ticker,
            a.symbol {{
                color: inherit;
                text-decoration: none;
            }}
            a.asset-chip:hover,
            a.asset-ticker:hover,
            a.symbol:hover {{
                color: var(--gold);
            }}
            .footer-nav {{
                display: flex;
                flex-wrap: wrap;
                gap: 10px 16px;
                margin-top: 28px;
                padding-top: 16px;
                border-top: 1px solid var(--border);
                font-size: 13px;
            }}
            .footer-nav a {{ color: var(--text-secondary); }}
            .stock-strip {{
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                margin-top: 16px;
            }}
            </style>
</head>
<body>
    <div class="wrap">
        {homepage_nav_html()}
        <header class="topbar reveal">
            <div class="edition">Foreign Investment Bank Research · {safe_date_range}</div>
            <div class="refresh-controls">
                {manual_refresh_html}
                {x_profile_link_html}
                <div class="theme-switcher" role="group" aria-label="切换主题">
                    <button type="button" class="theme-btn active" data-theme="dark" onclick="setTheme('dark')" title="暗色" aria-label="暗色主题"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg></button>
                    <button type="button" class="theme-btn" data-theme="light" onclick="setTheme('light')" title="明亮" aria-label="明亮主题"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><line x1="12" y1="1" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg></button>
                    <button type="button" class="theme-btn" data-theme="sepia" onclick="setTheme('sepia')" title="护眼" aria-label="护眼主题"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></button>
                </div>
                <span class="update-time" id="updateTime"></span>
            </div>
        </header>

        <div class="auto-refresh-note reveal">
            <span class="pulse"></span>
            系统每 4 小时采集并刷新分析 · 每日开盘前完整重算
        </div>
        {degraded_banner_html}

        <div id="generatedAt" data-time="{safe_generated_at_attr}" style="display:none"></div>

        <section class="hero reveal reveal-delay-1">
            <div>
                <h1 class="hero-title serif">AI 金融<em>研究</em>平台</h1>
                <p class="hero-sub">
                    FResearch 追踪外资投行对 AI 算力、半导体与数据中心主线的评级、目标价与研报变动。
                    覆盖 {len(coverage_banks)} 家主要外资机构，排除中国本土投行；
                    由内部聚合分析引擎基于 {len(meta.get('data_sources', []))} 个数据源做结构化摘要。
                    每只关注股票、每个主题和每条研究方法都有独立可索引 URL。
                </p>
                {f'<div class="bank-strip">{bank_bubbles}</div>' if bank_bubbles else ''}
                <div class="stock-strip" aria-label="关注股票">{stock_strip}</div>
            </div>
            <div class="meta-grid">
                <div class="meta-item">
                    <div class="meta-label">Institution</div>
                    <div class="meta-value">{safe_institution}</div>
                </div>
                <div class="meta-item">
                    <div class="meta-label">Date Range</div>
                    <div class="meta-value">{safe_date_range}</div>
                </div>
                <div class="meta-item">
                    <div class="meta-label">Generated At</div>
                    <div class="meta-value mono">{safe_generated_at} <span class="muted">({safe_refresh_mode})</span></div>
                </div>
                <div class="meta-item">
                    <div class="meta-label">Data Points</div>
                    <div class="meta-value mono">{news_count} news / {rec_count} recs</div>
                </div>
                <div class="meta-item">
                    <div class="meta-label">Assets Covered</div>
                    <div class="meta-value" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:4px;">{assets_html if assets_html else '<span class="muted">—</span>'}</div>
                </div>
            </div>
        </section>

        <section class="section reveal reveal-delay-2">
            <div class="section-head">
                <span class="section-title">Core Thesis</span>
                <span class="section-count">01</span>
            </div>
            <div class="panel">
                <div class="thesis serif">{safe_core_thesis}</div>
            </div>
        </section>

        <section class="section reveal reveal-delay-3">
            <div class="section-head">
                <span class="section-title">Rating Changes</span>
                <span class="section-count">{len(changes):02d}</span>
            </div>
            {analysis_panel_html}
            <div class="rating-toolbar" aria-label="评级变化视图设置">
                <div class="rating-toolbar-group">
                    <span class="rating-toolbar-label">时间范围</span>
                    <div class="rating-segment" role="group" aria-label="筛选最近天数">
                        <button class="rating-control" type="button" data-rating-days="7" onclick="setRatingWindow(7)">7 天</button>
                        <button class="rating-control" type="button" data-rating-days="14" onclick="setRatingWindow(14)">14 天</button>
                        <button class="rating-control active" type="button" data-rating-days="30" onclick="setRatingWindow(30)">30 天</button>
                    </div>
                </div>
                <div class="rating-toolbar-group">
                    <span class="rating-toolbar-label">排序</span>
                    <div class="rating-segment" role="group" aria-label="评级排序方式">
                        <button class="rating-control active" type="button" data-rating-sort="time" onclick="setRatingSort('time')">按时间</button>
                        <button class="rating-control" type="button" data-rating-sort="symbol" onclick="setRatingSort('symbol')">按个股</button>
                    </div>
                    <span class="rating-visible-count" id="ratingVisibleCount">0 条</span>
                    <span class="rating-visible-count">* 为首次记录日</span>
                </div>
            </div>
            <div class="table-wrap reveal">
                <table>
                    <thead>
                        <tr>
                            <th></th>
                            <th>Symbol</th>
                            <th>Bank</th>
                            <th>Action</th>
                            <th>Old</th>
                            <th></th>
                            <th>New</th>
                            <th>Target</th>
                            <th>Time</th>
                            <th>Priority</th>
                        </tr>
                    </thead>
                    <tbody id="ratingTableBody">
                        {rating_rows}
                    </tbody>
                </table>
            </div>
        </section>

        <section class="section reveal reveal-delay-4">
            <div class="section-head">
                <span class="section-title">Asset Targets</span>
                <span class="section-count">{len(asset_targets):02d}</span>
            </div>
            <div class="asset-grid">
                {asset_cards}
            </div>
        </section>

        <section class="section reveal reveal-delay-4">
            <div class="section-head">
                <span class="section-title">Macro Variables</span>
                <span class="section-count">EXP</span>
            </div>
            <div class="panel">
                {macro_html}
            </div>
        </section>

        <div class="split-grid reveal reveal-delay-5">
            <section class="section">
                <div class="section-head">
                    <span class="section-title">Statistical Evidence</span>
                    <span class="section-count">02</span>
                </div>
                <div class="panel">
                    <div class="evidence-text">{safe_stat_evidence}</div>
                </div>
            </section>

            <section class="section">
                <div class="section-head">
                    <span class="section-title">Tail Risks</span>
                    <span class="section-count">03</span>
                </div>
                <div class="panel">
                    {_render_tail_risks(tail_risks)}
                </div>
            </section>
        </div>

        <footer class="footer reveal reveal-delay-5">
            <div>
                数据来源: {sources_html} · AI 摘要: 内部聚合分析引擎 · 框架: 对冲基金量化研究标准
            </div>
            <div style="text-align:right;">
                本报告仅供信息参考，不构成投资建议
            </div>
        </footer>

        <!-- Buy Me a Coffee -->
        <div class="bmc-panel reveal reveal-delay-5">
            <div class="bmc-text">
                <div class="bmc-title">自愿支持本站维护</div>
                <div class="bmc-sub">如果这份整理对你有帮助，可以自愿请我喝杯咖啡，用于服务器、数据与日常维护。</div>
                <div class="bmc-disclaimer">打赏不解锁内容，也不是付费投资建议；本站不处理银行卡信息。若第三方页面暂未显示支付入口，表示收款账户仍在配置中。</div>
            </div>
            <a class="bmc-btn" data-testid="bmc-support-link" href="{escape(BMC_URL, quote=True)}" target="_blank" rel="noopener noreferrer">☕ Buy me a coffee</a>
        </div>

        <!-- Contact -->
        <div class="contact-panel reveal reveal-delay-5">
            <div class="contact-text">
                <div class="contact-title">有反馈或建议？</div>
                <div class="contact-sub">欢迎发邮件交流，或告诉我你希望新增的数据源与功能。</div>
            </div>
            <a class="contact-btn" href="mailto:{CONTACT_EMAIL}" target="_blank" rel="noopener">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
                {CONTACT_EMAIL}
            </a>
            {x_profile_link_html}
        </div>

        <!-- Compliance Disclaimer -->
        <div class="disclaimer-panel reveal reveal-delay-5">
            <div class="disclaimer-title">Disclaimer</div>
            <div class="disclaimer-body">
                Fresearch is a research and information software platform. We do not provide investment advisory, brokerage, asset management, or trade execution services. Information provided by the platform is for research and educational purposes only and does not constitute investment advice.
            </div>
        </div>
        {homepage_footer_nav_html()}
    </div>

    <style>
        .bmc-panel {{
            display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 16px;
            margin-top: 48px; padding: 24px 28px;
            border: 1px solid var(--border); border-radius: 16px;
            background: linear-gradient(135deg, rgba(201,164,92,0.08) 0%, transparent 60%);
        }}
        .bmc-title {{ font-size: 16px; font-weight: 600; color: var(--text); }}
        .bmc-sub {{ font-size: 13px; color: var(--muted); margin-top: 4px; }}
        .bmc-disclaimer {{ font-size: 11px; color: var(--muted); margin-top: 7px; opacity: 0.86; }}
        .contact-panel {{
            display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 16px;
            margin-top: 16px; padding: 20px 28px;
            border: 1px solid var(--border); border-radius: 16px;
            background: var(--surface);
        }}
        .contact-panel > .contact-text {{ flex: 1 1 240px; }}
        .contact-panel > a {{ flex: 0 0 auto; }}
        .x-icon-link {{
            display: inline-flex; align-items: center; justify-content: center;
            width: 36px; height: 36px; color: var(--gold);
            border: 1px solid var(--border); border-radius: 999px;
            text-decoration: none; transition: all 0.2s ease;
        }}
        .x-icon-link:hover {{
            border-color: var(--gold); background: var(--gold-dim);
            transform: translateY(-2px); box-shadow: 0 6px 18px rgba(201,164,92,0.22);
        }}
        .refresh-controls .x-icon-link {{ width: 32px; height: 32px; }}
        .contact-title {{ font-size: 15px; font-weight: 600; color: var(--text); }}
        .contact-sub {{ font-size: 13px; color: var(--muted); margin-top: 3px; }}
        .contact-btn {{
            display: inline-flex; align-items: center; gap: 8px;
            color: var(--gold); font-family: "IBM Plex Mono", monospace; font-size: 13px;
            text-decoration: none; border: 1px solid var(--border); border-radius: 999px;
            padding: 8px 16px; transition: all 0.2s ease;
        }}
        .contact-btn:hover {{ border-color: var(--gold); background: var(--gold-dim); }}
        .bmc-btn {{
            display: inline-flex; align-items: center; gap: 8px;
            background: var(--gold); color: var(--bg); font-weight: 600;
            padding: 10px 18px; border-radius: 999px; text-decoration: none;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
        }}
        .bmc-btn:hover {{ transform: translateY(-2px); box-shadow: 0 8px 24px rgba(201,164,92,0.25); }}
        .disclaimer-panel {{
            margin-top: 16px; padding: 18px 28px;
            border: 1px solid var(--border); border-radius: 16px;
            background: var(--surface);
        }}
        .disclaimer-title {{ font-size: 12px; font-weight: 600; letter-spacing: 0.12em; text-transform: uppercase; color: var(--gold); margin-bottom: 6px; }}
        .disclaimer-body {{ font-size: 12px; line-height: 1.7; color: var(--muted); }}
    </style>

    <div class="toast" id="toast"></div>
    <script>
        const REFRESH_TOKEN = {refresh_token_js};
        function showToast(message, isError = false) {{
            const t = document.getElementById('toast');
            t.textContent = message;
            t.style.borderColor = isError ? 'rgba(239,68,68,0.4)' : 'var(--border-strong)';
            t.classList.add('show');
            setTimeout(() => t.classList.remove('show'), 3200);
        }}
        const ratingView = {{ days: 30, sort: 'time' }};
        function ratingDateValue(row) {{
            const value = row.dataset.date || '';
            if (!/^\\d{{4}}-\\d{{2}}-\\d{{2}}$/.test(value)) return null;
            const parsed = new Date(value + 'T00:00:00');
            return Number.isNaN(parsed.getTime()) ? null : parsed;
        }}
        function applyRatingView() {{
            const tbody = document.getElementById('ratingTableBody');
            if (!tbody) return;
            const rows = Array.from(tbody.querySelectorAll('.rating-row'));
            const emptyRow = document.getElementById('ratingFilterEmpty');
            rows.sort((a, b) => {{
                const dateOrder = (b.dataset.date || '').localeCompare(a.dataset.date || '');
                const symbolOrder = (a.dataset.symbol || '').localeCompare(b.dataset.symbol || '', 'en');
                const bankOrder = (a.dataset.bank || '').localeCompare(b.dataset.bank || '', 'zh-CN');
                if (ratingView.sort === 'symbol') return symbolOrder || dateOrder || bankOrder;
                return dateOrder || symbolOrder || bankOrder;
            }});

            const cutoff = new Date();
            cutoff.setHours(0, 0, 0, 0);
            cutoff.setDate(cutoff.getDate() - (ratingView.days - 1));
            let visibleCount = 0;
            let previousSymbol = null;
            rows.forEach(row => {{
                const eventDate = ratingDateValue(row);
                const visible = eventDate ? eventDate >= cutoff : ratingView.days === 30;
                row.hidden = !visible;
                row.classList.remove('group-start');
                if (visible) {{
                    visibleCount += 1;
                    if (ratingView.sort === 'symbol' && row.dataset.symbol !== previousSymbol) {{
                        row.classList.add('group-start');
                        previousSymbol = row.dataset.symbol;
                    }}
                }}
                tbody.appendChild(row);
            }});
            if (emptyRow) {{
                emptyRow.hidden = visibleCount !== 0;
                tbody.appendChild(emptyRow);
            }}
            const counter = document.getElementById('ratingVisibleCount');
            if (counter) counter.textContent = `${{visibleCount}} 条`;
        }}
        function setRatingWindow(days) {{
            ratingView.days = days;
            document.querySelectorAll('[data-rating-days]').forEach(button => {{
                const active = Number(button.dataset.ratingDays) === days;
                button.classList.toggle('active', active);
                button.setAttribute('aria-pressed', active ? 'true' : 'false');
            }});
            applyRatingView();
        }}
        function setRatingSort(mode) {{
            ratingView.sort = mode === 'symbol' ? 'symbol' : 'time';
            document.querySelectorAll('[data-rating-sort]').forEach(button => {{
                const active = button.dataset.ratingSort === ratingView.sort;
                button.classList.toggle('active', active);
                button.setAttribute('aria-pressed', active ? 'true' : 'false');
            }});
            applyRatingView();
        }}
        async function refreshData(mode = 'full') {{
            const btnId = mode === 'fast' ? 'refreshBtnFast' : 'refreshBtnFull';
            const btn = document.getElementById(btnId);
            if (!btn) return;
            if (btn.classList.contains('spinning')) return;
            btn.classList.add('spinning');
            showToast(mode === 'fast' ? '正在快速刷新 (RSS)...' : '正在完整刷新...');
            try {{
                const r = await fetch(`/api/ib-research/refresh?mode=${{mode}}`, {{ method: 'POST', headers: {{ 'X-Refresh-Token': REFRESH_TOKEN }} }});
                const j = await r.json();
                if (j.status === 'ok') {{
                    showToast((j.message || '刷新完成') + '，即将重载页面');
                    setTimeout(() => location.reload(), 900);
                }} else if (j.status === 'busy') {{
                    showToast('刷新正在进行中', true);
                    btn.classList.remove('spinning');
                }} else {{
                    showToast('刷新失败: ' + (j.message || 'unknown'), true);
                    btn.classList.remove('spinning');
                }}
            }} catch (e) {{
                showToast('网络错误: ' + e.message, true);
                btn.classList.remove('spinning');
            }}
        }}
        function updateRelativeTime() {{
            const el = document.getElementById('generatedAt');
            const out = document.getElementById('updateTime');
            if (!el || !out) return;
            const t = new Date(el.dataset.time);
            if (isNaN(t)) return;
            const diffMs = Date.now() - t.getTime();
            const mins = Math.floor(diffMs / 60000);
            const hours = Math.floor(mins / 60);
            let text;
            if (mins < 1) text = '刚刚更新';
            else if (mins < 60) text = `${{mins}} 分钟前`;
            else if (hours < 24) text = `${{hours}} 小时前`;
            else text = `${{Math.floor(hours/24)}} 天前`;
            out.textContent = text;
        }}
        async function refreshPendingLogos(attempt = 0) {{
            const pending = Array.from(document.querySelectorAll('img[data-logo-pending="1"]'));
            if (!pending.length || attempt >= 4) return;
            const groups = new Map();
            pending.forEach(img => {{
                const key = img.getAttribute('src');
                if (!groups.has(key)) groups.set(key, []);
                groups.get(key).push(img);
            }});
            await Promise.all(Array.from(groups.entries()).map(async ([path, images]) => {{
                try {{
                    const checkUrl = new URL(path, location.origin);
                    checkUrl.searchParams.set('check', Date.now().toString());
                    const response = await fetch(checkUrl, {{ method: 'HEAD', cache: 'no-store' }});
                    if (response.ok && response.headers.get('x-logo-source') === 'cache') {{
                        const freshUrl = new URL(path, location.origin);
                        freshUrl.searchParams.set('refresh', Date.now().toString());
                        images.forEach(img => {{
                            img.dataset.logoPending = '0';
                            img.src = freshUrl.pathname + freshUrl.search;
                        }});
                    }}
                }} catch (_) {{
                    // 下一轮自动重试；不打断页面其它功能。
                }}
            }}));
            if (document.querySelector('img[data-logo-pending="1"]')) {{
                const delays = [3500, 8000, 16000, 30000];
                setTimeout(() => refreshPendingLogos(attempt + 1), delays[attempt]);
            }}
        }}
        function setTheme(theme) {{
            const valid = ['dark', 'light', 'sepia'];
            if (valid.indexOf(theme) === -1) theme = 'dark';
            document.documentElement.setAttribute('data-theme', theme);
            try {{ localStorage.setItem('ib-theme', theme); }} catch (e) {{}}
            document.querySelectorAll('.theme-btn').forEach(b => b.classList.toggle('active', b.dataset.theme === theme));
        }}
        (function() {{
            let saved = 'dark';
            try {{ saved = localStorage.getItem('ib-theme') || 'dark'; }} catch (e) {{}}
            setTheme(saved);
        }})();
        applyRatingView();
        updateRelativeTime();
        setTimeout(() => refreshPendingLogos(), 2000);
        setInterval(updateRelativeTime, 60000);
        document.addEventListener('keydown', e => {{
            if (document.getElementById('refreshBtnFast') && (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'r') {{
                e.preventDefault();
                refreshData('fast');
            }}
        }});
    </script>
</body>
</html>"""
    return html


@app.route("/api/ib-research")
def api_ib_research():
    """JSON API: 返回最新研报总结"""
    if _rate_limited("api", 30):
        return jsonify({"status": "error", "message": "请求过于频繁"}), 429
    try:
        report = _get_report_cached()
        return jsonify({
            "status": "ok",
            "data": report,
        })
    except Exception as e:
        print(f"[api_ib_research] {e!r}", file=sys.stderr)
        return jsonify({"status": "error", "message": "内部错误"}), 500


@app.route("/api/ib-research/raw")
def api_ib_research_raw():
    """JSON API: 返回原始数据"""
    if _rate_limited("api", 30):
        return jsonify({"status": "error", "message": "请求过于频繁"}), 429
    try:
        report = _get_report_cached()
        return jsonify({
            "status": "ok",
            "data": report.get("raw", {}),
        })
    except Exception as e:
        print(f"[api_ib_research_raw] {e!r}", file=sys.stderr)
        return jsonify({"status": "error", "message": "内部错误"}), 500


@app.route("/api/ib-research/refresh", methods=["POST"])
def api_refresh():
    """手动触发数据刷新：mode=fast 仅刷新 RSS，mode=full 完整抓取"""
    if not _is_refresh_authorized_request():
        return jsonify({"status": "forbidden", "message": "手动刷新仅限已授权请求"}), 403

    if _refresh_lock.locked():
        return jsonify({"status": "busy", "message": "刷新正在进行中"}), 429

    with _refresh_lock:
        try:
            mode = request.args.get("mode", "full")
            fast = mode == "fast"
            # 快速刷新：采集数据并按固定 4 小时节奏刷新分析（不再按评级条数门槛拦截）。
            report = run_fetch(force=not fast, fast=fast)
            report_meta = report.get("meta", {})
            if report_meta.get("refresh_skipped") == "busy":
                return jsonify({"status": "busy", "message": "完整刷新正在运行"}), 429
            analysis_status = report_meta.get("analysis_status", "degraded")
            quality_status = report_meta.get("quality_status", "degraded")
            if fast and quality_status == "ok":
                pending_count = report_meta.get("pending_rating_count", 0)
                if analysis_status == "degraded":
                    pending_note = (
                        f"，{pending_count} 条待处理评级将于下次采集分析"
                        if pending_count
                        else "，将在下个 4 小时周期重试"
                    )
                    message = (
                        "数据采集完成，已保留最近一次可用摘要"
                        + pending_note
                    )
                elif analysis_status == "pending":
                    message = (
                        f"数据已更新，{pending_count} 条待处理评级将于下次采集分析"
                    )
                else:
                    message = "数据采集完成，分析已按 4 小时节奏刷新"
                return jsonify({
                    "status": "ok",
                    "message": message,
                    "mode": "fast",
                    "generated_at": report_meta.get("generated_at"),
                    "pending_rating_count": pending_count,
                })
            if analysis_status != "ok" or quality_status != "ok":
                return jsonify({
                    "status": analysis_status if analysis_status != "ok" else "degraded",
                    "message": "原始数据已更新，但本次未通过分析或数据质量检查",
                    "mode": "fast" if fast else "full",
                    "generated_at": report_meta.get("generated_at"),
                }), 503
            return jsonify({
                "status": "ok",
                "message": "刷新完成",
                "mode": "fast" if fast else "full",
                "generated_at": report.get("meta", {}).get("generated_at"),
            })
        except Exception as e:
            print(f"[api_refresh] {e!r}", file=sys.stderr)
            return jsonify({"status": "error", "message": "刷新执行失败，详情见服务端日志"}), 500


@app.route("/")
def index():
    """HTML报告页面"""
    if _rate_limited("index", 60):
        return Response("too many requests", status=429, mimetype="text/plain")
    try:
        html, _report = _get_html_cached(can_refresh=_is_owner_request())
        response = Response(html, mimetype="text/html")
        if request.headers.get("CF-Connecting-IP"):
            response.headers["Cache-Control"] = (
                "public, max-age=60, s-maxage=300, stale-while-revalidate=600"
            )
        else:
            response.headers["Cache-Control"] = "no-store"
        response.set_etag(hashlib.sha256(html.encode("utf-8")).hexdigest())
        return response.make_conditional(request)
    except Exception as e:
        print(f"[index] {e!r}", file=sys.stderr)
        return "<h1>Error</h1><p>服务内部错误，详情见服务端日志</p>", 500


register_geo_routes(app)


if __name__ == "__main__":
    _load_env()
    _bind_host = os.getenv("IB_RESEARCH_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    # 不对外泄露 Werkzeug/Python 版本指纹
    from werkzeug.serving import WSGIRequestHandler
    WSGIRequestHandler.server_version = "ib-research"
    WSGIRequestHandler.sys_version = ""
    print("=" * 60)
    print("外资投行研报总结服务")
    print("=" * 60)
    print("端点:")
    print("  http://localhost:8081/                -> HTML报告")
    print("  http://localhost:8081/api/ib-research -> JSON API")
    print("  POST http://localhost:8081/api/ib-research/refresh -> 手动刷新(需令牌)")
    print(f"  绑定地址: {_bind_host}:8081 (IB_RESEARCH_BIND_HOST 可覆盖)")
    print("=" * 60)
    app.run(host=_bind_host, port=8081, debug=False)
