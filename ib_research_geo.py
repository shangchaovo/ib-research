#!/usr/bin/env python3
"""FResearch SEO / GEO 公开页：robots、sitemap、股票实体页、主题、知识库与信任页。

只索引有明确研究对象的页面（关注池内股票 + 手写主题/知识页），
不为任意 ticker 批量生成空壳文章。
"""
from __future__ import annotations

import json
import os
import re
from html import escape
from typing import Any, Optional
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

from flask import Response, abort, redirect

from design_system import PAGE_CSS
from ib_research_fetcher import HOT_SYMBOLS, TARGET_BANKS, get_latest_report
from institution_intelligence import ACTION_LABELS, TRACKED_INSTITUTIONS, build_institution_intelligence

SITE_NAME = "FResearch"
SITE_TAGLINE = "投行目标价与研报观点追踪｜市场反应与机构情绪"
CONTACT_EMAIL = "shangchaoxie888@gmail.com"
X_PROFILE_URL = "https://x.com/johny_xie"
X_HANDLE = "@johny_xie"
DEFAULT_ORIGIN = "https://fresearch.cc.cd"
GOOGLE_SITE_VERIFICATION = "s8mvn7tXPvT_q4BHyD2tZAXPFuj3xdUSksDjR6BCj1g"

# 手写知识页与主题页的首次发布日（内容变更时更新）。
CONTENT_PUBLISHED = "2026-08-30"


def public_origin() -> str:
    return os.getenv("IB_RESEARCH_PUBLIC_ORIGIN", DEFAULT_ORIGIN).rstrip("/")


def _abs(path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return public_origin() + path


def _safe_text(value: Any) -> str:
    return escape(str(value if value is not None else ""), quote=False)


def _safe_attr(value: Any) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def _json_ld(payload: Any) -> str:
    dumped = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    dumped = dumped.replace("<", "\\u003c").replace(">", "\\u003e")
    return f'<script type="application/ld+json">{dumped}</script>'


def _iso_date(value: str) -> str:
    raw = str(value or "").strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}", raw)
    if match:
        return match.group(0)
    return CONTENT_PUBLISHED


def _parse_summary(report: dict) -> dict:
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except json.JSONDecodeError:
            summary = {}
    return summary if isinstance(summary, dict) else {}


def _report_generated_at(report: dict) -> str:
    meta = report.get("meta", {}) if isinstance(report, dict) else {}
    return str((meta or {}).get("generated_at") or "")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def _stock(
    ticker: str,
    name: str,
    name_zh: str,
    sector_zh: str,
    industry: str,
    one_liner: str,
    summary: str,
    topics: list[str],
    related: list[str],
) -> dict:
    return {
        "ticker": ticker,
        "name": name,
        "name_zh": name_zh,
        "exchange": "NASDAQ" if ticker not in {"TSM", "ASML"} else ("NYSE" if ticker == "TSM" else "NASDAQ"),
        "sector_zh": sector_zh,
        "industry": industry,
        "one_liner": one_liner,
        "summary": summary,
        "topics": topics,
        "related": related,
    }


STOCK_PROFILES: dict[str, dict] = {
    item["ticker"]: item
    for item in [
        _stock("NVDA", "NVIDIA", "英伟达", "半导体", "AI GPU / 数据中心加速",
               "全球 AI 训练与推理加速器的核心供应商，数据中心收入是其研究主线。",
               "NVIDIA 是 FResearch 关注池中权重最高的 AI 算力标的。研究重点不是复述股价，而是追踪外资投行对其数据中心、Blackwell 出货、毛利率和资本开支假设的评级与目标价变动。",
               ["ai", "semiconductors", "ai-infrastructure"], ["AMD", "AVGO", "TSM", "ASML"]),
        _stock("AMD", "Advanced Micro Devices", "超威半导体", "半导体", "CPU / GPU / AI 加速",
               "在数据中心 CPU 与 GPU 两侧同时挑战英特尔和英伟达的半导体公司。",
               "AMD 的投行研究通常围绕 MI 系列加速器能否切走部分 AI 训练/推理份额、服务器 CPU 份额，以及毛利率能否随 AI 产品组合改善。FResearch 把它作为 NVDA 最直接的算力对照。",
               ["ai", "semiconductors"], ["NVDA", "INTC", "AVGO", "TSM"]),
        _stock("AVGO", "Broadcom", "博通", "半导体", "定制 ASIC / 网络芯片",
               "AI 集群里的定制加速芯片与高速网络连接的关键供应商。",
               "Broadcom 的研究价值在于 hyperscaler 定制 ASIC 与以太网交换。投行材料里它经常作为 NVIDIA GPU 之外的 AI 基础设施受益者出现，适合和 NVDA、ANET 交叉阅读。",
               ["ai", "semiconductors", "ai-infrastructure"], ["NVDA", "ANET", "AVGO", "MRVL"]),
        _stock("MRVL", "Marvell Technology", "迈威尔科技", "半导体", "数据中心连接 / 定制硅",
               "数据中心光连接、DSP 与定制硅片供应商。",
               "Marvell 常出现在 CPO、DSP 和云厂商定制芯片相关研报中。FResearch 用它观察 AI 集群从计算向连接扩散的订单节奏。",
               ["ai", "semiconductors", "ai-infrastructure"], ["AVGO", "ANET", "NVDA", "CRWV"]),
        _stock("INTC", "Intel", "英特尔", "半导体", "CPU / 代工",
               "传统 CPU 龙头，同时在代工和 AI 加速上试图重建叙事。",
               "Intel 的外资研报分歧通常很大：一边是 PC/服务器 CPU 份额流失，另一边是代工与 AI 加速的期权价值。FResearch 把它作为 TSM 和 AMD 的对照，而不是单独的 AI 赢家页。",
               ["semiconductors"], ["AMD", "TSM", "NVDA", "ASML"]),
        _stock("TSM", "Taiwan Semiconductor", "台积电", "半导体", "晶圆代工",
               "先进制程代工龙头，AI GPU 与 ASIC 产能的上游瓶颈。",
               "台积电是几乎所有 AI 半导体故事的上游。FResearch 追踪外资对其 3nm/2nm、CoWoS 先进封装产能和资本开支指引的目标价调整，因为它决定 NVDA、AMD、AVGO 的供给上限。",
               ["ai", "semiconductors", "ai-infrastructure"], ["NVDA", "ASML", "INTC", "AMAT"]),
        _stock("ARM", "Arm Holdings", "安谋", "半导体", "CPU IP / 授权",
               "以 CPU 架构授权进入云和 AI 终端的半导体 IP 公司。",
               "Arm 的研究焦点是云 CPU 采用率、版税结构和 AI 终端侧授权，而不是自己制造芯片。FResearch 把它放在 AI 平台层，而不是纯制造层。",
               ["ai", "semiconductors"], ["NVDA", "TSM", "MSFT", "GOOGL"]),
        _stock("MU", "Micron Technology", "美光科技", "半导体", "DRAM / HBM",
               "存储芯片公司，HBM 是当前 AI 服务器的关键瓶颈之一。",
               "Micron 的外资研报已从传统 DRAM 周期转向 HBM 供给。FResearch 关注其 HBM 份额、ASP 和资本开支，并与西部数据、希捷等存储主线对照。",
               ["semiconductors", "hbm"], ["NVDA", "WDC", "STX", "SNDK"]),
        _stock("WDC", "Western Digital", "西部数据", "存储", "NAND / HDD",
               "NAND 与硬盘存储供应商，受益于数据中心容量扩张。",
               "西部数据连接 AI 数据湖和近线存储需求。投行材料里它常和希捷一起出现在“AI 不只是 GPU”的存储容量叙事中。",
               ["hbm", "ai-infrastructure"], ["STX", "SNDK", "MU", "NVDA"]),
        _stock("STX", "Seagate Technology", "希捷", "存储", "HDD",
               "大容量硬盘龙头，面向数据中心近线存储。",
               "希捷的研究意义在于 AI 训练数据与对象存储对大容量 HDD 的拉动。它不是算力股，但是 AI 基础设施容量层的重要对照。",
               ["hbm", "ai-infrastructure"], ["WDC", "SNDK", "MU", "DELL"]),
        _stock("SNDK", "Sandisk", "闪迪", "存储", "NAND",
               "NAND 闪存公司，从西部数据拆分后独立交易。",
               "Sandisk 让 NAND 周期可以和 HDD、HBM 分开观察。FResearch 用它跟踪闪存价格与数据中心 SSD 需求，而不是把它写成另一篇 AI 概念文。",
               ["hbm"], ["WDC", "STX", "MU", "DELL"]),
        _stock("ASML", "ASML Holding", "阿斯麦", "半导体设备", "光刻机",
               "EUV 光刻设备垄断供应商，先进制程扩产的上游。",
               "ASML 是台积电、英特尔、三星扩产的设备瓶颈。外资目标价往往跟随逻辑/存储资本开支周期，而不是单季芯片出货。",
               ["semiconductors", "ai-infrastructure"], ["TSM", "AMAT", "LRCX", "KLAC"]),
        _stock("AMAT", "Applied Materials", "应用材料", "半导体设备", "薄膜 / 制程设备",
               "覆盖沉积、刻蚀与检测的半导体设备平台公司。",
               "Applied Materials 比 ASML 更分散，能同时反映逻辑、DRAM/HBM 和先进封装设备需求。FResearch 用它观察资本开支是“GPU 独有”还是“全产业链扩产”。",
               ["semiconductors"], ["LRCX", "KLAC", "ASML", "TSM"]),
        _stock("LRCX", "Lam Research", "泛林集团", "半导体设备", "刻蚀 / 沉积",
               "在刻蚀和沉积设备上深度暴露存储与先进制程。",
               "Lam 对 DRAM/NAND 资本开支更敏感。HBM 层数增加会改变刻蚀需求，因此它是 MU 与设备股之间的桥梁。",
               ["semiconductors", "hbm"], ["AMAT", "KLAC", "ASML", "MU"]),
        _stock("KLAC", "KLA Corporation", "科磊", "半导体设备", "过程控制 / 检测",
               "芯片制造过程控制与检测设备龙头。",
               "KLA 的订单往往滞后于扩产决策、但持续性更好。FResearch 把它作为先进制程良率和 HBM 复杂度提升的过程控制指标。",
               ["semiconductors"], ["AMAT", "LRCX", "ASML", "TSM"]),
        _stock("ANET", "Arista Networks", "飞塔网络", "通信设备", "数据中心以太网",
               "云数据中心高速交换与网络操作系统供应商。",
               "Arista 是 AI 集群网络侧的核心观察对象。当研报讨论 GPU 集群扩展时，以太网交换、800G 和网络操作系统会出现在 ANET 的目标价假设里。",
               ["ai-infrastructure", "ai"], ["AVGO", "MRVL", "NVDA", "CRWV"]),
        _stock("DELL", "Dell Technologies", "戴尔", "硬件", "服务器 / AI 机架",
               "企业服务器与 AI 机架集成商。",
               "Dell 把 GPU、存储和服务器集成卖给企业与云。外资研报关注的是 AI server 订单能见度，而不是 PC。FResearch 用它观察需求是否从云下沉到企业。",
               ["ai-infrastructure"], ["ANET", "VRT", "CRWV", "NVDA"]),
        _stock("CRWV", "CoreWeave", "CoreWeave", "云计算", "GPU 云 / AI 基础设施",
               "面向 AI 训练与推理的 GPU 云服务商。",
               "CoreWeave 代表“卖算力时长”而不是卖芯片。研究时需要同时看 GPU 供给、电力/数据中心约束和客户集中度，因此它和 NVDA、VRT 必须交叉阅读。",
               ["ai", "ai-infrastructure"], ["NVDA", "VRT", "MSFT", "AMZN"]),
        _stock("VRT", "Vertiv", "维谛技术", "电气设备", "数据中心供电与冷却",
               "数据中心电源、散热与机柜基础设施供应商。",
               "Vertiv 把 AI 资本开支翻译成电力和液冷订单。FResearch 关注它是否确认 GPU 交付正在变成可安装的机柜，而不是停留在芯片层面的叙事。",
               ["ai-infrastructure"], ["CRWV", "DELL", "NVDA", "ANET"]),
        _stock("MSFT", "Microsoft", "微软", "软件", "云 / AI 平台",
               "通过 Azure 和 OpenAI 相关产品把 AI 变成云收入的平台公司。",
               "微软是 AI 资本开支与云收入转化的核心样本。FResearch 追踪外资对其 Azure 增长、AI 变现和资本开支指引的评级变化，并对照 GOOGL、AMZN、META。",
               ["ai", "ai-infrastructure"], ["GOOGL", "AMZN", "META", "ORCL"]),
        _stock("GOOGL", "Alphabet", "谷歌", "互联网", "搜索 / 云 / AI",
               "搜索广告龙头，同时运营 Google Cloud 与自研 TPU。",
               "Alphabet 同时是 AI 应用层（搜索/广告）和基础设施层（TPU/云）公司。投行分歧常在广告周期与云加速之间，FResearch 用它对照微软的云 AI 变现路径。",
               ["ai", "ai-infrastructure"], ["MSFT", "AMZN", "META", "NVDA"]),
        _stock("AMZN", "Amazon", "亚马逊", "互联网", "电商 / AWS",
               "AWS 是全球最大公有云之一，也是 AI 训练与推理的主要买家。",
               "Amazon 的研究主线是 AWS 增长、Trainium/Inferentia 自研芯片和零售利润率。对 AI 基础设施来说，它既是 NVDA 的客户，也是定制硅的需求方。",
               ["ai", "ai-infrastructure"], ["MSFT", "GOOGL", "NVDA", "AVGO"]),
        _stock("META", "Meta Platforms", "Meta", "互联网", "社交 / AI 基础设施",
               "以广告变现，同时投入大规模开源模型与 AI 数据中心。",
               "Meta 的外资研报通常把广告复苏和 AI 资本开支放在同一张表里。FResearch 关注其 capex 指引是否转化为可观察的 GPU/网络/电力订单。",
               ["ai", "ai-infrastructure"], ["GOOGL", "MSFT", "AMZN", "NVDA"]),
        _stock("ORCL", "Oracle", "甲骨文", "软件", "数据库 / 云基础设施",
               "数据库软件公司，云基础设施订单因 AI 训练需求而重新定价。",
               "Oracle 近期研究焦点是云基础设施合同与 AI 训练集群，而不是传统许可。它提供了“软件公司被 AI capex 重估”的样本。",
               ["ai", "ai-infrastructure"], ["MSFT", "AMZN", "CRWV", "NVDA"]),
        _stock("IBM", "IBM", "IBM", "软件", "企业 IT / 混合云",
               "面向企业的混合云、软件与咨询公司。",
               "IBM 不是 AI 算力主线的核心贝塔，但企业 AI 落地、咨询和混合云订单会在研报里周期性出现。FResearch 用它观察 AI 是否进入传统企业预算。",
               ["ai"], ["MSFT", "ORCL", "PLTR", "CRM"]),
        _stock("PLTR", "Palantir", "Palantir", "软件", "数据平台 / AI 应用",
               "面向政府和商业客户的数据与 AI 应用平台。",
               "Palantir 代表应用层 AI，而不是芯片。外资研报争论的是商业化扩张速度与估值。FResearch 把它作为“AI 软件变现”对照，避免所有页面都写成 GPU 故事。",
               ["ai"], ["MSFT", "SNOW", "CRM", "IBM"]),
        _stock("SNOW", "Snowflake", "Snowflake", "软件", "数据云",
               "云数据平台，AI 功能建立在企业数据仓库之上。",
               "Snowflake 的研究问题是：企业把更多数据和工作负载放到数据云后，AI 产品能否提高净保留率。它连接存储、云和应用层。",
               ["ai"], ["PLTR", "MSFT", "AMZN", "CRM"]),
        _stock("AAPL", "Apple", "苹果", "消费电子", "终端 / 服务",
               "消费电子与服务平台，AI 主要体现在端侧与服务生态。",
               "Apple 不是数据中心 GPU 的直接映射。FResearch 仍覆盖它，是因为端侧 AI、服务利润率和对亚洲半导体供应链的需求会进入外资科技组的比较框架。",
               ["ai"], ["MSFT", "GOOGL", "TSM", "QCOM"]),
        _stock("TSLA", "Tesla", "特斯拉", "汽车", "电动车 / 机器人 / 自动驾驶",
               "电动车公司，市场对其 AI 叙事集中在 FSD、Dojo 与机器人。",
               "Tesla 的投行材料混合了汽车周期和 AI 期权。FResearch 只把它放在 AI 应用/机器人对照里，并明确区分交付量事实和未验证的机器人假设。",
               ["ai"], ["NVDA", "AMD", "AAPL", "MSFT"]),
        _stock("CRM", "Salesforce", "Salesforce", "软件", "CRM / 企业应用",
               "企业 CRM 龙头，正把生成式 AI 嵌入销售与服务工作流。",
               "Salesforce 用来观察企业应用软件能否把 AI 变成可续费功能，而不是一次性演示。它和 PLTR、MSFT 构成应用层三角。",
               ["ai"], ["MSFT", "PLTR", "SNOW", "ORCL"]),
    ]
}

