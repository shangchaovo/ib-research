#!/usr/bin/env bash
# ib-research 一键部署：同步采集运行环境、验证本机预览，并发布 Cloudflare 静态站。
#
# 模型：~/ib-research 是唯一的代码源头(改代码、走 PR 都在这里);
#       ~/.openclaw/workspace 是运行环境——数据(data/)和密钥(.env)留在那边不动,
#       只覆盖代码文件。launchd / cron 引用 workspace；公网由 Workers Assets 提供。
#
# 用法: bash deploy.sh        # 同步 + 校验 + 重启 + 冒烟
set -euo pipefail

SRC="${IB_RESEARCH_SOURCE_DIR:-$HOME/ib-research}"
DST="${IB_RESEARCH_DEPLOY_DIR:-$HOME/.openclaw/workspace}"
PY="${IB_RESEARCH_PYTHON:-/opt/homebrew/bin/python3}"

# 需要同步的代码文件(数据与 .env 不在其列)
FILES=(
  ib_research_server.py
  ib_research_fetcher.py
  ib_research_geo.py
  ib_research_sources.py
  ib_research_source_pages.py
  ib_research_health.py
  ib_research_cron_runner.py
  ib_research_seo_monitor.py
  export_static_site.py
  publish_cloudflare.py
  wrangler.jsonc
  design_system.py
  institution_intelligence.py
  llm_fallback.py
  collectors/__init__.py
  collectors/yahoo_provider.py
  test_ib_research_geo.py
  test_ib_research_sources.py
  test_ib_research_source_pages.py
  test_export_static_site.py
  test_institution_intelligence.py
  test_ib_research_seo_monitor.py
  og-image.png
)

# Validate the source tree before replacing any file in the running workspace.
PYTHON_FILES=()
for f in "${FILES[@]}"; do
  if [ ! -f "$SRC/$f" ]; then
    echo "ERROR: 缺少部署文件 $SRC/$f" >&2
    exit 1
  fi
  if [[ "$f" == *.py ]]; then
    PYTHON_FILES+=("$SRC/$f")
  fi
done

echo "==> 源码语法校验(运行环境尚未修改)"
"$PY" -m py_compile "${PYTHON_FILES[@]}"

test_cache_dir="$(mktemp -d)"
smoke_file=""
cleanup() {
  rm -rf "$test_cache_dir"
  if [ -n "$smoke_file" ]; then
    rm -f "$smoke_file"
  fi
}
trap cleanup EXIT

echo "==> 源码单测(包括事件信源与静态发布；隔离生产数据和密钥)"
( cd "$SRC" && IB_RESEARCH_ENV=/dev/null IB_RESEARCH_CACHE_DIR="$test_cache_dir" \
  "$PY" -m unittest discover -t . -s . -p 'test_*.py' )

echo "==> 部署 ib-research: $SRC -> $DST"
for f in "${FILES[@]}"; do
  mkdir -p "$(dirname "$DST/$f")"
  cp "$SRC/$f" "$DST/$f"
  echo "    synced $f"
done

echo "==> 重启 com.openclaw.ib-research-server"
launchctl kickstart -k "gui/$(id -u)/com.openclaw.ib-research-server"

echo "==> 冒烟: 等待 8081 就绪并检查 GSC 验证 meta"
ok=""
smoke_file="$(mktemp)"
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
  echo "    ERROR: 45s 内未在 8081 首页发现验证 meta；停止公网发布，请查日志" >&2
  exit 1
fi
echo "==> 构建并发布 Cloudflare Workers 静态快照"
IB_RESEARCH_CACHE_DIR="$DST/data/ib_research" \
IB_RESEARCH_ENV="$DST/.env" \
  "$PY" "$DST/publish_cloudflare.py"
echo "==> 完成"
