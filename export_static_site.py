#!/usr/bin/env python3
"""Render the read-only FResearch Flask site into Cloudflare-ready static assets."""

from __future__ import annotations

import argparse
import copy
import html
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = PROJECT_DIR / "cloudflare_dist"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
LOGO_RE = re.compile(
    r'''(?:src|href)=["'](/assets/logos/[^"'?#]+\.svg)(?:\?[^"']*)?["']''',
    re.I,
)


def _write(root: Path, relative: str, body: bytes | str) -> Path:
    target = root / relative.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, str):
        target.write_text(body, encoding="utf-8")
    else:
        target.write_bytes(body)
    return target


def _page_target(path: str) -> str:
    if path == "/":
        return "index.html"
    clean = path.strip("/")
    return f"{clean}/index.html"


def _headers_file(*, noindex_paths: set[str] | None = None, revalidate_logos: bool = False) -> str:
    headers = """/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  X-Frame-Options: DENY
  Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'

/
  Content-Type: text/html; charset=utf-8
  Cache-Control: public, max-age=120, must-revalidate
  X-Robots-Tag: index, follow, max-image-preview:large, max-snippet:-1

/stocks/*
  Content-Type: text/html; charset=utf-8

/topics/*
  Content-Type: text/html; charset=utf-8

/institutions/*
  Content-Type: text/html; charset=utf-8

/reactions/
  Content-Type: text/html; charset=utf-8

/events/*
  Content-Type: text/html; charset=utf-8
  Cache-Control: public, max-age=120, must-revalidate

/authors/*
  Content-Type: text/html; charset=utf-8

/about/
  Content-Type: text/html; charset=utf-8

/methodology/
  Content-Type: text/html; charset=utf-8

/ai-methodology/
  Content-Type: text/html; charset=utf-8

/sources/
  Content-Type: text/html; charset=utf-8

/editorial-policy/
  Content-Type: text/html; charset=utf-8

/disclosures/
  Content-Type: text/html; charset=utf-8

/ai-info/
  Content-Type: text/html; charset=utf-8

/api/*
  Cache-Control: public, max-age=60, must-revalidate
  X-Robots-Tag: noindex, nofollow

/api/ib-research
  Content-Type: application/json; charset=utf-8

/api/ib-research.json
  Content-Type: application/json; charset=utf-8

/data/events.json
  Content-Type: application/json; charset=utf-8
  Cache-Control: public, max-age=120, must-revalidate
  X-Robots-Tag: noindex, follow

/feed.xml
  Content-Type: application/rss+xml; charset=utf-8
  Cache-Control: public, max-age=120, must-revalidate
  X-Robots-Tag: noindex, follow

/sitemap.xml
  Content-Type: application/xml; charset=utf-8
  Cache-Control: public, max-age=300, must-revalidate

/robots.txt
  Content-Type: text/plain; charset=utf-8
  Cache-Control: public, max-age=600, must-revalidate

/llms.txt
  Content-Type: text/plain; charset=utf-8
  Cache-Control: public, max-age=600, must-revalidate

/assets/logos/*
  Content-Security-Policy: script-src 'none'
  Cache-Control: public, max-age=86400, must-revalidate

/favicon.svg
  Cache-Control: public, max-age=31536000, immutable

/favicon.ico
  Cache-Control: public, max-age=31536000, immutable

/apple-touch-icon.png
  Cache-Control: public, max-age=31536000, immutable

/og-image.png
  Cache-Control: public, max-age=3600, must-revalidate

/404.html
  X-Robots-Tag: noindex
"""
    if revalidate_logos:
        # A single rule keeps placeholders refreshable without spending one
        # platform header rule per ticker. Existing cached logos retain ETags.
        headers = headers.replace(
            "  Content-Security-Policy: script-src 'none'\n  Cache-Control: public, max-age=86400, must-revalidate",
            "  Content-Security-Policy: script-src 'none'\n  Cache-Control: no-cache, max-age=0, must-revalidate",
        )
    for path in sorted(noindex_paths or set()):
        headers += f"\n{path}\n  X-Robots-Tag: noindex, follow\n"
    return headers