for _ticker in HOT_SYMBOLS:
    STOCK_PROFILES.setdefault(
        _ticker,
        _stock(_ticker, _ticker, _ticker, "股票", "覆盖标的",
               f"{_ticker} 属于 FResearch AI 算力与半导体关注池。",
               f"FResearch 持续追踪外资投行对 {_ticker} 的评级、目标价与相关研报标题。",
               ["ai"], []),
    )

TOPIC_PAGES: dict[str, dict] = {
    "ai": {
        "slug": "ai",
        "title": "AI 股票与人工智能产业研究",
        "h1": "AI 产业研究",
        "description": "FResearch 从外资投行研报出发，拆解 AI 股票的算力、云、应用三层结构，覆盖英伟达、微软、博通等核心公司。",
        "one_liner": "AI 投资研究不应只停留在“概念股清单”，而应分成算力、基础设施和应用变现三层。",
        "stocks": ["NVDA", "AMD", "AVGO", "MSFT", "GOOGL", "AMZN", "META", "ORCL", "PLTR", "CRWV"],
        "sections": [
            ("AI 是什么，对股票意味着什么？",
             "在 FResearch 的框架里，AI 不是一个行业代码，而是一条资本开支与收入转化链条：模型训练需要 GPU/HBM/网络，推理需要云与电力，应用层需要把能力变成订阅或广告。读研报时先问：这家公司卖的是链条上的哪一层。"),
            ("算力层",
             "算力层以 NVIDIA 为中心，AMD、定制 ASIC（AVGO、AMZN、GOOGL）和代工（TSM）构成供给约束。外资目标价对这一层最敏感的变量通常是数据中心收入、毛利率和出货节奏。"),
            ("基础设施层",
             "没有网络、存储、电力和机柜，GPU 只是仓库里的芯片。ANET、MRVL、DELL、VRT、CRWV 以及 MU 的 HBM 都属于这一层。它们的订单能见度，往往比应用软件更早确认资本开支是否落地。"),
            ("应用与平台层",
             "MSFT、GOOGL、META、PLTR、CRM、SNOW 代表把模型变成云收入、广告或企业软件的路径。这一层研报更常争论变现速度，而不是晶圆产能。"),
            ("FResearch 怎么跟踪",
             "我们不生成一份“最佳 AI 股票”排行。我们把外资投行 30 天内的评级与目标价变动，按公司归集到各自的股票页，并在主题页上保持同一套分层，避免把存储、网络和软件混写成同一篇故事。"),
        ],
    },
    "semiconductors": {
        "slug": "semiconductors",
        "title": "半导体股票与产业链研究",
        "h1": "半导体产业链研究",
        "description": "从设计、代工、设备到存储，FResearch 用外资投行评级变化跟踪半导体周期，而不是只看单只 GPU 股票。",
        "one_liner": "半导体研究要按设计、制造、设备、存储拆开，否则很容易把所有涨价都解释成同一件事情。",
        "stocks": ["NVDA", "AMD", "AVGO", "MRVL", "INTC", "TSM", "ARM", "ASML", "AMAT", "LRCX", "KLAC"],
        "sections": [
            ("设计公司",
             "NVDA、AMD、AVGO、MRVL、ARM 卖的是架构、芯片或 IP。它们的研报关键变量是产品周期、份额和毛利率，而不是一座工厂的折旧。"),
            ("制造与代工",
             "TSM 是先进制程和先进封装的瓶颈；INTC 同时是设计公司和代工挑战者。产能、CoWoS 和客户结构会直接限制上游设计公司的出货上限。"),
            ("设备",
             "ASML、AMAT、LRCX、KLAC 把晶圆厂资本开支翻译成订单。逻辑扩产、DRAM/HBM 扩产和先进封装扩产，对这四家的影响并不相同。"),
            ("如何读评级",
             "同一周出现“上调 NVDA、下调设备股”并不矛盾：前者可能在交易产品周期，后者在交易资本开支峰值。FResearch 把两类事件分开放在对应股票页，而不是合成一句行业结论。"),
        ],
    },
    "hbm": {
        "slug": "hbm",
        "title": "HBM 与 AI 存储研究",
        "h1": "HBM / AI 存储研究",
        "description": "高带宽内存是 AI 服务器的关键瓶颈。FResearch 跟踪美光等存储公司的外资评级，并对照 NAND 与 HDD 的数据中心需求。",
        "one_liner": "HBM 把存储从传统周期股，部分改写成了 AI 服务器的配套瓶颈。",
        "stocks": ["MU", "WDC", "STX", "SNDK", "NVDA", "TSM", "LRCX"],
        "sections": [
            ("HBM 是什么？",
             "HBM（High Bandwidth Memory）是通过堆叠 DRAM 并向 GPU 提供极高带宽的内存。训练和部分推理负载会同时消耗算力和内存带宽，因此 HBM 供给会限制 GPU 出货。"),
            ("和普通 DRAM / NAND 有何不同？",
             "普通 DRAM 和 NAND 仍有消费电子与传统服务器周期；HBM 更贴近 AI 加速器的板级设计。把 MU 的故事直接套到 WDC 或 SNDK 上，会混淆两种完全不同的需求函数。"),
            ("FResearch 的用法",
             "看 MU 的目标价时，同时看 NVDA 的数据中心叙事和 LRCX 的存储设备订单。若只有 GPU 叙事、没有存储或设备确认，需要把结论的置信度下调。"),
        ],
    },
    "ai-infrastructure": {
        "slug": "ai-infrastructure",
        "title": "AI 基础设施与数据中心研究",
        "h1": "AI 基础设施研究",
        "description": "AI 资本开支会变成网络、服务器、电力、冷却和 GPU 云订单。FResearch 用外资研报跟踪这条落地链条。",
        "one_liner": "芯片订单只是 AI capex 的第一层；网络、电力和机柜决定它能不能变成可运行的集群。",
        "stocks": ["NVDA", "AVGO", "ANET", "MRVL", "DELL", "CRWV", "VRT", "MSFT", "AMZN", "GOOGL", "ORCL"],
        "sections": [
            ("资本开支如何落地",
             "微软、谷歌、亚马逊、Meta 的资本开支指引是需求侧；NVIDIA 是计算侧；Arista/Broadcom 是网络侧；Vertiv 是电力与冷却；Dell/CoreWeave 是交付形态。缺少其中一层，叙事就不完整。"),
            ("为什么要对着研报读",
             "同一家投行可能在上调 NVDA 的同时，对电力或网络公司更谨慎，因为交付瓶颈已经从芯片转移到机柜。FResearch 把这些评级分开放在股票页，主题页只负责说明结构。"),
        ],
    },
}

RESEARCH_LENS_PAGES: dict[str, dict] = {
    "price-targets": {
        "slug": "price-targets",
        "title": "投行目标价怎么看｜上调、下调与一致预期",
        "h1": "投行目标价与评级动作",
        "description": "解释投行目标价、评级上调与下调如何归集，区分单家机构目标价、分析师一致预期和未经验证的模型数字。",
        "one_liner": "先确认是谁、何时、把什么从多少调到多少，再讨论目标价意味着什么。",
        "stocks": HOT_SYMBOLS[:12],
        "sections": [
            ("一条目标价需要哪些证据？", "至少需要机构、股票、日期、动作与可回溯的公开来源。只有模型推断、没有共识或标题交叉验证的数字不会进入目标价卡。"),
            ("单家目标价和一致预期", "单家目标价反映某一机构的估值假设，一致预期是多位分析师的聚合。两者不能混写成同一个结论，页面会分别标注来源等级。"),
            ("上调目标价不等于上调评级", "机构可能维持 Hold 同时上调目标价，也可能维持 Buy 同时下调目标价。评级方向和目标价方向要分开阅读。"),
            ("下一步看市场验证", "公开动作只是事件起点。FResearch 再观察其后 1、5、20 个交易日相对 SPY 的表现，区分市场确认、未确认和样本不足。"),
        ],
    },
    "institution-views": {
        "slug": "institution-views",
        "title": "机构研报观点聚合｜共识、分歧与连续动作",
        "h1": "机构观点聚合",
        "description": "按高盛、摩根士丹利、摩根大通等机构聚合近期评级、目标价动作与公开研报观点，观察共识、分歧和边际变化。",
        "one_liner": "把同一机构近期对不同股票的公开动作放在一起，才看得见它的边际倾向。",
        "stocks": HOT_SYMBOLS[:12],
        "sections": [
            ("观点如何整合？", "只归纳公开标题、可公开摘要和结构化评级字段明确表达的内容；缺少理由时保留为空，不让模型补写机构没有说过的话。"),
            ("如何判断偏多或偏空？", "评级上调和目标价上调记为公开偏多动作；下调记为公开偏空动作；重申评级按评级本身的方向分类。"),
            ("为什么要看连续动作？", "单次调价可能是估值参数更新，连续覆盖多个标的或前后反转更能说明公开观点的边际变化，但仍不等于机构真实持仓。"),
        ],
    },
    "market-reaction": {
        "slug": "market-reaction",
        "title": "投行观点后的市场反应｜1、5、20日相对收益",
        "h1": "观点后的市场反应",
        "description": "追踪投行评级和目标价发布后股票 1、5、20 个交易日相对 SPY 的表现，识别市场确认、未确认与尚待观察。",
        "one_liner": "观点是否响亮不重要，随后价格是否同向、样本是否成熟更重要。",
        "stocks": HOT_SYMBOLS[:12],
        "sections": [
            ("市场反应怎么计算？", "从事件前一交易日收盘开始，计算其后 1、5、20 个交易日的复权收益，并与 SPY 同期收益比较。"),
            ("为什么只能说确认或未确认？", "事件通常只有日期、没有盘前盘后时间；同日还可能有财报或多家机构动作。因此相对收益是验证线索，不是单一事件的因果证明。"),
            ("观察窗口不够怎么办？", "未满对应交易日的事件显示等待验证，不拿当日快照冒充完整的 5 日或 20 日结果。"),
        ],
    },
    "institution-signals": {
        "slug": "institution-signals",
        "title": "机构公开信号与市场确认度｜审慎识别言行背离",
        "h1": "机构信号一致性",
        "description": "比较机构公开评级、目标价方向与后续市场表现，展示方向一致率、样本量和置信度，不推断未披露交易意图。",
        "one_liner": "把‘公开怎么说’与‘市场后来怎么走’分开，才能审慎讨论信号是否可信。",
        "stocks": HOT_SYMBOLS[:12],
        "sections": [
            ("什么叫公开信号背离？", "例如评级偏多但目标价下调，或公开偏多后股票相对 SPY 下跌。页面称其为信号混合或市场未确认，不称为出货或操纵。"),
            ("为什么不能推断真实意图？", "卖方研究、交易、投行和资管部门可能彼此独立。公开评级不能证明自营盘、客户盘或关联资管实体的持仓方向。"),
            ("置信度从哪里来？", "置信度取决于可验证事件数、成熟观察窗口、来源完整度和同日混杂因素。低样本只展示事实，不做机构排名。"),
        ],
    },
}


