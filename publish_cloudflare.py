#!/usr/bin/env python3
"""Build and deploy the current FResearch snapshot to Cloudflare Workers Assets."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from export_static_site import DEFAULT_OUTPUT, build_static_site


WORKSPACE = Path(__file__).resolve().parent
DATA_DIR = Path(os.path.expanduser(os.environ.get(
    "IB_RESEARCH_CACHE_DIR", str(WORKSPACE / "data" / "ib_research")
)))
LOCK_FILE = DATA_DIR / ".cloudflare-publish.lock"
STATE_FILE = DATA_DIR / "cloudflare_publish_state.json"
LOG_FILE = DATA_DIR / "cloudflare_publish.log"
WRANGLER_VERSION = "4.127.1"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def _log(section: str, text: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{timestamp}] {section}\n{text.rstrip() or '(no output)'}\n")


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_FILE)


def _notify_qq(message: str) -> None:
    try:
        sys.path.insert(0, str(WORKSPACE / "scripts"))
        from cron_utils import send_qq_message
        send_qq_message(message=message)
    except Exception as exc:
        _log("QQ NOTIFY FAILED", repr(exc))


def _wrangler_command() -> list[str]:
    local_wrangler = WORKSPACE / "node_modules" / ".bin" / "wrangler"
    if local_wrangler.exists():
        return [str(local_wrangler)]
    return ["/opt/homebrew/bin/npx", "--yes", f"wrangler@{WRANGLER_VERSION}"]


def publish() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _log("SKIP", "another Cloudflare publish is already running")
            return 0

        state = _load_state()
        try:
            manifest = build_static_site(DEFAULT_OUTPUT)
            _log("BUILD OK", json.dumps(manifest, ensure_ascii=False))

            env = os.environ.copy()
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + env.get("PATH", "")
            env.setdefault("NODE_USE_ENV_PROXY", "1")
            if not env.get("HTTPS_PROXY") and not env.get("https_proxy"):
                env["HTTPS_PROXY"] = "http://127.0.0.1:1082"
            command = _wrangler_command() + ["deploy", "--config", str(WORKSPACE / "wrangler.jsonc")]
            result = subprocess.run(
                command,
                cwd=WORKSPACE,
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
            _log("DEPLOY", output)
            if result.returncode != 0:
                raise RuntimeError(f"wrangler exited with {result.returncode}")

            recovered = int(state.get("consecutive_failures", 0) or 0) >= 2
            state.update({
                "status": "ok",
                "consecutive_failures": 0,
                "last_success_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
                "last_manifest": manifest,
            })
            _save_state(state)
            if recovered:
                _notify_qq("✅ FResearch Cloudflare 自动发布已恢复")
            return 0
        except Exception as exc:
            failures = int(state.get("consecutive_failures", 0) or 0) + 1
            state.update({
                "status": "error",
                "consecutive_failures": failures,
                "last_failure_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
                "last_error": f"{type(exc).__name__}: {exc}",
            })
            _save_state(state)
            _log("FAILED", state["last_error"])
            if failures == 2:
                _notify_qq(f"⚠️ FResearch Cloudflare 自动发布连续失败 2 次\n{state['last_error']}")
            return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="构建并发布 FResearch 到 Cloudflare")
    parser.parse_args()
    return publish()


if __name__ == "__main__":
    raise SystemExit(main())
