# IB Research — 外资投行研报聚合器

追踪外资投行(UBS / Citi / Morgan Stanley / JPM 等)对 AI 算力主线个股的评级与目标价变动,多源抓取 + LLM 摘要,每 4 小时自动刷新。

**Live Demo**: <https://fresearch.cc.cd/>

## 功能

- **评级变动追踪**:抓取 Finnhub / Yahoo Finance / Google News RSS / Benzinga / Zacks / StreetInsider 等来源的 upgrade / downgrade / PT 调整事件,按 (bank, symbol, action, date) 去重后写入 30 天滚动台账
- **韩股覆盖**:通过 Google News 韩语 RSS 追踪 SK 海力士 / 三星的本土券商目标价(万韩元自动换算美元参考)
- **三层可信度**:目标价按 `av_consensus`(Alpha Vantage 一致预期)> `verified_headline`(标题正则提取,无 LLM)> `llm_inferred`(Kimi 提取且通过交叉验证)分级展示,不把未验证的 LLM 数字当事实
- **LLM 摘要**:Kimi(`kimi-code`,JSON mode)生成结构化核心观点,Qwen(阿里云百炼)自动兜底
- **评级回填**:Yahoo `from_grade→to_grade` 精确回填缺失的 OLD/NEW 评级与目标价;摘要正则从新闻正文补提取模糊标题的目标价
- **API + 页面**:`ib_research_server.py` 提供 JSON API 与内置研报页面

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
python3 -m unittest discover -s tests -t . -v
```

定时任务参考 `ib_research_cron_runner.py`（进程锁、日志、异常告警）。

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
| `HTTPS_PROXY` / `HTTP_PROXY` | 可选代理；未设置则直连 |
| `IB_RESEARCH_SSL_VERIFY` | 默认校验证书；设为 `0` 时关闭（仅自签名代理） |
| `IB_RESEARCH_HOST` / `IB_RESEARCH_PORT` | HTTP 服务绑定（默认 `0.0.0.0:8081`） |

## 数据来源说明

页面上所有评级/目标价都来自公开信源;LLM 仅用于归纳,推断数字必须通过交叉验证才展示。免费方案实测不可用的源(Barron's 401、Seeking Alpha PerimeterX 403、TipRanks 403 等)已明确不接入,详见代码注释。

## License

MIT