LEARN_PAGES: dict[str, dict] = {
    "pe-ratio": {
        "slug": "pe-ratio",
        "title": "市盈率 PE 是什么？",
        "h1": "市盈率（PE）是什么？",
        "description": "PE 用股价除以每股盈利。FResearch 说明它在 AI 与半导体股票上的误用，以及如何和投行目标价一起读。",
        "one_liner": "市盈率（PE Ratio）是股价除以每股盈利，用来比较市场愿意为每一单位利润支付的价格。",
        "related_learn": ["forward-pe", "price-target", "free-cash-flow"],
        "related_stocks": ["NVDA", "AAPL", "MSFT"],
        "sections": [
            ("直接答案",
             "PE = 股价 / 每股盈利（EPS）。例如股价 100 美元、EPS 5 美元，则 PE 为 20 倍。它回答的是“现在的价格对应多少倍利润”，不是“这只股票会不会涨”。"),
            ("PE 不能单独决定买不买",
             "高 PE 可能意味着增长预期很强，也可能意味着盈利暂时被压低。低 PE 可能便宜，也可能正在衰退。AI 半导体公司因为利润跳跃式增长，静态 PE 会迅速过时。"),
            ("和投行目标价的关系",
             "外资投行给出目标价时，经常在内部使用远期 PE 或 EV/EBITDA，而不是只报一个倍数。FResearch 的股票页优先展示评级与目标价变动，再提醒读者回到盈利和现金流假设。"),
            ("常见误读",
             "把 NVDA 的 PE 直接和银行股或公用事业股比较没有意义。跨行业比较 PE，必须先确认盈利质量和增长差是否可比。"),
        ],
    },
    "forward-pe": {
        "slug": "forward-pe",
        "title": "远期市盈率 Forward PE 是什么？",
        "h1": "远期市盈率（Forward PE）是什么？",
        "description": "Forward PE 用未来 12 个月预期盈利做分母。它比静态 PE 更接近投行给成长股定价的方式。",
        "one_liner": "Forward PE 使用分析师预期的未来盈利，而不是已经公布的过去盈利。",
        "related_learn": ["pe-ratio", "price-target", "dcf"],
        "related_stocks": ["NVDA", "AMD", "TSM"],
        "sections": [
            ("直接答案",
             "Forward PE = 当前股价 / 未来 12 个月（或下一财年）预期 EPS。它把市场定价和“尚未实现的盈利”连在一起。"),
            ("为什么成长股更常用它",
             "若一家公司未来一年盈利可能翻倍，静态 PE 会显得极贵，Forward PE 才会显示市场实际在为什么定价。AI 硬件股尤其如此。"),
            ("风险",
             "分母是预期，不是事实。预期下修时，Forward PE 会在股价还没动之前先恶化。读 FResearch 评级页时，要同时看目标价是上调还是下调，而不能只看倍数本身。"),
        ],
    },
    "free-cash-flow": {
        "slug": "free-cash-flow",
        "title": "自由现金流 FCF 是什么？",
        "h1": "自由现金流（FCF）是什么？",
        "description": "自由现金流是经营现金流减去资本开支后，公司真正可自由支配的现金。AI 资本开支周期里，FCF 往往比净利润更早发出信号。",
        "one_liner": "自由现金流（Free Cash Flow）= 经营现金流 − 资本开支。",
        "related_learn": ["dcf", "pe-ratio", "price-target"],
        "related_stocks": ["MSFT", "META", "NVDA"],
        "sections": [
            ("直接答案",
             "FCF 衡量公司在维持和扩张业务之后还剩多少现金。净利润包含折旧等非现金项目，FCF 更接近“口袋里还能剩多少钱”。"),
            ("AI capex 为什么会压低 FCF",
             "云厂商和部分半导体公司为了抢产能，会把资本开支抬到很高。利润可能仍在增长，但 FCF 暂时转弱。这不一定是坏事，但必须在估值里显式处理。"),
            ("怎么和研报一起用",
             "当投行上调目标价、同时公司指引大幅增加 capex 时，要问：增量开支有没有对应的增量回报假设。FResearch 不会在知识页里给买卖结论，只要求把 FCF 和目标价放在同一张逻辑表上。"),
        ],
    },
    "dcf": {
        "slug": "dcf",
        "title": "DCF 现金流折现是什么？",
        "h1": "DCF（现金流折现）是什么？",
        "description": "DCF 把未来自由现金流折成现值。理解它，才能看懂部分投行目标价背后的长期假设。",
        "one_liner": "DCF 是把未来自由现金流按折现率折回今天，得到企业价值的估值方法。",
        "related_learn": ["free-cash-flow", "price-target", "pe-ratio"],
        "related_stocks": ["MSFT", "GOOGL", "AAPL"],
        "sections": [
            ("直接答案",
             "DCF 的核心公式是：企业价值 ≈ Σ (未来各年 FCF / (1+r)^t) + 终值折现。r 是折现率，通常与 WACC 相关。"),
            ("对 AI 股票的限制",
             "若未来五年的收入和资本开支都高度不确定，DCF 对折现率和终值增长极其敏感。此时它更适合做情景比较（牛/熊），而不是给出一个精确“内在价值”。"),
            ("FResearch 的立场",
             "我们公开追踪的是投行评级与目标价，而不是用内部 DCF 替代市场。DCF 知识页的作用，是让读者知道目标价变动可能来自增长率、折现率或终值，而不是来自一个神秘数字。"),
        ],
    },
    "price-target": {
        "slug": "price-target",
        "title": "投行目标价是什么？",
        "h1": "投行目标价（Price Target）是什么？",
        "description": "目标价是卖方分析师在其假设下给出的未来价格锚。FResearch 说明如何读上调/下调，以及它不是投资建议。",
        "one_liner": "目标价是分析师基于其财务假设，给出的未来 12 个月左右价格锚，不是保证成交价。",
        "related_learn": ["analyst-rating", "forward-pe", "dcf"],
        "related_stocks": ["NVDA", "TSM", "AMD"],
        "sections": [
            ("直接答案",
             "Price Target 通常对应分析师对未来约 12 个月的估值结果。上调目标价意味着假设变得更乐观，或估值倍数被提高；下调则相反。"),
            ("目标价不是预测成交价",
             "不同投行模型不同，覆盖时点不同。同一天出现 120 和 180 并不自动意味着有人算错。FResearch 展示来源分级：一致预期、标题验证、以及需要交叉验证的推断。"),
            ("怎么读变动",
             "优先看方向和原因，而不是绝对数字。‘维持买入但下调目标价’和‘上调至买入’的信息含量不同。个股页会保留银行、新旧评级、目标价和时间。"),
        ],
    },
    "analyst-rating": {
        "slug": "analyst-rating",
        "title": "分析师评级是什么？",
        "h1": "分析师评级（Buy / Hold / Sell）是什么？",
        "description": "外资投行常用 Buy、Overweight、Neutral、Underweight 等词汇。FResearch 解释这些标签，以及升级/降级为什么重要。",
        "one_liner": "分析师评级是卖方对相对配置的建议标签，常用买入、中性、卖出或超配、标配、低配。",
        "related_learn": ["price-target", "pe-ratio"],
        "related_stocks": ["NVDA", "MSFT", "INTC"],
        "sections": [
            ("直接答案",
             "评级把分析师观点压缩成标签。Goldman、Morgan Stanley、JPM、BofA、Citi、UBS 等机构用词不完全统一：Overweight 近似偏多，Equal-weight/Neutral 近似中性，Underweight 近似偏空。"),
            ("升级和降级",
             "Upgrade / Downgrade 是标签本身的变化，通常比“维持买入但改目标价”更强。FResearch 首页和个股页都把这类事件单独列表，并标注时间。"),
            ("不要把评级当交易指令",
             "卖方有覆盖关系和模型约束。FResearch 是研究信息整理，不提供投资顾问服务。评级只是公开信息流的一层。"),
        ],
    },
    "ev-ebitda": {
        "slug": "ev-ebitda",
        "title": "EV/EBITDA 是什么？",
        "h1": "EV/EBITDA 是什么？",
        "description": "EV/EBITDA 用企业价值对经营利润做倍数比较，常用于资本结构不同的公司之间对照。",
        "one_liner": "EV/EBITDA 是企业价值除以息税折旧摊销前利润，用来比较不同杠杆公司的经营估值。",
        "related_learn": ["pe-ratio", "free-cash-flow", "dcf"],
        "related_stocks": ["DELL", "VRT", "AMZN"],
        "sections": [
            ("直接答案",
             "EV（Enterprise Value）通常是市值 + 净债务。EBITDA 近似经营现金流的粗口径。两者相除，得到不直接受资本结构扭曲的经营倍数。"),
            ("何时比 PE 更合适",
             "当两家公司负债水平差很多，或折旧政策不同时，PE 会失真。数据中心设备、服务器集成商等重资产公司更常出现 EV/EBITDA。"),
            ("限制",
             "EBITDA 忽略资本开支。对正在大举建设 AI 数据中心的公司，只看 EBITDA 会低估真实现金消耗，需要和 FCF 一起看。"),
        ],
    },
    "gross-margin": {
        "slug": "gross-margin",
        "title": "毛利率是什么？",
        "h1": "毛利率（Gross Margin）是什么？",
        "description": "毛利率衡量产品卖出后还剩多少覆盖研发和费用。AI 硬件股的毛利率变动经常先于评级调整出现在电话会里。",
        "one_liner": "毛利率 =（收入 − 营业成本）/ 收入，反映产品本身的盈利厚度。",
        "related_learn": ["pe-ratio", "free-cash-flow"],
        "related_stocks": ["NVDA", "AMD", "TSM"],
        "sections": [
            ("直接答案",
             "毛利率高，意味着每一美元收入在扣除直接成本后剩下更多。芯片设计公司通常高于代工和硬件集成商。"),
            ("AI 硬件为什么盯它",
             "新产品（如更高端加速器）若组合提升，毛利率会上升；若供给瓶颈缓解、折扣增加，毛利率会回落。不少目标价模型对毛利率的敏感度高于对收入的敏感度。"),
            ("和 FResearch 的关系",
             "我们不会在每个股票页编造毛利率预测。若研报标题或摘要提到 margin，会作为定性上下文出现；定量仍以可验证来源为准。"),
        ],
    },
    "capex": {
        "slug": "capex",
        "title": "资本开支 Capex 是什么？",
        "h1": "资本开支（Capex）是什么？",
        "description": "资本开支是公司投向厂房、设备、数据中心等长期资产的钱。AI 行情里，超大规模云厂商的 capex 指引是整条算力链最先被交易的变量。",
        "one_liner": "Capex = 公司购建长期资产的支出，AI 时代主要指数据中心、GPU 与电力。",
        "related_learn": ["free-cash-flow", "gross-margin", "price-target"],
        "related_stocks": ["MSFT", "GOOGL", "AMZN", "META", "NVDA", "VRT"],
        "sections": [
            ("直接答案",
             "资本开支（Capital Expenditure）是公司花在长期资产上的钱：盖厂、买设备、建数据中心。它不一次计入当期成本，而是分年折旧。和自由现金流的关系是：FCF ≈ 经营现金流 − Capex。"),
            ("为什么 AI 股盯 capex 指引",
             "微软、谷歌、亚马逊、Meta 每个季度给出的资本开支指引，决定了下游 NVIDIA 的订单、Arista/Broadcom 的网络需求、Vertiv 的电力与冷却订单。指引上调，整条链的目标价往往跟着动；指引放缓，先跌的也常常是同一批股票。"),
            ("capex 不是越高越好",
             "市场担心的是“投了但收不回”：如果 capex 高速增长而云收入转化跟不上，研报会开始质疑回报率（ROI），相关公司估值承压。所以读研报要同时看 capex 增速和收入转化，而不是只看绝对额。"),
            ("FResearch 怎么用",
             "我们把 capex 相关讨论当作需求侧信号。当外资研报因 capex 指引调整而集中上调/下调算力链目标价时，这些事件会按公司归集到各自股票页，帮助你看到“谁在交易 capex 见顶/见底”。"),
        ],
    },
    "earnings-revision": {
        "slug": "earnings-revision",
        "title": "盈利预期修正 Earnings Revision 是什么？",
        "h1": "盈利预期修正（Earnings Revision）是什么？",
        "description": "盈利预期修正是分析师上调或下调未来盈利预测的行为。它比单次评级动作更能解释股价的中期方向。",
        "one_liner": "Earnings Revision = 分析师上调/下调未来盈利预测，方向比一次评级动作更能驱动股价。",
        "related_learn": ["forward-pe", "analyst-rating", "price-target"],
        "related_stocks": ["NVDA", "AVGO", "MU", "TSM"],
        "sections": [
            ("直接答案",
             "每家投行都会对覆盖公司做未来几年的盈利模型。当新信息（财报、订单、capex 指引）出现，分析师会调高或调低这些预测，这就是盈利预期修正。Forward PE 的分母正是这些预期。"),
            ("为什么比评级更关键",
             "“维持买入但上调盈利预测”往往比“上调评级但不动模型”更能推高股价——因为目标价 = 预期盈利 × 目标倍数，真正变化的是分子。外资研报里 PTS（盈利超预期）和预期上修常同时出现。"),
            ("怎么识别方向",
             "同一季度内，若多家投行对同一家公司连续上修，通常意味着基本面超预期在被确认；若只有一家孤立上修，则置信度低。AI 半导体公司因为盈利基数快速变化，预期修正的幅度和频率都高于传统行业。"),
            ("FResearch 怎么用",
             "我们把评级与目标价变动按公司和日期归集。当某只股票在 30 天内被多家外资集中上调，你能在它的股票页看到这条“修正簇”，而不是只看到孤立的某一条新闻。"),
        ],
    },
    "buyback": {
        "slug": "buyback",
        "title": "股票回购 Buyback 是什么？",
        "h1": "股票回购（Buyback）是什么？",
        "description": "股票回购是公司用现金买回自家股票，减少流通股数从而提升每股指标。它是现金充裕的大科技公司回报股东的主要方式之一。",
        "one_liner": "Buyback = 公司用钱买回自家股票，流通股减少，EPS 等每股指标被动提升。",
        "related_learn": ["free-cash-flow", "pe-ratio", "capex"],
        "related_stocks": ["AAPL", "MSFT", "GOOGL", "META"],
        "sections": [
            ("直接答案",
             "回购（Share Buyback）是公司在二级市场买回自己的股票并注销或库存。流通股变少后，即使总利润不变，每股收益（EPS）也会上升。它和分红一样，都是把现金返还给股东的方式。"),
            ("回购什么时候是加分项",
             "当公司现金充裕、股价被低估时，回购是高效的资本配置；当公司靠借钱在高位回购，则可能损害长期价值。外资研报会评价回购的“价格是否划算”，而不只是回购的规模。"),
            ("和成长投入的平衡",
             "对 AI 公司，市场会同时看回购和 capex：一家公司若一边大额回购、一边又需要巨额 capex 投入数据中心，研报会分析其现金流能否两者兼顾。这是判断财务健康度的关键角度。"),
            ("FResearch 怎么用",
             "回购本身不是评级事件，但当外资研报把“回购 + 现金流”作为上调目标价的理由时，会出现在对应股票的评级与目标价变动里。我们把它作为定性上下文呈现，不单独编造回购数据。"),
        ],
    },
}

COMPARE_PAIRS: list[tuple[str, str]] = [
    ("NVDA", "AMD"),
    ("NVDA", "AVGO"),
    ("TSM", "INTC"),
    ("GOOGL", "MSFT"),
    ("AMAT", "LRCX"),
    ("WDC", "STX"),
]


def compare_slug(left: str, right: str) -> str:
    return f"{left.lower()}-vs-{right.lower()}"


COMPARE_PAGES = {compare_slug(a, b): (a, b) for a, b in COMPARE_PAIRS}

TRUST_PAGES = ("about", "methodology", "ai-methodology", "sources", "editorial-policy", "disclosures", "ai-info")


# ---------------------------------------------------------------------------
# Shared chrome
# ---------------------------------------------------------------------------

