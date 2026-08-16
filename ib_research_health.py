#!/usr/bin/env python3
"""
外资投行研报数据健康检查 v2
可作为独立脚本运行，也可被 health_check.py 调用

检查项：
1. 最新缓存文件是否存在且未过期（< 24h）
2. 缓存内数据完整性（新闻数、Rating_Changes、Kimi summary 状态）
3. 缓存文件大小异常（过大/过小）
4. 增量更新状态（是否 fallback 到历史数据）
5. 历史数据连续性（最近 3 天是否有数据）
"""
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timezone, timedelta
from typing import Optional

CACHE_DIR = os.environ.get("IB_RESEARCH_CACHE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ib_research"))


def _load_recent_reports(days: int = 7, files: list = None) -> list:
    """加载最近 N 天的报告"""
    reports = []
    if not os.path.isdir(CACHE_DIR):
        return reports
    if files is None:
        files = sorted(
            [f for f in os.listdir(CACHE_DIR) if f.startswith("report_") and f.endswith(".json")],
            reverse=True,
        )
    for f in files[:days]:
        try:
            with open(os.path.join(CACHE_DIR, f), "r", encoding="utf-8") as fh:
                reports.append((f, json.load(fh)))
        except Exception:
            continue
    return reports


def _extract_latest_news_date(raw: dict) -> Optional[date]:
    """从 raw news 中提取最新一条的日期"""
    latest = None
    for n in raw.get("news", []):
        ts = n.get("time_published", "")
        if ts and len(ts) >= 8:
            try:
                d = datetime.strptime(ts[:8], "%Y%m%d").date()
                if latest is None or d > latest:
                    latest = d
            except ValueError:
                continue
    return latest


def _aware_generated_at(generated_at: str) -> datetime:
    """Parse report timestamps. Naive values are local time, not UTC."""
    dt = datetime.fromisoformat(generated_at)
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt.astimezone(timezone.utc)


