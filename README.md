# 销售副驾（sales-agent-v2）

面向 4–10 人销售小团队的**销售流程引擎**：把「线索怎么往前走」变成可配置的流程、可留痕的事实、可度量的指标；AI 只在底层帮忙整理信息，不替人做决定。

主演示场景是 **ToB 企业销售**（卖企业软件 / 设备集成，单笔 3 万–120 万）；另外三份模板（教培 / 软件外包 / 建材）用来证明「同一套块换配置就能跑另一条流程」——它们不是摆设，每次 `pytest` 都跟着过一遍模板校验。

三条卖点：

1. **AI 在底层提炼**：客户原话进来先存原文，再由模型提炼成带原文片段的事实；AI 不出现在"下结论"的位置。
2. **流程约束 AI 产出**：话术草稿必须有资料引用、必须人工确认才落库；提炼没有原文片段的字段一律丢弃。
3. **可机器验证的平台边界**：分层依赖、行业词禁令、块契约、禁用词都由 pytest / import-linter 锁死，不是口号。

## 它解决什么问题

- **员工层（前提）**：一线销售的信息太碎（聊天、电话、笔记散在各处），整理成本高于收益，台账最后只剩"已联系"。
- **公司层（买单理由）**：客户被销售捏在自己手里，人一走客户就没了——过程看不见、无法交接、无法度量。
- **AI 的位置**：只做「采集 → 提炼 → 生效」管道和话术草稿；不改归属、不推进阶段、不判断"这单会成/会丢"、不自动对外发消息。

## 四份行业模板（主演示是第一份）

| 模板 | 名称 | 流程块数 | 说明 |
|---|---|---|---|
| `tob_enterprise` | ToB 企业销售 | 6（含「方案会」） | **主演示**，也是默认模板（`DEFAULT_TEMPLATE`） |
| `edu_training` | 教培（成人职教） | 6（含「试听」） | 第二模板，首响 30 分钟 |
| `software_outsourcing` | 软件外包 | 6（含「方案会」） | 首响按天算（1440 分钟） |
| `building_materials` | 建材 | 5（跳过「方案会」） | 证明同一个引擎能砍块 |

行业差异**只在 YAML 与 `data/knowledge/` 里**：模板给块的组合顺序、阈值、分级权重与信息卡字段；
引擎层由 `tests\test_no_industry_words.py` 锁死不含行业词。模板清单也是「设置」页上那四张卡（`GET /settings`）。

## 五个入口（演示动线）

| 入口 | 干什么 | 主要接口 |
|---|---|---|
| 分拣台 | 今日待办 + 投料 → 分拣单（归属 / 要素 / 分级 / 下一步）+ 待认领池 | `GET /todo`、`POST /sorting`、`GET/POST /sorting/...` |
| 客户 | 仪表盘 + 客户货架（A/B/C 分组、覆盖度、风险色点）+ 线索管理 | `GET /dashboard`、`GET /leads/shelf`、`GET /leads/pool` |
| 客户详情 | 四维分级（已知 / 未知）+ 改判留痕 + 信息卡缺项问题句 + 跟进 + 话术 | `GET /leads/{id}/card`、`GET /leads/{id}/grading/latest`、`POST /leads/{id}/draft` |
| 复盘 | 本周数字 + 卡点 + 缺项前三 + 本周改档 + 无价值跟进 + 3 个校准问题 | `GET /review`、`POST /review/answers` |
| 设置 | 运行模式 + 分级模式说明 + 四份模板的阈值与权重 + 导入 CSV | `GET /health`、`GET /settings`、`POST /leads/import` |

「客户详情」不是页签：从客户货架或分拣台待办点进去，点「返回」回到原来那一页。

## 快速开始

两条路径，先跑 A 就能看到完整界面（假模型，0 成本、不联网）。

前置：Windows + PowerShell，Python ≥ 3.11（本项目实测 3.14.6）；Node 仅前端需要。

### A. 假模型（0 成本，2 分钟）

```powershell
# 1) 建环境 + 装依赖
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# 2) 造 ToB 演示数据（默认写 data/app.sqlite3；SALESCOPILOT_DB 可改路径）
.venv\Scripts\python.exe scripts\seed_demo.py

# 3) 建一次资料索引（假向量，离线；只索引 ToB 资料，第 5 步「生成话术」才不会引到别的行业的资料）
.venv\Scripts\python.exe scripts\build_index.py --fake --docs-dir data\knowledge\tob

# 4) 起后端
$env:PYTHONPATH='src'; .venv\Scripts\python.exe -m uvicorn salescopilot.entrypoints.api.app:app --port 8000

# 5) 另开一个终端：起前端
cd web; npm install; npm run dev      # → http://localhost:5173
```

演示数据造好后，脚本会打印 `SEED_OK leads=5 followups=3 facts=41 grades=3`；
再跑一次会打印 `SEED_SKIP existing=5`（幂等，不会把数据翻倍）。
索引建好后会打印 `INDEX_OK chunks=24 model=fake`（只算 `data/knowledge/tob/` 那三份）。

三步就是：**建索引 → 播种 → 起服务**（上面的 2 / 3 / 4），顺序换掉会导致第 5 步「生成话术」被拒「未引用资料」。

### B. 真模型（需要 `.env` 里的 `DEEPSEEK_API_KEY` 与 `DASHSCOPE_API_KEY`）