_GEO_ONLY_CSS = """
/* 子页专属：机构信号卡与目标价共识块 */
.meta-row {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 20px;
    color: var(--text-tertiary);
    font-size: 12.5px;
    margin-bottom: 24px;
}
.signal-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 16px;
    margin: 0 0 24px;
}
.signal-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: 20px 22px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.25s var(--ease-out);
}
.signal-card:hover { border-color: var(--border-strong); }
.signal-card:after {
    content: "";
    position: absolute;
    width: 110px;
    height: 110px;
    border: 1px solid var(--border);
    border-radius: 50%;
    right: -45px;
    bottom: -55px;
}
.signal-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-bottom: 10px;
}
.signal-head h2 {
    margin: 0;
    font-family: var(--font-serif);
    font-size: 20px;
    letter-spacing: 0;
    color: var(--text);
}
.signal-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 7px 14px;
    color: var(--text-tertiary);
    font-size: 12px;
    margin-bottom: 10px;
}

/* 目标价共识：现价 / 一致目标价 / 隐含空间 */
.consensus {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 1px;
    background: var(--border);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    overflow: hidden;
    margin: 0 0 20px;
}
.consensus-cell { background: var(--surface); padding: 18px 20px; }
.consensus-label {
    font-size: 11px;
    letter-spacing: 0.06em;
    color: var(--text-tertiary);
    margin-bottom: 7px;
}
.consensus-value {
    font-family: var(--font-mono);
    font-size: 25px;
    font-weight: 500;
    line-height: 1.15;
    color: var(--text);
    font-variant-numeric: tabular-nums;
}
.consensus-value.is-bull { color: var(--bull); }
.consensus-value.is-bear { color: var(--bear); }
.consensus-sub { margin-top: 6px; font-size: 11.5px; color: var(--text-tertiary); }

/* 目标价区间条 */
.pt-range { margin: 4px 0 0; }
.pt-range-bar {
    position: relative;
    height: 6px;
    border-radius: 999px;
    background: var(--surface-3);
    margin: 14px 0 8px;
}
.pt-range-span {
    position: absolute;
    top: 0;
    height: 6px;
    border-radius: 999px;
    background: linear-gradient(90deg, rgba(201, 164, 92, 0.35), var(--gold));
}
.pt-range-marker {
    position: absolute;
    top: -4px;
    width: 2px;
    height: 14px;
    background: var(--text);
    border-radius: 1px;
}
.pt-range-scale {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-tertiary);
}
.pt-range-legend {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-family: var(--font-sans);
    letter-spacing: 0.02em;
}
.pt-range-legend i {
    display: inline-block;
    border-radius: 2px;
}
.pt-legend-marker { width: 3px; height: 12px; background: var(--text-primary); }
.pt-legend-span { width: 16px; height: 4px; margin-left: 6px; background: var(--accent); opacity: 0.75; }
@media (max-width: 640px) {
    .pt-range-legend { display: none; }
}
.viewpoint-list { list-style: none; padding: 0; margin: 0; }
.viewpoint-list li {
    display: grid;
    grid-template-columns: 150px 1fr auto;
    gap: 10px 16px;
    align-items: baseline;
    padding: 12px 0;
    border-top: 1px solid var(--border);
}
.viewpoint-list li:first-child { border-top: 0; }
.viewpoint-bank { font-size: 14px; }
.viewpoint-text { color: var(--text-secondary); font-size: 13.5px; line-height: 1.65; }
.viewpoint-date { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-tertiary); }
@media (max-width: 760px) {
    .reaction-table { min-width: 860px; }
    .viewpoint-list li { grid-template-columns: 1fr; gap: 4px; }
}
"""

_GEO_CSS = PAGE_CSS + _GEO_ONLY_CSS


def site_nav_html(current: str = "") -> str:
    links = [
        ("/", "最新观点"),
        ("/stocks/", "股票目标价"),
        ("/institutions/", "机构观点"),
        ("/reactions/", "市场验证"),
        ("/methodology/", "研究方法"),
    ]
    items = []
    for href, label in links:
        current_attr = ' aria-current="page"' if current.rstrip("/") == href.rstrip("/") else ""
        items.append(f'<a href="{href}"{current_attr}>{label}</a>')
    return (
        '<nav class="site-nav" aria-label="站点">'
        f'<a class="brand" href="/">{SITE_NAME}</a>'
        + "".join(items)
        + '<span class="nav-spacer"></span>'
        + theme_switcher_html()
        + "</nav>"
    )


_THEME_ICONS = {
    "dark": '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
    "light": (
        '<circle cx="12" cy="12" r="4"/><line x1="12" y1="1" x2="12" y2="4"/>'
        '<line x1="12" y1="20" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>'
        '<line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="4" y2="12"/>'
        '<line x1="20" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>'
        '<line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>'
    ),
    "sepia": '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
}
_THEME_TITLES = {"dark": "暗色", "light": "明亮", "sepia": "护眼"}


def theme_switcher_html() -> str:
    buttons = []
    for theme, icon in _THEME_ICONS.items():
        active = " active" if theme == "dark" else ""
        title = _THEME_TITLES[theme]
        buttons.append(
            f'<button type="button" class="theme-btn{active}" data-theme="{theme}"'
            f' onclick="setTheme(\'{theme}\')" title="{title}" aria-label="{title}主题">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
            f' stroke-linecap="round" stroke-linejoin="round">{icon}</svg></button>'
        )
    return (
        '<div class="theme-switcher" role="group" aria-label="切换主题">'
        + "".join(buttons)
        + "</div>"
    )


# 子页没有首页那套脚本，主题切换需要自带一份最小实现（与首页同一个
# localStorage key，跨页切换保持一致）。
THEME_SCRIPT = """<script>
function setTheme(theme){
  if(['dark','light','sepia'].indexOf(theme)===-1){theme='dark';}
  document.documentElement.setAttribute('data-theme',theme);
  try{localStorage.setItem('ib-theme',theme);}catch(e){}
  document.querySelectorAll('.theme-btn').forEach(function(b){
    b.classList.toggle('active', b.dataset.theme===theme);
  });
}
(function(){var s='dark';try{s=localStorage.getItem('ib-theme')||'dark';}catch(e){}setTheme(s);})();
</script>"""


def site_footer_nav_html(include_disclaimer: bool = True) -> str:
    links = [
        ("/", "最新观点"),
        ("/stocks/", "股票目标价"),
        ("/institutions/", "机构观点"),
        ("/reactions/", "市场验证"),
        ("/topics/", "分析框架"),
        ("/methodology/", "研究方法"),
        ("/sources/", "数据来源"),
        ("/ai-methodology/", "AI 方法"),
        ("/editorial-policy/", "编辑标准"),
        ("/disclosures/", "披露"),
        ("/about/", "关于"),
    ]
    items = "".join(f'<a href="{href}">{label}</a>' for href, label in links)
    disclaimer = (
        '<p class="disclaimer">FResearch 是研究与信息整理平台，不提供投资顾问、经纪或交易执行服务。'
        "页面信息仅供研究与教育用途，不构成投资建议。</p>"
        if include_disclaimer
        else ""
    )
    return f'<nav class="footer-nav">{items}</nav>{disclaimer}'


def _breadcrumb(items: list[tuple[str, str]]) -> tuple[str, dict]:
    html_parts = []
    elements = []
    for index, (name, path) in enumerate(items, start=1):
        url = _abs(path)
        if index == len(items):
            html_parts.append(f"<span>{_safe_text(name)}</span>")
        else:
            html_parts.append(f'<a href="{_safe_attr(path)}">{_safe_text(name)}</a>')
        elements.append({"@type": "ListItem", "position": index, "name": name, "item": url})
    html = '<nav class="crumb">' + " / ".join(html_parts) + "</nav>"
    schema = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": elements}
    return html, schema


def seo_head(
    title: str,
    description: str,
    canonical_path: str,
    schemas: list[dict],
    date_modified: str = "",
    include_style: bool = True,
    robots_directive: str = "index,follow,max-image-preview:large,max-snippet:-1",
) -> str:
    canonical = _abs(canonical_path)
    og_image = _abs("/og-image.png")
    robots = robots_directive
    og_title = _safe_attr(title)
    og_desc = _safe_attr(description)
    og_type = "article" if any(str(item.get("@type")) == "Article" for item in schemas) else "website"
    modified_tag = (
        f'<meta property="article:modified_time" content="{_safe_attr(_iso_date(date_modified))}" />'
        if date_modified
        else ""
    )
    return f"""
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_safe_text(title)}</title>
<meta name="description" content="{og_desc}">
<meta name="google-site-verification" content="{GOOGLE_SITE_VERIFICATION}">
<meta name="robots" content="{robots}">
<link rel="canonical" href="{_safe_attr(canonical)}">
<link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2">
<link rel="apple-touch-icon" href="/apple-touch-icon.png?v=2">
<meta property="og:type" content="{og_type}">
<meta property="og:site_name" content="{SITE_NAME}">
<meta property="og:title" content="{og_title}">
<meta property="og:description" content="{og_desc}">
<meta property="og:url" content="{_safe_attr(canonical)}">
<meta property="og:locale" content="zh_CN">
<meta property="og:image" content="{_safe_attr(og_image)}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:type" content="image/png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:site" content="{X_HANDLE}">
<meta name="twitter:creator" content="{X_HANDLE}">
<meta name="twitter:title" content="{og_title}">
<meta name="twitter:description" content="{og_desc}">
<meta name="twitter:image" content="{_safe_attr(og_image)}">
<meta name="twitter:image:alt" content="{og_title}">
{modified_tag}
{"".join(_json_ld(item) for item in schemas)}
{f"<style>{_GEO_CSS}</style>" if include_style else ""}
"""


def render_page(
    title: str,
    description: str,
    canonical_path: str,
    body: str,
    schemas: list[dict],
    current_nav: str = "",
    date_modified: str = "",
    lang: str = "zh-CN",
    robots_directive: str = "index,follow,max-image-preview:large,max-snippet:-1",
) -> str:
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
{seo_head(title, description, canonical_path, schemas, date_modified, robots_directive=robots_directive)}
</head>
<body>
<div class="wrap">
{site_nav_html(current_nav)}
<main>{body}</main>
{site_footer_nav_html()}
</div>
{THEME_SCRIPT}
</body>
</html>"""


def organization_schema() -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": SITE_NAME,
        "alternateName": ["Fresearch", "外资投行研报"],
        "url": _abs("/"),
        "logo": _abs("/favicon.svg"),
        "email": CONTACT_EMAIL,
        "sameAs": [X_PROFILE_URL],
        "description": "聚合外资投行股票评级、目标价与公开研报观点，并追踪观点发布后的市场反应。",
    }


def website_schema() -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": SITE_NAME,
        "url": _abs("/"),
        "inLanguage": "zh-CN",
        "description": SITE_TAGLINE,
        "publisher": {"@type": "Organization", "name": SITE_NAME, "url": _abs("/")},
    }


def software_schema() -> dict:
    """只在 /about/ 使用——那一页的主体确实是这个工具本身。"""
    return {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "@id": _abs("/about/") + "#software",
        "name": SITE_NAME,
        "applicationCategory": "FinanceApplication",
        "applicationSubCategory": "Sell-side research tracker",
        "operatingSystem": "Web",
        "url": _abs("/"),
        "description": "追踪外资投行股票评级、目标价、公开研报观点及其后 1、5、20 个交易日市场反应。",
        "featureList": [
            "投行评级与目标价变动时间线",
            "公开观点发布后 1/5/20 日相对 SPY 超额收益",
            "按机构聚合的方向一致率",
        ],
        "isAccessibleForFree": True,
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
        "publisher": {"@type": "Organization", "name": SITE_NAME},
    }


def webpage_schema(name: str, description: str, path: str, page_type: str = "WebPage") -> dict:
    return {
        "@context": "https://schema.org",
        "@type": page_type,
        "name": name,
        "description": description,
        "url": _abs(path),
        "inLanguage": "zh-CN",
        "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": _abs("/")},
        "publisher": organization_schema(),
    }


def article_schema(headline: str, description: str, path: str, date_modified: str = "") -> dict:
    date_value = _iso_date(date_modified or CONTENT_PUBLISHED)
    return {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": headline,
        "description": description,
        "url": _abs(path),
        "inLanguage": "zh-CN",
        "datePublished": CONTENT_PUBLISHED,
        "dateModified": date_value,
        "author": {"@type": "Organization", "name": SITE_NAME, "url": _abs("/about/")},
        "publisher": organization_schema(),
        "mainEntityOfPage": {"@type": "WebPage", "@id": _abs(path)},
        "image": _abs("/og-image.png"),
    }


def _faq_schema(page: dict) -> Optional[dict]:
    """把页面里的"疑问式"内容转成 FAQPage 结构化数据,提高被精选摘要/AI 引用概率。

    取 h1(知识页多为疑问句, 如"市盈率(PE)是什么?")配 one_liner 作为首条问答,
    再加上 sections 里以"？"结尾的小节。少于 2 条有效问答则不出 schema。
    """
    qa: list[dict] = []

    def _add(question: str, answer: str) -> None:
        q = str(question).strip().rstrip("？?")
        a = str(answer).strip()
        if q and a:
            qa.append({
                "@type": "Question",
                "name": q,
                "acceptedAnswer": {"@type": "Answer", "text": a},
            })

    h1 = str(page.get("h1", "")).strip()
    if h1.endswith(("？", "?")):
        _add(h1, page.get("one_liner", ""))
    for heading, text in page.get("sections", []):
        if str(heading).strip().endswith(("？", "?")):
            _add(heading, text)
    # 单条问答的 FAQPage 不符合 Google 的富媒体条件，反而是低质结构化数据信号。
    if len(qa) < 2:
        return None
    return {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": qa[:6]}


def homepage_head_html(date_range: str = "", generated_at: str = "") -> str:
    title = f"投行目标价与研报观点追踪｜市场反应与机构情绪 - {SITE_NAME}"
    description = (
        "聚合高盛、摩根士丹利、摩根大通等机构对美股的评级、目标价与公开研报观点，"
        "追踪发布后的1、5、20日相对表现，观察机构公开表态与市场反应是否一致。"
    )
    if date_range and date_range not in {"N/A", ""}:
        description += f" 当前覆盖区间：{date_range}。"
    # 首页的主体是研报事件集合(CollectionPage)，不是一个"应用"。同页再挂
    # SoftwareApplication 属于类型错配，Google 只会当噪声忽略，还可能连带
    # 让整页标记不被信任；它只保留在 /about/ ——那页确实在介绍这个工具。
    schemas = [
        organization_schema(),
        website_schema(),
        webpage_schema(title, description, "/", "CollectionPage"),
    ]
    return seo_head(title, description, "/", schemas, generated_at, include_style=False)


def homepage_nav_html() -> str:
    return site_nav_html("/")


def homepage_footer_nav_html() -> str:
    return site_footer_nav_html(include_disclaimer=False)


# ---------------------------------------------------------------------------
# Report slicing
# ---------------------------------------------------------------------------

def extract_symbol_slice(report: dict, symbol: str) -> dict:
    symbol = symbol.upper()
    summary = _parse_summary(report)
    changes = [
        dict(item)
        for item in (summary.get("Rating_Changes") or [])
        if isinstance(item, dict) and str(item.get("Symbol") or "").strip().upper() == symbol
    ]
    targets = [
        item
        for item in (summary.get("Asset_Targets") or [])
        if isinstance(item, dict)
        and str(item.get("Asset") or "").strip().upper() == symbol
        and (
            item.get("_Source") in {"av_consensus", "verified_headline", "verified_summary", "yahoo_structured"}
            or item.get("_Verified_PT")
            or item.get("_Consensus")
        )
    ]
    news_items = []
    raw_news = ((report.get("raw") or {}).get("news") or []) if isinstance(report, dict) else []
    for item in raw_news:
        if not isinstance(item, dict):
            continue
        tickers = item.get("tickers") or []
        ticker_hit = any(
            str((t or {}).get("ticker") if isinstance(t, dict) else t).upper() == symbol
            for t in tickers
        )
        blob = f"{item.get('title', '')} {item.get('summary', '')}".upper()
        symbol_hit = re.search(
            rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])",
            blob,
        ) is not None
        if ticker_hit or symbol_hit:
            news_items.append(item)
        if len(news_items) >= 8:
            break
    url_by_title = {
        str(item.get("title") or "").strip(): str(item.get("url") or "").strip()
        for item in raw_news
        if isinstance(item, dict) and item.get("title") and item.get("url")
    }
    for change in changes:
        headline = str(change.get("_Headline") or "").strip()
        if headline and not change.get("_Source_URL"):
            change["_Source_URL"] = url_by_title.get(headline, "")
    history = ((report.get("market_context") or {}).get("history") or {}) if isinstance(report, dict) else {}
    series = history.get(symbol) or []
    last_close = None
    last_close_date = ""
    if isinstance(series, list):
        for point in reversed(series):
            if not isinstance(point, dict):
                continue
            try:
                last_close = float(point.get("close"))
            except (TypeError, ValueError):
                continue
            last_close_date = str(point.get("date") or "")
            break
    return {
        "changes": changes,
        "targets": targets,
        "news": news_items,
        "generated_at": _report_generated_at(report),
        "core_thesis": str(summary.get("Core_Thesis") or ""),
        "last_close": last_close,
        "last_close_date": last_close_date,
    }


def _to_price(value: Any) -> Optional[float]:
    """从 '$182.50' / '182.5' / 182.5 里取出数字。"""
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _plausible_targets(
    targets: list[tuple[str, float]]
) -> tuple[list[tuple[str, float]], int]:
    """剔除明显离群的目标价，返回 (保留项, 被剔除数量)。

    上游标题解析偶尔会吐出截断或串标的的数字（实测有 NVDA 的 `$31`
    和 `$1,150`）。单个离群值就能把共识区间拉成毫无意义的宽带，所以按
    中位数的 1/3 ~ 3 倍设可信带；剔除数量会在页面上如实标注。
    """
    if len(targets) < 3:
        return targets, 0
    prices = sorted(price for _, price in targets)
    median = prices[len(prices) // 2]
    if median <= 0:
        return targets, 0
    low, high = median / 3, median * 3
    kept = [(bank, price) for bank, price in targets if low <= price <= high]
    return (kept, len(targets) - len(kept)) if kept else (targets, 0)


def _consensus_panel(slice_data: dict, symbol: str) -> str:
    """现价 / 一致目标价 / 隐含空间 三联卡 + 各家目标价区间条。

    这是股票页上最该被一眼看到的东西，之前埋在 kv 表和项目符号里。
    """
    last_close = slice_data.get("last_close")
    targets = slice_data.get("targets") or []
    consensus_pt = None
    week_low = week_high = None
    if targets:
        consensus = targets[0].get("_Consensus") or {}
        consensus_pt = _to_price(consensus.get("analyst_target_price")) or _to_price(
            targets[0].get("_Verified_PT") or targets[0].get("Target_Price")
        )
        week_low = _to_price(consensus.get("week_52_low"))
        week_high = _to_price(consensus.get("week_52_high"))

    # 各家投行公开目标价，用来画共识区间
    raw_targets = []
    for change in slice_data.get("changes") or []:
        price = _to_price(change.get("Price_Target"))
        if price:
            raw_targets.append((str(change.get("Bank") or "—"), price))
    bank_targets, dropped = _plausible_targets(raw_targets)

    if last_close is None and consensus_pt is None and not bank_targets:
        return ""

    cells = []
    if last_close is not None:
        as_of = slice_data.get("last_close_date") or ""
        cells.append(
            '<div class="consensus-cell"><div class="consensus-label">最新收盘</div>'
            f'<div class="consensus-value">${last_close:,.2f}</div>'
            f'<div class="consensus-sub">复权收盘 · {_safe_text(as_of)}</div></div>'
        )
    if consensus_pt is not None:
        cells.append(
            '<div class="consensus-cell"><div class="consensus-label">一致目标价</div>'
            f'<div class="consensus-value">${consensus_pt:,.2f}</div>'
            '<div class="consensus-sub">Alpha Vantage 卖方一致预期</div></div>'
        )
    if last_close and consensus_pt:
        implied = consensus_pt / last_close - 1
        tone = "is-bull" if implied > 0 else "is-bear" if implied < 0 else ""
        cells.append(
            '<div class="consensus-cell"><div class="consensus-label">隐含空间</div>'
            f'<div class="consensus-value {tone}">{implied:+.1%}</div>'
            '<div class="consensus-sub">一致目标价相对最新收盘</div></div>'
        )
    if bank_targets:
        low = min(price for _, price in bank_targets)
        high = max(price for _, price in bank_targets)
        note = f"{len(bank_targets)} 家近 30 日公开目标价"
        if dropped:
            note += f" · 已剔除 {dropped} 个异常值"
        # 只有一个价位时写成 "$455–$455" 像坏数据，直接改成单值口径。
        if low == high:
            label, value = "公开目标价", f"${low:,.0f}"
        else:
            label, value = "公开目标价区间", f"${low:,.0f}–${high:,.0f}"
        cells.append(
            f'<div class="consensus-cell"><div class="consensus-label">{label}</div>'
            f'<div class="consensus-value">{value}</div>'
            f'<div class="consensus-sub">{_safe_text(note)}</div></div>'
        )

    range_html = ""
    scale_low = min(
        [value for value in (week_low, last_close) if value]
        + [price for _, price in bank_targets]
        or [0]
    )
    scale_high = max(
        [value for value in (week_high, last_close) if value]
        + [price for _, price in bank_targets]
        or [1]
    )
    # 只有一个目标价时区间条退化成一条线，反而误导；至少两个不同价位才画。
    if len({price for _, price in bank_targets}) >= 2 and scale_high > scale_low:
        span = scale_high - scale_low

        def _pos(value: float) -> float:
            return max(0.0, min(100.0, (value - scale_low) / span * 100))

        low = min(price for _, price in bank_targets)
        high = max(price for _, price in bank_targets)
        marker = (
            f'<span class="pt-range-marker" style="left:{_pos(last_close):.1f}%" '
            f'title="最新收盘 ${last_close:,.2f}"></span>'
            if last_close
            else ""
        )
        range_html = f"""
