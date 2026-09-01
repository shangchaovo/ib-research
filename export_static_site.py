#!/usr/bin/env python3
"""Render the read-only FResearch Flask site into Cloudflare-ready static assets."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = PROJECT_DIR / "cloudflare_dist"
PUBLIC_ORIGIN = os.environ.get("IB_RESEARCH_PUBLIC_ORIGIN", "https://fresearch.cc.cd").rstrip("/")
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


def _headers_file() -> str:
    return """/*
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

/assets/*
  Cache-Control: public, max-age=86400, must-revalidate

/favicon.svg
  Cache-Control: public, max-age=31536000, immutable

/favicon.ico
  Cache-Control: public, max-age=31536000, immutable

/apple-touch-icon.png
  Cache-Control: public, max-age=31536000, immutable

/og-image.png
  Cache-Control: public, max-age=31536000, immutable
"""


def _redirects_file() -> str:
    return """# Static JSON API compatibility
/api/ib-research /api/ib-research.json 200
/api/ib-research/raw /api/ib-research-raw.json 200

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


def build_static_site(output_dir: Path = DEFAULT_OUTPUT) -> dict:
    """Build into a temporary directory, then atomically replace the prior snapshot."""
    output_dir = output_dir.resolve()
    if output_dir == PROJECT_DIR or output_dir == Path.home() or output_dir == Path("/"):
        raise ValueError(f"refusing unsafe output directory: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    # Imports happen after callers have set IB_RESEARCH_CACHE_DIR/IB_RESEARCH_ENV.
    from ib_research_fetcher import HOT_SYMBOLS
    from ib_research_geo import TOPIC_PAGES, TRUST_PAGES
    from ib_research_server import app
    from institution_intelligence import TRACKED_INSTITUTIONS

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
    legacy_topic_slugs = {"ai", "semiconductors", "hbm", "ai-infrastructure"}
    page_paths.update(
        f"/topics/{slug}/" for slug in TOPIC_PAGES if slug not in legacy_topic_slugs
    )
    page_paths.update(f"/institutions/{slug}/" for slug in TRACKED_INSTITUTIONS)
    page_paths.update(f"/{slug}/" for slug in TRUST_PAGES)

    tmp_path = Path(tempfile.mkdtemp(prefix=".cloudflare-dist-", dir=output_dir.parent))
    logo_paths: set[str] = set()
    try:
        with app.test_client() as client:
            public_headers = {"CF-Connecting-IP": "203.0.113.10"}
            for path in sorted(page_paths):
                response = client.get(path, base_url=PUBLIC_ORIGIN, headers=public_headers)
                if response.status_code != 200:
                    raise RuntimeError(f"static export failed: {path} returned {response.status_code}")
                page = response.get_data(as_text=True)
                logo_paths.update(html.unescape(match) for match in LOGO_RE.findall(page))
                _write(tmp_path, _page_target(path), page)

            for path in ("/robots.txt", "/sitemap.xml", "/llms.txt"):
                response = client.get(path, base_url=PUBLIC_ORIGIN, headers=public_headers)
                if response.status_code != 200:
                    raise RuntimeError(f"static export failed: {path} returned {response.status_code}")
                _write(tmp_path, path, response.data)

            for path in ("/favicon.svg", "/favicon.ico", "/apple-touch-icon.png", "/og-image.png"):
                response = client.get(path, base_url=PUBLIC_ORIGIN, headers=public_headers)
                if response.status_code != 200:
                    raise RuntimeError(f"static export failed: {path} returned {response.status_code}")
                _write(tmp_path, path, response.data)

            for path in sorted(logo_paths):
                safe_path = urlsplit(path).path
                if not safe_path.startswith("/assets/logos/") or ".." in safe_path:
                    raise RuntimeError(f"unsafe logo path in rendered HTML: {path}")
                response = client.get(safe_path, base_url=PUBLIC_ORIGIN, headers=public_headers)
                if response.status_code != 200:
                    raise RuntimeError(f"static export failed: {safe_path} returned {response.status_code}")
                _write(tmp_path, safe_path, response.data)

            api_response = client.get("/api/ib-research", base_url=PUBLIC_ORIGIN, headers=public_headers)
            raw_response = client.get("/api/ib-research/raw", base_url=PUBLIC_ORIGIN, headers=public_headers)
            if api_response.status_code != 200 or raw_response.status_code != 200:
                raise RuntimeError("static API export failed")
            _write(tmp_path, "/api/ib-research.json", api_response.data)
            _write(tmp_path, "/api/ib-research-raw.json", raw_response.data)

            not_found = client.get("/__fresearch_missing__", base_url=PUBLIC_ORIGIN, headers=public_headers)
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

        _write(tmp_path, "_headers", _headers_file())
        _write(tmp_path, "_redirects", _redirects_file())
        manifest = {
            "built_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
            "origin": PUBLIC_ORIGIN,
            "pages": len(page_paths),
            "logos": len(logo_paths),
            # Include build-info.json itself in the reported snapshot size.
            "files": sum(1 for path in tmp_path.rglob("*") if path.is_file()) + 1,
        }
        _write(tmp_path, "build-info.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if output_dir.exists():
            shutil.rmtree(output_dir)
        tmp_path.replace(output_dir)
        return manifest
    except Exception:
        shutil.rmtree(tmp_path, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 FResearch Cloudflare 静态站点")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    manifest = build_static_site(args.output)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
