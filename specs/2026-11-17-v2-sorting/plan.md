# 2026-11-17 v2 计划：分拣中转站（ToB 主场景）

> 依据：`specs/2026-11-17-v2-sorting/spec.md`（已确认，含 8 条已决决策）＋ `workflows\开发.md` 计划六项判据（拆步 / 改动清单 / 每步验证 / 回退点 / 不碰清单 / 风险与未知）。
> 本文件只写"改哪些文件 + 每步怎么验证"，不写实现代码。没列进改动清单的文件一律不许改。

## 六项判据对照

| 判据 | 落在哪 |
|---|---|
| 拆步 | 阶段 A–D 共 10 个增量（A1–A4 / B1–B2 / C1–C2 / D1–D2），每个增量能独立跑通，不留半成品 |
| 改动清单 | 每个增量下的「改动清单」，逐条写文件路径与新建 / 修改 |
| 每步验证 | 每个增量下的「验证」，一条可粘贴的命令或一次可观察操作 |
| 回退点 | 每个「阶段收尾」的回退点 + 文末「回退总则」 |
| 不碰清单 | 每个「阶段收尾」的不碰清单 |
| 风险与未知 | 每个「阶段收尾」的风险 + 文末「跨阶段风险与未知」 |

## 全局约定

- 工作目录 `D:\workspace\work\python\sales-agent-v2`；后端解释器 `.venv\Scripts\python.exe`。
- 三条固定验证命令（除单独写明外，每个增量收尾都要跑）：
  - `.\\.venv\\Scripts\\python.exe -m pytest -q` —— 基线是 **247 passed**，新用例只能加不能减。
  - `.\\.venv\\Scripts\\python.exe -m ruff check .`
  - `$env:PYTHONPATH='src'; .\\.venv\\Scripts\\lint-imports.exe` —— 分层契约（`services` 不 import `adapters`、`domain` 只叶子）。
- 一次一个增量：改 → 跑该增量验证 → 贴输出 → 确认 → commit。schema 变更（加表 / 加列）单独停，不与其他改动混在一个 commit。
- 新增后端接口一律挂在 `entrypoints/api/app.py` 现有模式上（路由不带 `/api` 前缀，前端由 Vite 代理补 `/api`）。

---

## 阶段 A：数据与分拣（S1）

**做完能演示**：粘一段通话纪要 → 得到一张分拣单（归属 + 要素 + 分级 + 出库四个判断，各带原文依据），点"同意"后事实落库、点"改归属"后改判可查；归属说不清的那条进待认领池。

### A1 数据层：`facts.card_section` ＋ 三张新表（单独停）

- **改动清单**
  - `src/salescopilot/adapters/db/sqlite.py`（修改）—— `SCHEMA` 新增 `sorting_batches` / `grading_history` / `preference_samples` 三张只追加表；`facts` 加 `card_section TEXT`；新增 `_ensure_column()`（`PRAGMA table_info` + `ALTER TABLE ... ADD COLUMN`）承接存量库，**不重建表、不搬数据**；`_SELECT_FACT` 与 `FactRepository` 读写带上新列。
  - `src/salescopilot/domain/models.py`（修改）—— `Fact` 加 `card_section: str`（存量空串）；新增 `SortingBatch` / `GradingRecord` / `PreferenceSample` 三个 frozen dataclass。
  - `src/salescopilot/domain/ports.py`（修改）—— 新增 `SortingBatchRepository` / `GradingRepository` / `PreferenceSampleRepository` 三个 Protocol（`add` + 查询，无 update/delete）。
  - `tests/test_migration_v2.py`（新建）—— 旧库（无 `card_section`）打开后列存在且旧行可读；三表存在；只追加（无 update 接口）。
  - `tests/test_fact_fields_sync.py`（修改，仅在断言字段集合处追加 `card_section`）。