<div class="pt-range">
  <div class="pt-range-bar">
    <span class="pt-range-span" style="left:{_pos(low):.1f}%;width:{max(1.0, _pos(high) - _pos(low)):.1f}%"></span>
    {marker}
  </div>
  <div class="pt-range-scale"><span>${scale_low:,.0f}</span>
    <span class="pt-range-legend"><i class="pt-legend-marker"></i>最新收盘<i class="pt-legend-span"></i>公开目标价区间</span>
    <span>${scale_high:,.0f}</span></div>
</div>"""

    return f'<div class="consensus">{"".join(cells)}</div>{range_html}'


def _institution_intelligence(report: dict) -> dict:
    cached = report.get("institution_intelligence", {}) if isinstance(report, dict) else {}
    summary = _parse_summary(report)
    raw_news = ((report.get("raw") or {}).get("news") or []) if isinstance(report, dict) else []
    url_by_headline = {
        str(item.get("title") or "").strip(): str(item.get("url") or "").strip()
        for item in raw_news
        if isinstance(item, dict) and item.get("title") and item.get("url")
    }
    changes = []
    for raw in summary.get("Rating_Changes", []) or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        headline = str(item.get("_Headline") or "").strip()
        if headline and not item.get("_Source_URL"):
            item["_Source_URL"] = url_by_headline.get(headline, "")
        changes.append(item)
    context = report.get("market_context", {}) if isinstance(report, dict) else {}
    history = context.get("history", {}) if isinstance(context, dict) else {}
    if not changes and isinstance(cached, dict) and cached.get("version"):
        return cached
    return build_institution_intelligence(
        changes,
        history,
        benchmark="SPY",
        as_of=_report_generated_at(report),
        universe=HOT_SYMBOLS,
    )


def _logo_img(symbol: str, css_class: str = "logo") -> str:
    return (
        f'<img class="{css_class}" src="/assets/logos/{quote(symbol, safe="-")}.svg?v=2" '
        f'alt="" width="36" height="36">'
    )


def _stock_chips(tickers: list[str], exclude: str = "") -> str:
    chips = []
    seen = set()
    for ticker in tickers:
        ticker = str(ticker or "").upper()
        if not ticker or ticker == exclude.upper() or ticker in seen or ticker not in HOT_SYMBOLS:
            continue
        seen.add(ticker)
        profile = STOCK_PROFILES.get(ticker, {})
        label = profile.get("name_zh") or ticker
        chips.append(
            f'<a class="chip" href="/stocks/{ticker.lower()}/">{_logo_img(ticker)}'
            f'{_safe_text(ticker)} · {_safe_text(label)}</a>'
        )
    return f'<div class="chip-row">{"".join(chips)}</div>' if chips else ""


def _changes_table(changes: list[dict]) -> str:
    if not changes:
        return '<p class="empty">最近 30 天公开来源中，尚未记录到该标的的外资评级或目标价变动。</p>'
    rows = []
    for item in changes:
        action = str(item.get("Action") or "")
        action_label = ACTION_LABELS.get(action, action.replace("_", " ") or "—")
        source_url = str(item.get("_Source_URL") or "")
        source_label = "已验证来源" if item.get("_Source") else "来源待补"
        source_html = _safe_text(source_label)
        if source_url.startswith("https://"):
            source_html = f'<a href="{_safe_attr(source_url)}" rel="noopener noreferrer">{source_html} ↗</a>'
        rows.append(
            "<tr>"
            f'<td>{_safe_text(item.get("Bank") or "—")}</td>'
            f'<td>{_safe_text(action_label)}</td>'
            f'<td>{_safe_text(item.get("Old_Rating") or "—")}</td>'
            f'<td>{_safe_text(item.get("New_Rating") or "—")}</td>'
            f'<td class="mono">{_safe_text(item.get("Price_Target") or "—")}</td>'
            f'<td class="mono">{_safe_text(str(item.get("Time") or "")[:10])}</td>'
            f'<td>{source_html}</td>'
            "</tr>"
        )
    return (
        "<table><caption class=\"sr-only\">外资评级与目标价动作</caption>"
        "<thead><tr><th>投行</th><th>动作</th><th>原评级</th>"
        "<th>新评级</th><th>目标价</th><th>日期</th><th>证据</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _news_list(news_items: list[dict]) -> str:
    if not news_items:
        return '<p class="empty">暂无与该标的直接相关的已抓取投行新闻标题。</p>'
    rows = []
    for item in news_items:
        title = item.get("title") or "未命名"
        url = str(item.get("url") or "")
        source = item.get("source") or item.get("bank") or ""
        safe_title = _safe_text(title)
        if url.startswith("https://"):
            title_html = f'<a href="{_safe_attr(url)}" rel="noopener noreferrer">{safe_title}</a>'
        else:
            title_html = safe_title
        rows.append(f"<li>{title_html} <span class='muted'>{_safe_text(source)}</span></li>")
    return f'<ul class="source-list">{"".join(rows)}</ul>'


def _target_panel(targets: list[dict]) -> str:
    if not targets:
        return '<p class="empty">当前报告未给出该标的的共识或已验证目标价。</p>'
    item = targets[0]
    consensus = item.get("_Consensus") or {}
    rows = [
        ("展示目标价", item.get("Target_Price") or "—"),
        ("来源分级", item.get("_Source") or "—"),
        ("已验证目标价", item.get("_Verified_PT") or "—"),
        ("已验证机构", item.get("_Verified_Bank") or "—"),
    ]
    if consensus:
        rows.extend(
            [
                ("一致目标价", consensus.get("analyst_target_price") or "—"),
                ("52 周区间", f"{consensus.get('week_52_low') or '—'} – {consensus.get('week_52_high') or '—'}"),
            ]
        )
    kv = "".join(
        f"<div>{_safe_text(k)}</div><div>{_safe_text(v)}</div>" for k, v in rows if v and v != "—"
    )
    return f'<div class="kv">{kv or "<div>目标价</div><div>—</div>"}</div>'


def _percent(value: Any, *, signed: bool = True) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:+.1%}" if signed else f"{number:.0%}"


def _reaction_table(events: list[dict], include_institution: bool = True) -> str:
    if not events:
        return '<p class="empty">暂无可对齐的关注池事件。</p>'
    rows = []
    for event in events[:40]:
        reaction = event.get("reaction", {}) or {}
        excess = reaction.get("excess_returns", {}) or {}
        returns = reaction.get("returns", {}) or {}
        alignment = str(reaction.get("alignment") or "pending")
        reaction_class = f"reaction-{alignment}"
        source_url = str(event.get("source_url") or "")
        action = _safe_text(event.get("action_label") or event.get("action") or "—")
        if source_url.startswith("https://"):
            action = f'<a href="{_safe_attr(source_url)}" rel="noopener noreferrer">{action} ↗</a>'
        institution_cell = (
            f'<td><a href="/institutions/{_safe_attr(event.get("institution_slug"))}/">'
            f'{_safe_text(event.get("institution"))}</a></td>'
            if include_institution
            else ""
        )
        rows.append(
            "<tr>"
            + institution_cell
            + f'<td><a href="/stocks/{_safe_attr(str(event.get("symbol") or "").lower())}/">{_safe_text(event.get("symbol"))}</a></td>'
            + f'<td>{action}</td>'
            + f'<td class="mono">{_safe_text(event.get("price_target") or "—")}</td>'
            + f'<td class="mono">{_safe_text(_percent(excess.get("1d")))}</td>'
            + f'<td class="mono">{_safe_text(_percent(excess.get("5d")))}</td>'
            + f'<td class="mono">{_safe_text(_percent(excess.get("20d")))}</td>'
            + f'<td class="{reaction_class}">{_safe_text(reaction.get("alignment_label") or "等待验证")}</td>'
            + f'<td class="mono muted">{_safe_text(event.get("date") or "")}</td>'
            + "</tr>"
        )
    first_heading = "<th>机构</th>" if include_institution else ""
    table = (
        '<table class="reaction-table"><thead><tr>'
        + first_heading
        + "<th>股票</th><th>公开动作</th><th>目标价</th><th>1D 相对</th><th>5D 相对</th><th>20D 相对</th><th>市场验证</th><th>日期</th>"
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    if len(events) > 40:
        table += (
            f'<p class="muted table-note">按日期显示最近 40 条 / 共 {len(events)} 条；'
            "总样本仍计入上方机构画像与市场验证指标。</p>"
        )
    return table


def _institution_cards(institutions: list[dict], limit: int = 20) -> str:
    cards = []
    for item in institutions[:limit]:
        tone = str(item.get("tone") or "中性")
        tone_class = "tone-bull" if tone == "偏多" else "tone-bear" if tone == "偏空" else "tone-mixed"
        rate = item.get("alignment_rate")
        cards.append(f"""
<article class="signal-card">
  <div class="signal-head">
    <h2><a href="/institutions/{_safe_attr(item.get('slug'))}/">{_safe_text(item.get('name'))}</a></h2>
    <span class="tone {tone_class}">公开倾向 · {_safe_text(tone)}</span>
  </div>
  <div class="signal-meta">
    <span>{int(item.get('event_count') or 0)} 条动作</span>
    <span class="tone-bull">{int(item.get('bullish') or 0)} 偏多</span>
    <span class="tone-bear">{int(item.get('bearish') or 0)} 偏空</span>
    <span>方向一致率 {_safe_text(_percent(rate, signed=False) if rate is not None else '待验证')}</span>
  </div>
  <p>{_safe_text(item.get('interpretation') or '')}</p>
  <p class="muted">验证样本 {int(item.get('market_evaluated') or 0)} · 置信度 {_safe_text(item.get('confidence') or '低')} · {_safe_text(item.get('latest_date') or '')}</p>