def _redirects_file() -> str:
    return """# Static JSON API compatibility
/api/ib-research /api/ib-research.json 200

# The first events page has one canonical URL
/events/page/1/ /events/ 308

# Legacy routes preserved from the Flask application
/learn/ /methodology/ 308
/learn/price-target/ /topics/price-targets/ 308
/learn/analyst-rating/ /topics/price-targets/ 308
/learn/earnings-revision/ /topics/price-targets/ 308
/learn/* /methodology/ 308
/compare/ /stocks/ 308
/compare/* /stocks/ 308
/topics/ai/ /topics/institution-views/ 308
/topics/semiconductors/ /topics/price-targets/ 308
/topics/hbm/ /topics/market-reaction/ 308
/topics/ai-infrastructure/ /topics/institution-signals/ 308
"""


def _has_usable_report(report: dict) -> bool:
    """Do not replace a live publication with the fetcher's empty-cache fallback."""
    if not isinstance(report, dict):
        return False
    meta = report.get("meta")
    if not isinstance(meta, dict) or not str(meta.get("generated_at") or "").strip():
        return False
    summary = report.get("summary")
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except (TypeError, json.JSONDecodeError):
            return False
    if not isinstance(summary, dict):
        return False
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    has_facts = bool(
        summary.get("Rating_Changes") or summary.get("Asset_Targets") or raw.get("news")
    )
    has_analysis = bool(str(summary.get("Core_Thesis") or "").strip())
    if str(meta.get("quality_status") or "").lower() == "degraded" and not has_facts:
        return False
    return has_facts or has_analysis


def _clear_render_caches(server) -> None:
    # A build can run in a long-lived test/interpreter process. Never leave its
    # report, owner HTML, or request rate counters in that process's caches.
    with server._page_cache_lock:
        server._page_cache.clear()
        server._hist_pt_cache.clear()
    with server._rate_lock:
        server._rate_buckets.clear()


@contextmanager
def _publication_snapshot(server, report: dict, events: list[dict]):
    settings = {
        "PUBLICATION_REPORT": report,
        "PUBLICATION_EVENTS": events,
        "PUBLICATION_EVENT_VIEW": None,
        "PUBLICATION_DISABLE_LOGO_FETCH": True,
    }
    missing = object()
    previous = {key: server.app.config.get(key, missing) for key in settings}
    _clear_render_caches(server)
    server.app.config.update(settings)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is missing:
                server.app.config.pop(key, None)
            else:
                server.app.config[key] = value
        _clear_render_caches(server)


def _replace_snapshot(tmp_path: Path, output_dir: Path) -> None:
    """Keep the previous complete directory available for rollback until commit."""
    backup_path = None
    if output_dir.exists():
        backup_path = Path(tempfile.mkdtemp(prefix=".cloudflare-previous-", dir=output_dir.parent))
        backup_path.rmdir()
        output_dir.replace(backup_path)
    try:
        tmp_path.replace(output_dir)
    except Exception:
        if backup_path is not None:
            backup_path.replace(output_dir)
        raise
    if backup_path is not None:
        shutil.rmtree(backup_path, ignore_errors=True)


