# 2026-11-17 v2 任务：分拣中转站

> 依据 `specs/2026-11-17-v2-sorting/plan.md`。一个增量一个可独立验收的交付，结束必须能跑、能看。
> 全局验证前缀：`.\\.venv\\Scripts\\python.exe -m pytest -q`（基线 247 passed）、`.\\.venv\\Scripts\\python.exe -m ruff check .`、`$env:PYTHONPATH='src'; .\\.venv\\Scripts\\lint-imports.exe`。
> 每个增量收尾：跑验收命令 → 贴输出 → 确认 → commit。带 🔒 的是 schema / 口径类高风险增量，单独停。

## 阶段 A：数据与分拣（S1）

- [x] **[T001] 数据层 + 迁移 + 不回归** 🔒
  - 目标：`facts` 多一个 `card_section` 列，新增 `sorting_batches` / `grading_history` / `preference_samples` 三张只追加表，旧库能平滑打开、v1 的 247 条不回归。
  - 涉及文件：`src/salescopilot/adapters/db/sqlite.py`、`src/salescopilot/domain/models.py`、`src/salescopilot/domain/ports.py`、`tests/test_migration_v2.py`（新建）、`tests/test_fact_fields_sync.py`（改断言）
  - 验收命令：`.\.venv\Scripts\python.exe -m pytest -q` → 247 passed；`.\.venv\Scripts\python.exe -m pytest -q tests\test_migration_v2.py` → 全绿；`$env:PYTHONPATH='src'; lint-imports` → 契约全过
  - 预估：6h　依赖：无

- [x] **[T002] 归属三级漏斗 + 审计日志**
  - 目标：`domain/matching.py` 实现规范化 → L0 精确键 → L1 字符串（rapidfuzz）→ L2 向量 的短路漏斗，按模板阈值给出 `auto` / `review` / `new_lead`，每次判定带 `scorer` / `score` / `threshold` / 候选列表；L1 ≥95、L2 ≥0.92 都必须有第二字段佐证才自动，向量永不单独授权合并。
  - 涉及文件：`src/salescopilot/domain/matching.py`（新建）、`src/salescopilot/domain/sorting.py`（新建，`card_section_of` 与出库优先级）、`tests/test_matching_domain.py`（新建）、`tests/test_sorting_domain.py`（新建）
  - 验收命令：`.\.venv\Scripts\python.exe -m pytest -q tests\test_matching_domain.py tests\test_sorting_domain.py`；`-m pytest -q tests\test_no_industry_words.py`；用例覆盖 L1 94.9/95.0/85.0/84.9 与 L2 0.919/0.92/0.80/0.799 边界
  - 预估：8–10h　依赖：T001（要用 `card_section` 常量）

- [x] **[T002b] 阈值校准脚本 + 50 对标注样本** 🔒（口径）
  - 目标：用 50 对人工标注的客户对子扫 precision / recall，产出建议阈值并可写回模板 `matching:` 段；校准报告落 `data/reports/`。
  - 涉及文件：`data/evalsets/entity_pairs.jsonl`（新建，50 对）、`scripts/calibrate_thresholds.py`（新建）、`data/reports/matching_calibration.md`（脚本产出）、`requirements-dev.txt`（追加 `rapidfuzz`）、`tests/test_calibrate_thresholds.py`（新建）
  - 验收命令：`.\.venv\Scripts\python.exe scripts\calibrate_thresholds.py --dry-run` 打印 50 对上的阈值扫描表与建议值；`.\.venv\Scripts\python.exe -m pytest -q tests\test_calibrate_thresholds.py`
  - 预估：3h　依赖：T002

- [x] **[T002c] 校准补全（funnel 模型）+ 键名统一 + 人读报告**
  - 目标：`calibrate_thresholds.py` 增 `--model funnel`（L0 精确键 → `suffix_only` 复核 → L1 阈值），与 `l1` 并列输出；模板阈值键统一为 `matching:` 嵌套段（`l1_auto` / `l1_review`，重复写不追加）；产出人读报告 `data/reports/matching_calibration.md`（样本构成、95 分 3 对误配、结论与局限）。
  - 涉及文件：`scripts/calibrate_thresholds.py`、`tests/test_calibrate_thresholds.py`（新建）、`data/reports/matching_calibration.md`（新建）、`docs/进度.md`、`specs/2026-11-17-v2-sorting/plan.md`
  - 验收命令：`.\.venv\Scripts\python.exe scripts\calibrate_thresholds.py --model funnel`；`.\.venv\Scripts\python.exe scripts\calibrate_thresholds.py --json %TEMP%\cal2.json`；`.\.venv\Scripts\python.exe -m pytest -q` 全绿
  - 预估：2h　依赖：T002b

