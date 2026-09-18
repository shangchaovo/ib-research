# 安全政策

## 支持版本

仅 `main` 分支最新代码接受安全修复(个人项目,滚动发布)。

## 报告漏洞

**不要开公开 Issue。** 请通过 GitHub 的
[Private Vulnerability Reporting](../../security/advisories/new) 提交,
预期 7 天内首次响应。

## 安全模型速览(审计入口)

- 线上刷新接口受 token 校验 + IP 白名单 + 速率限制保护
- 服务端出站请求带 TLS 校验;LLM 抽取的评级/目标价标注为 `llm_inferred`,
  与 `av_consensus` / `verified_headline` 分层,不当作已验证事实展示
- API keys 只存在于部署环境的 `.env`(已 gitignore,模板见 `.env.example`),绝不入库
- 公网静态站只含聚合后的展示数据,不含原始研报全文与密钥
