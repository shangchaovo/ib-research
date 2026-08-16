#!/usr/bin/env python3
"""Shared .env loading and runtime settings for the standalone repo.

The original tree defaulted HTTP to a private local proxy and disabled TLS
verification. This module keeps those as explicit opt-in so a fresh clone
works on a normal network.
"""
from __future__ import annotations

import os
from typing import Optional

_FALSEY = {"0", "false", "no", "off"}


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_dotenv(path: Optional[str] = None) -> str:
    """Load KEY=VALUE pairs into os.environ without overwriting existing keys.

    Supports optional `export ` prefixes and quoted values. Returns the path
    that was read, or an empty string if no file existed.
    """
    env_path = path or os.environ.get(
        "IB_RESEARCH_ENV",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    )
    if not env_path or not os.path.isfile(env_path):
        return ""
    with open(env_path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            os.environ.setdefault(key, _strip_quotes(value))
    return env_path


def get_proxy() -> str:
    """Return the configured HTTP(S) proxy, or empty string if none is set."""
    return (
        os.environ.get("HTTPS_PROXY", "").strip()
        or os.environ.get("https_proxy", "").strip()
        or os.environ.get("HTTP_PROXY", "").strip()
        or os.environ.get("http_proxy", "").strip()
    )


def ssl_verify() -> bool:
    """TLS verification is on unless IB_RESEARCH_SSL_VERIFY is explicitly off."""
    raw = os.environ.get("IB_RESEARCH_SSL_VERIFY", "1").strip().lower()
    return raw not in _FALSEY


def request_proxy_modes(proxy: Optional[str] = None) -> list[tuple[Optional[dict], str]]:
    """Build (proxies, mode) pairs: optional proxy first, then direct."""
    resolved = get_proxy() if proxy is None else (proxy or "").strip()
    modes: list[tuple[Optional[dict], str]] = []
    if resolved:
        modes.append(({"http": resolved, "https": resolved}, "proxy"))
    modes.append((None, "direct"))
    return modes
