# IB Research — 投行目标价、公开观点与市场验证

追踪 UBS、Citi、Morgan Stanley、JPMorgan 等机构对关注股票的评级、目标价与公开研报观点，并检验观点发布后 1、5、20 个交易日相对 SPY 的表现。多源采集与摘要每 4 小时刷新，市场验证每日补齐。

**Live Demo**: <https://fresearch.cc.cd/>

## 功能

- **评级变动追踪**:抓取 Finnhub / Yahoo Finance / Google News RSS / Benzinga / Zacks / StreetInsider 等来源的 upgrade / downgrade / PT 调整事件,按 (bank, symbol, action, date) 去重后写入 30 天滚动台账
- **韩股覆盖**:通过 Google News 韩语 RSS 追踪 SK 海力士 / 三星的本土券商目标价(万韩元自动换算美元参考)
- **研报观点整合**:把公开标题、摘要与结构化评级事件按股票和机构归并，突出共识、分歧与仍待验证的风险；不声称持有未公开的完整研报
- **证据优先的目标价**:优先使用 Alpha Vantage 一致预期、结构化评级数据和可回溯公开标题；LLM 提取的数字没有外部交叉证据时不进入目标价卡片
- **市场反应验证**:使用复权收盘价计算观点发布后 1 / 5 / 20 个交易日回报，并以 SPY 的几何超额收益判断公开倾向是否得到市场确认
- **机构倾向蒸馏**:按机构汇总看多、看空与混合动作，展示样本量、历史一致率和置信度；明确区分卖方公开观点与未披露交易行为
- **LLM 摘要**:Kimi(`kimi-code`,JSON mode)生成结构化核心观点,Qwen(阿里云百炼)自动兜底
- **评级回填**:Yahoo `from_grade→to_grade` 精确回填缺失的 OLD/NEW 评级与目标价;摘要正则从新闻正文补提取模糊标题的目标价
- **API + 页面**:`ib_research_server.py` 提供 JSON API、首页摘要、股票目标价页、机构页、市场反应页、方法页、`robots.txt` 与 `sitemap.xml`

## SEO / GEO

技术底座已经按“能抓取 → 能索引 → 能引用”接好，不依赖单独的 GEO 黑科技：

| 入口 | 作用 |
| --- | --- |
| `/robots.txt` | 允许 Googlebot / OAI-SearchBot；禁止 `/api/`；默认 Disallow `GPTBot` 训练抓取 |
| `/sitemap.xml` | 首页、关注池股票页、已有数据的机构页、市场验证页、分析框架与信任页 |
| `/llms.txt` | 可选实验，给生成式系统看的站点说明书；**不是** Google AI Overviews 的前置条件 |
| `/stocks/nvda/` 等 | 关注池内每只股票的独立 URL，带 Title / H1 / JSON-LD |
| `/institutions/` | 各机构近期公开倾向、覆盖股票、市场确认样本与证据边界 |
| `/reactions/` | 逐事件展示 1 / 5 / 20 日绝对与相对市场反应 |
| `/topics/` | 目标价、机构观点、市场验证和信号一致性的分析框架；旧行业主题永久重定向 |
| `/about/` `/methodology/` `/sources/` `/disclosures/` | 品牌实体、方法、来源与 YMYL 披露 |

旧 `/learn/*` 与 `/compare/*` 地址永久重定向到研究方法、目标价框架或股票页，不再作为独立内容入口。

上线后请立刻：

1. 在 [Google Search Console](https://search.google.com/search-console) 添加 `https://fresearch.cc.cd/`，用 URL Inspection 测首页是否 `URL is available to Google`
2. 提交 `https://fresearch.cc.cd/sitemap.xml`
3. 若前面仍有 Cloudflare Bot Fight / JS Challenge / WAF，把 Googlebot 与 OAI-SearchBot 放行；否则 Search 与 ChatGPT Search 都会超时
4. 用 `IB_RESEARCH_PUBLIC_ORIGIN` 指定对外 canonical 域名（默认 `https://fresearch.cc.cd`）

Google 当前对 AI Overviews / AI Mode 的指导仍是：先把传统 SEO 做好（可抓取、独特内容、清晰结构），不需要 `llms.txt` 或特殊 AI markup。


## 关注池

AI 算力 + 存储/HBM + 半导体设备 + 网络主线 30 只:NVDA AMD AVGO MRVL INTC TSM ARM / MU WDC STX SNDK / ASML AMAT LRCX KLAC / ANET DELL CRWV VRT / MSFT GOOGL AMZN META ORCL IBM PLTR SNOW / AAPL TSLA CRM。改 `ib_research_fetcher.py` 里的 `HOT_SYMBOLS` 一处即可全局生效。

## 快速开始

```bash
pip3 install -r requirements.txt
cp .env.example .env   # 填入 API keys(仅 Finnhub 必需,其余可选降级)
python3 ib_research_fetcher.py          # 完整抓取 + LLM 摘要
python3 ib_research_fetcher.py --fast   # 快速模式(读 AV 缓存,适合高频 cron)
python3 ib_research_server.py           # http://localhost:8081
python3 ib_research_health.py           # 数据新鲜度 / 质量检查
python3 -m unittest -v
```

定时任务参考 `ib_research_cron_runner.py`(带锁、日志、异常告警)。

## 代码结构

| 文件 | 职责 |
|------|------|
| `ib_research_fetcher.py` | 抓取(Finnhub / Alpha Vantage / Yahoo)+ LLM 摘要,产出 `report.json` |
| `ib_research_server.py` | Flask 服务与首页渲染;首屏只服务器渲染一页评级行,全量事件放进 JSON 岛供前端筛选分页 |
| `ib_research_geo.py` | 全部 GEO 子页(股票/机构/市场验证/信任页)、sitemap、robots、JSON-LD |
| `design_system.py` | 主题 token + 基础样式 + 公共组件 CSS,首页与 GEO 子页共用,避免两套观感 |
| `institution_intelligence.py` | 把事件按机构聚合,计算方向一致率与按方向计的相对 SPY 超额收益 |
| `ib_research_seo_monitor.py` | 定期抓公网域名校验关键页面与结构化数据未回归 |

## 环境变量

| Key | 说明 |
|-----|------|
| `FINNHUB_API_KEY` | Finnhub 评级与新闻(必需) |
| `ALPHA_VANTAGE_API_KEY` | 一致目标价(每日 25 次免费额度,按天缓存) |
| `TWELVEDATA_API_KEY` / `MARKETDATA_API_KEY` | 行情补充 |
| `KIMI_API_KEY` | Kimi LLM 摘要 |
| `LLM_QWEN_API_KEY` | Qwen 兜底(可选) |
| `IB_RESEARCH_REFRESH_TOKEN` | 保护 `/refresh` 端点(可选) |
| `IB_RESEARCH_CACHE_DIR` | 数据目录(默认 `./data/ib_research`) |
| `IB_RESEARCH_PUBLIC_ORIGIN` | 对外 canonical 域名(默认 `https://fresearch.cc.cd`) |

## 数据来源说明

页面上的评级、目标价和研报观点均来自公开可访问的结构化数据、标题或摘要；LLM 只做归纳，无法交叉验证的数字不会作为事实展示。市场验证采用日期级事件与复权价格，同日多家机构发声会标记混杂，样本不足会降低置信度。

“市场确认 / 未确认”只描述公开观点与后续价格之间的关系，不能证明机构持仓、交易台行为，也不能据此断言“唱多出货”或“唱空抄底”。免费方案实测不可用的来源（Barron's 401、Seeking Alpha PerimeterX 403、TipRanks 403 等）不接入。

## License

MIT