- **验证**：`.\.venv\Scripts\python.exe -m pytest -q` → 247 passed（不回归）＋ `-m pytest -q tests\test_migration_v2.py` 通过；`$env:PYTHONPATH='src'; lint-imports` 通过。
- **演示点**：`sqlite3 data/app.sqlite3 ".schema facts"` 能看到 `card_section`，旧数据一条不少。

### A2 `domain/sorting.py`：归属候选打分（纯规则）

- **改动清单**
  - `src/salescopilot/domain/sorting.py`（新建）—— `score_candidates(text_features, candidates) -> list[Candidate]`（公司名 / 联系人 / 联系方式三类命中加权）、`decide(candidates, threshold=0.7) -> Decision`（唯一胜出 → 建议归属；多候选或最高分 < 0.7 → `pending_claim`）；要素块判定 `card_section_of(field)`；出库优先级纯函数。
  - `tests/test_sorting_domain.py`（新建）—— 唯一命中 / 多候选 / 低置信 / 无候选四种走位；阈值边界 0.699 / 0.700；不出现行业词。
- **验证**：`.\.venv\Scripts\python.exe -m pytest -q tests\test_sorting_domain.py`；`-m pytest -q tests\test_no_industry_words.py`。
- **注**：`domain` 不许碰时钟与仓储，`now` 一律由调用方传入。

### A3 `services/sorting.py` ＋ 分拣接口（fake LLM 可测）

- **改动清单**
  - `src/salescopilot/adapters/llm/prompts_sorting.py`（新建）—— 四类判断的提示词与 JSON schema 白名单（归属 / 要素 / 分级 / 出库），含 prompt 版本号。
  - `src/salescopilot/adapters/llm/fake.py`（修改）—— 加 `SortingJudge` 假实现：按原文关键词返回四类判断，可注入"无依据"样例用于验证不落库。
  - `src/salescopilot/adapters/llm/deepseek.py`（修改）—— 真实实现同一 `SortingJudge` 端口（只补一个方法，不动既有 extract / draft）。
  - `src/salescopilot/domain/ports.py`（修改，与 A1 同一文件、不同增量）—— 加 `SortingJudge` Protocol。
  - `src/salescopilot/services/sorting.py`（新建）—— `submit()`：原文 → 调 judge → 用 `domain/sorting.py` 复核 → 落 `sorting_batches`（无 `source_span` 的判断直接丢弃并留诊断）。
  - `src/salescopilot/entrypoints/api/app.py`（修改）—— `POST /sorting`、`GET /sorting/batches` + 两个依赖注入函数。
  - `src/salescopilot/entrypoints/bootstrap.py`（修改）—— 装配 `SortingService` 与 fake / deepseek judge 的切换（沿用现有 `mode` 开关）。
  - `tests/test_sorting_service.py`、`tests/test_sorting_api.py`（新建）。
- **验证**：`.\.venv\Scripts\python.exe -m pytest -q tests\test_sorting_service.py tests\test_sorting_api.py`；真机观察 `POST /sorting` 返回四块内容且每块带 `source_span`。
- **演示点**：`/docs` 里能对 `POST /sorting` 发一次请求并看到分拣单 JSON。

### A4 确认与待认领池：`POST /sorting/{id}/confirm`

- **改动清单**
  - `src/salescopilot/services/sorting.py`（修改）—— `confirm(batch_id, action)`：`agree`（落事实与分级）/ `reassign`（改归属，另起一行留痕）/ `ignore`（整单作废，原文留存）；归属进池的单不落 `lead_id`。
  - `src/salescopilot/adapters/db/sqlite.py`（修改）—— `SortingBatchRepository.list_pending_claim()`。
  - `src/salescopilot/entrypoints/api/app.py`（修改）—— `POST /sorting/{batch_id}/confirm`；`GET /sorting/batches?pending_claim=1`。
  - `tests/test_sorting_confirm.py`（新建）—— 同意后 `facts` 多一行且 `card_section` 正确；改归属后原单仍可查（只追加）；忽略后无新事实、`raw_item` 仍在；置信 0.68 的样例进池。
