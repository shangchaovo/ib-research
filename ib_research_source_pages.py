#!/usr/bin/env python3
"""Read-only public event pages and feeds, backed by the publication archive.

The archive owns event identity and publication dates.  Request handlers never
archive, refresh, or collect data.  A static export can pin PUBLICATION_EVENTS
and PUBLICATION_REPORT on the Flask app for a consistent publication snapshot.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timezone
from email.utils import format_datetime
from html import escape
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from flask import Response, abort, current_app, g, redirect, request

from ib_research_fetcher import CACHE_DIR, get_latest_report
from ib_research_geo import SITE_NAME, public_origin, render_page
from ib_research_sources import get_public_events, safe_source_url


PAGE_SIZE = 50
FEED_SIZE = 50
_EVENT_ID = re.compile(r"[0-9a-f]{24}\Z")
PUBLIC_EVENT_FIELDS = (
    "id", "symbol", "bank", "action", "action_label", "event_date", "date_basis",
    "old_rating", "new_rating", "price_target", "previous_price_target",
    "source_type", "source_label", "source_title", "source_url", "quality_flags",
    "first_seen_at", "last_seen_at", "published_at", "modified_at",
)

_FLAG_LABELS = {
    "inferred_event_date": "日期来自首次发现，不能确认是事件发生日",
    "missing_new_rating": "来源未明确给出新评级",
    "missing_old_rating": "来源未明确给出原评级",
    "missing_source_url": "缺少可回查的来源链接",
    "pt_backfilled": "目标价由后续证据补全，不一定直接出现在当前来源标题中",
    "old_rating_backfilled": "原评级由更早的历史记录回填",
    "new_rating_inferred": "新评级含推断，尚未独立核验",
    "yahoo_backfilled": "部分字段由 Yahoo 结构化记录补全，未保留逐字段原文链接",
}


def _text(value: Any) -> str:
    return str(value) if value is not None else ""


def _html(value: Any) -> str:
    return escape(_text(value), quote=True)


def _absolute(path: str) -> str:
    return public_origin().rstrip("/") + path


def _iso_value(value: Any) -> str:
    """Validate a known date without inventing a fallback or timezone."""
    value = _text(value).strip()
    if not value:
        return ""
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).isoformat()
        if "T" not in value:
            return ""
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return ""


def _rss_date(value: Any) -> str:
    value = _iso_value(value)
    if not value or "T" not in value:
        return ""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return ""
    return format_datetime(parsed.astimezone(timezone.utc), usegmt=True)


def _public_event(event: dict) -> dict:
    """Copy a small, explicit publication schema; never serialize a raw report."""
    result = {}
    for field in PUBLIC_EVENT_FIELDS:
        value = event.get(field)
        if field == "quality_flags":
            result[field] = [item for item in value if isinstance(item, str)] if isinstance(value, (list, tuple)) else []
        elif field == "source_url":
            result[field] = safe_source_url(value)
        elif field in {"published_at", "modified_at"}:
            result[field] = _iso_value(value)
        elif isinstance(value, float) and not math.isfinite(value):
            result[field] = ""
        elif value is None or isinstance(value, (str, int, float, bool)):
            # Keep valid numeric data numeric for downstream consumers.
            result[field] = value if value is not None else ""
        else:
            result[field] = ""
    result["url"] = _absolute(f"/events/{result['id']}/")
    return result


def _normalized_events(events: Iterable[dict] | None) -> list[dict]:
    by_id = {}
    for event in events or []:
        if not isinstance(event, dict):
            continue
        event_id = _text(event.get("id"))
        if not _EVENT_ID.fullmatch(event_id):
            continue
        by_id.setdefault(event_id, _public_event(event))
    return sorted(
        by_id.values(),
        key=lambda item: (_text(item.get("event_date")), _text(item.get("first_seen_at")), item["id"]),
        reverse=True,
    )


def _event_view() -> dict:
    """Normalize a fixed publication once and reuse its constant-time ID map.

    PUBLICATION_EVENTS is an immutable-by-convention snapshot: callers replace
    the list to update it, rather than mutating its records in place.  Exporters
    must restore or clear PUBLICATION_EVENT_VIEW with the other snapshot keys.
    Ordinary requests cache only on Flask's request context, never across live
    report updates.
    """
    if "PUBLICATION_EVENTS" in current_app.config:
        raw_events = current_app.config["PUBLICATION_EVENTS"]
        cached = current_app.config.get("PUBLICATION_EVENT_VIEW")
        origin = public_origin()
        if isinstance(cached, dict) and cached.get("_input") is raw_events and cached.get("origin") == origin:
            return cached
        events = _normalized_events(raw_events)
        view = {
            "_input": raw_events, "origin": origin, "events": events,
            "by_id": {event["id"]: event for event in events},
            "lastmod": _latest_modified(events),
        }
        current_app.config["PUBLICATION_EVENT_VIEW"] = view
        return view
    cached = getattr(g, "_source_event_view", None)
    if cached is not None:
        return cached
    report = (
        current_app.config["PUBLICATION_REPORT"]
        if "PUBLICATION_REPORT" in current_app.config
        else get_latest_report()
    )
    events = _normalized_events(get_public_events(report, cache_dir=CACHE_DIR))
    view = {
        "events": events, "by_id": {event["id"]: event for event in events},
        "lastmod": _latest_modified(events),
    }
    g._source_event_view = view
    return view


def _load_events() -> list[dict]:
    return _event_view()["events"]


def _modified(event: dict) -> str:
    # A projection may contain newer fields than its saved archive copy.  If
    # its modified_at is unknown, its original publication date is not a valid
    # substitute for lastmod.
    return _iso_value(event.get("modified_at"))


def _latest_modified(events: list[dict]) -> str:
    modified = [_modified(event) for event in events]
    # A newly projected event with no saved timestamp can change the index.
    # Do not attribute that index state to another event's older timestamp.
    return max(modified, default="") if all(modified) else ""


def event_paths(events: Iterable[dict] | None) -> list[str]:
    """All HTML routes needed by a static publication, including an empty index."""
    events = _normalized_events(events)
    count = math.ceil(len(events) / PAGE_SIZE)
    return ["/events/"] + [f"/events/page/{page}/" for page in range(2, count + 1)] + [
        f"/events/{event['id']}/" for event in events
    ]


def source_sitemap_entries(events: Iterable[dict] | None) -> list[tuple[str, str]]:
    """Return public HTML URLs with real dates only; an unknown lastmod is ''."""
    events = _normalized_events(events)
    if not events:
        return []
    index_paths = ["/events/"] + [
        f"/events/page/{page}/" for page in range(2, math.ceil(len(events) / PAGE_SIZE) + 1)
    ]
    latest = _latest_modified(events)
    return [(path, latest) for path in index_paths] + [
        (f"/events/{event['id']}/", _modified(event)) for event in events
    ]


def _title(event: dict) -> str:
    action = event.get("action_label") or event.get("action") or "公开动作"
    return f"{event.get('bank') or '机构待确认'} · {event.get('symbol') or '标的待确认'} · {action}"


def _date_explanation(event: dict) -> tuple[str, str]:
    if event.get("date_basis") == "first_seen" or "inferred_event_date" in event.get("quality_flags", []):
        return "首次发现日期（事件日期未确认）", "此日期来自本站首次发现记录，不能据此确认事件实际发生或报道发布日期。"
    return "记录中的事件日期（未独立确认）", "此日期来自采集记录；未独立核验原始公告时间，不表示本站页面当日已发布。"


def _time_row(label: str, value: Any) -> str:
    value = _text(value).strip()
    safe_value = _iso_value(value)
    display = f'<time datetime="{_html(safe_value)}">{_html(value)}</time>' if safe_value else _html(value or "未记录")
    return f"<div>{_html(label)}</div><div>{display}</div>"


def _response(body: str | bytes, content_type: str, *, indexable: bool = True) -> Response:
    response = Response(body, content_type=content_type)
    response.headers["Cache-Control"] = "public, max-age=120, s-maxage=300, stale-while-revalidate=1800"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Robots-Tag"] = "index, follow" if indexable else "noindex, follow"
    return response


def _render_index(events: list[dict], page: int, *, modified_at: str | None = None) -> str:
    pages = max(1, math.ceil(len(events) / PAGE_SIZE))
    path = "/events/" if page == 1 else f"/events/page/{page}/"
    page_events = events[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    rows = []
    for event in page_events:
        date_label, _ = _date_explanation(event)
        rows.append(
            "<tr>"
            f'<td><a href="/events/{event["id"]}/">{_html(_title(event))}</a></td>'
            f'<td>{_html(event.get("price_target") or "未记录")}</td>'
            f'<td>{_html(event.get("event_date") or "未确认")}<br><small>{_html(date_label)}</small></td>'
            f'<td>{_html(event.get("source_label") or "证据类型未标注")}</td>'
            "</tr>"
        )
    if rows:
        content = (
            '<div class="table-wrap"><table><caption class="sr-only">公开评级与目标价事件档案</caption>'
            '<thead><tr><th>事件记录</th><th>目标价（原始口径）</th><th>日期</th><th>证据</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
        )
    else:
        content = (
            '<section class="panel"><h2>等待首批可回查事件</h2>'
            '<p>事件档案由后台采集与发布流程生成。记录将包含来源标题、公开链接、证据类型与日期说明。</p>'
            '<p>当前没有可公开记录；本页不会把空白数据或模型推断包装成已核实事件。</p>'
            '<p><a href="/methodology/">阅读研究方法</a> · <a href="/sources/">查看数据来源</a></p></section>'
        )
    navigation = []
    if page > 1:
        previous = "/events/" if page == 2 else f"/events/page/{page - 1}/"
        navigation.append(f'<a rel="prev" href="{previous}">上一页</a>')
    navigation.append(f"<span>第 {page} / {pages} 页 · 共 {len(events)} 条</span>")
    if page < pages:
        navigation.append(f'<a rel="next" href="/events/page/{page + 1}/">下一页</a>')
    body = f"""
