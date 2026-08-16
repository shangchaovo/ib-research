#!/usr/bin/env python3
"""Run the daily IB research refresh synchronously and fail closed.

OpenClaw executes this file as a command job.  The fetcher's verbose output is
kept in a local log; stdout is reserved for the short QQ/cron delivery summary.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TextIO
from zoneinfo import ZoneInfo


WORKSPACE = Path(__file__).resolve().parent
DATA_DIR = WORKSPACE / "data" / "ib_research"
FETCHER = WORKSPACE / "ib_research_fetcher.py"
HEALTH_CHECK = WORKSPACE / "ib_research_health.py"
RUN_LOG = DATA_DIR / "ib_research_cron.log"
CRON_LOCK = DATA_DIR / ".cron.lock"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("IB_RESEARCH_CRON_TIMEOUT_SECONDS", "1500"))


def _append_log(section: str, text: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    with RUN_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{timestamp}] {section}\n")
        handle.write(text.rstrip() or "(no output)")
        handle.write("\n")


def _run(
    command: list[str], timeout: int, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=WORKSPACE,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _timeout_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _acquire_cron_lock() -> Optional[TextIO]:
    """Prevent overlapping cron runs from racing the daily report."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = CRON_LOCK.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _load_today_report(started_at: datetime, require_fresh: bool) -> tuple[dict[str, Any] | None, str | None]:
    report_path = DATA_DIR / f"report_{started_at:%Y-%m-%d}.json"
    if not report_path.exists():
        return None, f"今日报告不存在: {report_path.name}"
    if require_fresh and report_path.stat().st_mtime < started_at.timestamp() - 1:
        return None, "本次运行没有更新今日报告"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"今日报告无法读取: {exc}"
    if not isinstance(report, dict):
        return None, "今日报告根节点不是对象"
    return report, None


def _as_summary(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary", {})
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except json.JSONDecodeError:
            return {"error": "summary JSON 解析失败"}
    return summary if isinstance(summary, dict) else {"error": "summary 格式异常"}


def _degraded_reason(report: dict[str, Any]) -> str | None:
    meta = report.get("meta", {}) if isinstance(report.get("meta"), dict) else {}
    summary = _as_summary(report)
    if summary.get("error"):
        return str(summary["error"])
    if summary.get("_fallback"):
        return str(summary.get("_fallback_reason") or "Kimi 分析使用了历史降级数据")
    if not isinstance(summary.get("Core_Thesis"), str) or not summary.get("Core_Thesis", "").strip():
        return "分析摘要缺少 Core_Thesis"
    if not isinstance(summary.get("Metadata"), dict):
        return "分析摘要 Metadata 格式异常"
    if not isinstance(summary.get("Rating_Changes"), list):
        return "分析摘要 Rating_Changes 格式异常"
    status = str(meta.get("analysis_status", "")).strip().lower()
    if status != "ok":
        return f"分析状态为 {status or 'missing'}"
    quality_status = str(meta.get("quality_status", "")).strip().lower()
    if quality_status != "ok":
        return f"质量状态为 {quality_status or 'missing'}"
    if meta.get("refresh_mode") != "full":
        return f"刷新模式不是 full（实际: {meta.get('refresh_mode', 'unknown')}）"
    return None


def _compact(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _success_summary(report: dict[str, Any]) -> str:
    raw = report.get("raw", {}) if isinstance(report.get("raw"), dict) else {}
    meta = report.get("meta", {}) if isinstance(report.get("meta"), dict) else {}
    summary = _as_summary(report)
    metadata = summary.get("Metadata", {}) if isinstance(summary.get("Metadata"), dict) else {}
    institutions = _compact(metadata.get("Institution", ""), 90) or "未标注"
    date_range = _compact(metadata.get("Date", ""), 30) or "未标注"
    thesis = _compact(summary.get("Core_Thesis", ""), 100) or "无"
    ratings = summary.get("Rating_Changes", [])
    rating_count = len(ratings) if isinstance(ratings, list) else 0
    generated = _compact(meta.get("generated_at", ""), 30) or "未标注"
    return "\n".join(
        [
            "✅ 外资投行研报每日更新成功",
            f"数据：新闻 {len(raw.get('news', []))} 条｜推荐 {len(raw.get('recommendations', []))} 条｜评级事件 {rating_count} 条",
            f"范围：{date_range}｜机构：{institutions}",
            f"核心：{thesis}",
            f"生成：{generated}",
        ]
    )


def _fetch_command() -> list[str]:
    """完整采集仍先比较数据/评级批次，不用 --force 绕过增量判断。"""
    return [sys.executable, "-u", str(FETCHER)]


def main() -> int:
    parser = argparse.ArgumentParser(description="外资投行研报 Cron 同步运行器")
    parser.add_argument("--validate-only", action="store_true", help="仅验证现有今日报告，不执行抓取")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    started_at = datetime.now(LOCAL_TZ)
    run_id = uuid.uuid4().hex
    lock_handle = _acquire_cron_lock()
    if lock_handle is None:
        print("❌ 外资投行研报更新失败：已有 Cron 任务在运行")
        return 5

    try:
        return _run_job(args, started_at, run_id)
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()


def _run_job(args: argparse.Namespace, started_at: datetime, run_id: str) -> int:
    fetch_result: subprocess.CompletedProcess[str] | None = None

    if not args.validate_only:
        fetch_env = os.environ.copy()
        fetch_env["IB_RESEARCH_RUN_ID"] = run_id
        try:
            fetch_result = _run(
                _fetch_command(),
                args.timeout_seconds,
                env=fetch_env,
            )
        except subprocess.TimeoutExpired as exc:
            output = _timeout_text(exc.stdout) + _timeout_text(exc.stderr)
            _append_log("FULL FETCH TIMEOUT", output)
            print(f"❌ 外资投行研报更新失败：完整抓取超过 {args.timeout_seconds} 秒")
            return 124
        _append_log("FULL FETCH", (fetch_result.stdout or "") + (fetch_result.stderr or ""))

    report, report_error = _load_today_report(started_at, require_fresh=not args.validate_only)
    if report_error or report is None:
        print(f"❌ 外资投行研报更新失败：{report_error}")
        return fetch_result.returncode if fetch_result and fetch_result.returncode else 2

    if not args.validate_only:
        report_run_id = str(report.get("meta", {}).get("run_id", ""))
        if report_run_id != run_id:
            print("❌ 外资投行研报更新失败：今日报告不是本次 Cron 运行生成")
            return 4

    degraded = _degraded_reason(report)
    if fetch_result is not None and fetch_result.returncode != 0:
        reason = degraded or f"抓取进程退出码 {fetch_result.returncode}"
        print(f"❌ 外资投行研报更新失败：{_compact(reason, 180)}")
        return fetch_result.returncode
    if degraded:
        print(f"❌ 外资投行研报处于降级状态：{_compact(degraded, 180)}")
        return 3

    health_result = _run([sys.executable, str(HEALTH_CHECK)], 60)
    _append_log("HEALTH CHECK", (health_result.stdout or "") + (health_result.stderr or ""))
    if health_result.returncode != 0:
        health_lines = [line.strip(" -") for line in health_result.stdout.splitlines() if line.strip().startswith("-")]
        reason = "；".join(health_lines[:3]) or f"健康检查退出码 {health_result.returncode}"
        print(f"❌ 外资投行研报健康检查未通过：{_compact(reason, 180)}")
        return health_result.returncode

    print(_success_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
