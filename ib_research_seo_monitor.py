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

# 每项: (key, 路径, 校验函数)  —— 校验返回 True 表示通过
CHECKS = [
    ("home", "/", lambda s, h: GSC_TOKEN in s),
    ("home_indexable", "/", lambda s, h: "index" in h.get("X-Robots-Tag", "")),
    ("home_ogimage", "/", lambda s, h: "og:image" in s),
    ("robots", "/robots.txt", lambda s, h: "sitemap" in s.lower()),
    ("sitemap", "/sitemap.xml", lambda s, h: "<urlset" in s and "/stocks/" in s),
    ("llms", "/llms.txt", lambda s, h: "FResearch" in s),
    ("og_png", "/og-image.png", lambda s, h: h.get("Content-Type", "").startswith("image/png")),
    ("stock_page", "/stocks/nvda/", lambda s, h: GSC_TOKEN in s and "<title>" in s),
]


def _fetch(path: str):
    """先直连, 失败再走代理; 返回 (status, text, headers) 或 None。"""
    url = urljoin(ORIGIN + "/", path.lstrip("/"))
    for proxies in (None, {"http": PROXY, "https": PROXY}):
        try:
            r = requests.get(url, timeout=20, proxies=proxies, verify=False,
                             headers={"Cache-Control": "no-cache", "User-Agent": "fresearch-seo-monitor/1.0"})
            return r.status_code, r.text, r.headers
        except Exception:
            continue
    return None


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


def main() -> int:
    results = {}
    for key, path, check in CHECKS:
        got = _fetch(path)
        if got is None:
            results[key] = (False, "unreachable")
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
    prev_failing = set(state.get("failing", []))
    now = time.time()

    for key, (ok, detail) in results.items():
        print(f"  {'✅' if ok else '❌'} {key:16s} {detail}")

    if failing:
        should_alert = set(failing) != prev_failing or (now - state.get("last_alert_ts", 0)) > 6 * 3600
        if should_alert:
            _notify_qq("⚠️ FResearch 线上监控异常\n"
                       + "\n".join(f"· {k}: {results[k][1]}" for k in failing)
                       + f"\n{ORIGIN}")
            state["last_alert_ts"] = now
        print(f"异常: {', '.join(failing)}")
    else:
        if prev_failing:
            _notify_qq(f"✅ FResearch 线上恢复正常 ({ORIGIN})")
        print("全部正常")

    state["failing"] = failing
    state["last_check_ts"] = now
    _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