- **验证**：`.\.venv\Scripts\python.exe -m pytest -q tests\test_sorting_confirm.py` ＋ 全量 `pytest -q`。
- **演示点**：一次投料 → 三个按钮各自的结果都能在库里查到，且原分拣单不被改写。

### 阶段 A 收尾

- **回退点**：`git commit -m "feat(sorting): A 阶段数据层与分拣链路"`。回到该 commit 即"v2 数据层 + 分拣链路可用、v1 页面与流程完全未动"；再往前一个 commit 就是纯 v1（247 条绿）。
- **风险**：① `sqlite.py` 目前只有 `CREATE TABLE IF NOT EXISTS`，**没有迁移机制**，加列是本阶段最容易踩坑的一步 —— 先写迁移测试再用真库跑一遍；② `Fact` 加字段会连带 `test_fact_fields_sync.py`、API 的 `_fact_body` 与前端事实表；③ LLM 一次调用出四类判断还是四次调用未定（见"跨阶段风险"），A3 先按"一次调用 + schema 白名单"实现，端口留得住改成四次。
- **不碰清单**：`domain/blocks.py`（流程块契约）、`domain/rules.py`（阈值与状态转移）、`services/reminder_service.py`（回收逻辑）、`web/`（本阶段前端不动）、教培 / 外包 / 建材三份模板内容。

---

## 阶段 B：信息卡与分级（S2–S3）

**做完能演示**：打开某个客户 → 看到四要素覆盖度与"缺什么 + 下次该问什么"的具体问题句；同页看到 A/B/C 分级、理由、依据片段和改判历史三列；切到"AI 直接定"模式后再投料，分级自动生效但仍留痕。

### B1 信息卡：`domain/card.py` ＋ `services/card.py` ＋ `GET /leads/{id}/card`

- **改动清单**
  - `src/salescopilot/domain/card.py`（新建）—— 四要素 → 字段清单映射（字段名来自模板，不写死行业字段）、`coverage(facts) -> Coverage`、`missing(facts) -> list[Missing]`（每项含 `section` / `field` / `question`）。
  - `src/salescopilot/services/card.py`（新建）—— 按客户取 facts → 调 domain 组装卡片 → 人工补录走既有 `PATCH /leads/{id}/facts/{field}`，不新开写入口。
  - `src/salescopilot/entrypoints/api/app.py`（修改）—— `GET /leads/{lead_id}/card`（`coverage` / `sections[]` / `missing[]`）。
  - `src/salescopilot/templates/*.yaml`（修改，三份都加 `card.fields` 分节，只加数据不改块契约）。
  - `tests/test_card_domain.py`、`tests/test_card_api.py`（新建）。
- **验证**：`.\.venv\Scripts\python.exe -m pytest -q tests\test_card_domain.py tests\test_card_api.py`；缺项每项 `question` 非空（断言，不靠肉眼）。
- **演示点**：客户详情接口返回覆盖度 `7/8` 与两条带问题句的缺项。

### B2 分级：`domain/grading.py` ＋ `services/grading.py` ＋ 双模式留痕

- **改动清单**
  - `src/salescopilot/domain/grading.py`（新建）—— 四维打分（金额 / 紧迫度 / 决策清晰 / 匹配度）＋ 模板权重 → A/B/C；`diff(ai_value, manual_value, reason)` 校验"改判必须写原因"；无依据片段时返回 `no_evidence` 而不是给分。
  - `src/salescopilot/adapters/template/loader.py`（修改）—— 校验 `grading_weights` 四维和为 1、缺项报错并指明位置（沿用现有校验风格）。
  - `src/salescopilot/services/grading.py`（新建）—— `propose()` / `apply()`；双模式（提议 / 直接定）；每次变更写 `grading_history` 一行（AI 值 / 人工值 / 差异原因 / 依据 / 操作人 / 时间）。
  - `src/salescopilot/entrypoints/api/app.py`（修改）—— `GET /leads/{lead_id}/grading`、`POST /leads/{lead_id}/grading`（人工改判）、`GET /settings` / `PATCH /settings`（全局模式开关，值只允许两枚）。
  - `tests/test_grading_domain.py`、`tests/test_grading_service.py`、`tests/test_templates.py`（修改，覆盖权重校验）。
