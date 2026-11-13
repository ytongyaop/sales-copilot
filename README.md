# SalesCopilot（销售副驾）

## 是什么

面向中小企业销售团队的**销售流程引擎**。把"线索怎么往前走"做成可配置的东西：

- **引擎**：一套代码，不含任何行业词，只认五种通用概念——阶段、字段、阈值、周期、出口条件。
- **流程块**：6 个可组合的块，封顶不扩——`intake` / `first_contact` / `qualify` / `appointment` / `proposal` / `close`。
- **行业模板**：块的有序组合 + 阈值参数 + 字段取值，行业差异全在 `templates/*.yaml`，换行业不改代码。
- **AI**：只做两件事——生成跟进话术草稿（必须引用资料、必须人确认）、归类客户异议。不写库、不推进阶段、不下"这单会成/会丢"的结论。

现状：**B1 骨架**。能跑起来、分层规则可自动检查，**还没有业务逻辑**——上面这些是 B2 之后逐步填进去的目标。

## 环境要求

- Windows + PowerShell（其它系统把 `.venv\Scripts\` 换成 `.venv/bin/`）
- Python **3.14.6**（`.venv` 已建好并装齐依赖；换机器就重建，见下）
- 不需要 Node / Docker / 数据库服务：业务数据将来存本地 SQLite

## 3 条命令

```powershell
# ① 重建环境（已有 .venv 可跳过）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -i https://mirrors.aliyun.com/pypi/simple/

# ② 跑全部检查（lint + 测试 + 分层契约）
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q
$env:PYTHONPATH='src'; .venv\Scripts\lint-imports.exe

# ③ 跑接口（浏览器打开 http://127.0.0.1:8000/docs 或 /health）
$env:PYTHONPATH='src'; .venv\Scripts\python.exe -m uvicorn salescopilot.entrypoints.api.app:app --reload
```

> 坑一：`src/` 布局必须让 Python 找得到包——pytest 已在 `pyproject.toml` 里配了 `pythonpath=["src"]`，跑 `uvicorn` / `lint-imports` 时则要手动设 `$env:PYTHONPATH='src'`。
> 坑二：`.importlinter` 必须**纯 ASCII**——import-linter 在 Windows 下按系统编码读配置，写中文会报 codec 错。

## 目录说明

```
sales-agent-v2/
├─ pyproject.toml             # 包信息 + pytest（pythonpath=src）+ ruff（line-length=100）
├─ .importlinter              # 分层契约：entrypoints|adapters → services → domain，且 services 禁 import adapters
├─ requirements-dev.txt       # 开发依赖（pip freeze 实际版本）
├─ src/salescopilot/
│  ├─ entrypoints/api/app.py  # FastAPI 实例 + GET /health（B1 只有这一个端点）
│  ├─ services/               # 用例层：编排规则 + 调端口（B2 填）
│  ├─ domain/                 # 实体 / 块契约 / 规则 / 端口协议 / 指标，纯函数无 IO（B2 填）
│  ├─ adapters/               # 端口实现：db / knowledge / llm / template / clock（B2 填）
│  └─ templates/              # 三份行业 YAML（B2 才填，是数据不是代码）
├─ tests/
│  ├─ test_smoke.py           # /health 冒烟
│  ├─ test_no_industry_words.py   # ADR-0004 的机器实现：扫 domain/services 查行业词
│  └─ fixtures/industry_words.txt # 行业词清单（pytest 与人工 grep 共用）
├─ web/                       # Vue3 前端待建，只通过 HTTP 调后端，node_modules 不入库
├─ data/knowledge/            # 资料原文（MD/TXT）
├─ data/seed/                 # 演示种子数据
└─ data/index/                # 向量索引（整体 gitignore）
```

## 文档

- 需求基线：`docs/PRD.md`｜方案与预算：`docs/pitch.md`｜分层与机制：`docs/架构.md`
- 技术决定：`docs/decisions/`（ADR）｜进度：`docs/进度.md`｜观察记录：`docs/观察.md`
- 动手前读 `AGENTS.md`：本地命令与项目红线在那儿，不复制到这里。