def check():
    """执行检查，返回 (ok: bool, issues: list[str])"""
    issues = []

    # 1. 缓存目录和文件
    if not os.path.isdir(CACHE_DIR):
        issues.append("缓存目录不存在")
        return False, issues

    files = sorted(
        [f for f in os.listdir(CACHE_DIR) if f.startswith("report_") and f.endswith(".json")],
        reverse=True,
    )
    if not files:
        issues.append("无缓存文件")
        return False, issues

    latest_path = os.path.join(CACHE_DIR, files[0])
    try:
        with open(latest_path, "r", encoding="utf-8") as f:
            report = json.load(f)
    except json.JSONDecodeError:
        issues.append(f"缓存文件损坏: {files[0]}")
        return False, issues
    except Exception as e:
        issues.append(f"读取缓存失败: {e}")
        return False, issues

    # 2. 缓存年龄
    meta = report.get("meta", {})
    generated_at = meta.get("generated_at", "")
    if generated_at:
        try:
            dt = _aware_generated_at(generated_at)
            age_hours = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600
            if age_hours > 48:
                issues.append(f"数据已过期 {age_hours:.0f} 小时")
            elif age_hours > 24:
                issues.append(f"数据 {age_hours:.0f} 小时前生成（建议刷新）")
        except ValueError:
            issues.append(f"generated_at 格式异常: {generated_at}")
    else:
        issues.append("缓存缺少 generated_at 时间戳")

    # 3. 数据完整性
    raw = report.get("raw", {})
    summary = report.get("summary", {})
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except Exception:
            issues.append("summary JSON 解析失败")
            summary = None
    if not isinstance(summary, dict):
        issues.append(f"summary 格式异常: {type(summary).__name__}")
        return False, issues

    news_count = len(raw.get("news", []))
    rec_count = len(raw.get("recommendations", []))

    if news_count < 5:
        issues.append(f"新闻数量过少: {news_count}")
    if rec_count == 0:
        issues.append("Finnhub 推荐数据为空")

    # 4. Kimi summary 状态
    if "error" in summary:
        issues.append(f"Kimi 分析失败: {summary['error']}")
    analysis_status = str(meta.get("analysis_status", "")).lower()
    try:
        pending_count = max(
            0, int(meta.get("pending_rating_count", 0) or 0)
        )
        threshold = max(
            1, int(meta.get("rating_batch_threshold", 5) or 5)
        )
    except (TypeError, ValueError):
        pending_count, threshold = 0, 5
    if pending_count:
        if pending_count >= threshold:
            issues.append(
                f"评级批次已达阈值但仍未完成分析: {pending_count}/{threshold}"
            )
        else:
            print(
                "    [INFO] 新评级按计划批量等待中"
                f"（{pending_count}/{threshold}）"
            )
    if (
        analysis_status not in ("", "ok", "pending")
        and not summary.get("_fallback")
        and "error" not in summary
    ):
        issues.append(f"分析状态异常: {analysis_status}")
    quality_status = str(meta.get("quality_status", "")).lower()
    if quality_status not in ("", "ok"):
        issues.append(f"质量状态异常: {quality_status}")
    # 增量更新或 fallback 时允许 Rating_Changes 为空
    is_incremental = meta.get("incremental", False)
    is_fallback = summary.get("_fallback", False)
    rating_changes = summary.get("Rating_Changes", [])
    if not rating_changes and not is_incremental and not is_fallback:
        issues.append("未提取到 Rating_Changes")
    if is_incremental:
        print(f"    [INFO] 增量更新: 数据无变化，复用昨日分析")
    if is_fallback:
        issues.append(f"降级恢复: 复用历史数据 (原因: {summary.get('_fallback_reason', 'unknown')})")

    # 4.1 Rating_Changes 质量检查
    if rating_changes:
        null_fields = 0
        checked_fields = 0
        for c in rating_changes:
            action = c.get("Action", "").lower()
            for field in ("New_Rating", "Old_Rating", "Price_Target", "Time"):
                # reiterate / price_target_* / initiate coverage 通常不强调新旧评级，允许 New_Rating/Old_Rating 缺失
                if action in ("reiterate", "initiate", "price_target_raise", "price_target_cut") and field in ("New_Rating", "Old_Rating"):
                    continue
                checked_fields += 1
                if c.get(field) is None:
                    null_fields += 1
        if checked_fields > 0:
            null_ratio = null_fields / checked_fields
            if null_ratio > 0.3:
                issues.append(f"Rating_Changes 字段缺失率过高: {null_ratio:.0%} ({null_fields}/{checked_fields})")

        # New_Rating 为 null 的 upgrade/downgrade 是严重问题
        incomplete_actions = [
            c for c in rating_changes
            if c.get("Action") in ("upgrade", "downgrade") and c.get("New_Rating") is None
        ]
        if meta.get("rating_window_days"):
            today = datetime.now().strftime("%Y-%m-%d")
            bad_actions = [c for c in incomplete_actions if c.get("_Last_Seen") == today]
            historical_incomplete = len(incomplete_actions) - len(bad_actions)
            if historical_incomplete:
                print(f"    [INFO] 账本含 {historical_incomplete} 条历史待补全评级事件")
        else:
            bad_actions = incomplete_actions
        if bad_actions:
            issues.append(f"{len(bad_actions)} 条 upgrade/downgrade 缺少 New_Rating")

    # 4.2 Metadata.Date 与实际数据日期范围一致性检查
    metadata = summary.get("Metadata", {})
    if isinstance(metadata, dict):
        reported_date = metadata.get("Date", "")
        latest_date = _extract_latest_news_date(raw)
        if latest_date and reported_date:
            # 提取 reported_date 中的最后一个日期作为结束日期
            try:
                end_part = reported_date.split("至")[-1].strip() if "至" in reported_date else reported_date.strip()
                reported_max = datetime.strptime(end_part[:10], "%Y-%m-%d").date()
                days_behind = (latest_date - reported_max).days
                if days_behind > 3 or days_behind < -3:
                    issues.append(f"Metadata.Date 结束日期 ({reported_max}) 与实际最新日期 ({latest_date}) 偏差 {days_behind} 天")
            except Exception:
                issues.append(f"Metadata.Date 格式异常: {reported_date}")

    # 5. 文件大小异常
    file_size = os.path.getsize(latest_path)
    if file_size < 1024:
        issues.append(f"缓存文件过小 ({file_size}B)，可能数据不完整")
    if file_size > 10 * 1024 * 1024:
        issues.append(f"缓存文件过大 ({file_size / 1024 / 1024:.1f}MB)，需检查")

    # 6. 历史数据连续性（最近 3 天）
    recent_dates = set()
    for f in files[:5]:
        try:
            date_str = f.replace("report_", "").replace(".json", "")
            datetime.strptime(date_str, "%Y-%m-%d")
            recent_dates.add(date_str)
        except ValueError:
            continue

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    day_before = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")

    missing_days = []
    for d in [yesterday, day_before]:
        if d not in recent_dates:
            missing_days.append(d)
    if missing_days:
        issues.append(f"历史数据缺失: {', '.join(missing_days)}")

    # 7. 跨天重复事件检查（同一 Bank+Symbol+Action 在 3 天内出现 >=2 次）
    from collections import defaultdict
    action_history = defaultdict(set)
    for fname, rpt in _load_recent_reports(days=5, files=files):
        s = rpt.get("summary", {})
        if isinstance(s, str):
            try:
                s = json.loads(s)
            except Exception:
                continue
        if not isinstance(s, dict):
            continue
        report_date = rpt.get("meta", {}).get("generated_at", "")[:10] or fname.replace("report_", "").replace(".json", "")
        for c in s.get("Rating_Changes", []):
            key = (c.get("Bank", ""), c.get("Symbol", ""), c.get("Action", ""))
            if all(key):
                action_history[key].add(report_date)

    repeated = [(k, dates) for k, dates in action_history.items() if len(dates) >= 3]
    if repeated:
        print(f"    [INFO] 发现 {len(repeated)} 组跨天重复评级变化 (同一事件在 3+ 天出现，属于正常持续信号)")

    # 8. 服务连通性检查（仅警告，不阻断）
    health_url = os.environ.get(
        "IB_RESEARCH_HEALTH_URL",
        f"http://127.0.0.1:{os.environ.get('IB_RESEARCH_PORT', '8081')}/api/ib-research",
    )
    try:
        req = urllib.request.Request(
            health_url,
            headers={"User-Agent": "ib-research-health/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                print(f"    [WARN] IB Research 服务状态异常: HTTP {resp.status}")
    except Exception as e:
        print(f"    [WARN] IB Research 服务 ({health_url}) 不可达: {e}")

    return len(issues) == 0, issues


def main():
    ok, issues = check()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if ok:
        print(f"✅ IB研报数据健康 ({now})")
        sys.exit(0)
    else:
        print(f"⚠️ IB研报数据异常 ({now}):")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)


if __name__ == "__main__":
    main()
