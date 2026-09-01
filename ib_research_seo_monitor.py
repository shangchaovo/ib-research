#!/usr/bin/env python3
"""FResearch 线上健康 + SEO 收录面监控。

背景:2026-08-18 曾因"GitHub 已合并但线上跑旧进程"导致 Search Console 一直
抓不到验证 meta。本脚本定期抓 **公网域名** fresearch.cc.cd 的关键地址,确认:
  - 首页/个股页带 GSC 验证 meta 且 X-Robots-Tag 允许索引(防回退到旧构建)
  - robots.txt / sitemap.xml / llms.txt / og-image.png 可访问
发现异常用 QQ 告警(运维告警只发 QQ,不刷 TG 群);恢复时也提示一次。

运行: python3 ib_research_seo_monitor.py          # 检查一次, 打印结果
cron/launchd 调用即可; 只在新出现故障或恢复时发消息, 避免刷屏。
"""
import json
import os
import sys
import time
from urllib.parse import urljoin

import requests

requests.packages.urllib3.disable_warnings()  # 走代理时关闭自签告警

ORIGIN = os.environ.get("IB_RESEARCH_PUBLIC_ORIGIN", "https://fresearch.cc.cd").rstrip("/")
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "http://127.0.0.1:1082"
GSC_TOKEN = "s8mvn7tXPvT_q4BHyD2tZAXPFuj3xdUSksDjR6BCj1g"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seo_monitor_state.json")
FAILURES_BEFORE_ALERT = max(1, int(os.environ.get("IB_RESEARCH_MONITOR_FAILURES_BEFORE_ALERT", "2")))
REMINDER_SECONDS = max(3600, int(os.environ.get("IB_RESEARCH_MONITOR_REMINDER_SECONDS", str(24 * 3600))))

# 每项: (key, 路径, 校验函数)  —— 校验返回 True 表示通过
CHECKS = [
    ("home", "/", lambda s, h: GSC_TOKEN in s),
    ("home_indexable", "/", lambda s, h: (
        "index" in h.get("X-Robots-Tag", "").lower()
        and "noindex" not in h.get("X-Robots-Tag", "").lower()
    )),
    ("home_ogimage", "/", lambda s, h: "og:image" in s),
    ("robots", "/robots.txt", lambda s, h: "sitemap" in s.lower()),
    ("sitemap", "/sitemap.xml", lambda s, h: "<urlset" in s and "/stocks/" in s),
    ("llms", "/llms.txt", lambda s, h: "FResearch" in s),
    ("og_png", "/og-image.png", lambda s, h: h.get("Content-Type", "").startswith("image/png")),
    ("stock_page", "/stocks/nvda/", lambda s, h: GSC_TOKEN in s and "<title>" in s),
    ("institutions", "/institutions/", lambda s, h: "机构观点、目标价与市场验证" in s and "<title>" in s),
    ("reactions", "/reactions/", lambda s, h: "投行观点发布后的市场反应" in s and "<title>" in s),
]


def _fetch(path: str):
    """先直连, 失败再走代理; 返回响应及可诊断的失败摘要。"""
    url = urljoin(ORIGIN + "/", path.lstrip("/"))
    failures = []
    attempts = (
        ("direct", None),
        ("proxy", {"http": PROXY, "https": PROXY}),
    )
    for label, proxies in attempts:
        try:
            r = requests.get(url, timeout=20, proxies=proxies, verify=False,
                             headers={"Cache-Control": "no-cache", "User-Agent": "fresearch-seo-monitor/1.0"})
            text = r.content.decode("utf-8", errors="replace")
            return (r.status_code, text, r.headers), f"{label}: http {r.status_code}"
        except Exception as exc:
            failures.append(f"{label}: {type(exc).__name__}")
    return None, "; ".join(failures)


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _notify_qq(message: str) -> None:
    """运维告警走 QQ; 失败静默, 绝不让监控脚本自己崩掉。"""
    try:
        sys.path.insert(0, os.path.expanduser("~/.openclaw/workspace/scripts"))
        from cron_utils import send_qq_message
        send_qq_message(message=message)
    except Exception as e:
        print(f"[seo_monitor] QQ 告警发送失败: {e!r}", file=sys.stderr)


def _notification_for_state(
    failing: list[str],
    results: dict[str, tuple[bool, str]],
    state: dict,
    now: float,
) -> tuple[str | None, dict]:
    """将多探针结果折叠为单个事故状态，避免失败集合抖动造成重复告警。"""
    previous_status = state.get("status")
    if previous_status not in {"healthy", "failing"}:
        previous_status = "failing" if state.get("failing") else "healthy"
    consecutive = int(state.get("consecutive_failures", 0) or 0)
    incident_alerted = bool(state.get("incident_alerted", False))

    message = None
    if failing:
        consecutive = consecutive + 1 if previous_status == "failing" else 1
        if previous_status != "failing":
            state["first_failure_ts"] = now

        last_alert = float(state.get("last_alert_ts", 0) or 0)
        initial_alert = not incident_alerted and consecutive >= FAILURES_BEFORE_ALERT
        reminder = incident_alerted and now - last_alert >= REMINDER_SECONDS
        if initial_alert or reminder:
            message = (
                "⚠️ FResearch 线上监控异常\n"
                + "\n".join(f"· {key}: {results[key][1]}" for key in failing)
                + f"\n已连续失败 {consecutive} 次\n{ORIGIN}"
            )
            state["last_alert_ts"] = now
            incident_alerted = True

        state["status"] = "failing"
        state["consecutive_failures"] = consecutive
        state["incident_alerted"] = incident_alerted
    else:
        if previous_status == "failing" and incident_alerted:
            message = f"✅ FResearch 线上恢复正常 ({ORIGIN})"
        state["status"] = "healthy"
        state["consecutive_failures"] = 0
        state["incident_alerted"] = False
        state.pop("first_failure_ts", None)

    state["failing"] = failing
    state["last_check_ts"] = now
    return message, state


def main() -> int:
    results = {}
    fetched = {}
    for key, path, check in CHECKS:
        if path not in fetched:
            fetched[path] = _fetch(path)
        got, fetch_detail = fetched[path]
        if got is None:
            results[key] = (False, f"unreachable ({fetch_detail})")
            continue
        status, text, headers = got
        ok = status == 200
        try:
            ok = ok and bool(check(text, headers))
        except Exception:
            ok = False
        results[key] = (ok, f"http {status}")

    failing = sorted(k for k, (ok, _) in results.items() if not ok)
    state = _load_state()
    now = time.time()

    for key, (ok, detail) in results.items():
        print(f"  {'✅' if ok else '❌'} {key:16s} {detail}")

    message, state = _notification_for_state(failing, results, state, now)
    if message:
        _notify_qq(message)

    if failing:
        print(f"异常: {', '.join(failing)}")
    else:
        print("全部正常")

    _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
