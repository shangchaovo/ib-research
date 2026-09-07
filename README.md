# FResearch — 投行目标价、公开观点与市场验证

追踪 UBS、Citi、Morgan Stanley、JPMorgan 等机构对关注股票的**评级、目标价与公开研报观点**，并检验观点发布后 1、5、20 个交易日相对 SPY 的表现。

**线上站点：[fresearch.cc.cd](https://fresearch.cc.cd/)**

Equity-research aggregator for **analyst ratings** and **target prices**, with LLM summaries and post-publication market validation versus SPY. Coverage focuses on AI compute, HBM/storage, semiconductor equipment and cloud.

## 核心用处

- 不想逐家翻新闻，也能看到「这家外资现在怎么看 NVDA / TSM / ASML」
- 目标价必须有公开交叉证据才会进卡片，LLM 抽出来的数字不会单独当事实
- 观点发出去之后有没有被行情确认，用相对 SPY 的超额收益说话

采集与摘要每 4 小时刷新，市场验证每日补齐。

> `git push` 只更新 GitHub。公网站点由本机 `8081` 经 Cloudflare Tunnel 对外；代码要上线请跑 `bash deploy.sh`。

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
| `/topics/` | 目标价、机构观点、市场验证、信号一致性的分析框架 |
| `/about/` `/methodology/` `/sources/` `/disclosures/` | 品牌、方法、来源与 YMYL 披露 |

首页与子页共用 `design_system.py` 的主题 token 和组件样式（暗色 / 明亮 / 护眼）。旧 `/learn/*`、`/compare/*` 永久重定向到研究方法、目标价框架或对应股票页。

## SEO / GEO

按「能抓取 → 能索引 → 能引用」接，不依赖单独的 GEO 黑科技。Google 对 AI Overviews 的指导仍是先把传统 SEO 做好。

| 入口 | 作用 |
| --- | --- |
| `/robots.txt` | 允许 Googlebot / Bingbot / OAI-SearchBot；禁止 `/api/`；`GPTBot` / `CCBot` 默认 Disallow |
| `/sitemap.xml` | 首页、有实质内容的关注池股票页、已有数据的机构页、市场验证、分析框架与信任页。**没有评级/目标价/新闻的薄股票页不进 sitemap** |
| `/llms.txt` | 给生成式系统看的站点说明书；**不是** Google AI Overviews 的前置条件 |
| JSON-LD | 首页 `CollectionPage`；股票/机构/反应页 `Article`；`SoftwareApplication` 只挂在 `/about/`（首页挂应用类型属于错配） |
| 社交卡片 | `og:*` + `twitter:site` / `creator` / `image:alt`；分享图 `/og-image.png` |

FAQ 结构化数据至少两组问答才输出。`mainEntityOfPage` 使用 `WebPage` 对象而不是裸 URL。

收录检查：`python3 ib_research_seo_monitor.py` 抓公网域名核对验证 meta、robots、sitemap 和关键页面未回归。

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
python3 -m unittest test_ib_research_geo test_institution_intelligence
```

定时任务见 `ib_research_cron_runner.py`（带锁、日志、异常告警）。

## 上线

本仓库是代码源头；`~/.openclaw/workspace` 是运行环境（数据目录和 `.env` 留在那边，不被覆盖）。

```bash
bash deploy.sh    # 同步代码 → 语法校验 → 单测 → 重启 8081 → 冒烟
```

公网 `https://fresearch.cc.cd` 经 `com.xieshangchao.cloudflare-8081` 转到本机 `127.0.0.1:8081`。HTML 有 `max-age=60` / `s-maxage=300`，刚部署后浏览器可能要硬刷新。

## 代码结构

| 文件 | 职责 |
|------|------|
| `ib_research_fetcher.py` | 抓取 + LLM 摘要，产出 `report.json` |
| `ib_research_server.py` | Flask 服务与首页；首屏一页评级行 + JSON 岛 |
| `ib_research_geo.py` | GEO 子页、sitemap、robots、JSON-LD、llms.txt |
| `design_system.py` | 主题 token、基础样式、公共组件 CSS |
| `institution_intelligence.py` | 按机构聚合方向一致率与按方向计的超额收益 |
| `collectors/yahoo_provider.py` | Yahoo 评级回填 |
| `llm_fallback.py` | Kimi 失败时切 Qwen |
| `ib_research_cron_runner.py` | 定时抓取入口 |
| `ib_research_health.py` | 数据新鲜度 / 质量检查 |
| `ib_research_seo_monitor.py` | 公网收录巡检 |
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
| `HTTPS_PROXY` | 境外源走本地代理（例 `http://127.0.0.1:1082`） |

## 数据来源说明

页面上的评级、目标价和研报观点均来自公开可访问的结构化数据、标题或摘要；LLM 只做归纳，无法交叉验证的数字不会作为事实展示。市场验证采用日期级事件与复权价格，同日多家机构发声会标记混杂，样本不足会降低置信度。

「市场确认 / 未确认」只描述公开观点与后续价格之间的关系，不能证明机构持仓或交易台行为，也不能据此断言「唱多出货」或「唱空抄底」。免费方案实测不可用的来源（Barron's 401、Seeking Alpha PerimeterX 403、TipRanks 403 等）不接入。

## License

MIT