</article>
""")
    return f'<div class="signal-grid">{"".join(cards)}</div>' if cards else '<p class="empty">暂无机构观点样本。</p>'


# ---------------------------------------------------------------------------
# Page renderers
# ---------------------------------------------------------------------------

def render_stocks_index(report: dict) -> str:
    summary = _parse_summary(report)
    covered = {
        str(item.get("Asset") or "").upper()
        for item in (summary.get("Asset_Targets") or [])
        if isinstance(item, dict)
    }
    rows = []
    for ticker in HOT_SYMBOLS:
        profile = STOCK_PROFILES[ticker]
        mark = "有目标价" if ticker in covered else "跟踪中"
        rows.append(
            "<tr>"
            f'<td><a href="/stocks/{ticker.lower()}/">{_logo_img(ticker)}{_safe_text(ticker)}</a></td>'
            f'<td>{_safe_text(profile["name_zh"])} / {_safe_text(profile["name"])}</td>'
            f'<td>{_safe_text(profile["sector_zh"])}</td>'
            f'<td class="muted">{_safe_text(mark)}</td>'
            "</tr>"
        )
    crumb, crumb_schema = _breadcrumb([("首页", "/"), ("股票", "/stocks/")])
    title = "美股投行目标价与评级汇总｜最新研报观点 - FResearch"
    description = (
        "按股票汇总外资投行最新评级、目标价上调或下调、公开研报核心观点及发布后的市场反应，"
        "覆盖 NVDA、TSM、AVGO、MU 等重点标的。"
    )
    body = f"""
{crumb}
<h1>股票投行目标价与研报观点</h1>
<p class="one-liner">按股票查看机构最新评级、目标价动作、公开观点，以及观点后的市场反应。</p>
<p class="lede">当前只覆盖明确维护的重点股票池。没有外部参照的模型数字不会进入目标价卡；市场验证不足时会明确显示等待样本。</p>
<div class="panel">
<table>
<thead><tr><th>代码</th><th>公司</th><th>赛道</th><th>状态</th></tr></thead>
<tbody>{"".join(rows)}</tbody>
</table>
</div>
"""
    schemas = [
        webpage_schema(title, description, "/stocks/", "CollectionPage"),
        crumb_schema,
        organization_schema(),
    ]
    return render_page(title, description, "/stocks/", body, schemas, "/stocks/", _report_generated_at(report))


def render_stock_page(symbol: str, report: dict) -> str:
    symbol = symbol.upper()
    profile = STOCK_PROFILES.get(symbol)
    if profile is None or symbol not in HOT_SYMBOLS:
        abort(404)
    slice_data = extract_symbol_slice(report, symbol)
    intelligence = _institution_intelligence(report)
    symbol_events = [
        item
        for item in (intelligence.get("events", []) or [])
        if isinstance(item, dict) and item.get("symbol") == symbol
    ]
    path = f"/stocks/{symbol.lower()}/"
    title = (
        f"{profile['name_zh']}（{symbol}）投行目标价与评级｜研报观点及市场反应 - {SITE_NAME}"
    )
    description = (
        f"汇总{profile['name_zh']}（{symbol}）近期投行评级、目标价变动与公开研报观点，"
        "并追踪观点发布后1、5、20个交易日相对SPY的表现。"
    )
    crumb, crumb_schema = _breadcrumb(
        [("首页", "/"), ("股票", "/stocks/"), (f"{symbol}", path)]
    )
    takeaways = [
        profile["one_liner"],
        f"FResearch 将 {symbol} 放在{profile['sector_zh']} / {profile['industry']}框架下跟踪，而不是写成孤立新闻。",
    ]
    if slice_data["changes"]:
        latest = slice_data["changes"][0]
        latest_action = str(latest.get("Action") or "")
        takeaways.append(
            f"最近公开记录包括 {latest.get('Bank') or '投行'} 对 {symbol} 的 "
            f"{ACTION_LABELS.get(latest_action, latest_action.replace('_', ' ')) or '评级变动'}"
            f"（{str(latest.get('Time') or '')[:10] or '日期未标注'}）。"
        )
    else:
        takeaways.append(f"最近 30 天账本尚未记录到 {symbol} 的评级变动，页面仍作为长期研究入口保留。")
    related = [t for t in profile.get("related") or [] if t in STOCK_PROFILES]
    topic_links = "".join(
        f'<a class="chip" href="/topics/{slug}/">{_safe_text(RESEARCH_LENS_PAGES[slug]["h1"])}</a>'
        for slug in ("price-targets", "institution-views", "market-reaction", "institution-signals")
    )
    institution_groups: dict[str, list[dict]] = {}
    for event in symbol_events:
        institution_groups.setdefault(str(event.get("institution_slug") or ""), []).append(event)
    viewpoint_items = []
    for slug, events in sorted(
        institution_groups.items(), key=lambda item: item[1][0].get("date", ""), reverse=True
    ):
        latest = events[0]
        bullish = sum(event.get("stance") == "bullish" for event in events)
        bearish = sum(event.get("stance") == "bearish" for event in events)
        tone = "偏多" if bullish > bearish else "偏空" if bearish > bullish else "分歧"
        evidence = latest.get("headline") or (
            f"最新公开动作：{latest.get('action_label') or latest.get('action') or '—'}"
        )
        # 每条都以「近 30 日公开倾向 X ·」开头会让整页变成同一句话复读，
        # 对读者和抓取都是噪音；倾向改成徽章，正文只留证据本身。
        tone_class = {"偏多": "tone-bull", "偏空": "tone-bear"}.get(tone, "tone-mixed")
        viewpoint_items.append(
            "<li>"
            f'<span class="viewpoint-bank">'
            f'<a href="/institutions/{_safe_attr(slug)}/">{_safe_text(latest.get("institution"))}</a>'
            f' <span class="tone {tone_class}">{_safe_text(tone)}</span></span>'
            f'<span class="viewpoint-text">{_safe_text(evidence)}</span>'
            f'<span class="viewpoint-date">{_safe_text(latest.get("date"))}</span>'
            "</li>"
        )
    viewpoint_html = (
        f'<ul class="viewpoint-list">{"".join(viewpoint_items)}</ul>'
        if viewpoint_items
        else '<p class="empty">最近 30 天暂无可归集的机构观点。</p>'
    )
    consensus_html = _consensus_panel(slice_data, symbol)
    generated = slice_data["generated_at"]
    body = f"""
{crumb}
<h1>{_logo_img(symbol)}{_safe_text(profile["name_zh"])}（{_safe_text(symbol)}）投行目标价与观点验证</h1>
<p class="one-liner">{_safe_text(profile["one_liner"])}</p>
<div class="meta-row">
  <span>交易所：{_safe_text(profile["exchange"])}</span>
  <span>赛道：{_safe_text(profile["sector_zh"])} · {_safe_text(profile["industry"])}</span>
  <span>更新：{_safe_text(generated[:19] or "等待采集")}</span>
</div>
{consensus_html}
<div class="panel">
  <h2>一句话定位</h2>
  <p>{_safe_text(profile["summary"])}</p>
</div>
<div class="panel">
  <h2>本页要点</h2>
  <ul class="source-list">{"".join(f"<li>{_safe_text(item)}</li>" for item in takeaways)}</ul>
</div>
<div class="panel">
  <h2>机构观点整合</h2>
  {viewpoint_html}
</div>
<div class="table-wrap">
  {_changes_table(slice_data["changes"])}
</div>
<div class="panel">
  <h2>目标价口径与共识明细</h2>
  {_target_panel(slice_data["targets"])}
</div>
<div class="panel">
  <h2>观点发布后的市场反应</h2>
  {_reaction_table(symbol_events)}
  <div class="evidence-note">相对收益以 SPY 为基准，按事件日期做日线近似。市场未确认不代表机构存在未披露交易行为。</div>
</div>
<div class="panel">
  <h2>相关公开标题</h2>
  {_news_list(slice_data["news"])}
</div>
<div class="panel">
  <h2>研究维度</h2>
  <div class="chip-row">{topic_links or '<span class="muted">—</span>'}</div>
  <h2 style="margin-top:18px">相关标的</h2>
  {_stock_chips(related, exclude=symbol)}
</div>
<div class="panel">
  <h2>Sources / Methodology</h2>
  <ul class="source-list">
    <li><a href="/sources/">数据来源</a>：Finnhub、Yahoo Finance、Google News RSS、Alpha Vantage 等公开接口</li>
    <li><a href="/methodology/">研究方法</a>：按 (银行, 代码, 动作, 日期) 去重，目标价分一致预期 / 标题验证 / 交叉验证推断</li>
    <li><a href="/ai-methodology/">AI 使用方法</a>：LLM 只做归纳，不把未验证数字当事实</li>
    <li>Data as of: {_safe_text(generated[:19] or CONTENT_PUBLISHED)}</li>
  </ul>
</div>
"""
    schemas = [
        article_schema(title, description, path, generated),
        webpage_schema(title, description, path),
        crumb_schema,
        organization_schema(),
    ]
    return render_page(title, description, path, body, schemas, "/stocks/", generated)


def render_topics_index() -> str:
    cards = []
    for slug, page in RESEARCH_LENS_PAGES.items():
        cards.append(
            f'<div class="panel"><h2><a href="/topics/{slug}/">{_safe_text(page["h1"])}</a></h2>'
            f'<p>{_safe_text(page["one_liner"])}</p></div>'
        )
    crumb, crumb_schema = _breadcrumb([("首页", "/"), ("分析框架", "/topics/")])
    title = "投行研报分析框架｜目标价、机构观点与市场验证 - FResearch"
    description = "FResearch 用四个维度阅读投行研报：目标价与评级动作、机构观点聚合、观点后的市场反应和机构信号一致性。"
    body = f"""
{crumb}
<h1>投行研报分析框架</h1>
<p class="one-liner">从“机构说了什么”，一直读到“市场后来是否验证”。</p>
<p class="lede">原有产业科普不再作为站点主入口；核心研究对象是机构、股票、目标价、时间与后续价格反应。</p>
{"".join(cards)}
"""
    return render_page(
        title,
        description,
        "/topics/",
        body,
        [webpage_schema(title, description, "/topics/", "CollectionPage"), crumb_schema],
        "/topics/",
    )


def render_topic_page(slug: str) -> str:
    page = RESEARCH_LENS_PAGES.get(slug)
    if not page:
        abort(404)
    path = f"/topics/{slug}/"
    crumb, crumb_schema = _breadcrumb([("首页", "/"), ("分析框架", "/topics/"), (page["h1"], path)])
    sections = "".join(
        f'<div class="panel"><h2>{_safe_text(heading)}</h2><p>{_safe_text(text)}</p></div>'
        for heading, text in page["sections"]
    )
    body = f"""
{crumb}
<h1>{_safe_text(page["h1"])}</h1>
<p class="one-liner">{_safe_text(page["one_liner"])}</p>
{sections}
<div class="panel">
  <h2>进入关注股票</h2>
  {_stock_chips(page["stocks"])}
</div>
"""
    schemas = [
        article_schema(page["title"], page["description"], path),
        crumb_schema,
        organization_schema(),
    ]
    faq = _faq_schema(page)
    if faq:
        schemas.append(faq)
    return render_page(page["title"] + " | FResearch", page["description"], path, body, schemas, "/topics/")


def render_learn_index() -> str:
    items = []
    for slug, page in LEARN_PAGES.items():
        items.append(
            f'<div class="panel"><h2><a href="/learn/{slug}/">{_safe_text(page["h1"])}</a></h2>'
            f'<p>{_safe_text(page["one_liner"])}</p></div>'
        )
    crumb, crumb_schema = _breadcrumb([("首页", "/"), ("知识", "/learn/")])
    title = "金融知识库｜PE、FCF、DCF、目标价与评级 - FResearch"
    description = "用直接答案解释 PE、Forward PE、FCF、DCF、投行目标价与分析师评级，并链回 FResearch 股票研究页。"
    body = f"""
{crumb}
<h1>金融知识库</h1>
<p class="one-liner">问题 → 直接答案 → 解释 → 和研报怎么一起用。</p>
{"".join(items)}
"""
    return render_page(
        title,
        description,
        "/learn/",
        body,
        [webpage_schema(title, description, "/learn/", "CollectionPage"), crumb_schema],
        "/learn/",
        robots_directive="noindex,follow",
    )


def render_learn_page(slug: str) -> str:
    page = LEARN_PAGES.get(slug)
    if not page:
        abort(404)
    path = f"/learn/{slug}/"
    crumb, crumb_schema = _breadcrumb([("首页", "/"), ("知识", "/learn/"), (page["h1"], path)])
    sections = "".join(
        f'<div class="panel"><h2>{_safe_text(heading)}</h2><p>{_safe_text(text)}</p></div>'
        for heading, text in page["sections"]
    )
    related_learn = "".join(
        f'<a class="chip" href="/learn/{item}/">{_safe_text(LEARN_PAGES[item]["h1"])}</a>'
        for item in page.get("related_learn") or []
        if item in LEARN_PAGES
    )
    body = f"""
{crumb}
<h1>{_safe_text(page["h1"])}</h1>
<p class="one-liner">{_safe_text(page["one_liner"])}</p>
{sections}
<div class="panel">
  <h2>相关知识</h2>
  <div class="chip-row">{related_learn}</div>
  <h2 style="margin-top:18px">相关股票页</h2>
  {_stock_chips(page.get("related_stocks") or [])}
</div>
"""
    schemas = [
        article_schema(page["title"], page["description"], path),
        crumb_schema,
        organization_schema(),
    ]
    faq = _faq_schema(page)
    if faq:
        schemas.append(faq)
    return render_page(
        page["title"] + " | FResearch",
        page["description"],
        path,
        body,
        schemas,
        "/learn/",
        robots_directive="noindex,follow",
    )


def render_compare_index() -> str:
    rows = []
    for left, right in COMPARE_PAIRS:
        slug = compare_slug(left, right)
        lname = STOCK_PROFILES[left]["name_zh"]
        rname = STOCK_PROFILES[right]["name_zh"]
        rows.append(
            f'<div class="panel"><h2><a href="/compare/{slug}/">{_safe_text(left)} vs {_safe_text(right)}</a></h2>'
            f'<p>{_safe_text(lname)} 与 {_safe_text(rname)} 的研究对照：赛道、投行跟踪与目标价线索。</p></div>'
        )
    crumb, crumb_schema = _breadcrumb([("首页", "/"), ("对比", "/compare/")])
    title = "股票对比｜NVDA vs AMD 等研究对照 - FResearch"
    description = "FResearch 对比页只覆盖有明确研究意义的配对，例如英伟达对 AMD、台积电对英特尔，避免程序化空壳。"
    body = f"{crumb}<h1>股票对比</h1><p class='one-liner'>对照的是研究框架，不是买卖指令。</p>{''.join(rows)}"
    return render_page(
        title,
        description,
        "/compare/",
        body,
        [webpage_schema(title, description, "/compare/", "CollectionPage"), crumb_schema],
        "/compare/",
        robots_directive="noindex,follow",
    )


def render_compare_page(slug: str, report: dict) -> str:
    pair = COMPARE_PAGES.get(slug)
    if not pair:
        abort(404)
    left, right = pair
    path = f"/compare/{slug}/"
    title = f"{left} vs {right}：股票与研究对照 | FResearch"
    description = (
        f"对照 {STOCK_PROFILES[left]['name_zh']} 与 {STOCK_PROFILES[right]['name_zh']} 的赛道位置、"
        "外资评级跟踪和目标价线索。不构成买卖建议。"
    )
    crumb, crumb_schema = _breadcrumb(
        [("首页", "/"), ("对比", "/compare/"), (f"{left} vs {right}", path)]
    )
    left_slice = extract_symbol_slice(report, left)
    right_slice = extract_symbol_slice(report, right)

    def _pt(slice_data: dict) -> str:
        targets = slice_data["targets"]
        if not targets:
            return "—"
        return str(targets[0].get("Target_Price") or "—")

    def _change_count(slice_data: dict) -> str:
        return str(len(slice_data["changes"]))

    table = f"""
