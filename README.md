# 销售副驾（sales-agent-v2）

面向 4–10 人销售小团队的**销售流程引擎**：把「线索怎么往前走」变成可配置的流程、可留痕的事实、可度量的指标；AI 只在底层帮忙整理信息，不替人做决定。

三条卖点：

1. **AI 在底层提炼**：客户原话进来先存原文，再由模型提炼成带原文片段的事实；AI 不出现在"下结论"的位置。
2. **流程约束 AI 产出**：话术草稿必须有资料引用、必须人工确认才落库；提炼没有原文片段的字段一律丢弃。
3. **可机器验证的平台边界**：分层依赖、行业词禁令、块契约、禁用词都由 pytest / import-linter 锁死，不是口号。

## 它解决什么问题

- **员工层（前提）**：一线销售的信息太碎（聊天、电话、笔记散在各处），整理成本高于收益，台账最后只剩"已联系"。
- **公司层（买单理由）**：客户被销售捏在自己手里，人一走客户就没了——过程看不见、无法交接、无法度量。
- **AI 的位置**：只做「采集 → 提炼 → 生效」管道和话术草稿；不改归属、不推进阶段、不判断"这单会成/会丢"、不自动对外发消息。

## 快速开始

两条路径，先跑 A 就能看到完整界面（假模型，0 成本、不联网）。

前置：Windows + PowerShell，Python ≥ 3.11（本项目实测 3.14.6）；Node 仅前端需要。

### A. 假模型（0 成本，2 分钟）

```powershell
# 1) 建环境 + 装依赖
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# 2) 造演示数据（默认写 data/app.sqlite3；SALESCOPILOT_DB 可改路径）
.venv\Scripts\python.exe scripts\seed_demo.py

# 3) 建一次资料索引（假向量，离线；第 5 步演示「生成话术」要用到引用）
.venv\Scripts\python.exe scripts\build_index.py --fake

# 4) 起后端
$env:PYTHONPATH='src'; .venv\Scripts\python.exe -m uvicorn salescopilot.entrypoints.api.app:app --port 8000

# 5) 另开一个终端：起前端
cd web; npm install; npm run dev      # → http://localhost:5173
```

演示数据造好后，脚本会打印 `SEED_OK leads=4 followups=2 facts=1`；再跑一次会打印
`SEED_SKIP existing=4`（幂等，不会把数据翻倍）。

### B. 真模型（需要 `.env` 里的 `DEEPSEEK_API_KEY` 与 `DASHSCOPE_API_KEY`）

```powershell
$env:SALESCOPILOT_LIVE='1'
.venv\Scripts\python.exe scripts\build_index.py      # 建资料索引（真向量）
# 起后端 / 前端同 A，只是不再设 SALESCOPILOT_LIVE 以外的开关
```

`GET /health` 应返回 `"mode": "live"` 与当前模型名（`models.chat` / `models.embed`）。
开了真模型却缺 key 时**启动直接失败并打印中文提示**，不会悄悄退回假模型。

## 演示前 60 秒检查清单

1. `GET /health` 的 `mode` 与你打算讲的一致（`fake` 要主动说明"数据不可信"）。
2. 提醒有红 / 黄 / 灰三种颜色（`GET /reminders` 各至少 1 条）。
3. 公海恰好 1 条（`GET /leads/pool`）。
4. 仪表盘数字不是全 0：线索总数 4、公海 1、本周新增 4、本周跟进 2、24h 首联率 100.0%。

## 项目结构与分层

```
src/salescopilot/
├─ entrypoints/   api/（FastAPI 路由）· bootstrap.py（唯一装配点）
├─ services/      用例编排：flow / leads / followup / ingest / draft / knowledge /
│                 dashboard / assignment / reminder_service / import_service
├─ domain/        实体与纯规则：models / blocks / rules / facts / reminders / dashboard /
│                 draft / csv_import / assignment / labels / ports / eval_metrics
├─ adapters/      端口实现：db·sqlite / llm（DeepSeek + fake）/ knowledge（Chroma + 向量）/
│                 template / ...
└─ templates/     三份行业 YAML（教培 / 软件外包 / 建材）——数据，不是代码

scripts/          seed_demo.py（演示数据）· build_index.py（建索引）· run_eval.py（基线评测）
web/              Vue3 + Vite 前端，只通过 HTTP 调后端
```

依赖方向只有 `entrypoints → services → domain`，`adapters` 与 `services` 平级、只由
`bootstrap.py` 装配；`services` 不许 import `adapters`，`domain` 不许 import 任何其它层。
完整层图、机制与数据边界见 [`docs/架构.md`](docs/架构.md)。

## 质量证据（都能自己跑一遍）

```powershell
.venv\Scripts\python.exe -m ruff check .                     # 全过（0 问题）
.venv\Scripts\python.exe -m pytest -q                        # 247 passed
$env:PYTHONPATH='src'; .venv\Scripts\lint-imports.exe        # 3 kept, 0 broken
.venv\Scripts\python.exe -m pytest -q tests\test_no_industry_words.py   # 引擎不含行业词
.venv\Scripts\python.exe -m pytest -q tests\test_templates.py::test_removing_appointment_breaks_edu_only
```

- 分层契约 3 条（layers / services-not-adapters / domain-leaf），写在 `.importlinter`。
- 行业词词表在 `tests/fixtures/industry_words.txt`，pytest 与人工 grep 共用同一份清单。
- 删块测试：把 `appointment` 块定义删掉后，教培模板校验失败、建材模板仍然通过（积木真复用）。
- 仪表盘指标口径写在 `domain/dashboard.py` 的字段注释里：`total` / `pool` / `new_this_week` /
  `followups_this_week` / `first_response_24h_rate` / `stale_ratio` / `silent_count` /
  `reminders_by_level`。

## 已知边界

- **不做 K12 学科**（双减）：示例只用成人职教 / 素质 / 语培。
- **不接抓取**：不写微信 / 群聊抓取脚本，也不接真实短信 / 外呼；输入只来自粘贴、文本导入与人工录入。
- **不自动发消息**：AI 只产草稿，必须人工确认后才落库为跟进记录。
- **评测样本是合成的**：`data/evalsets/extract_cases.jsonl` 只用于建基线与观察漂移，不代表真实客户分布。

更多：需求基线 [`docs/PRD.md`](docs/PRD.md)｜方案与砍单顺序 [`docs/pitch.md`](docs/pitch.md)｜
现场怎么讲 [`docs/演示脚本.md`](docs/演示脚本.md)｜进度 [`docs/进度.md`](docs/进度.md)。