- **验证**：`.\.venv\Scripts\python.exe -m pytest -q tests\test_grading_domain.py tests\test_grading_service.py tests\test_templates.py`；`pytest -q` 全量。
- **演示点**：B→A 改判后 `grading_history` 多一行三列齐全；切到"直接定"后新分拣单自动带分级且历史有记录。

### 阶段 B 收尾

- **回退点**：`git commit -m "feat(card,grading): B 阶段信息卡与分级"`。回到该 commit：卡片与分级可用、分拣链路仍可用、v1 一切照旧；回退到阶段 A 结尾 commit 即"只有数据层与分拣"。
- **风险**：① `templates/*.yaml` 加 `card.fields` / `grading_weights` 会同时动校验器与三份模板，`test_templates.py`、`test_template_loader.py` 会连锁失败 —— 先改校验器再改模板；② 缺项"问题句"最容易写成行业词硬编码，`test_no_industry_words.py` 会当场抓；③ 权重和必须为 1，浮点比较用容差，不要 `==`。
- **不碰清单**：`services/sorting.py` 的分拣落库逻辑（B 阶段只调用不改）、v1 的提炼管线（`services/ingest.py`）、`facts` 锁定语义、`web/`。

---

## 阶段 C：待办与复盘（S4–S5）

**做完能演示**：打开今日待办 → 看到按 A/B/C + 超时 + 缺项排好的一列，每条带"建议动作 + 给什么价值"，每家最多一条，缺价值点的不出现；打开复盘 → 看到本周推进 / 卡点 / 分级变动，回答 3 个校准问题并落进 `preference_samples`。

### C1 待办：`services/todo.py` ＋ `followups` 加"本次提供的价值"（schema，单独停）

- **改动清单**
  - `src/salescopilot/adapters/db/sqlite.py`（修改）—— `followups` 加 `value_provided TEXT`（复用 A1 的 `_ensure_column`），`_SELECT_FOLLOWUP` 与 `FollowupRepository` 带上。
  - `src/salescopilot/domain/models.py`（修改）—— `Followup` 加 `value_provided: str`。
  - `src/salescopilot/domain/rules.py`（修改，只加一条规则）—— 三要素校验保持不变，"本次提供的价值"为空**不阻塞保存**，只标 `no_value`。
  - `src/salescopilot/services/followup.py`（修改）—— 透传新字段；`entrypoints/api/app.py` 的跟进入参 / 出参同步。
  - `src/salescopilot/services/todo.py`（新建）—— 排序：A/B/C 降序 → 超时（已过期 > 今天 > 本周）→ 缺项多者优先；每家最多一条；同分取最久未联系；每条带"建议动作 + 价值点"，**无价值点直接过滤**。
  - `src/salescopilot/entrypoints/api/app.py`（修改）—— `GET /todo`；`GET /leads/{lead_id}/followups` 返回 `no_value`。
  - `tests/test_todo_service.py`（新建）、`tests/test_followup_service.py` / `tests/test_reminders_rules.py`（修改，断言不回归）。
- **验证**：`.\.venv\Scripts\python.exe -m pytest -q tests\test_todo_service.py tests\test_followup_service.py` ＋ 全量 `pytest -q`。
- **演示点**：`GET /todo` 返回里每条 `value` 非空、每个 `lead_id` 只出现一次、顺序符合三级规则。

### C2 复盘：`services/review.py` ＋ `GET /review`