```powershell
$env:SALESCOPILOT_LIVE='1'
# 真向量重建索引（同样只索引 ToB 资料；换 embedding 模型必须整库重建）
.venv\Scripts\python.exe scripts\build_index.py --docs-dir data\knowledge\tob
# 起后端 / 前端同 A，只是不再设 SALESCOPILOT_LIVE 以外的开关
```

`GET /health` 应返回 `"mode": "live"` 与当前模型名（`models.chat` / `models.embed`）。
开了真模型却缺 key 时**启动直接失败并打印中文提示**，不会悄悄退回假模型。

## 演示前 60 秒检查清单

1. `GET /health` 的 `mode` 与你打算讲的一致（`fake` 要主动说明"数据不可信"）。
2. 提醒有红 / 黄 / 灰三种颜色（`GET /reminders` 红 1 / 黄 2 / 灰 1）。
3. 公海恰好 1 条（`GET /leads/pool`）。
4. 仪表盘数字不是全 0：客户总数 5、公海 1、本周新增 4、本周跟进 3、24h 首联率 100.0%、无下次跟进 20.0%。
5. 「分拣台」今日待办 5 家（A 1 / B 1 / C 1 / 待分级 2），「设置」页模板 4 份。

## 项目结构与分层

```
src/salescopilot/
├─ entrypoints/   api/（FastAPI 路由）· bootstrap.py（唯一装配点）
├─ services/      用例编排：flow / leads / followup / ingest / draft / knowledge /
│                 dashboard / assignment / reminder_service / import_service /
│                 sorting / card / grading / todo / review / shelf
├─ domain/        实体与纯规则：models / blocks / rules / facts / reminders / dashboard /
│                 draft / csv_import / assignment / labels / ports / eval_metrics /
│                 matching / sorting / card / grading / todo / review / shelf
├─ adapters/      端口实现：db·sqlite / llm（DeepSeek + fake）/ knowledge（Chroma + 向量）/
│                 template / ...
└─ templates/     四份行业 YAML（ToB 企业销售 / 教培 / 软件外包 / 建材）——数据，不是代码

scripts/          seed_demo.py（ToB 演示数据）· build_index.py（建索引）· run_eval.py（基线评测）·
                  calibrate_thresholds.py（归属阈值校准）· scan_reclaim.py（首响超时扫描）
web/              Vue3 + Vite 前端（五个入口），只通过 HTTP 调后端
```

依赖方向只有 `entrypoints → services → domain`，`adapters` 与 `services` 平级、只由
`bootstrap.py` 装配；`services` 不许 import `adapters`，`domain` 不许 import 任何其它层。
完整层图、机制与数据边界见 [`docs/架构.md`](docs/架构.md)。

## 质量证据（都能自己跑一遍）

```powershell
.venv\Scripts\python.exe -m ruff check .                     # 全过（0 问题）
.venv\Scripts\python.exe -m pytest -q                        # 480 passed
$env:PYTHONPATH='src'; .venv\Scripts\lint-imports.exe        # 3 kept, 0 broken
.venv\Scripts\python.exe -m pytest -q tests\test_no_industry_words.py   # 引擎不含行业词
.venv\Scripts\python.exe -m pytest -q tests\test_templates.py            # 四份模板全过校验
.venv\Scripts\python.exe scripts\seed_demo.py                # SEED_OK leads=5 followups=3 facts=41 grades=3
.venv\Scripts\python.exe scripts\build_index.py --fake --docs-dir data\knowledge\tob   # INDEX_OK chunks=24
```

- 分层契约 3 条（layers / services-not-adapters / domain-leaf），写在 `.importlinter`。
- 行业词词表在 `tests/fixtures/industry_words.txt`，pytest 与人工 grep 共用同一份清单。
- 删块测试：把 `appointment` 块定义删掉后，ToB 与教培模板校验失败、建材模板仍然通过（积木真复用）。
- 仪表盘指标口径写在 `domain/dashboard.py` 的字段注释里：`total` / `pool` / `new_this_week` /
  `followups_this_week` / `first_response_24h_rate` / `stale_ratio` / `silent_count` /
  `reminders_by_level`。

## 已知边界

- **不做 K12 学科**（双减）：教培模板只用成人职教。
- **不接抓取**：不写微信 / 群聊抓取脚本，也不接真实短信 / 外呼；输入只来自粘贴、文本导入与人工录入。
- **不自动发消息**：AI 只产草稿，必须人工确认后才落库为跟进记录。
- **评测样本是合成的**：`data/evalsets/extract_cases.jsonl` 只用于建基线与观察漂移，不代表真实客户分布。
- **演示数据的红 / 黄 / 灰依赖造数时刻**：`seed_demo.py` 用相对时间造「5 小时未首联」「约定 3 天前」，
  隔天再看「已过 N 天」的数字会变大（颜色通常不变）。
- **知识库按行业分目录**：`data/knowledge/tob/` 是 ToB 演示资料，顶层三份是教培资料；
  演示只索引一个目录，避免假向量把别的行业资料引到话术里。

更多：需求基线 [`docs/PRD.md`](docs/PRD.md)｜方案与砍单顺序 [`docs/pitch.md`](docs/pitch.md)｜
现场怎么讲 [`docs/演示脚本.md`](docs/演示脚本.md)｜进度 [`docs/进度.md`](docs/进度.md)。