- [ ] **[T003] 一次投料 → 分拣单（fake LLM + 出处对齐校验）**
  - 目标：`POST /sorting` 一次投料产出含归属 / 要素 / 分级 / 出库四块的分拣单，每块带能在原文定位到的 `source_span`；归属只在 `band='review'` 且候选 ≤3 时调 LLM 复核（其余档位不调，可断言调用次数）；`source_span` 落库前过 `domain/span_align.py`，对不上的字段丢弃并记 `span_misaligned` 诊断。
  - 涉及文件：`src/salescopilot/adapters/llm/prompts_sorting.py`（新建）、`src/salescopilot/domain/span_align.py`（新建）、`src/salescopilot/adapters/llm/fake.py`、`src/salescopilot/adapters/llm/deepseek.py`、`src/salescopilot/domain/ports.py`、`src/salescopilot/services/sorting.py`（新建）、`src/salescopilot/entrypoints/api/app.py`、`src/salescopilot/entrypoints/bootstrap.py`、`tests/test_sorting_service.py`（新建）、`tests/test_sorting_api.py`（新建）、`tests/test_span_align.py`（新建）
  - 验收命令：`.\.venv\Scripts\python.exe -m pytest -q tests\test_sorting_service.py tests\test_sorting_api.py tests\test_span_align.py`；`/docs` 里发一次 `POST /sorting` 看到四块 JSON 且 `source_span` 能在原文定位
  - 预估：9h　依赖：T002、T002b

- [ ] **[T004] 分拣确认与待认领池**
  - 目标：同意 / 改归属 / 忽略三个动作各走通且只追加；`band='review'`（灰区无佐证 / 多候选 / L2 落在 0.80–0.92）的投料进待认领池，不落客户。
  - 涉及文件：`src/salescopilot/services/sorting.py`、`src/salescopilot/adapters/db/sqlite.py`、`src/salescopilot/entrypoints/api/app.py`、`tests/test_sorting_confirm.py`（新建）
  - 验收命令：`.\.venv\Scripts\python.exe -m pytest -q tests\test_sorting_confirm.py` ＋ 全量 `pytest -q`；`GET /sorting/batches?pending_claim=1` 能过滤出在池的单
  - 预估：6h　依赖：T003

## 阶段 B：信息卡与分级（S2–S3）

- [ ] **[T005] 四要素信息卡与缺项问题句**
  - 目标：`GET /leads/{id}/card` 返回覆盖度、四要素分组与缺项清单，每个缺项带"下次该问什么"的具体问题句。
  - 涉及文件：`src/salescopilot/domain/card.py`（新建）、`src/salescopilot/services/card.py`（新建）、`src/salescopilot/entrypoints/api/app.py`、`src/salescopilot/templates/*.yaml`（加 `card.fields` 数据）、`tests/test_card_domain.py`（新建）、`tests/test_card_api.py`（新建）
  - 验收命令：`.\.venv\Scripts\python.exe -m pytest -q tests\test_card_domain.py tests\test_card_api.py`；接口返回 `missing[]` 每项 `question` 非空（断言）
  - 预估：6h　依赖：T001

- [ ] **[T006] 分级打分 + 双模式 + 留痕三列** 🔒（口径）
  - 目标：权重从模板读，四维打分出 A/B/C + 理由 + 依据；提议 / 直接定两种模式都写 `grading_history`（AI 值 / 人工值 / 差异原因）。
  - 涉及文件：`src/salescopilot/domain/grading.py`（新建）、`src/salescopilot/services/grading.py`（新建）、`src/salescopilot/adapters/template/loader.py`、`src/salescopilot/entrypoints/api/app.py`、`src/salescopilot/templates/*.yaml`（加 `grading_weights`）、`tests/test_grading_domain.py`（新建）、`tests/test_grading_service.py`（新建）、`tests/test_templates.py`（改）
  - 验收命令：`.\.venv\Scripts\python.exe -m pytest -q tests\test_grading_domain.py tests\test_grading_service.py tests\test_templates.py` ＋ 全量 `pytest -q`；改判后查 `grading_history` 多一行三列齐全
  - 预估：8h　依赖：T001、T005（缺项参与匹配度打分）

## 阶段 C：待办与复盘（S4–S5）