<nav class="crumb"><a href="/">首页</a> / <span>事件档案</span></nav>
<h1>公开评级与目标价事件档案</h1>
<p class="one-liner">每条公开记录有独立链接，保留来源、证据类型和时间口径，便于回查与引用。</p>
<p class="lede">首页展示近期动态；此处保留已建档记录，后台建档后随站点发布公开。日期与数值以各条记录的出处和说明为准，报道出处不等于投行原始研报。</p>
<p><a href="/feed.xml">订阅 RSS</a> · <a href="/data/events.json">下载公开事件 JSON</a> · <a href="/editorial-policy/">编辑与更正标准</a></p>
{content}
<nav class="chip-row" aria-label="事件档案分页">{" ".join(navigation)}</nav>
"""
    schema = {
        "@context": "https://schema.org", "@type": "CollectionPage",
        "name": "公开评级与目标价事件档案", "url": _absolute(path),
        "inLanguage": "zh-CN", "mainEntity": {
            "@type": "ItemList", "itemListElement": [
                {"@type": "ListItem", "position": (page - 1) * PAGE_SIZE + index,
                 "url": event["url"], "name": _title(event)}
                for index, event in enumerate(page_events, 1)
            ],
        },
    }
    title = f"事件档案{' · 第 ' + str(page) + ' 页' if page > 1 else ''} | {SITE_NAME}"
    return render_page(
        title, "可回查的公开投行评级与目标价事件，附来源链接、证据层级和日期说明。",
        path, body, [schema], "/events/", _latest_modified(events) if modified_at is None else modified_at,
        robots_directive="index,follow,max-snippet:-1" if events else "noindex,follow",
    )


def _render_event(event: dict) -> str:
    path = f"/events/{event['id']}/"
    title = _title(event)
    date_label, date_note = _date_explanation(event)
    source_title = _html(event.get("source_title") or "来源未提供标题")
    source_url = safe_source_url(event.get("source_url"))
    source = (
        f'<a href="{_html(source_url)}" rel="noopener noreferrer">{source_title}</a>'
        if source_url else source_title + "（当前无可回查的公开链接）"
    )
    field_rows = "".join(
        f"<tr><th scope=\"row\">{label}</th><td>{_html(event.get(field) or '未记录')}</td></tr>"
        for label, field in (
            ("机构", "bank"), ("股票代码", "symbol"), ("公开动作", "action_label"),
            ("原评级", "old_rating"), ("新评级", "new_rating"),
            ("目标价（原始口径）", "price_target"), ("先前目标价", "previous_price_target"),
        )
    )
    flags = event.get("quality_flags", [])
    flags_html = (
        '<h3>数据质量标记</h3><ul>'
        + "".join(f'<li>{_html(_FLAG_LABELS.get(flag, flag))}</li>' for flag in flags)
        + '</ul>'
        if flags else ""
    )
    body = f"""
