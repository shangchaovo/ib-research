# FResearch · 投行评级与目标价追踪

同一只美股，有的机构上调目标价，有的维持原评级。打开 [NVDA 股票页](https://fresearch.cc.cd/stocks/nvda/)，先分清最新收盘价、一致目标价和各家公开报价的区间，再沿时间线查到哪家机构在何时改了什么。要看股价随后有没有跑赢大盘，到 [市场验证](https://fresearch.cc.cd/reactions/) 查它相对 SPY 的表现。

[近 30 天评级台账](https://fresearch.cc.cd/) · [按机构查观点](https://fresearch.cc.cd/institutions/) · [研究方法](https://fresearch.cc.cd/methodology/) · [数据来源](https://fresearch.cc.cd/sources/)

## 从公开观点到市场反应

| 读者的问题 | 页面怎样回答 | 解读时要留意 |
| --- | --- | --- |
| 是升评级，还是只调了目标价？ | 台账归并 Finnhub、Yahoo Finance、Google News RSS 等公开来源，按机构、股票、动作和日期去重。Yahoo 的新旧评级和可追溯标题、摘要可补全缺项。 | 覆盖限于公开评级、标题和摘要，无法还原付费研报全文。首页默认展示关注池近 30 天，首屏 30 行，筛选和翻页可看其余事件。 |
| 一致预期和单家机构的报价差多少？ | 股票页优先列 Alpha Vantage 一致目标价，并展示有公开出处的机构目标价。有至少三条报价时，绘制区间前按中位数的 1/3 至 3 倍过滤异常值。 | Alpha Vantage 数值缺失时，当前实现可能在「一致目标价」位置回用单家已核实的报价，解读前请核对来源。Kimi、Qwen 等模型参与观点归纳，仅由模型提取的数字不能单独进入目标价卡片。 |
| 发布后跑赢了 SPY 吗？ | 取事件日前最近一个交易日的复权收盘为起点，计算自事件日起第 1、5、20 个交易日的收益。相对收益为 `(1 + 个股收益) / (1 + SPY 收益) − 1`。 | 按日期对齐，盘前盘后可能落在同一窗口。同股同日多家发声会标记混杂，尚未走完的窗口留空。 |
| 哪家机构的公开判断与价格同向？ | 机构页列覆盖标的、样本量、方向一致率，以及按看多或看空方向调整的 5 日超额收益中位数。 | 同向是事后观察，小样本尤其容易波动。这些数据无法推断机构的真实持仓、交易意图或评级对价格的因果影响。 |

关注池由 [采集器中的 `HOT_SYMBOLS`](ib_research_fetcher.py) 统一定义，覆盖 AI 算力、存储/HBM、半导体设备、网络和云平台等 30 只美股。另有韩文新闻源跟踪 SK 海力士、三星的本土券商目标价，原始单位为万韩元，美元换算仅作参考。[分析框架](https://fresearch.cc.cd/topics/) · [利益披露](https://fresearch.cc.cd/disclosures/)

## 在本机生成一份报告

在仓库根目录运行下面的命令。采集器使用 POSIX 文件锁，适合 macOS、Linux 或 WSL。仓库 CI 使用 Python 3.13。先建虚拟环境，复制配置样例，再在 `.env` 填写要使用的数据源凭据。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

在同一终端指定本次采集的配置和输出目录。示例关闭采集成功后的自动云端发布，结果留在本机。

```sh
export IB_RESEARCH_ENV="$PWD/.env"
export IB_RESEARCH_CACHE_DIR="$PWD/data/ib_research"
export IB_RESEARCH_CLOUDFLARE_PUBLISH=0
python ib_research_fetcher.py
```

报告按日写入 `data/ib_research/report_YYYY-MM-DD.json`。写入后采集器会尝试启动预览，打开 [本机首页](http://127.0.0.1:8081/) 即可查看。如果预览未启动，在同一环境运行 `python ib_research_server.py`。缺少数据源或摘要生成失败时，查看采集输出中的质量状态和报告时间。

| 日常操作 | 命令 |
| --- | --- |
| 在已有报告上增量补充新事件 | `python ib_research_fetcher.py --fast` |
| 检查新鲜度、完整性和历史连续性 | `python ib_research_health.py` |
| 运行仓库测试 | `python -m unittest discover -s . -p 'test_*.py'` |

### 配置与覆盖范围

- `FINNHUB_API_KEY` 用于 Finnhub 数据。未配置时采集器会跳过相应调用。`ALPHA_VANTAGE_API_KEY` 用于一致预期，查询结果可缓存。`TWELVEDATA_API_KEY`、`MARKETDATA_API_KEY` 用于补充行情。
- `KIMI_API_KEY`、`LLM_QWEN_API_KEY` 分别用于摘要主通道和可选回退。可用的摘要、报价和历史窗口取决于服务响应、授权与已有数据。上游需要代理时按运行环境设置 `HTTPS_PROXY`。
- `IB_RESEARCH_REFRESH_TOKEN` 用于手动刷新端点，`IB_RESEARCH_PUBLIC_ORIGIN` 设置对外页面的规范域名。配置项见 [环境样例](.env.example)。

## 本机预览、静态快照与收录

本机 Flask 提供研究页面、`/api/ib-research` 和手动刷新。公开站点使用 Cloudflare Workers Static Assets，读者看到的是最近发布的快照。代码推送和站点发布是两个独立步骤。

| 入口 | 用途 |
| --- | --- |
| [定时采集](ib_research_cron_runner.py) / [健康检查](ib_research_health.py) | 检查本轮报告、数据质量和新鲜度，记录运行结果。快速采集可以复用最近的报告。 |
| [行情与评级回填](collectors/yahoo_provider.py) / [机构指标](institution_intelligence.py) | 对照上游数据、事件窗口和方向统计的具体实现。 |
| [页面生成](ib_research_geo.py) / [共享样式](design_system.py) | 股票、机构、研究框架与披露页共用主题。旧 `/learn/` 和 `/compare/` 链接由路由重定向。 |
| [导出](export_static_site.py) / [发布](publish_cloudflare.py) | `python export_static_site.py` 生成 `cloudflare_dist/`。部署到自己的站点前，核对 [Wrangler 配置](wrangler.jsonc)、域名和发布凭据。已有环境的同步、测试、预览检查与发布流程见 [部署脚本](deploy.sh)，其中路径需按部署环境调整。 |

`/sitemap.xml` 只纳入有内容的股票和机构等页面。`/robots.txt` 区分搜索抓取与训练抓取，并限制 `/api/`。`/llms.txt` 提供站点导航。首页、文章和关于页使用对应的 JSON-LD 类型，FAQ 至少有两组问答才输出；分享图使用 `/og-image.png`。`python ib_research_seo_monitor.py` 可检查公开站点的收录相关入口。

软件采用 [MIT 许可证](LICENSE)。