- [ ] **[T007] 今日待办 + "本次提供的价值"** 🔒（schema）
  - 目标：`GET /todo` 按 A/B/C → 超时 → 缺项排序，每家最多一条，同分取最久未联系；每条带建议动作 + 价值点，无价值点不出现；跟进可标记"无价值跟进"。
  - 涉及文件：`src/salescopilot/adapters/db/sqlite.py`、`src/salescopilot/domain/models.py`、`src/salescopilot/domain/rules.py`（只加一条标记规则，三要素校验不动）、`src/salescopilot/services/followup.py`、`src/salescopilot/services/todo.py`（新建）、`src/salescopilot/entrypoints/api/app.py`、`tests/test_todo_service.py`（新建）、`tests/test_followup_service.py`（改）
  - 验收命令：`.\.venv\Scripts\python.exe -m pytest -q tests\test_todo_service.py tests\test_followup_service.py` ＋ 全量 `pytest -q`；`GET /todo` 返回每条 `value` 非空、`lead_id` 不重复
  - 预估：6h　依赖：T005、T006

- [ ] **[T008] 周复盘 + 3 个校准问题**
  - 目标：`GET /review` 汇总本周推进 / 卡点 / 分级变动 / 无价值跟进数，并接受 3 个校准问题作答写入 `preference_samples`。
  - 涉及文件：`src/salescopilot/services/review.py`（新建）、`src/salescopilot/entrypoints/api/app.py`、`tests/test_review_service.py`（新建）
  - 验收命令：`.\.venv\Scripts\python.exe -m pytest -q tests\test_review_service.py`；`GET /review` 四块内容非空、`no_value_count ≥ 1` 可复现；作答后 `preference_samples` 加 3 行
  - 预估：5h　依赖：T007

## 阶段 D：前端与换场（S6–S7）

- [ ] **[T009] 五入口前端重构**
  - 目标：分拣台 / 客户 / 客户详情 / 复盘 / 设置五个入口串成一条链，所有数字只来自后端接口，后端挂掉显示业务语言错误。
  - 涉及文件：`web/src/api.js`、`web/src/App.vue`、`web/src/pages/SortingDesk.vue`（新建）、`LeadsShelf.vue`（新建）、`LeadDetail.vue`（新建）、`Review.vue`（新建）、`Settings.vue`（新建）、`web/src/main.js`（必要时）
  - 验收命令：`cd web; npm run build` 通过；`npm run dev` + 后端跑起来，逐页观察五个入口（投料 → 同意 → 客户出现 → 详情见要素与分级 → 复盘见记录 → 设置切模式生效）
  - 预估：10h　依赖：T004、T005、T006、T008

- [ ] **[T010] ToB 模板 + 演示资料库 + 种子数据 + 全量验收** 🔒（换场口径）
  - 目标：ToB 成为主演示路径，教培降为第二模板且仍通过校验；清库播种后五入口全链路一次跑通。
  - 涉及文件：`src/salescopilot/templates/tob_enterprise.yaml`（新建）、`src/salescopilot/templates/edu_training.yaml`（只补数据）、`data/knowledge/tob_*.md`（新建）、`scripts/seed_demo.py`、`tests/test_templates.py`、`docs/演示脚本.md`、`web/README.md`
  - 验收命令：`.\.venv\Scripts\python.exe -m pytest -q` 全量绿；`.\.venv\Scripts\python.exe scripts\seed_demo.py` 后按 `docs/演示脚本.md` 走通五入口；`-m pytest -q tests\test_no_industry_words.py`
  - 预估：6h　依赖：T009

## 汇总

- 增量数：**11**（阶段 A 5 个：T001 / T002 / T002b / T003 / T004；B 2 个；C 2 个；D 2 个）
- 总预估：**73 小时**（6 + 9 + 3 + 9 + 6 + 6 + 8 + 6 + 5 + 10 + 6；T002 取 8–10h 的中值 9h），不含真实模型联调与演示排练；每个增量结束按实际耗时回填。
- 依赖关系：**T002 → T002b → T003**（阈值先校准、再让分拣链路用上校准值，避免拿拍脑袋的阈值去跑真实投料）；其余依赖不变：T001 → T002 / T005，T003 → T004，T004 + T005 + T006 + T008 → T009，T009 → T010。
- 排序原则：先数据层与迁移（T001，保证后面每一步都有地方落）→ 再按"分拣（含阈值校准）→ 卡片 → 分级 → 出库"的数据依赖推进 → 前端最后接（后端接口定型后再画页面）→ 最后换场并做全量验收。不勾完上一阶段不进下一阶段。
- 并行机会：T002 与 T005 互不依赖，可并行；T002b 的 50 对标注样本可以提前单独标（纯人工，不占开发工时）；T009 的两个页面组（分拣台+客户 / 详情+复盘+设置）可由两人分头做，但共用 `api.js` 需先合并一次。