<nav class="crumb"><a href="/">首页</a> / <a href="/events/">事件档案</a> / <span>记录详情</span></nav>
<h1>{_html(title)}</h1>
<p class="one-liner">公开来源中的评级与目标价记录；本页保留其出处与时间口径。</p>
<section class="panel">
  <h2>记录内容</h2><div class="table-wrap"><table><tbody>{field_rows}</tbody></table></div>
  <p class="muted">价格保留来源记录的数值口径；未提供币种或换算依据时，不作币种或交易单位推定。</p>
</section>
<section class="panel">
  <h2>来源与证据</h2><p>{source}</p>
  <p>证据类型：{_html(event.get('source_label') or '未标注')}（{_html(event.get('source_type') or 'unknown')}）。</p>
  <p>链接指向公开报道或数据出处，不代表本站取得或核验了投行完整原始研报。</p>
  <h3>字段补全说明</h3>
  <p>原评级、新评级与目标价可能在后续采集中由结构化来源或历史记录补全。当前档案不提供逐字段出处映射；事件级证据标签不表示每个字段都已独立核验。请结合原始链接复核，空值表示未记录。</p>
  {flags_html}
</section>
<section class="panel">
  <h2>日期与发布记录</h2>
  <div class="kv">
    {_time_row(date_label, event.get('event_date'))}
    {_time_row('档案首次记录', event.get('first_seen_at'))}
    {_time_row('档案最近观察', event.get('last_seen_at'))}
    {_time_row('本站首次建档', event.get('published_at'))}
    {_time_row('本站记录实质更新', event.get('modified_at'))}
  </div>
  <p>{_html(date_note)}</p>
  <p>本站首次建档与评级事件日期分别记录。后台建档后，记录随站点发布更新到公网；建档时间不代表公网当时已可访问。缺少建档或修改时间时显示“未记录”，不以本次访问时间或报告生成时间代替。</p>
