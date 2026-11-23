"""Public, attributable event records and their durable publication archive.

This module never fetches sources.  It only projects the local report's explicit
fields; in particular a headline extractor is not a claim of editorial or
institutional verification.  GET handlers use ``get_public_events`` (read only),
while the report writer/exporter explicitly calls ``sync_public_archive``.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from institution_intelligence import ACTION_LABELS, canonical_institution


LOGGER = logging.getLogger(__name__)
ARCHIVE_FILENAME = "public_events_archive.json"
ARCHIVE_VERSION = 1
PUBLIC_SOURCE_TYPES = frozenset({"verified_headline", "verified_summary"})
_SOURCE_LABELS = {
    "verified_headline": "公开新闻标题规则抽取",
    "verified_summary": "公开新闻摘要规则抽取",
    "av_consensus": "Alpha Vantage 分析师一致预期",
    "yahoo": "Yahoo 结构化记录（未保存单条原文链接）",
    "yahoo_structured": "Yahoo 结构化记录（未保存单条原文链接）",
    "kr_headline": "韩文新闻聚合（调价方向含推断）",
    "llm_inferred": "模型整理或推断（未经独立核验）",
    "unattributed": "来源未标明（未经独立核验）",
}
_QUALITY_FLAGS = frozenset({
    "inferred_event_date", "missing_new_rating", "pt_backfilled",
    "old_rating_backfilled", "new_rating_inferred", "yahoo_backfilled",
})
_STAMP_FIELDS = ("first_seen_at", "last_seen_at", "published_at", "modified_at")
_CONTENT_FIELDS = (
    "id", "symbol", "bank", "action", "action_label", "event_date", "date_basis",
    "old_rating", "new_rating", "price_target", "previous_price_target",
    "source_type", "source_label", "source_title", "source_url", "quality_flags",
)
_BANK_ALIASES = {
    "高盛": "Goldman Sachs", "摩根士丹利": "Morgan Stanley",
    "摩根大通": "JPMorgan", "美银证券": "Bank of America",
    "美国银行": "Bank of America", "花旗": "Citi", "citigroup": "Citi",
    "瑞银": "UBS", "德意志银行": "Deutsche Bank", "巴克莱": "Barclays",
    "富国银行": "Wells Fargo", "汇丰": "HSBC", "野村": "Nomura",
    "杰富瑞": "Jefferies", "伯恩斯坦": "Bernstein", "派珀桑德勒": "Piper Sandler",
    "瑞穗": "Mizuho", "瑞穗证券": "Mizuho", "bofa securities": "Bank of America",
    "bank of america securities": "Bank of America", "jpmorgan chase": "JPMorgan",
    "jpmorgan chase & co.": "JPMorgan", "jpmorgan chase & co": "JPMorgan",
    "sanford c. bernstein": "Bernstein", "sanford bernstein": "Bernstein",
    "roth capital partners": "Roth Capital", "roth mkm": "Roth Capital",
    "truist securities": "Truist", "rbc capital": "RBC", "rbc capital markets": "RBC",
    "cantor fitzgerald": "Cantor", "cantor fitzgerald & co.": "Cantor",
    "wells fargo securities": "Wells Fargo", "piper sandler & co.": "Piper Sandler",
    "raymond james financial": "Raymond James", "goldman sachs group": "Goldman Sachs",
    "morgan stanley & co.": "Morgan Stanley", "ubs group": "UBS",
    "ubs securities": "UBS", "barclays capital": "Barclays",
    "deutsche bank ag": "Deutsche Bank", "hsbc holdings": "HSBC",
    "hsbc securities": "HSBC", "jefferies llc": "Jefferies",
    "jefferies & company": "Jefferies",
}
_SECRET_QUERY_KEYS = frozenset({
    "token", "access_token", "refresh_token", "api_key", "apikey", "api-key",
    "authorization", "auth", "password", "passwd", "secret", "client_secret",
    "signature", "sig", "x-amz-signature", "x-goog-signature",
})


def _text(value: Any, limit: int = 2000) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or value.casefold() in {"null", "none", "n/a", "nan"}:
        return ""
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        return ""
    return value[:limit]


def source_label(source: Any) -> str:
    """Describe the method honestly, including legacy or unlabelled records."""
    return _SOURCE_LABELS.get(_text(source, 80), _SOURCE_LABELS["unattributed"])


def safe_source_url(value: Any) -> str:
    """Accept an external HTTP(S) citation without credentials or secret params.

    No DNS lookup or request is made.  This is a link validation boundary, not an
    SSRF authorization for fetching the resulting URL.
    """
    if not isinstance(value, str) or not value or len(value) > 4096:
        return ""
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        return ""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        _ = parsed.port  # Reject malformed/out-of-range ports.
        host = hostname.rstrip(".").casefold()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".invalid")):
            return ""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            ascii_host = host.encode("idna").decode("ascii")
            if "." not in ascii_host or re.fullmatch(r"[\d.]+", ascii_host):
                return ""
            if len(ascii_host) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in ascii_host.split(".")
            ):
                return ""
        else:
            if not address.is_global:
                return ""
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.casefold() in _SECRET_QUERY_KEYS:
                return ""
    except (ValueError, UnicodeError):
        return ""
    return value


def _day(value: Any) -> str:
    value = _text(value, 80)
    match = re.match(r"^(\d{4})[-/]?(\d{2})[-/]?(\d{2})(?:$|[T\s])", value)
    if not match:
        return ""
    try:
        return datetime.strptime("".join(match.groups()), "%Y%m%d").date().isoformat()
    except ValueError:
        return ""


def _bank(value: Any) -> str:
    text = " ".join(_text(value, 160).split())
    if not text or not re.fullmatch(r"[\w\s&.,'’()/\-]+", text):
        return ""
    text = _BANK_ALIASES.get(text.casefold(), text)
    return canonical_institution(text) or text


def _symbol(value: Any) -> str:
    value = _text(value, 32).upper()
    return value if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-^]{0,23}", value) else ""


def _identity(change: Any) -> tuple[str, str, str, str] | None:
    if not isinstance(change, dict):
        return None
    bank = _bank(change.get("Bank"))
    symbol = _symbol(change.get("Symbol"))
    action = _text(change.get("Action"), 60).lower()
    day = _day(change.get("Time")) or _day(change.get("_First_Seen"))
    if not bank or not symbol or action not in ACTION_LABELS or not day:
        return None
    return bank, symbol, action, day


def event_id(change: Any) -> str:
    """Stable identity for the current same-bank/symbol/action/day convention.

    Mutable ratings, targets, headlines and source URLs are deliberately absent.
    Correcting an identity field is an explicit new identity, never a fuzzy merge.
    """
    key = _identity(change)
    if key is None:
        return ""
    canonical = (key[0].casefold(), *key[1:])
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _summary(report: Any) -> dict:
    value = report.get("summary") if isinstance(report, dict) else None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def _quality_flags(change: dict) -> list[str]:
    raw = change.get("_Quality_Flags")
    flags = {flag for flag in raw if isinstance(flag, str) and flag in _QUALITY_FLAGS} if isinstance(raw, list) else set()
    if change.get("_Time_Inferred") is True or not _day(change.get("Time")):
        flags.add("inferred_event_date")
    return sorted(flags)


def _project_change(change: Any, source_urls: dict[str, str]) -> dict | None:
    identity = _identity(change)
    if identity is None:
        return None
    source = _text(change.get("_Source"), 80)
    title = _text(change.get("_Headline"), 2000)
    if source not in PUBLIC_SOURCE_TYPES or not title:
        return None
    url = safe_source_url(change.get("_Source_URL")) or source_urls.get(title, "")
    if not url:
        return None
    bank, symbol, action, day = identity
    new_rating = _text(change.get("New_Rating"), 160) or None
    if action in {"upgrade", "downgrade"} and not new_rating:
        return None
    flags = _quality_flags(change)
    return {
        "id": event_id(change),
        "symbol": symbol,
        "bank": bank,
        "action": action,
        "action_label": ACTION_LABELS[action],
        "event_date": day,
        "date_basis": "first_seen" if "inferred_event_date" in flags else "reported_date_unconfirmed",
        "old_rating": _text(change.get("Old_Rating"), 160) or None,
        "new_rating": new_rating,
        "price_target": _text(change.get("Price_Target"), 256) or None,
        "previous_price_target": _text(change.get("_Old_Price_Target"), 256) or None,
        "source_type": source,
        "source_label": source_label(source),
        "source_title": title,
        "source_url": url,
        "quality_flags": flags,
    }


def _sort_events(events: list[dict]) -> list[dict]:
    return sorted(events, key=lambda event: (event["event_date"], event["bank"].casefold(), event["symbol"], event["id"]), reverse=True)


def _merge_content(older: dict, newer: dict) -> dict:
    """Keep useful archived fields if an observation is incomplete."""
    merged = {key: older.get(key) for key in _CONTENT_FIELDS}
    for key in _CONTENT_FIELDS:
        value = newer.get(key)
        if value is not None and value != "":
            merged[key] = value
    # Flags describe possible derivations.  An incomplete later observation must
    # not silently remove an earlier disclosure of inference/backfilling.
    merged["quality_flags"] = sorted(set(older.get("quality_flags", [])) | set(newer.get("quality_flags", [])))
    if "inferred_event_date" in merged["quality_flags"]:
        merged["date_basis"] = "first_seen"
    return merged


def project_events(report: Any) -> list[dict]:
    """Whitelisted, attributable current events; no I/O and no made-up timestamps."""
    source_urls: dict[str, str] = {}
    raw = report.get("raw", {}) if isinstance(report, dict) else {}
    news = raw.get("news", []) if isinstance(raw, dict) else []
    for item in news if isinstance(news, list) else []:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"), 2000)
        url = safe_source_url(item.get("url"))
        if title and url:
            source_urls.setdefault(title, url)
    changes = _summary(report).get("Rating_Changes", [])
    by_id: dict[str, dict] = {}
    for change in changes if isinstance(changes, list) else []:
        event = _project_change(change, source_urls)
        if event is not None:
            by_id[event["id"]] = _merge_content(by_id[event["id"]], event) if event["id"] in by_id else event
    return _sort_events(list(by_id.values()))


def _utc_timestamp(value: Any = None) -> str:
    if value is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("publication timestamp must be datetime or ISO text")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _existing_stamp(value: Any) -> str:
    if not isinstance(value, str) or "T" not in value:
        return ""
    try:
        return _utc_timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return ""


def _stored_event(record: Any) -> dict | None:
    """Revalidate a stored event and reject unknown/internal fields on read."""
    if not isinstance(record, dict):
        return None
    raw = {
        "Bank": record.get("bank"), "Symbol": record.get("symbol"),
        "Action": record.get("action"), "Time": record.get("event_date"),
        "Old_Rating": record.get("old_rating"), "New_Rating": record.get("new_rating"),
        "Price_Target": record.get("price_target"), "_Old_Price_Target": record.get("previous_price_target"),
        "_Source": record.get("source_type"), "_Headline": record.get("source_title"),
        "_Source_URL": record.get("source_url"), "_Quality_Flags": record.get("quality_flags"),
        "_Time_Inferred": record.get("date_basis") == "first_seen",
    }
    clean = _project_change(raw, {})
    if clean is None or record.get("id") != clean["id"]:
        return None
    for key in _STAMP_FIELDS:
        stamp = _existing_stamp(record.get(key))
        if stamp:
            clean[key] = stamp
    return clean


def _read_archive(cache_dir: Any) -> tuple[list[dict], bool]:
    path = Path(cache_dir) / ARCHIVE_FILENAME
    if not path.exists():
        return [], False
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or payload.get("schema_version") != ARCHIVE_VERSION or not isinstance(payload.get("events"), list):
        raise ValueError("public event archive has an unsupported or invalid structure")
    events: dict[str, dict] = {}
    for record in payload["events"]:
        clean = _stored_event(record)
        if clean is None:
            # Do not silently overwrite a partially corrupt archive on sync.
            raise ValueError("public event archive contains an invalid event")
        if clean["id"] in events:
            raise ValueError("public event archive contains duplicate event ids")
        events[clean["id"]] = clean
    return _sort_events(list(events.values())), payload.get("bootstrapped") is True


def load_public_archive(cache_dir: Any) -> list[dict]:
    """Read an archive; invalid files raise so callers can report degraded state."""
    if cache_dir is None:
        return []
    return _read_archive(cache_dir)[0]


@contextmanager
def _archive_lock(cache_dir: Path):
    cache_dir.mkdir(parents=True, exist_ok=True)
    with (cache_dir / ".public_events_archive.lock").open("a+", encoding="ascii") as handle:
        deadline = time.monotonic() + 5.0
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("public event archive is busy")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_archive_write(path: Path, payload: dict) -> None:
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".public-events-", suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def sync_public_archive(report: Any, cache_dir: Any, now: Any = None) -> list[dict]:
    """Publish/update attributable events without expiring older permalinks.

    The initial bootstrap processes existing daily reports one at a time.  A bad
    historical file is logged and keeps bootstrap retryable; a bad archive or a
    failed atomic write raises and leaves the previous archive untouched.
    """
    directory = Path(cache_dir)
    timestamp = _utc_timestamp(now)
    with _archive_lock(directory):
        existing, bootstrapped = _read_archive(directory)
        by_id = {event["id"]: event for event in existing}
        already_published = set(by_id)
        bootstrap_ok = True

        def observe(events: list[dict], *, historical: bool = False) -> None:
            for event in events:
                # Retrying a failed bootstrap must not let an old daily report
                # roll an already-published correction back to stale content.
                if historical and event["id"] in already_published:
                    continue
                old = by_id.get(event["id"])
                if old is None:
                    merged = dict(event)
                    merged.update({key: timestamp for key in _STAMP_FIELDS})
                else:
                    merged = _merge_content(old, event)
                    changed = any(merged.get(key) != old.get(key) for key in _CONTENT_FIELDS)
                    merged.update({key: old[key] for key in _STAMP_FIELDS if key in old})
                    merged.setdefault("first_seen_at", timestamp)
                    merged.setdefault("published_at", timestamp)
                    merged["last_seen_at"] = max(old.get("last_seen_at", timestamp), timestamp)
                    merged["modified_at"] = timestamp if changed else old.get("modified_at", old.get("published_at", timestamp))
                by_id[event["id"]] = merged

        if not bootstrapped:
            for path in sorted(directory.glob("report_*.json")):
                if not re.fullmatch(r"report_\d{4}-\d{2}-\d{2}\.json", path.name):
                    continue
                try:
                    with path.open("r", encoding="utf-8") as handle:
                        historical = json.load(handle)
                    if not isinstance(historical, dict):
                        raise ValueError("report root must be an object")
                    observe(project_events(historical), historical=True)
                except (OSError, ValueError, TypeError) as exc:
                    bootstrap_ok = False
                    LOGGER.warning("Skipped invalid historical report %s (%s); existing public records are retained", path.name, type(exc).__name__)
        observe(project_events(report))
        events = _sort_events(list(by_id.values()))
        payload = {"schema_version": ARCHIVE_VERSION, "bootstrapped": bootstrapped or bootstrap_ok, "events": events}
        _atomic_archive_write(directory / ARCHIVE_FILENAME, payload)
        return events


def get_public_events(report: Any, cache_dir: Any = None) -> list[dict]:
    """Read-only current + archived view; preserve archived timestamps/evidence."""
    try:
        archived = load_public_archive(cache_dir)
    except (OSError, ValueError, TypeError) as exc:
        LOGGER.warning("Public event archive is unavailable (%s); serving current attributable records", type(exc).__name__)
        archived = []
    by_id = {event["id"]: event for event in archived}
    for event in project_events(report):
        old = by_id.get(event["id"])
        if old is None:
            by_id[event["id"]] = event
        else:
            merged = _merge_content(old, event)
            for key in _STAMP_FIELDS:
                if key in old:
                    merged[key] = old[key]
            # Current content may be newer than the persisted publication.  Do
            # not claim an old modified time describes content not yet archived.
            if any(merged.get(key) != old.get(key) for key in _CONTENT_FIELDS):
                merged.pop("modified_at", None)
            by_id[event["id"]] = merged
    return _sort_events(list(by_id.values()))


def _strings(value: Any, limit: int = 50, text_limit: int = 2000) -> list[str]:
    if not isinstance(value, list):
        return []
    return [clean for item in value[:limit] if (clean := _text(item, text_limit))]


def _public_raw_change(change: Any) -> dict | None:
    identity = _identity(change)
    if identity is None:
        return None
    bank, symbol, action, day = identity
    source = _text(change.get("_Source"), 80)
    source = source if source in _SOURCE_LABELS else "unattributed"
    clean = {
        "Bank": bank, "Symbol": symbol, "Action": action, "Time": day,
        "Old_Rating": _text(change.get("Old_Rating"), 160) or None,
        "New_Rating": _text(change.get("New_Rating"), 160) or None,
        "Price_Target": _text(change.get("Price_Target"), 256) or None,
        "_Old_Price_Target": _text(change.get("_Old_Price_Target"), 256) or None,
        "_Source": source, "_Source_Label": source_label(source),
        "_Headline": _text(change.get("_Headline"), 2000),
        "_Source_URL": safe_source_url(change.get("_Source_URL")),
        "_Quality_Flags": _quality_flags(change),
    }
    if "inferred_event_date" in clean["_Quality_Flags"]:
        clean["_Time_Inferred"] = True
    return clean


def public_report(report: Any) -> dict:
    """Small compatibility API projection, never a recursive raw-report copy.

    Model prose remains explicitly model output.  Unknown nested objects,
    internal hashes/provider/run state, raw input and free-form errors are absent.
    """
    report = report if isinstance(report, dict) else {}
    raw_meta = report.get("meta")
    raw_meta = raw_meta if isinstance(raw_meta, dict) else {}
    meta: dict[str, Any] = {}
    for key in ("generated_at", "analysis_generated_at"):
        value = _text(raw_meta.get(key), 80)
        if value and _day(value):
            meta[key] = value
    for key in ("rating_window_days", "rating_event_count", "refresh_interval_minutes"):
        value = raw_meta.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            meta[key] = value
    allowed_data_sources = {"finnhub", "finnhub_recommendations", "public_news_rss", "alpha_vantage_consensus", "yahoo_finance_market_history", "reddit_public_posts"}
    meta["data_sources"] = [value for value in _strings(raw_meta.get("data_sources"), 30, 80) if value in allowed_data_sources]
    for key, choices in (("analysis_status", {"ok", "pending", "degraded"}), ("quality_status", {"ok", "degraded"})):
        if isinstance(raw_meta.get(key), str) and raw_meta[key] in choices:
            meta[key] = raw_meta[key]
    summary = _summary(report)
    public_summary: dict[str, Any] = {}
    metadata = summary.get("Metadata")
    if isinstance(metadata, dict):
        clean_metadata = {key: _text(metadata.get(key), 400) for key in ("Institution", "Date") if _text(metadata.get(key), 400)}
        clean_metadata["Primary_Assets_Covered"] = [symbol for value in _strings(metadata.get("Primary_Assets_Covered"), 100, 32) if (symbol := _symbol(value))]
        public_summary["Metadata"] = clean_metadata
    for key in ("Core_Thesis", "Statistical_Evidence"):
        value = _text(summary.get(key), 16000)
        if value:
            public_summary[key] = value
    public_summary["Tail_Risks"] = _strings(summary.get("Tail_Risks"), 50, 2000)
    changes = summary.get("Rating_Changes", [])
    public_summary["Rating_Changes"] = [clean for change in changes if (clean := _public_raw_change(change)) is not None] if isinstance(changes, list) else []
    targets = summary.get("Asset_Targets", [])
    public_targets = []
    for target in targets if isinstance(targets, list) else []:
        if not isinstance(target, dict) or not _symbol(target.get("Asset")):
            continue
        source = _text(target.get("_Source"), 80)
        source = source if source in _SOURCE_LABELS else "unattributed"
        clean = {"Asset": _symbol(target["Asset"]), "_Source": source, "_Source_Label": source_label(source)}
        for key in ("Target_Price", "Timeframe", "_Verified_PT", "_Verified_Bank"):
            value = _text(target.get(key), 400)
            if value:
                clean[key] = value
        clean["Technical_Levels"] = _strings(target.get("Technical_Levels"), 20, 256)
        public_targets.append(clean)
    public_summary["Asset_Targets"] = public_targets
    return {"meta": meta, "summary": public_summary, "publication_note": "公开字段投影；模型概述与推断不代表原始机构声明，来源级别及字段补全见记录标记。"}