- **改动清单**
  - `src/salescopilot/services/review.py`（新建）—— 周汇总（推进 / 卡点 / 分级变动 / 无价值跟进计数）＋ 3 个校准问题与作答入口 → 写 `preference_samples`（只追加，不回改历史分级）。
  - `src/salescopilot/entrypoints/api/app.py`（修改）—— `GET /review?week=`、`POST /review/answers`。
  - `tests/test_review_service.py`（新建）—— `no_value_count ≥ 1` 可复现；答案落库；历史 `grading_history` 不被改写。
- **验证**：`.\.venv\Scripts\python.exe -m pytest -q tests\test_review_service.py` ＋ 全量 `pytest -q`；`GET /review` 观察四块内容非空。
- **演示点**：复盘返回里能同时看到"本周分级变动 2 条、无价值跟进 1 次、卡点 1 条"，作答后 `preference_samples` 加 3 行。

### 阶段 C 收尾

- **回退点**：`git commit -m "feat(todo,review): C 阶段待办与复盘"`。回到该 commit：出库口两个接口可用；回到阶段 B 结尾 commit 即"卡片 + 分级可用、待办与复盘未做"，`followups` 多一列不影响旧读。
- **风险**：① `followups` 加列是第二个 schema 变更，旧跟进记录的 `value_provided` 为空会被判成"无价值跟进"—— 迁移时旧行写 `''` 并在复盘口径里注明"v2 前记录不参与统计"，否则首周复盘数字会失真；② 待办排序同分规则需要 `now` 注入，`services` 传时钟、`domain` 不碰（否则违反分层测试）；③ "无价值点不出现"依赖出库建议的质量，fake LLM 必须能构造出"有价值 / 无价值"两种样例。
- **不碰清单**：`domain/grading.py` 的打分公式（C 只读调用）、`services/reminder_service.py` 的回收口径、`dashboard` 指标口径、`web/`。

---

## 阶段 D：前端与换场（S6–S7）

**做完能演示**：五个入口（分拣台 / 客户 / 客户详情 / 复盘 / 设置）串成一条链，用 ToB 种子数据从头走一遍"投料 → 分拣 → 货架 → 出库"；教培模板仍在、校验仍通过。

### D1 五个入口重构（`web/`）

- **改动清单**
  - `web/src/api.js`（修改）—— 补 `/sorting`、`/sorting/batches`、`/card`、`/grading`、`/todo`、`/review`、`/settings` 七个调用。
  - `web/src/App.vue`（修改）—— 顶部五入口导航 + 路由状态（**不引入 vue-router**，用 `ref` 切换页面，避免为一个纯前端分支加依赖；要加依赖先停下来问）。
  - `web/src/pages/SortingDesk.vue`、`LeadsShelf.vue`、`LeadDetail.vue`、`Review.vue`、`Settings.vue`（新建）—— 每页只读后端返回，不复制阈值 / 权重 / 排序规则；分拣台四块判断 + 四个操作；客户详情四要素 + 缺项问题句 + 分级历史三列。
  - `web/src/main.js`（修改，仅在挂载方式变化时）。
- **验证**：`cd web; npm run build` 通过；起后端 + `npm run dev`，逐页观察（分拣台投一次料 → 同意 → 客户页出现该客户 → 客户详情看到要素与分级 → 复盘页看到本周记录 → 设置页切模式生效）。后端停掉时每页显示业务语言错误而不是白屏。
- **演示点**：五个入口点一遍，不出现"页面各自为政"的死链；任何数字都能追到后端接口。

### D2 ToB 模板 ＋ 演示资料库 ＋ 种子数据（教培保留）

- **改动清单**
  - `src/salescopilot/templates/tob_enterprise.yaml`（新建）—— ToB 流程块组合 + 阈值 + `card.fields` + `grading_weights`（ToB 一套）。
  - `src/salescopilot/templates/edu_training.yaml`（修改）—— 只补 `card.fields` 与 `grading_weights`（ToC 一套），块组合一字不动。
  - `data/knowledge/tob_*.md`（新建，3–4 份演示资料：服务能力、验收清单、报价口径、案例）。
  - `scripts/seed_demo.py`（修改）—— 追加 ToB 种子（6–8 家客户、跨 A/B/C、带待认领样例与无价值跟进样例），保留现有教培种子。
  - `web/README.md`、`docs/演示脚本.md`（修改）—— 换场说明：主演示路径改 ToB，教培降为第二模板。
  - `tests/test_templates.py`（修改）—— 四份模板全通过校验；删掉 `appointment` 块后教培失败、建材仍通过（沿用现有断言方式）。