</section>
<section class="panel">
  <h2>引用与更正</h2>
  <p>引用本页请保留永久链接、记录中的日期口径和原始出处。记录标识：<code>{event['id']}</code>。</p>
  <p><a href="{_html(event['url'])}">本条永久链接</a> · <a href="/events/">返回全部事件</a> · <a href="/editorial-policy/">更正方式</a> · <a href="/methodology/">研究方法</a></p>
</section>
"""
    schema = {
        "@context": "https://schema.org", "@type": "Article", "headline": title,
        "description": "附来源与日期口径的公开评级、目标价事件记录。",
        "url": event["url"], "identifier": event["id"], "inLanguage": "zh-CN",
        "mainEntityOfPage": {"@type": "WebPage", "@id": event["url"]},
        "author": {"@type": "Organization", "name": SITE_NAME, "url": _absolute("/about/")},
        "publisher": {"@type": "Organization", "name": SITE_NAME, "url": _absolute("/")},
    }
    if event.get("published_at"):
        schema["datePublished"] = event["published_at"]
    if event.get("modified_at"):
        schema["dateModified"] = event["modified_at"]
    if source_url:
        schema["citation"] = {"@type": "CreativeWork", "name": _text(event.get("source_title")), "url": source_url}
    return render_page(title + f" | {SITE_NAME}", schema["description"], path, body, [schema], "/events/", _modified(event))


def _rss(events: list[dict]) -> bytes:
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = f"{SITE_NAME} 公开事件档案"
    ET.SubElement(channel, "link").text = _absolute("/events/")
    ET.SubElement(channel, "description").text = "近期建档的评级与目标价记录；pubDate 为本站首次建档时间，记录随发布更新到公网。附公开报道出处和日期口径，不代表投行原始研报全文。"
    ET.SubElement(channel, "language").text = "zh-CN"
    build_date = _rss_date(_latest_modified(events))
    if build_date:
        ET.SubElement(channel, "lastBuildDate").text = build_date
    latest = sorted(events, key=lambda event: (event.get("published_at") or "", _text(event.get("event_date")), event["id"]), reverse=True)
    for event in latest[:FEED_SIZE]:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = _title(event)
        ET.SubElement(item, "link").text = event["url"]
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = event["url"]
        date_label, note = _date_explanation(event)
        ET.SubElement(item, "description").text = (
            f"{date_label}：{event.get('event_date') or '未确认'}。{note} "
            f"证据类型：{event.get('source_label') or '未标注'}。"
            f"报道出处：{event.get('source_title') or '未记录'} {event.get('source_url') or ''}。"
            "报道出处不等于投行原始研报；详情请查看永久链接。"
        )
        published = _rss_date(event.get("published_at"))
        if published:
            ET.SubElement(item, "pubDate").text = published
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def register_source_routes(app) -> None:
    @app.route("/events/")
    @app.route("/events/page/<int:page>/")
    def public_events_index(page: int = 1):
        if page < 1:
            abort(404)
        if page == 1 and request.path != "/events/":
            return redirect("/events/", code=308)
        view = _event_view()
        events = view["events"]
        if page > max(1, math.ceil(len(events) / PAGE_SIZE)):
            abort(404)
        return _response(_render_index(events, page, modified_at=view["lastmod"]), "text/html; charset=utf-8", indexable=bool(events))

    @app.route("/events/<event_id>/")
    def public_event_detail(event_id: str):
        if not _EVENT_ID.fullmatch(event_id):
            abort(404)
        event = _event_view()["by_id"].get(event_id)
        if event is None:
            abort(404)
        return _response(_render_event(event), "text/html; charset=utf-8")

    @app.route("/feed.xml")
    def public_events_feed():
        return _response(_rss(_load_events()), "application/rss+xml; charset=utf-8", indexable=False)

    @app.route("/data/events.json")
    def public_events_data():
        events = _load_events()
        payload = {
            "schema_version": 1, "site": SITE_NAME, "url": _absolute("/events/"),
            "updated_at": _latest_modified(events) or None, "count": len(events), "events": events,
        }
        return _response(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n", "application/json; charset=utf-8", indexable=False)
