# FResearch

看外资怎么给关注的股票定价。UBS、Citi、摩根士丹利、JPMorgan 这些机构的评级变动、目标价和公开标题会收进来，按股票和机构归在一起；发完之后 1 天、5 天、20 个交易日相对 SPY 走没走，也会记一笔。

**站点：[fresearch.cc.cd](https://fresearch.cc.cd/)**

本来是给自己看算力、HBM、设备这几条线用的。NVDA、TSM、ASML 这类票，评级和目标价散落在 Finnhub、Yahoo、各家新闻 RSS 里，每天翻一遍很烦。这个站把 upgrade / downgrade / 调目标价收到一张 30 天台账里，再让 LLM 归纳公开标题（Kimi 为主，Qwen 兜底）。数字必须有公开交叉证据才进目标价卡片，模型自己抽出来的不会单独当事实。

采集大约四小时一轮，行情验证每天补。韩股那边会跟一下海力士、三星的本土券商目标价。首页默认看关注池近 30 天；点进股票页能看到现价对一致目标价、公开目标价区间和机构时间线。

> `git push` 只更新 GitHub。公网站点由 Cloudflare Workers Static Assets 提供；本机负责采集并自动发布只读快照。代码上线请跑 `bash deploy.sh`。

## 功能

- **评级变动追踪**：Finnhub / Yahoo Finance / Google News RSS / Benzinga / Zacks / StreetInsider 等来源的 upgrade / downgrade / 目标价调整，按 `(bank, symbol, action, date)` 去重，写入 30 天滚动台账
- **韩股覆盖**：Google News 韩语 RSS 追踪 SK 海力士 / 三星的本土券商目标价（万韩元换算美元参考）
- **研报观点整合**：公开标题、摘要与结构化评级事件按股票和机构归并，标出共识、分歧与仍待验证的风险；不声称持有未公开的完整研报
- **证据优先的目标价**：优先 Alpha Vantage 一致预期、结构化评级和可回溯公开标题；LLM 抽出的数字没有外部交叉证据时不进目标价卡片。各家公开目标价按中位数启发式剔除异常值后再画区间
- **市场反应验证**：复权收盘价计算观点发布后 1 / 5 / 20 个交易日回报，以相对 SPY 的几何超额收益判断公开倾向有没有被随后价格确认
- **机构倾向蒸馏**：按机构汇总看多 / 看空、样本量、方向一致率，以及**按看多/看空方向计**的 5 日相对 SPY 超额收益；结论写进真实数字，不用全站共用的模板句
- **LLM 摘要**：Kimi（`kimi-code`，JSON mode）生成结构化核心观点，Qwen（阿里云百炼）自动兜底
- **评级回填**：Yahoo `from_grade → to_grade` 回填缺失的旧/新评级与目标价；摘要正则从新闻正文补提取模糊标题里的目标价

## 页面与性能

| 入口 | 内容 |
| --- | --- |
| `/` | 本期观点整合 + 评级台账。默认看关注池近 30 天；首屏只渲染当前一页（30 行），全量事件放进 JSON 岛，第一次筛选/翻页才解析 |
| `/stocks/<ticker>/` | 现价 vs 一致目标价、公开目标价区间、机构观点时间线 |
| `/institutions/`、`/institutions/<slug>/` | 各机构公开倾向、覆盖标的、同向率与市场确认样本 |
| `/reactions/` | 逐事件 1 / 5 / 20 日绝对与相对回报 |
| `/events/`、`/events/page/2/` | 可回查事件档案，每页 50 条真实 HTML 链接，无 JavaScript 也可遍历 |
| `/events/<id>/` | 单条评级 / 目标价永久记录，附报道出处、抽取方式、补全标记和时间口径 |
| `/feed.xml`、`/data/events.json` | 最近 50 条建档记录的 RSS、全量公开事件 JSON |
| `/topics/` | 目标价、机构观点、市场验证、信号一致性的分析框架 |
| `/about/` `/methodology/` `/sources/` `/disclosures/` | 品牌、方法、来源与 YMYL 披露 |

首页与子页共用 `design_system.py` 的主题 token 和组件样式（暗色 / 明亮 / 护眼）。旧 `/learn/*`、`/compare/*` 永久重定向到研究方法、目标价框架或对应股票页。

## SEO / GEO

按「能抓取 → 能索引 → 能引用」接，不依赖单独的 GEO 黑科技。Google 对 AI Overviews 的指导仍是先把传统 SEO 做好。

| 入口 | 作用 |
| --- | --- |
| `/robots.txt` | 允许 Googlebot / Bingbot / OAI-SearchBot；禁止 `/api/`；`GPTBot` / `CCBot` 默认 Disallow |
| `/sitemap.xml` | 首页、有实质内容的关注池股票页、已有数据的机构页、市场验证、分析框架、信任页和永久事件档案。没有评级/目标价/新闻的股票页同时设置 `noindex,follow` |
| `/llms.txt` | 给生成式系统看的站点说明书；**不是** Google AI Overviews 的前置条件 |
| JSON-LD | 首页与股票 / 机构 / 反应页用 `CollectionPage`；独立事件记录用 `Article`；`SoftwareApplication` 只挂在 `/about/` |
| 社交卡片 | `og:*` + `twitter:site` / `creator` / `image:alt`；分享图 `/og-image.png` |

FAQ 结构化数据至少两组问答才输出。`mainEntityOfPage` 使用 `WebPage` 对象而不是裸 URL。

日期有证据才输出。事件页的 `datePublished` 对应本站首次建档，`dateModified` / sitemap `lastmod` 对应公开内容的实质变更；重复采集不刷新这些时间。其他集合页没有逐页修改记录时省略 `lastmod`，不把整轮报告刷新当作每页发表日期。

收录检查：`python3 ib_research_seo_monitor.py` 抓公网域名核对验证 meta、robots、sitemap 和关键页面未回归。

## 公开信源档案

新档案只收录具有机构、股票、有效动作与日期、新闻标题和可回查外部链接的 `verified_headline` / `verified_summary` 事件。标签展示为“公开新闻标题 / 摘要规则抽取”，不表示投行审核过本站内容。缺少单条原文的 Yahoo 结构化记录、韩语聚合推断、空来源和模型推断仍可按既有口径出现在总览，但不会因此自动成为永久档案里的可引用记录。

每条记录区分事件记录日期、档案首次观察、首次建档与实质更新时间。推定日期、历史评级回填、新评级推断、目标价补全等随记录公开；报道链接不是完整投行原始研报，也不能证明所有补全字段均来自同一篇报道。

持久档案位于 `IB_RESEARCH_CACHE_DIR/public_events_archive.json`，只保存允许公开的字段，不进入 Git。首次成功采集或静态导出会逐个读取本地仍保留的 `report_YYYY-MM-DD.json` 建档，此后增量更新。已入档事件不受首页 30 天滚动窗口影响。GET 请求只读档案，不采集、不调用模型、不写文件。

事件 ID 由归一后的机构、股票、动作、日期生成；补目标价、评级、标题和链接不会改变 ID。同机构同股票同日同动作按现有账本约定合并，不等同于投行原始研报编号。更正机构 / 日期 / 动作会形成不同身份，旧链接仍保留；目前不自动推断跨日更正或删除旧记录，应人工核对。上线前备份整个运行时数据目录，长期保留档案文件；无法回填已经删除的历史报告。

公开 API `/api/ib-research` 保留 `status` / `data` 外层，`data` 改为字段白名单：必要元信息、公开概述、评级与目标价；不返回 `raw`、内部运行 ID、模型错误、供应商状态和未知新增字段。原始数据 `/api/ib-research/raw` 仅本机可信访问可读，静态发布不再导出 raw JSON。抓取机器数据使用 `/data/events.json`；它允许抓取、设置 `noindex,follow`，引用落到 HTML 永久页。

静态构建固定一份报告和事件快照，目录、详情、RSS、JSON、sitemap 使用相同数据；不会为了 Logo 触发网络抓取。构建校验完成后才替换旧输出，失败保留上一份完整快照。默认拒绝用无有效报告的空缓存覆盖已有发布；`--allow-empty` 只供测试和诊断。

## 关注池

AI 算力 + 存储/HBM + 半导体设备 + 网络主线 30 只：

`NVDA AMD AVGO MRVL INTC TSM ARM` / `MU WDC STX SNDK` / `ASML AMAT LRCX KLAC` / `ANET DELL CRWV VRT` / `MSFT GOOGL AMZN META ORCL IBM PLTR SNOW` / `AAPL TSLA CRM`

改 `ib_research_fetcher.py` 里的 `HOT_SYMBOLS` 一处即可全局生效。

## 快速开始

```bash
pip3 install -r requirements.txt
cp .env.example .env          # 仅 FINNHUB_API_KEY 必需，其余缺失自动降级
python3 ib_research_fetcher.py
python3 ib_research_fetcher.py --fast   # 读 AV 缓存，适合高频 cron
python3 ib_research_server.py           # http://localhost:8081
python3 ib_research_health.py
python3 -m unittest discover -t . -s . -p 'test_*.py'
```

定时任务见 `ib_research_cron_runner.py`（带锁、日志、异常告警）。每次采集成功且质量检查通过后，`publish_cloudflare.py` 会在后台导出并发布新的 Cloudflare 静态快照。

## 上线

本仓库是代码源头；`~/.openclaw/workspace` 是采集运行环境（数据目录和 `.env` 留在那边，不被覆盖）。公网请求不再依赖本机 Flask/Tunnel 在线。

```bash
bash deploy.sh    # 同步代码 → 校验/单测 → 本机预览冒烟 → 发布 Cloudflare
python3 publish_cloudflare.py  # 仅重新导出并发布当前只读快照
```

代码检出在其他位置时，通过 `IB_RESEARCH_SOURCE_DIR` 指定。先在本机生成可审阅快照，确认事件页和来源，再发布：

```bash
cd /path/to/ib-research
IB_RESEARCH_CACHE_DIR="$HOME/.openclaw/workspace/data/ib_research" \
IB_RESEARCH_ENV="$HOME/.openclaw/workspace/.env" \
  python3 export_static_site.py
# 上一行只导出本地 cloudflare_dist，不发布 Cloudflare。

# 准备发布时运行；源码测试在复制前执行，冒烟失败即停止发布。
IB_RESEARCH_SOURCE_DIR="$PWD" bash deploy.sh
```

公网 `https://fresearch.cc.cd/*` 由 Worker route 直接命中静态资产；`127.0.0.1:8081` 仅保留为本机预览和手动刷新入口。Cloudflare 部署是不可变版本，发布失败时线上继续提供上一版内容。

## 代码结构

| 文件 | 职责 |
|------|------|
| `ib_research_fetcher.py` | 抓取 + LLM 摘要，产出 `report.json` |
| `ib_research_server.py` | Flask 服务与首页；首屏一页评级行 + JSON 岛 |
| `ib_research_geo.py` | GEO 子页、sitemap、robots、JSON-LD、llms.txt |
| `ib_research_sources.py` | 公开字段投影、来源分级、稳定事件 ID 与持久档案 |
| `ib_research_source_pages.py` | 只读事件目录、详情、RSS 与公开事件 JSON |
| `design_system.py` | 主题 token、基础样式、公共组件 CSS |
| `institution_intelligence.py` | 按机构聚合方向一致率与按方向计的超额收益 |
| `collectors/yahoo_provider.py` | Yahoo 评级回填 |
| `llm_fallback.py` | Kimi 失败时切 Qwen |
| `ib_research_cron_runner.py` | 定时抓取入口 |
| `ib_research_health.py` | 数据新鲜度 / 质量检查 |
| `ib_research_seo_monitor.py` | 公网收录巡检 |
| `export_static_site.py` | 把 Flask 只读页面、JSON API 与图标导出为静态站点 |
| `publish_cloudflare.py` | 带锁构建并通过 Wrangler 发布 Cloudflare 快照 |
| `wrangler.jsonc` | Workers Static Assets、预览地址与生产 route 配置 |
| `deploy.sh` | 同步到 workspace 并重启服务 |
| `test_ib_research_geo.py` / `test_institution_intelligence.py` | 页面与机构分析单测 |

## 环境变量

| Key | 说明 |
|-----|------|
| `FINNHUB_API_KEY` | Finnhub 评级与新闻（必需） |
| `ALPHA_VANTAGE_API_KEY` | 一致目标价（每日 25 次免费额度，按天缓存） |
| `TWELVEDATA_API_KEY` / `MARKETDATA_API_KEY` | 行情补充 |
| `KIMI_API_KEY` | Kimi LLM 摘要 |
| `LLM_QWEN_API_KEY` | Qwen 兜底（可选） |
| `IB_RESEARCH_REFRESH_TOKEN` | 保护 `/refresh` 端点（可选） |
| `IB_RESEARCH_CACHE_DIR` | 数据目录（默认 `./data/ib_research`） |
| `IB_RESEARCH_PUBLIC_ORIGIN` | 对外 canonical 域名（默认 `https://fresearch.cc.cd`） |
| `IB_RESEARCH_SOURCE_DIR` / `IB_RESEARCH_DEPLOY_DIR` / `IB_RESEARCH_PYTHON` | `deploy.sh` 的代码目录、运行目录与 Python 路径覆盖 |
| `HTTPS_PROXY` | 境外源走本地代理（例 `http://127.0.0.1:1082`） |

## 数据来源说明

页面上的评级、目标价和研报观点均来自公开可访问的结构化数据、标题或摘要；LLM 只做归纳，无法交叉验证的数字不会作为事实展示。市场验证采用日期级事件与复权价格，同日多家机构发声会标记混杂，样本不足会降低置信度。

「市场确认 / 未确认」只描述公开观点与后续价格之间的关系，不能证明机构持仓或交易台行为，也不能据此断言「唱多出货」或「唱空抄底」。免费方案实测不可用的来源（Barron's 401、Seeking Alpha PerimeterX 403、TipRanks 403 等）不接入。

## License

MIT
