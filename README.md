# sales-copilot

[![CI](https://github.com/ytongyaop/sales-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/ytongyaop/sales-copilot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](pyproject.toml)

A sales process engine for small teams: composable flow blocks plus industry templates, a rule-graded pipeline, and source-traceable AI extraction and drafting that a human has to confirm.

面向 4–10 人销售小团队的**销售流程引擎**：把「线索怎么往前走」变成可配置的流程块、可留痕的事实、可度量的指标。AI 只做三件事——从原文里抽要素、在归属灰区做复核、生成带资料引用的草稿；判断权（归属、分级、推进、对外发送）留给规则和人工。主演示场景是 ToB 企业软件 / 设备销售（单笔 3 万–120 万），另外三份行业模板（教培 / 软件外包 / 建材）用来证明「同一套块换配置就能跑另一条流程」——它们不是摆设，每次 `pytest` 都跟着过一遍模板校验。

技术栈：Python 3.14 · FastAPI · Vue 3 + Vite · SQLite · Chroma · DeepSeek · 阿里云 text-embedding-v3

## 它解决什么问题

- **员工层（前提）**：一线销售的信息太碎（聊天、电话、笔记散在各处），整理成本高于收益，台账最后只剩一列「已联系」。
- **公司层（买单理由）**：客户被销售捏在自己手里，人一走客户就没了——过程看不见、无法交接、无法度量。
- **AI 的位置**：做「采集 → 提炼 → 生效」的管道和话术草稿；不改归属、不推进阶段、不判断「这单会成会丢」、不自动对外发消息。

## 设计要点：规则做主判定，模型负责抽取与表达

| 判断 | 谁做 | 产出 |
|---|---|---|
| 归属（这条投料是谁的客户） | 确定性漏斗：L0 精确键 → 「仅组织形式不同」强制复核 → L1 相似度阈值（97 / 86，来自校准报告） | 命中 / 灰区待复核，灰区才问模型 |
| 要素抽取 | 模型抽，但每个值都必须带能在原文定位的 `source_span` | 带出处的事实，定位不上就丢弃并计入 `dropped` |
| 分级 | 纯规则四维加权，权重与阈值只从模板 `grading:` 段读 | A / B / C / `needs_info` + 中文理由 + 原文依据 |
| 今日待办 / 价值 | 纯规则四层排序 + 模板文案 | 每家最多一条，`max_items` 截断 |
| 话术 | 模型写，引用、日期双重校验，人工确认后才落库 | 草稿（未确认不落库） |

### 三条硬约束

1. **无出处不落库**：事实的 `source_span` 必须能在原文里定位，对不上就丢弃该条并在 `dropped` 里报出——模型编的片段进不了库。
2. **无引用不生成**：话术草稿必须引用 `data/knowledge/` 里的资料；引用为空时自动重试一次并强化引用指令，仍空则返回 422，不产出一段无依据的话。
3. **分级四维齐全才 A**：金额 / 紧迫度 / 决策清晰度 / 需求匹配度四维加权，缺任一维时上下限跨档，即使加权分够也判 `needs_info` 而不是 A。

## 快速开始（假模型 / 真模型两条路）

前置：Python ≥ 3.11（本项目实测 3.14.6）；Node 20+ 仅前端需要。先跑 A 就能看到完整界面——假模型，离线、0 成本。

当前版本 0.1.3：分拣/信息卡/分级/待办/复盘；下一步 0.2.0 账号与归属。

### A. 假模型（0 成本，2 分钟）

```bash
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

# 1) 造 ToB 演示数据（默认写 data/app.sqlite3；SALESCOPILOT_DB 可改路径）
python scripts/seed_demo.py                        # → SEED_OK leads=5 followups=3 facts=41 grades=3

# 2) 建一次资料索引（假向量、离线；只索引 ToB 资料）
python scripts/build_index.py --fake --docs-dir data/knowledge/tob   # → INDEX_OK chunks=24 model=fake

# 3) 起后端
PYTHONPATH=src python -m uvicorn salescopilot.entrypoints.api.app:app --port 8000
```

```bash
# 4) 另一个终端：起前端 → http://localhost:5173
cd web && npm install && npm run dev
```

顺序是**建索引 → 播种 → 起服务**；换掉顺序，第 5 步「生成话术」会被拒「未引用资料」。
`seed_demo.py` 幂等：再跑一次打印 `SEED_SKIP existing=5`，不会把数据翻倍。

### B. 真模型（需要 `.env` 里的 `DEEPSEEK_API_KEY` 与 `DASHSCOPE_API_KEY`）

```bash
export SALESCOPILOT_LIVE=1           # Windows PowerShell: $env:SALESCOPILOT_LIVE='1'

# 真向量重建索引（同样只索引 ToB 资料；换 embedding 模型必须整库重建）
python scripts/build_index.py --docs-dir data/knowledge/tob

# 起后端 / 前端同 A
```

`GET /health` 会返回 `"mode": "live"` 与当前实际模型名（`models.chat` / `models.embed`）。
开了真模型却缺 key 时**启动直接失败并打印中文提示**，不会悄悄退回假模型。

## 配置（`.env` 项）

复制 `.env.example` 为 `.env` 再填值；`.env` 已在 `.gitignore` 里，留空的项一律当作「没配」，不冒充有效配置。

| 变量 | 说明 | 默认 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 对话模型（DeepSeek，OpenAI 兼容接口）的 Key，走真模型时必填 | 空 |
| `DASHSCOPE_API_KEY` | 向量模型（阿里云 `text-embedding-v3`）的 Key，走真模型时必填 | 空 |
| `CHAT_MODEL` | 对话模型名 | `deepseek-chat` |
| `EMBED_MODEL` | 向量模型名 | `text-embedding-v3` |
| `SALESCOPILOT_LIVE` | 真模型开关，只认 `1` / `true` / `yes`；不设即假模型（离线、不花钱） | 关 |
| `SALESCOPILOT_DB` | SQLite 路径（后端与 `seed_demo.py` 共用同一份配置） | `data/app.sqlite3` |
| `SALESCOPILOT_INDEX_DIR` | 向量索引目录 | `data/index` |

## 数据与评测

| 资料 | 内容 | 怎么来的 |
|---|---|---|
| `data/evalsets/entity_pairs.jsonl` | **50 对**合成实体样本（17 正例 / 33 负例，含简称、仅组织形式不同、别字、完全无关） | 手写，用于归属阈值校准 |
| `data/evalsets/extract_cases.jsonl` | **20 条**合成抽取样本 | 手写，用于建基线与观察漂移 |
| `data/reports/matching_calibration.md` | **阈值校准报告**：`l1_auto=97`（precision 1.000 / recall 0.059）、`l1_review=86`（灰区下沿，覆盖 17/17 正例） | `scripts/calibrate_thresholds.py` 跑出，未手调 |
| `data/knowledge/tob/` | **3 份** ToB 演示资料（方案概述 / 报价政策 / 案例） | 虚构，仅用于话术引用 |
| `data/knowledge/`（顶层） | **3 份**教培 / 服务资料 | 虚构，证明多行业可换 |

- 校准口径与局限（合成样本难例密度高、50 对下 precision ≥ 0.95 约等于 0 误配）写在报告正文，别只看阈值数字。
- 抽取基线：`python scripts/run_eval.py`（离线，只用假模型）与 `--live`（真模型，会花钱）。
- 演示数据同样由 `scripts/seed_demo.py` 现场生成，不入库；`*.sqlite3` 与 `data/index/` 都在 `.gitignore` 里。

## 目录结构

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
web/              Vue 3 + Vite 前端（五个入口），只通过 HTTP 调后端
data/             evalsets（样本）· reports（校准报告）· knowledge（虚构资料）· seed
tests/            70 个测试文件；默认全用假模型，不联网
```

依赖方向只有 `entrypoints → services → domain`，`adapters` 与 `services` 平级、只由 `bootstrap.py` 装配；`services` 不许 import `adapters`，`domain` 不许 import 其它任何层。完整层图、机制与数据边界见 [`docs/架构.md`](docs/架构.md)。

## 质量门禁

```bash
python -m ruff check .                    # All checks passed
python -m pytest -q                       # 520 passed
PYTHONPATH=src lint-imports               # Contracts: 3 kept, 0 broken
cd web && npm run build                   # vite build 成功
```

- **520 passed**：全部用例默认用假模型（`FakeLLM` / `FakeEmbedder`），CI 不联网、不调真模型、不花钱。
- **3 kept**：三条分层契约写在 `.importlinter`（layers / services-not-adapters / domain-leaf）。
- **无行业词**：`tests/test_no_industry_words.py` 用 `tests/fixtures/industry_words.txt` 扫 `src/` 与 `tests/`，行业叫法只准出现在 `templates/*.yaml` 与 `data/knowledge/*.md`。
- **模板校验**：`tests/test_templates.py` 对四份模板逐块校验，删块测试保证「积木真复用」。
- **npm build**：前端只依赖 `vue` + `vite`，构建产物不入库。
- CI 见 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)：`python` job（ruff → pytest → lint-imports）+ `web` job（`npm ci` → `npm run build`）。

## 隐私与边界

- **不做抓取**：不写微信 / 群聊抓取脚本，也不接真实短信 / 外呼；输入只来自粘贴、文件导入与人工录入。
- **不自动发送**：AI 只产草稿，必须人工确认后才落库为跟进记录；本仓库不含任何发送通道。
- **演示数据全部虚构**：`scripts/seed_demo.py` 里的公司名（华远建设集团、明远软件科技、宏图建材、恒信智能…）、联系人（李娜、王强、王主任…）与电话号码（`1380000xxxx` 段位）都是编的，与真实企业、真实个人无关。
- **不含任何密钥**：代码只从环境变量 / `.env` 读 Key，`.env` 不入库；配置缺项时明确报错，不静默降级。
- **数据在本地**：SQLite 库与向量索引都写在 `data/` 下并被 `.gitignore` 忽略，不上传、不外传。

## 免责声明

本项目是**工程样例与演示系统**，不是可直接投产的商业产品：不含真实账号体系与多租户隔离，不含审计合规、数据加密、备份恢复等生产必需项；AI 输出可能出错，任何对外使用前必须人工复核。作者不对因使用本代码导致的任何直接或间接损失负责。详见 [`LICENSE`](LICENSE)。

## 版本（v0.1.3）

当前版本 **0.1.3**：分拣 / 信息卡 / 分级 / 待办 / 复盘 + 五入口前端 + ToB 演示动线，另含 0.1.1 降噪、0.1.2 人工补全体验、0.1.3 认领放宽。版本号在 `src/salescopilot/__init__.py` 的 `__version__`，`GET /health` 会带 `"version"` 字段，部署方一眼能对上是哪个版本在跑。

后续计划（真实账号与归属 0.2.0）见 [`docs/进度.md`](docs/进度.md) 的「版本计划」。

## English

A sales process engine for small sales teams (4–10 people). The pitch: turn "how a lead moves forward" into configurable flow blocks, source-traceable facts, and measurable numbers — and keep the model out of the judgment seat.

- **Rules make the calls.** Lead ownership comes from a deterministic funnel (exact key → organization-suffix review → similarity thresholds `97 / 86` calibrated on 50 synthetic entity pairs); grading is pure four-dimension rule math (amount / urgency / clarity / fit) with weights read from the industry template; today's to-dos are rule-sorted. The model only reviews gray-zone ownership, extracts elements, and writes drafts.
- **Three hard constraints.** (1) No fact lands without a `source_span` that can be located in the raw text — fabricated spans are dropped and reported. (2) No draft is generated without a citation from `data/knowledge/`, retried once, then rejected with 422. (3) A grade of A requires all four dimensions known; a missing dimension downgrades to `needs_info`.
- **Four industry templates** (ToB software/equipment, adult education, software outsourcing, building materials) prove the same engine runs another process by config only. Industry wording is banned from `src/` by a test.
- **Stack**: Python 3.14 (requires ≥ 3.11) · FastAPI · Vue 3 + Vite · SQLite · Chroma · DeepSeek `deepseek-chat` · Alibaba Cloud `text-embedding-v3`.
- **Quick start (fake models, offline, $0)**: `pip install -r requirements-dev.txt` → `python scripts/seed_demo.py` → `python scripts/build_index.py --fake --docs-dir data/knowledge/tob` → `PYTHONPATH=src python -m uvicorn salescopilot.entrypoints.api.app:app --port 8000`, plus `cd web && npm install && npm run dev`. Real models need `DEEPSEEK_API_KEY` + `DASHSCOPE_API_KEY` and `SALESCOPILOT_LIVE=1`; a missing key fails at startup instead of silently falling back.
- **Quality gates**: `ruff check .` clean, `pytest -q` 520 passed (all fake models, CI never calls a model), 3 import-linter contracts kept, `npm run build` green.
- **Demo data is fictional** (company names, contacts and the `1380000xxxx` phone numbers are invented). This is a reference implementation, not a production-ready product — review every AI output before use.

## License

[MIT](LICENSE) © 2026 ytongyaop