<table>
<thead><tr><th></th><th>{_safe_text(left)}</th><th>{_safe_text(right)}</th></tr></thead>
<tbody>
<tr><td>中文名</td><td>{_safe_text(STOCK_PROFILES[left]['name_zh'])}</td><td>{_safe_text(STOCK_PROFILES[right]['name_zh'])}</td></tr>
<tr><td>赛道</td><td>{_safe_text(STOCK_PROFILES[left]['industry'])}</td><td>{_safe_text(STOCK_PROFILES[right]['industry'])}</td></tr>
<tr><td>一句话</td><td>{_safe_text(STOCK_PROFILES[left]['one_liner'])}</td><td>{_safe_text(STOCK_PROFILES[right]['one_liner'])}</td></tr>
<tr><td>展示目标价</td><td class="mono">{_safe_text(_pt(left_slice))}</td><td class="mono">{_safe_text(_pt(right_slice))}</td></tr>
<tr><td>近 30 天评级事件</td><td class="mono">{_safe_text(_change_count(left_slice))}</td><td class="mono">{_safe_text(_change_count(right_slice))}</td></tr>
</tbody>
</table>
"""
    verdict = (
        f"{left} 更适合用来观察 {STOCK_PROFILES[left]['industry']}；"
        f"{right} 的核心研究问题在 {STOCK_PROFILES[right]['industry']}。"
        "两者不是简单的替代品，配置含义取决于投资者跟踪的是算力份额、上游产能还是云变现。"
    )
    generated = _report_generated_at(report)
    body = f"""
{crumb}
<h1>{_safe_text(left)} vs {_safe_text(right)}</h1>
<p class="one-liner">对照研究框架与公开投行跟踪，而不是给出买入/卖出指令。</p>
<div class="panel">{table}</div>
<div class="panel"><h2>FResearch Verdict</h2><p>{_safe_text(verdict)}</p></div>
<div class="panel">
  <h2>进入个股页</h2>
  {_stock_chips([left, right])}
</div>
"""
    schemas = [article_schema(title, description, path, generated), crumb_schema]
    return render_page(
        title,
        description,
        path,
        body,
        schemas,
        "/compare/",
        generated,
        robots_directive="noindex,follow",
    )


def render_institutions_index(report: dict) -> str:
    intelligence = _institution_intelligence(report)
    institutions = intelligence.get("institutions", []) or []
    sentiment = intelligence.get("market_sentiment", {}) or {}
    coverage = intelligence.get("market_reaction_coverage", {}) or {}
    generated = _report_generated_at(report)
    path = "/institutions/"
    title = "投行观点与目标价记录｜机构情绪及市场验证 - FResearch"
    description = (
        "按机构汇总近期股票评级、目标价调整与公开研报观点，比较公开倾向、后续调价和市场反应；"
        "不推断未披露的交易意图。"
    )
    crumb, crumb_schema = _breadcrumb([("首页", "/"), ("机构观点", path)])
    body = f"""
{crumb}
<h1>机构观点、目标价与市场验证</h1>
<p class="one-liner">把同一机构近期对不同股票的公开动作合并，再看后续市场是否同向。</p>
<p class="lede">“公开倾向”来自评级与目标价动作；“方向一致率”来自事件后相对 SPY 的日线表现。两者都不能证明机构自营盘或关联资管实体的真实持仓。</p>
<div class="metric-row">
  <div class="metric"><b>{len(institutions)}</b><span>有事件机构</span></div>
  <div class="metric"><b>{int(sentiment.get('event_count') or 0)}</b><span>关注池事件</span></div>
  <div class="metric"><b>{_safe_text(sentiment.get('label') or '中性')}</b><span>公开信号合计</span></div>
  <div class="metric"><b>{int(coverage.get('evaluated') or 0)}</b><span>方向性验证样本</span></div>
</div>
{_institution_cards(institutions)}
<div class="panel">
  <h2>最近公开动作与价格反应</h2>
  {_reaction_table(intelligence.get('events', []) or [])}
</div>
<div class="evidence-note">本页只聚合当前关注股票池内、机构和日期可识别的公开卖方动作。方向一致率样本不足时不做机构排名；同一股票同日多机构动作属于混杂样本。</div>
"""
    schemas = [
        webpage_schema(title, description, path, "CollectionPage"),
        crumb_schema,
        organization_schema(),
    ]
    return render_page(title, description, path, body, schemas, path, generated)


def render_institution_page(slug: str, report: dict) -> str:
    profile = TRACKED_INSTITUTIONS.get(slug)
    if not profile:
        abort(404)
    intelligence = _institution_intelligence(report)
    aggregate = next(
        (item for item in (intelligence.get("institutions", []) or []) if item.get("slug") == slug),
        None,
    )
    events = [
        item
        for item in (intelligence.get("events", []) or [])
        if isinstance(item, dict) and item.get("institution_slug") == slug
    ]
    generated = _report_generated_at(report)
    name = profile["name"]
    path = f"/institutions/{slug}/"
    title = f"{name}近期股票观点｜目标价、评级与市场反应 - FResearch"
    description = (
        f"查看{name}近30日对关注股票的评级与目标价动作、公开观点摘要及事件后的市场反应；"
        "不推断未披露交易意图。"
    )
    crumb, crumb_schema = _breadcrumb(
        [("首页", "/"), ("机构观点", "/institutions/"), (name, path)]
    )
    if aggregate:
        alignment = (
            _percent(aggregate.get("alignment_rate"), signed=False)
            if aggregate.get("alignment_rate") is not None
            else "待验证"
        )
        metrics = f"""
<div class="metric-row">
  <div class="metric"><b>{int(aggregate.get('event_count') or 0)}</b><span>近 30 日动作</span></div>
  <div class="metric"><b>{_safe_text(aggregate.get('tone') or '中性')}</b><span>公开倾向</span></div>
  <div class="metric"><b>{_safe_text(alignment)}</b><span>方向一致率</span></div>
  <div class="metric"><b>{_safe_text(aggregate.get('confidence') or '低')}</b><span>样本置信度</span></div>
</div>
<div class="panel"><h2>观点蒸馏</h2><p>{_safe_text(aggregate.get('interpretation') or '')}</p><p class="muted">覆盖股票：{_safe_text('、'.join(aggregate.get('symbols') or []) or '—')}</p></div>
"""
    else:
        metrics = '<div class="panel"><p class="empty">最近 30 天关注池内暂无可验证公开动作。</p></div>'
    body = f"""
{crumb}
<h1>{_safe_text(name)} 近期观点与市场反应</h1>
<p class="one-liner">{_safe_text(profile['description'])}</p>
{metrics}
<div class="panel">
  <h2>评级、目标价与市场验证</h2>
  {_reaction_table(events, include_institution=False)}
</div>
<div class="evidence-note">卖方研究、交易、投行与资产管理部门可能相互独立。本页分析的是公开表态及其后市场反应，不是对{name}持仓、出货、抄底或主观意图的认定。</div>
"""
    schemas = [article_schema(title, description, path, generated), crumb_schema, organization_schema()]
    return render_page(title, description, path, body, schemas, "/institutions/", generated)


def render_reactions_page(report: dict) -> str:
    intelligence = _institution_intelligence(report)
    events = intelligence.get("events", []) or []
    coverage = intelligence.get("market_reaction_coverage", {}) or {}
    generated = _report_generated_at(report)
    path = "/reactions/"
    title = "投行观点市场验证｜评级后1、5、20日股票反应 - FResearch"
    description = (
        "追踪投行评级和目标价发布后股票的1、5、20日相对收益，区分市场确认、公开观点未获确认与尚待观察。"
    )
    crumb, crumb_schema = _breadcrumb([("首页", "/"), ("市场验证", path)])
    mature = int(coverage.get("evaluated") or 0)
    neutral = int(coverage.get("neutral") or 0)
    pending = int(coverage.get("pending") or 0)
    body = f"""
{crumb}
<h1>投行观点发布后的市场反应</h1>
<p class="one-liner">观点发布是起点；1、5、20 个交易日后的相对收益才是可观察的验证。</p>
<div class="metric-row">
  <div class="metric"><b>{len(events)}</b><span>可识别事件</span></div>
  <div class="metric"><b>{mature}</b><span>方向性验证样本</span></div>
  <div class="metric"><b>{neutral} / {pending}</b><span>中性动作 / 等待窗口</span></div>
  <div class="metric"><b>SPY</b><span>相对收益基准</span></div>
</div>
<div class="panel">{_reaction_table(events)}</div>
<div class="panel">
  <h2>计算口径</h2>
  <ul class="source-list">
    <li>使用复权日线收盘价，从事件前一交易日收盘开始计算。</li>
    <li>相对表现 = 股票累计收益相对于 SPY 同期累计收益的几何差。</li>
    <li>公开方向与相对表现同向且幅度至少 1% 才记为市场确认；窗口未成熟显示等待验证。</li>
    <li>中性重申会展示价格反应，但不强行归入“确认”或“未确认”。</li>
    <li>事件通常只有日期，无法精确区分盘前与盘后；同日财报或多机构动作可能造成混杂。</li>
  </ul>
</div>
<div class="evidence-note">市场未确认只说明随后价格没有配合公开观点，不能证明机构在出货、抄底或操纵市场。</div>
"""
    schemas = [article_schema(title, description, path, generated), crumb_schema, organization_schema()]
    return render_page(title, description, path, body, schemas, path, generated)


def _trust_body(slug: str) -> tuple[str, str, str, str]:
    banks = "、".join(TARGET_BANKS[:12])
    symbols = "、".join(HOT_SYMBOLS[:12]) + " 等"
    pages = {
        "about": (
            "关于 FResearch",
            "FResearch 聚合外资投行股票评级、目标价与公开研报观点，并追踪观点发布后的市场反应。",
            "FResearch 是什么？",
            f"""
<p class="one-liner">FResearch 把公开的投行目标价、评级与研报观点，整理成可追溯、可验证的股票与机构研究页。</p>
<div class="panel">
  <h2>谁</h2>
  <p>FResearch（Fresearch）是独立研究整理站点，运营者公开联系方式为 {CONTACT_EMAIL}，社交账号 {X_HANDLE}。</p>
  <h2>是什么</h2>
  <p>投行观点验证引擎：聚合评级变动、目标价与公开研报标题，再把每次公开动作和其后 1、5、20 个交易日的市场反应对齐。</p>
  <h2>服务谁</h2>
  <p>需要跟踪 NVDA、TSM、AVGO 等重点股票的机构目标价、观点分歧与后续市场验证的投资者和研究者。</p>
  <h2>解决什么</h2>
  <p>把分散的 upgrade / downgrade / price target 事件变成按股票、机构和时间归集的公开记录，并明确区分事实、摘要与市场验证。</p>
</div>
<div class="panel">
  <h2>当前覆盖</h2>
  <p>关注池：{_safe_text(symbols)}。</p>
  <p>主要外资机构关键词：{_safe_text(banks)}。</p>
  <p>排除中国本土投行研报，避免把不可比的覆盖混在一起。</p>
</div>
""",
        ),
        "methodology": (
            "FResearch 研究方法",
            "FResearch 如何采集、去重、分级展示外资评级与目标价，以及为什么不把 LLM 数字直接当事实。",
            "研究方法",
            """
<p class="one-liner">先有可验证事件，再有摘要。数字必须能回溯到来源。</p>
<div class="panel">
  <h2>采集</h2>
  <p>定期从 Finnhub、Yahoo Finance、Google News RSS、Benzinga、Zacks、StreetInsider 等公开来源抓取评级与新闻标题。完整刷新与 4 小时快速刷新分离。</p>
  <h2>去重</h2>
  <p>评级事件按 (bank, symbol, action, date) 写入 30 天滚动账本，避免同一条升级在不同转载源重复计数。</p>
  <h2>目标价分级</h2>
  <p>av_consensus（Alpha Vantage 一致预期）与 verified_headline / verified_summary（公开标题或摘要正则）可进入展示。模型提取的数字只作为候选，没有外部参照时不进入目标价卡。</p>
  <h2>市场验证</h2>
  <p>使用复权日线，从事件前一交易日收盘起计算 1、5、20 个交易日收益，并与 SPY 同期收益比较。窗口未成熟显示等待验证。</p>
  <h2>机构信号</h2>
  <p>公开评级和目标价动作可用于描述“公开倾向”，但不能证明机构持仓或交易意图。卖方研究、交易、投行与资管部门可能相互独立。</p>
  <h2>为什么不用十万只股票程序化文章</h2>
  <p>金融内容属于 YMYL。FResearch 只为关注池内公司建立实体页，并要求页面能展示真实跟踪状态。这是为了避免无新增价值的规模化内容。</p>
</div>
""",
        ),
        "ai-methodology": (
            "FResearch 的 AI 使用方法",
            "FResearch 用大模型归纳研报事件，但目标价与评级以可验证来源为准。此处说明人机分工。",
            "AI 使用方法",
            """
<p class="one-liner">AI 负责归纳，证据负责约束。</p>
<div class="panel">
  <h2>How</h2>
  <p>Kimi 以 JSON mode 生成结构化核心观点，Qwen 作为国内网络环境下的兜底。摘要输入是已抓取的公开事件，而不是让模型凭空生成研报。</p>
  <h2>What AI is not allowed to do</h2>
  <p>不能单独决定展示一个未交叉验证的价格目标；不能把“分析服务暂时不可用”之类占位句包装成市场观点；不能为了 SEO 批量写没有数据的个股文。</p>
  <h2>Who</h2>
  <p>采集规则、银行名单、关注池和展示分级由站点维护者设定。模型不拥有最终数字解释权。</p>
  <h2>Why</h2>
  <p>金融稳定相关信息需要可核对。公开说明 AI 边界，是为了让 Google、ChatGPT 和其他引用者知道哪些句子是摘要，哪些是数据。</p>
