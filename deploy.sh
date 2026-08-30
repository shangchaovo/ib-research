#!/usr/bin/env bash
# ib-research 一键部署：把本仓库(GitHub 权威源)的代码同步到线上 workspace 并重启服务。
#
# 模型：~/ib-research 是唯一的代码源头(改代码、走 PR 都在这里);
#       ~/.openclaw/workspace 是运行环境——数据(data/)和密钥(.env)留在那边不动,
#       只覆盖代码文件。launchd / cron 引用的都是 workspace 路径,无需改动。
#
# 用法: bash deploy.sh        # 同步 + 校验 + 重启 + 冒烟
set -euo pipefail

SRC="$HOME/ib-research"
DST="$HOME/.openclaw/workspace"
PY=/opt/homebrew/bin/python3

# 需要同步的代码文件(数据与 .env 不在其列)
FILES=(
  ib_research_server.py
  ib_research_fetcher.py
  ib_research_geo.py
  ib_research_health.py
  ib_research_cron_runner.py
  ib_research_seo_monitor.py
  design_system.py
  institution_intelligence.py
  llm_fallback.py
  collectors/__init__.py
  collectors/yahoo_provider.py
  test_ib_research_geo.py
  test_institution_intelligence.py
  og-image.png
)

echo "==> 部署 ib-research: $SRC -> $DST"
for f in "${FILES[@]}"; do
  mkdir -p "$(dirname "$DST/$f")"
  cp "$SRC/$f" "$DST/$f"
  echo "    synced $f"
done

echo "==> 语法校验(避免坏代码上线)"
"$PY" -m py_compile \
  "$DST"/ib_research_server.py \
  "$DST"/ib_research_fetcher.py \
  "$DST"/ib_research_geo.py \
  "$DST"/design_system.py \
  "$DST"/institution_intelligence.py \
  "$DST"/collectors/yahoo_provider.py

echo "==> 页面与机构分析单测"
( cd "$DST" && "$PY" -m unittest test_ib_research_geo test_institution_intelligence )

echo "==> 重启 com.openclaw.ib-research-server"
launchctl kickstart -k "gui/$(id -u)/com.openclaw.ib-research-server"

echo "==> 冒烟: 等待 8081 就绪并检查 GSC 验证 meta"
ok=""
smoke_file="$(mktemp)"
trap 'rm -f "$smoke_file"' EXIT
for _ in $(seq 1 45); do
  if curl -fsS --max-time 5 -o "$smoke_file" http://127.0.0.1:8081/ \
    && grep -q 'google-site-verification' "$smoke_file"; then
    ok=1
    break
  fi
  sleep 1
done
if [ -n "$ok" ]; then
  echo "    OK: 8081 首页已带验证 meta"
else
  echo "    WARN: 45s 内未在 8081 首页发现验证 meta,请查日志" >&2
fi
echo "==> 完成"