def build_static_site(output_dir: Path = DEFAULT_OUTPUT, *, allow_empty: bool = False) -> dict:
    """Render one read-only snapshot; retain old output if validation or swap fails.

    ``allow_empty`` is an explicit fixture/diagnostic option. Production publishing
    keeps the default so a missing report cannot replace an existing live site.
    """
    output_dir = output_dir.resolve()
    if output_dir == PROJECT_DIR or output_dir == Path.home() or output_dir == Path("/"):
        raise ValueError(f"refusing unsafe output directory: {output_dir}")
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError(f"output is not a directory: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    # Imports happen after callers have set IB_RESEARCH_CACHE_DIR/IB_RESEARCH_ENV.
    from ib_research_fetcher import CACHE_DIR, HOT_SYMBOLS, get_latest_report
    from ib_research_geo import RESEARCH_LENS_PAGES, TRUST_PAGES, public_origin
    import ib_research_server as server
    from ib_research_source_pages import event_paths
    from ib_research_sources import sync_public_archive
    from institution_intelligence import TRACKED_INSTITUTIONS

    # Importing the fetcher above loads the configured env file. Resolve origin
    # afterwards, and use exactly this report for every page, feed and JSON file.
    origin = public_origin()
    report = copy.deepcopy(get_latest_report())
    if not allow_empty and not _has_usable_report(report):
        raise RuntimeError("refusing to publish an empty or unavailable report; keep the previous snapshot")
    events = sync_public_archive(report, CACHE_DIR)

    page_paths = {
        "/",
        "/stocks/",
        "/topics/",
        "/institutions/",
        "/reactions/",
        "/authors/",
        "/authors/xie-shangchao/",
    }
    page_paths.update(f"/stocks/{symbol.lower()}/" for symbol in HOT_SYMBOLS)
    page_paths.update(f"/topics/{slug}/" for slug in RESEARCH_LENS_PAGES)
    page_paths.update(f"/institutions/{slug}/" for slug in TRACKED_INSTITUTIONS)
    page_paths.update(f"/{slug}/" for slug in TRUST_PAGES)
    page_paths.update(event_paths(events))

    tmp_path = Path(tempfile.mkdtemp(prefix=".cloudflare-dist-", dir=output_dir.parent))
    logo_paths: set[str] = set()
    noindex_paths: set[str] = set()
    fallback_logo_present = False
    try:
        with _publication_snapshot(server, report, events), server.app.test_client() as client:
            public_headers = {"CF-Connecting-IP": "203.0.113.10"}
            for path in sorted(page_paths):
                response = client.get(path, base_url=origin, headers=public_headers)
                if response.status_code != 200:
                    raise RuntimeError(f"static export failed: {path} returned {response.status_code}")
                page = response.get_data(as_text=True)
                if "noindex" in response.headers.get("X-Robots-Tag", "").lower():
                    noindex_paths.add(path)
                logo_paths.update(html.unescape(match) for match in LOGO_RE.findall(page))
                _write(tmp_path, _page_target(path), page)

            for path in ("/robots.txt", "/sitemap.xml", "/llms.txt", "/feed.xml", "/data/events.json"):
                response = client.get(path, base_url=origin, headers=public_headers)
                if response.status_code != 200:
                    raise RuntimeError(f"static export failed: {path} returned {response.status_code}")
                _write(tmp_path, path, response.data)

            for path in ("/favicon.svg", "/favicon.ico", "/apple-touch-icon.png", "/og-image.png"):
                response = client.get(path, base_url=origin, headers=public_headers)
                if response.status_code != 200:
                    raise RuntimeError(f"static export failed: {path} returned {response.status_code}")
                _write(tmp_path, path, response.data)

            for path in sorted(logo_paths):
                safe_path = urlsplit(path).path
                if not safe_path.startswith("/assets/logos/") or ".." in safe_path:
                    raise RuntimeError(f"unsafe logo path in rendered HTML: {path}")
                response = client.get(safe_path, base_url=origin, headers=public_headers)
                if response.status_code != 200:
                    raise RuntimeError(f"static export failed: {safe_path} returned {response.status_code}")
                fallback_logo_present = fallback_logo_present or response.headers.get("X-Logo-Source") == "fallback"
                _write(tmp_path, safe_path, response.data)

            api_response = client.get("/api/ib-research", base_url=origin, headers=public_headers)
            if api_response.status_code != 200:
                raise RuntimeError("static API export failed")
            _write(tmp_path, "/api/ib-research.json", api_response.data)

            not_found = client.get("/__fresearch_missing__/", base_url=origin, headers=public_headers)
            if not_found.status_code != 404:
                raise RuntimeError("static 404 export failed: unknown path is not a real 404")
            _write(tmp_path, "404.html", not_found.data)

        refresh_token = os.environ.get("IB_RESEARCH_REFRESH_TOKEN", "").strip()
        if refresh_token:
            token_bytes = refresh_token.encode("utf-8")
            leaked_files = [
                str(path.relative_to(tmp_path))
                for path in tmp_path.rglob("*")
                if path.is_file() and token_bytes in path.read_bytes()
            ]
            if leaked_files:
                raise RuntimeError(
                    "refusing to publish: refresh token leaked into "
                    + ", ".join(leaked_files)
                )

        _write(tmp_path, "_headers", _headers_file(
            noindex_paths=noindex_paths, revalidate_logos=fallback_logo_present,
        ))
        _write(tmp_path, "_redirects", _redirects_file())
        manifest = {
            "built_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
            "origin": origin,
            "report_generated_at": (report.get("meta") or {}).get("generated_at", ""),
            "pages": len(page_paths),
            "events": len(events),
            "logos": len(logo_paths),
            # Include build-info.json itself in the reported snapshot size.
            "files": sum(1 for path in tmp_path.rglob("*") if path.is_file()) + 1,
        }
        _write(tmp_path, "build-info.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        _replace_snapshot(tmp_path, output_dir)
        return manifest
    except Exception:
        shutil.rmtree(tmp_path, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 FResearch Cloudflare 静态站点")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--allow-empty", action="store_true", help="仅供测试/诊断：允许空缓存导出")
    args = parser.parse_args()
    manifest = build_static_site(args.output, allow_empty=args.allow_empty)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
