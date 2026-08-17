# IB Research — 外资投行研报聚合器

追踪外资投行(UBS / Citi / Morgan Stanley / JPM 等)对 AI 算力主线个股的评级与目标价变动,多源抓取 + LLM 摘要,每 4 小时自动刷新。

**Live Demo**: <https://fresearch.cc.cd/>

## 功能

- **评级变动追踪**:抓取 Finnhub / Yahoo Finance / Google News RSS / Benzinga / Zacks / StreetInsider 等来源的 upgrade / downgrade / PT 调整事件,按 (bank, symbol, action, date) 去重后写入 30 天滚动台账
- **韩股覆盖**:通过 Google News 韩语 RSS 追踪 SK 海力士 / 三星的本土券商目标价(万韩元自动换算美元参考)
- **三层可信度**:目标价按 `av_consensus`(Alpha Vantage 一致预期)> `verified_headline`(标题正则提取,无 LLM)> `llm_inferred`(Kimi 提取且通过交叉验证)分级展示,不把未验证的 LLM 数字当事实
- **LLM 摘要**:Kimi(`kimi-code`,JSON mode)生成结构化核心观点,Qwen(阿里云百炼)自动兜底
- **评级回填**:Yahoo `from_grade→to_grade` 精确回填缺失的 OLD/NEW 评级与目标价;摘要正则从新闻正文补提取模糊标题的目标价
- **API + 页面**:`ib_research_server.py` 提供 JSON API、首页研报、股票/主题/知识公开页、`robots.txt` 与 `sitemap.xml`

## SEO / GEO

技术底座已经按“能抓取 → 能索引 → 能引用”接好，不依赖单独的 GEO 黑科技：

| 入口 | 作用 |
| --- | --- |
| `/robots.txt` | 允许 Googlebot / OAI-SearchBot；禁止 `/api/`；默认 Disallow `GPTBot` 训练抓取 |
| `/sitemap.xml` | 首页、关注池股票页、主题、知识库、对比页与信任页 |
| `/llms.txt` | 可选实验，给生成式系统看的站点说明书；**不是** Google AI Overviews 的前置条件 |
| `/stocks/nvda/` 等 | 关注池内每只股票的独立 URL，带 Title / H1 / JSON-LD |
| `/topics/` `/learn/` `/compare/` | 手写主题、金融知识和对照页，不批量生成空壳 |
| `/about/` `/methodology/` `/sources/` `/disclosures/` | 品牌实体、方法、来源与 YMYL 披露 |

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
python3 -m unittest test_ib_research_geo.py
```

定时任务参考 `ib_research_cron_runner.py`(带锁、日志、异常告警)。

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

页面上所有评级/目标价都来自公开信源;LLM 仅用于归纳,推断数字必须通过交叉验证才展示。免费方案实测不可用的源(Barron's 401、Seeking Alpha PerimeterX 403、TipRanks 403 等)已明确不接入,详见代码注释。

## License

MIT