</div>
""",
        ),
        "sources": (
            "FResearch 数据来源",
            "FResearch 使用的公开数据源清单，以及明确不接入的来源。",
            "数据来源",
            """
<p class="one-liner">只使用能稳定返回、且允许被引用为公开信息的来源。</p>
<div class="panel">
  <h2>当前接入</h2>
  <ul class="source-list">
    <li>Finnhub：公司新闻与推荐数据</li>
    <li>Yahoo Finance：评级回填与补充行情</li>
    <li>Google News RSS：投行相关标题，含部分韩语来源的本土目标价线索</li>
    <li>Alpha Vantage：分析师一致目标价与评级分布（按日缓存）</li>
    <li>Benzinga / Zacks / StreetInsider：公开评级标题补充</li>
  </ul>
  <h2>明确不接入</h2>
  <p>Barron's（实测 401）、Seeking Alpha PerimeterX（403）、TipRanks（403）等免费方案不可用的源不会 silently 重试后伪装成数据。</p>
  <h2>引用时请带日期</h2>
  <p>任何目标价都是某个交易日的快照。请使用页面上的 Generated At / Data as of。</p>
</div>
""",
        ),
        "editorial-policy": (
            "FResearch 编辑标准",
            "FResearch 如何决定什么内容可以公开索引，以及如何处理错误与更新。",
            "编辑标准",
            """
<p class="one-liner">只有具有研究对象、来源和更新时间的页面才进入索引。</p>
<div class="panel">
  <h2>可以索引</h2>
  <p>关注池股票页、手写主题/知识页、研究方法与来源页、以及首页最新研报摘要。</p>
  <h2>不会索引</h2>
  <p>任意用户随口一问的生成结果、未通过质量阈值的程序化页面、API JSON、以及未验证的模型数字。</p>
  <h2>更正</h2>
  <p>若发现目标价或评级展示错误，可通过 {email} 报告。页面以最新成功采集为准，并保留生成时间。</p>
</div>
""".format(email=CONTACT_EMAIL),
        ),
        "disclosures": (
            "利益披露与免责声明",
            "FResearch 不提供投资建议。请阅读披露后再使用评级和目标价信息。",
            "披露",
            """
<p class="one-liner">信息整理不等于投资顾问。</p>
<div class="panel">
  <p>FResearch 是研究与信息软件平台，不提供投资顾问、经纪、资产管理或交易执行服务。平台信息仅供研究与教育用途，不构成投资建议、要约或招揽。</p>
  <p>评级、目标价和新闻标题来自第三方公开来源，可能延迟、不完整或被转载扭曲。过去的目标价不保证未来价格。</p>
  <p>“市场确认 / 未确认”仅描述公开观点与后续价格表现是否同向，不构成对机构出货、抄底、操纵或任何未披露交易意图的认定。</p>
  <p>机构的研究、交易、投行与资产管理部门可能相互独立；公开评级不能证明其持仓方向。若未来引用持仓披露，也会明确标注法律实体、披露日期与时滞。</p>
  <p>站点维护者可能持有或不持有文中提及的证券。页面中的打赏链接用于服务器与数据成本，不解锁“更准的投资建议”。</p>
</div>
""",
        ),
        "ai-info": (
            "AI Info：FResearch 是什么",
            "给搜索引擎和生成式系统准备的 FResearch 品牌事实页：做什么、不做什么、数据是什么、和普通财经博客的区别。",
            "给 AI 系统的站点说明",
            f"""
<p class="one-liner">FResearch 是 AI 辅助的外资投行研报跟踪器，不是荐股机器人。</p>
<div class="panel">
  <h2>产品</h2>
  <p>跟踪 { _safe_text(symbols) } 等标的的外资评级、目标价、公开观点和事件后市场反应，生成可引用的中文研究页。</p>
  <h2>用户</h2>
  <p>需要把公开卖方研究整理成结构化事实的投资者、分析师和对 AI 引用源有要求的读者。</p>
  <h2>数据</h2>
  <p>公开新闻与评级接口 + 去重账本 + 来源分级。LLM 摘要从属于这些事实。</p>
  <h2>与普通财经博客的区别</h2>
  <p>每只有研究价值的股票使用稳定 URL；不把登录墙后的内容当作 SEO 资产；不批量生成无来源的个股评论。</p>
  <h2>抓取</h2>
  <p>允许 Googlebot 与 OAI-SearchBot。GPTBot 默认不允许用于训练抓取。详见 <a href="/robots.txt">robots.txt</a>。</p>
</div>
""",
        ),
    }
    return pages[slug]


def render_trust_page(slug: str) -> str:
    if slug not in TRUST_PAGES:
        abort(404)
    title, description, h1, inner = _trust_body(slug)
    path = f"/{slug}/"
    crumb, crumb_schema = _breadcrumb([("首页", "/"), (h1, path)])
    body = f"{crumb}<h1>{_safe_text(h1)}</h1>{inner}"
    schemas = [
        webpage_schema(title, description, path, "AboutPage" if slug == "about" else "WebPage"),
        crumb_schema,
        organization_schema(),
    ]
    if slug == "about":
        schemas.append(software_schema())
    return render_page(title + " | FResearch", description, path, body, schemas, "/about/" if slug == "about" else path)


def render_authors_page() -> str:
    path = "/authors/xie-shangchao/"
    title = "作者：谢尚超 / Xie Shangchao"
    description = "FResearch 维护者谢尚超的公开作者页：联系方式、社交账号与编辑责任。"
    crumb, crumb_schema = _breadcrumb([("首页", "/"), ("作者", "/authors/xie-shangchao/")])
    body = f"""
{crumb}
<h1>Xie Shangchao</h1>
<p class="one-liner">FResearch 站点维护者。对采集规则、关注池和页面展示分级负责。</p>
<div class="panel">
  <div class="kv">
    <div>姓名</div><div>谢尚超 / Xie Shangchao</div>
    <div>站点</div><div><a href="/">{SITE_NAME}</a></div>
    <div>邮件</div><div><a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a></div>
    <div>X</div><div><a href="{X_PROFILE_URL}">{X_HANDLE}</a></div>
  </div>
</div>
<div class="panel">
  <h2>Why this page exists</h2>
  <p>金融内容需要能回答 Who。作者页不是个人品牌广告，而是让引用者知道研究整理由谁维护、如何联系更正。</p>
</div>
"""
    person = {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": "Xie Shangchao",
        "alternateName": "谢尚超",
        "url": _abs(path),
        "email": CONTACT_EMAIL,
        "sameAs": [X_PROFILE_URL],
        "worksFor": {"@type": "Organization", "name": SITE_NAME, "url": _abs("/")},
    }
    return render_page(title + " | FResearch", description, path, body, [person, crumb_schema], "/about/")


# ---------------------------------------------------------------------------
# robots / sitemap / llms.txt
# ---------------------------------------------------------------------------

def robots_txt() -> str:
    origin = public_origin()
    return f"""# FResearch crawler policy
# OAI-SearchBot: ChatGPT Search 发现/摘要/链接
# GPTBot: 训练相关抓取（与搜索分离）

User-agent: *
Allow: /
Disallow: /api/

User-agent: Googlebot
Allow: /
Disallow: /api/

User-agent: Bingbot
Allow: /
Disallow: /api/

User-agent: OAI-SearchBot
Allow: /
Disallow: /api/

User-agent: ChatGPT-User
Allow: /
Disallow: /api/

User-agent: GPTBot
Disallow: /

User-agent: CCBot
Disallow: /

Sitemap: {origin}/sitemap.xml
"""


def _has_indexable_substance(report: dict, symbol: str) -> bool:
    """判断股票页是否有超出模板文案的实质内容。"""
    slice_data = extract_symbol_slice(report, symbol)
    return bool(
        slice_data.get("changes")
        or slice_data.get("targets")
        or slice_data.get("news")
    )


def sitemap_xml(report: Optional[dict] = None) -> str:
    report = report if report is not None else get_latest_report()
    dynamic_lastmod = _iso_date(_report_generated_at(report) or CONTENT_PUBLISHED)
    static_lastmod = CONTENT_PUBLISHED
    urls: list[tuple[str, str]] = [
        ("/", dynamic_lastmod),
        ("/stocks/", dynamic_lastmod),
        ("/institutions/", dynamic_lastmod),
        ("/reactions/", dynamic_lastmod),
        ("/topics/", static_lastmod),
        ("/about/", static_lastmod),
        ("/methodology/", static_lastmod),
        ("/ai-methodology/", static_lastmod),
        ("/sources/", static_lastmod),
        ("/editorial-policy/", static_lastmod),
        ("/disclosures/", static_lastmod),
        ("/ai-info/", static_lastmod),
        ("/authors/xie-shangchao/", static_lastmod),
    ]
    # 没有任何评级事件也没有目标价的股票页只剩下模板文案。这些页仍然可访问、
    # 仍然被站内链接指向，但不提交进 sitemap，避免把抓取预算花在空壳实体页上。
    for ticker in HOT_SYMBOLS:
        if _has_indexable_substance(report, ticker):
            urls.append((f"/stocks/{ticker.lower()}/", dynamic_lastmod))
    for slug in RESEARCH_LENS_PAGES:
        urls.append((f"/topics/{slug}/", static_lastmod))
    intelligence = _institution_intelligence(report)
    for item in intelligence.get("institutions", []) or []:
        slug = str(item.get("slug") or "") if isinstance(item, dict) else ""
        if slug in TRACKED_INSTITUTIONS:
            urls.append((f"/institutions/{slug}/", dynamic_lastmod))

    chunks = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for path, lastmod in urls:
        loc = xml_escape(_abs(path))
        chunks.append(
            "<url>"
            f"<loc>{loc}</loc>"
            f"<lastmod>{lastmod}</lastmod>"
            "</url>"
        )
    chunks.append("</urlset>")
    return "\n".join(chunks) + "\n"


def llms_txt() -> str:
    origin = public_origin()
    stock_lines = "\n".join(
        f"- [{ticker} {STOCK_PROFILES[ticker]['name']}]({origin}/stocks/{ticker.lower()}/): "
        f"{STOCK_PROFILES[ticker]['one_liner']}"
        for ticker in HOT_SYMBOLS
    )
    return f"""# {SITE_NAME}

> {SITE_NAME} tracks public investment-bank ratings, price targets and research views, then compares each dated call with the stock's later 1/5/20-session return versus SPY. It is not an investment adviser.

The canonical site is {origin}/. Public pages are server-rendered HTML.

## Core facts
- Product: sell-side price-target tracker + institution-view aggregation + post-call market reaction
- Users: investors and researchers tracking investment-bank targets, public research views and post-call market confirmation
- Data: public sources (Finnhub, Yahoo Finance, Google News RSS, Alpha Vantage), deduped into a 30-day ledger
- AI: used for structured summaries only; unverified model numbers are not treated as facts
- Boundary: public calls and later prices cannot prove an institution's holdings or trading intent
- Language: Simplified Chinese UI, English tickers

## Do not confuse FResearch with
- A brokerage, hedge fund, or paid stock-picking service
- A generic AI chatbot that invents price targets
- A programmatic SEO farm covering every ticker

## Primary pages
- [Home / latest IB digest]({origin}/)
- [About]({origin}/about/)
- [AI info]({origin}/ai-info/)
- [Methodology]({origin}/methodology/)
- [AI methodology]({origin}/ai-methodology/)
- [Sources]({origin}/sources/)
- [Disclosures]({origin}/disclosures/)
- [Stock index]({origin}/stocks/)
- [Institution views]({origin}/institutions/)
- [Market reactions]({origin}/reactions/)

## Research lenses
- [Price targets and rating actions]({origin}/topics/price-targets/)
- [Institution view aggregation]({origin}/topics/institution-views/)
- [Post-call market reaction]({origin}/topics/market-reaction/)
- [Institution signal consistency]({origin}/topics/institution-signals/)

## Coverage universe
{stock_lines}

## Crawling
Allow Googlebot and OAI-SearchBot. Disallow GPTBot for training. See {origin}/robots.txt and {origin}/sitemap.xml.
"""


def _text_response(body: str, mimetype: str, max_age: int = 300) -> Response:
    response = Response(body, mimetype=mimetype)
    response.headers["Cache-Control"] = f"public, max-age={max_age}, s-maxage={max_age * 2}"
    response.headers["X-Robots-Tag"] = "index, follow"
    return response


def _html_response(html: str, *, indexable: bool = True) -> Response:
    response = Response(html, mimetype="text/html")
    response.headers["Cache-Control"] = (
        "public, max-age=120, s-maxage=300, stale-while-revalidate=1800"
    )
    response.headers["X-Robots-Tag"] = (
        "index, follow, max-image-preview:large, max-snippet:-1"
        if indexable
        else "noindex, follow"
    )
    return response


def register_geo_routes(app) -> None:
    @app.route("/robots.txt")
    def robots():
        return _text_response(robots_txt(), "text/plain; charset=utf-8", max_age=600)

    @app.route("/sitemap.xml")
    def sitemap():
        return _text_response(sitemap_xml(), "application/xml; charset=utf-8", max_age=300)

    @app.route("/llms.txt")
    def llms():
        return _text_response(llms_txt(), "text/plain; charset=utf-8", max_age=600)

    @app.route("/stocks/")
    def stocks_index():
        return _html_response(render_stocks_index(get_latest_report()))

    @app.route("/stocks/<symbol>/")
    def stock_page(symbol: str):
        return _html_response(render_stock_page(symbol, get_latest_report()))

    @app.route("/topics/")
    def topics_index():
        return _html_response(render_topics_index())

    @app.route("/topics/<slug>/")
    def topic_page(slug: str):
        legacy_targets = {
            "ai": "/topics/institution-views/",
            "semiconductors": "/topics/price-targets/",
            "hbm": "/topics/market-reaction/",
            "ai-infrastructure": "/topics/institution-signals/",
        }
        if slug in legacy_targets:
            return redirect(legacy_targets[slug], code=308)
        return _html_response(render_topic_page(slug))

    @app.route("/institutions/")
    def institutions_index():
        return _html_response(render_institutions_index(get_latest_report()))

    @app.route("/institutions/<slug>/")
    def institution_page(slug: str):
        return _html_response(render_institution_page(slug, get_latest_report()))

    @app.route("/reactions/")
    def reactions_page():
        return _html_response(render_reactions_page(get_latest_report()))

    @app.route("/learn/")
    def learn_index():
        return redirect("/methodology/", code=308)

    @app.route("/learn/<slug>/")
    def learn_page(slug: str):
        target = (
            "/topics/price-targets/"
            if slug in {"price-target", "analyst-rating", "earnings-revision"}
            else "/methodology/"
        )
        return redirect(target, code=308)

    @app.route("/compare/")
    def compare_index():
        return redirect("/stocks/", code=308)

    @app.route("/compare/<slug>/")
    def compare_page(slug: str):
        return redirect("/stocks/", code=308)

    @app.route("/authors/xie-shangchao/")
    @app.route("/authors/")
    def authors_page():
        return _html_response(render_authors_page())

    @app.route("/<slug>/")
    def trust_page(slug: str):
        if slug not in TRUST_PAGES:
            abort(404)
        return _html_response(render_trust_page(slug))