- **验证**：`.\\.venv\\Scripts\\python.exe -m pytest -q` 全量；`.\\.venv\\Scripts\\python.exe scripts\\seed_demo.py` 后按演示脚本走一遍五入口；`pytest -q tests\test_no_industry_words.py`（ToB 端口径不进 `domain/`）。
- **演示点**：清库 → 播种 → 五入口全链路一次跑通，教培模板仍在模板下拉里可用。

### 阶段 D 收尾

- **回退点**：`git commit -m "feat(web,templates): D 阶段五入口与 ToB 换场"`。回到该 commit：可完整演示；回到阶段 C 结尾 commit 即"后端全能力 + 旧前端"，演示降级为接口层。
- **风险**：① 前端从单文件 `App.vue`（现约 480 行）拆五页，容易拆成五个巨型文件 —— 每页先接一个接口再长；② 不引入 vue-router 的取舍若被否，要回头改导航与构建；③ ToB 种子数据里的行业词不得进 `domain/`（测试只扫代码，靠人工守）；④ 演示资料进 `data/knowledge` 后需重建索引（`scripts/build_index.py`），否则话术引用为空、生成被拒。
- **不碰清单**：`docs/PRD.md` 的 v1 章节语义、`domain/blocks.py` 六块封顶、v1 三份模板的块组合、`data/seed` 既有演示数据（只追加不覆盖）、`.importlinter` 契约。

---

## 跨阶段风险与未知

| 项 | 现状 | 处理 |
|---|---|---|
| 四类判断用一次 LLM 调用还是四次 | 未定；影响成本、失败重试粒度与 schema 白名单写法 | A3 先按"一次调用出四块 + 白名单校验"实现，`SortingJudge` 端口留着，改四次不动 services |
| 真实 DeepSeek 输出稳定性 | 只有 fake 可测；真实输出可能缺 `source_span` | 沿用 v1 "无片段即丢弃 + 留诊断"，用 `scripts/run_eval.py` 加一组分拣样例做基线 |
| 存量库迁移 | 目前无迁移机制，只有 `CREATE TABLE IF NOT EXISTS` | A1 引入最小 `_ensure_column`，不引第三方迁移框架；先测旧库再动真库（真库先备份 `data/app.sqlite3`） |
| 待认领池要不要独立页面 | 规格只要求"进池"，未要求专属界面 | A4 先做成 `GET /sorting/batches?pending_claim=1` 过滤视图，D1 挂在分拣台顶部一条 |
| 前端是否引入 vue-router | `package.json` 只有 vue + vite | 默认不加；要加先停下问，别擅自改依赖 |
| `AGENTS.md` 里"AI 只负责生成话术草稿与归类异议" | 与 v2 口径冲突 | 不在本次改动范围（只改 PRD / 架构），列入待办由主管决定是否同步 |
| 工时估算 | 66 小时为拆分估算，未含真实模型联调与演示排练 | 每个增量结束按实际耗时回填 `tasks.md` |

## 回退总则

- 每个增量一次 commit；每个阶段结束一个可演示的 commit（阶段 A/B/C/D 各一个），阶段内增量可 squash。
- 回退到某个阶段：`git reset --hard <该阶段收尾 commit>`；只回退单个增量：`git revert <增量 commit>`（不改写已推送历史）。
- 数据回退：schema 只加列不删列，旧代码读新库仍可用（新列被忽略）；真库动 schema 前先复制 `data/app.sqlite3` 到 `data/app.sqlite3.bak-<日期>`。
