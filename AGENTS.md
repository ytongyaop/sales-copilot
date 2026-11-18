# 项目：销售副驾（sales-agent-v2）

> 用法：放项目根。填 3 处（项目名 / 一句话 / 专属约束），命令与规则照用。
> 全局门禁与流程：见 `AGENTS.md#门禁`（`~/.config/opencode/AGENTS.md`），此处不复制正文。

## 是什么

面向中小企业销售团队的流程引擎，用可组合流程块 + 行业模板把销售跟进流程变成可配置、可留痕、可度量的系统；AI 负责四类分拣判断（归属复核 / 要素抽取 / 分级 / 出库建议）与话术生成，其中归属以确定性漏斗为主，模型只在灰区复核（见 `docs/decisions/0007-归属用确定性漏斗.md`）。

## 本地命令（本项目唯一事实源）

- lint：`.venv\Scripts\python.exe -m ruff check .`
- test：`.venv\Scripts\python.exe -m pytest -q`
- 分层检查：`$env:PYTHONPATH='src'; .venv\Scripts\lint-imports.exe`
- 跑接口：`$env:PYTHONPATH='src'; .venv\Scripts\python.exe -m uvicorn salescopilot.entrypoints.api.app:app --reload`

## 本项目特有约束

- 依赖方向只有 `entrypoints → services → domain`，`adapters` 与 `services` 平级；装配只在 `entrypoints`。
- 新功能先在 `specs/NNN-短名/` 写 spec + plan + tasks；计划未确认不写代码。
- 小修小补记 `docs/进度.md` 的 `## 修复记录`，不新开文档。
- 数据源只来自粘贴 / 文件导入，不做网页或 IM 抓取。
- 不自动对外发送任何消息；AI 产出必须人确认后才用。
- 事实与分级的历史表只追加，不 update 业务内容（`decide` / `attach_lead` 属窄更新例外）。

## 收尾

- 一个增量一停：改 → 跑上面命令 → 贴输出 → 确认后再 commit。
