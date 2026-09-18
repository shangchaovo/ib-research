# 参与贡献

欢迎 Issue 和 Pull Request。

## 开发模型

本仓库(`~/ib-research` ↔ GitHub `shangchaovo/ib-research`)是**唯一代码源头**;
线上运行环境(数据 `data/`、密钥 `.env`)在部署机的工作区,经 `bash deploy.sh`
按显式文件清单同步。因此:

- 改代码、走 PR 都在本仓库;不要把数据文件或 `.env` 提交进来
- 新增需要部署的代码文件时,记得把它加进 `deploy.sh` 的 `FILES` 列表

## 测试

```bash
pip3 install -r requirements.txt
python3 -m unittest discover -t . -s . -p 'test_*.py'
```

CI 会对每个 PR 运行编译检查 + 上述单元测试。

## 约定

- LLM 输出必须标注置信层级(`av_consensus` / `verified_headline` / `llm_inferred`),
  新增数据源或抽取逻辑时保持这一分层,不要把推断当事实
- 提交信息使用中文 + 约定式前缀(`feat/fix/docs/ci/refactor`),与仓库历史保持一致
