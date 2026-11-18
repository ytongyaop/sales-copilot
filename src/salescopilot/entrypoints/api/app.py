"""FastAPI 应用实例。

路由只做三件事：解析入参 → 调 service → 把结果映射成 HTTP 响应。校验规则一律在
`domain/rules.py`，这里不重复判断任何字段；业务语言的中文提示也在这里统一收口，
保证前端只需要认一种错误体：`{"issues": [{"location", "message"}]}`。
"""

from collections.abc import Iterator
from dataclasses import asdict
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.requests import Request

from salescopilot.domain.blocks import BLOCKS
from salescopilot.domain.card import Card
from salescopilot.domain.dashboard import Dashboard
from salescopilot.domain.grading import GradingResult
from salescopilot.domain.labels import block_label
from salescopilot.domain.models import (
    DEFAULT_TEMPLATE,
    ExtractRun,
    Fact,
    Followup,
    GradingRecord,
    Lead,
    RawItem,
    StageEvent,
)
from salescopilot.domain.reminders import Reminder
from salescopilot.domain.review import ReviewSummary
from salescopilot.domain.rules import ValidationIssue
from salescopilot.domain.shelf import ShelfEntry
from salescopilot.domain.sorting import SortingSuggestion
from salescopilot.domain.todo import TodoItem
from salescopilot.entrypoints.bootstrap import (
    build_assignment_service,
    build_card_service,
    build_dashboard_service,
    build_draft_service,
    build_flow_service,
    build_followup_service,
    build_grading_service,
    build_import_service,
    build_ingest_service,
    build_lead_service,
    build_reclaim_service,
    build_reminder_service,
    build_review_service,
    build_shelf_service,
    build_sorting_service,
    build_todo_service,
    check_live_config,
    mode_info,
    templates,
)
from salescopilot.services.assignment import AssignmentService
from salescopilot.services.card import CardService
from salescopilot.services.dashboard import DashboardService
from salescopilot.services.draft import DraftService
from salescopilot.services.flow import FlowService
from salescopilot.services.followup import FollowupService
from salescopilot.services.grading import GradingService
from salescopilot.services.import_service import ImportService
from salescopilot.services.ingest import IngestService
from salescopilot.services.leads import LeadService
from salescopilot.services.reminder_service import ReclaimService, ReminderService
from salescopilot.services.review import ReviewService
from salescopilot.services.shelf import ShelfService
from salescopilot.services.sorting import SortingService
from salescopilot.services.todo import TodoService

app = FastAPI(title="SalesCopilot", version="0.1.0")

# 启动自检：开了真模型却缺 key 就在这里抛 ConfigError（并打印中文提示），
# 不让进程「看起来起来了、实际悄悄用假模型」。
check_live_config()

# pydantic 层校验失败时，把字段名翻译成业务语言（不含行业词，只有通用字段）。
FIELD_LABELS = {
    "name": "名称",
    "contact": "联系方式",
    "source": "来源",
    "owner": "归属人",
    "template": "模板",
    "target_stage": "目标阶段",
    "conclusion": "跟进结论",
    "objection": "客户异议",
    "next_followup_at": "下次跟进时间",
    "lead_id": "线索",
    "content": "内容",
    "value": "取值",
    "csv_text": "CSV 内容",
    "default_owner": "默认归属人",
    "action": "处置动作",
    "reason": "理由",
    "create_name": "客户名称",
    "mode": "分级模式",
    "actor": "操作者",
    "answers": "答题列表",
    "key": "问题编号",
    "answer": "回答",
}


class LeadIn(BaseModel):
    """POST /leads 的请求体：只声明形状，取值合法性交给 domain 规则。"""

    name: str
    contact: str
    source: str
    owner: str | None = None
    template: str = DEFAULT_TEMPLATE


class AdvanceIn(BaseModel):
    """POST /leads/{id}/advance 的请求体：只想推进到哪一步。"""

    target_stage: str


class LeadFieldsIn(BaseModel):
    """PATCH /leads/{id}/fields 的请求体：只想补哪几个字段（键由 domain 白名单校验）。"""

    fields: dict[str, str]


class FollowupIn(BaseModel):
    """POST /leads/{id}/followups 的请求体：跟进三要素 ＋ 可选的「本次提供的价值」。

    `value_provided` 不填就是空串（显式标记「无价值跟进」），不当作缺字段报 422。
    """

    conclusion: str
    objection: str
    next_followup_at: str
    value_provided: str = ""


class RawIn(BaseModel):
    """POST /leads/{id}/raws 的请求体：一段原文 + 来源。"""

    content: str
    source: str


class FactPatchIn(BaseModel):
    """PATCH /leads/{id}/facts/{field} 的请求体：只给新取值。"""

    value: str


class DraftConfirmIn(BaseModel):
    """POST /leads/{id}/drafts/{draft_id}/confirm 的请求体：人工补的异议与下次跟进时间。

    `value_provided` 不填就是空串（显式标记「这次跟进没给客户新东西」），不当作缺字段报 422。
    """

    objection: str
    next_followup_at: str
    value_provided: str = ""


class ClaimIn(BaseModel):
    """POST /leads/{id}/claim 的请求体：认领人。"""

    owner: str


class ImportIn(BaseModel):
    """POST /leads/import 的请求体：CSV 原文 + 可选的默认归属人。"""

    csv_text: str
    default_owner: str | None = None


class SortingIn(BaseModel):
    """POST /sorting 的请求体：一段原文 + 来源 + 可选的已知线索。

    `lead_id` 给了就是「这票货已经确定是哪家」；不给就走归属漏斗，灰区进待认领池。
    """

    content: str
    source: str
    lead_id: str | None = None


class SortingConfirmIn(BaseModel):
    """POST /sorting/{id}/confirm 的请求体：处置动作 + 可选的改归属目标 / 拒绝理由。

    `action` 只认 accept（同意）/ fix（改归属后同意）/ reject（拒绝）；
    取值合不合法由用例层的 `domain/sorting.py` 判，这里只声明形状。
    """

    action: str
    lead_id: str | None = None
    reason: str | None = None


class SortingClaimIn(BaseModel):
    """POST /sorting/{id}/claim 的请求体：挂已有客户（`lead_id`）或新建（`create_name`）。"""

    lead_id: str | None = None
    create_name: str | None = None
    owner: str | None = None


class GradingDecideIn(BaseModel):
    """POST /leads/{id}/grading/decide 的请求体：定档 + 改判理由 + 可选模式与操作者。

    `value` 只能是 A / B / C；`reason` 在改档时必填（≥2 字），由用例层判。
    """

    value: str
    reason: str | None = None
    mode: str | None = None
    actor: str | None = None


class ReviewAnswerIn(BaseModel):
    """一条校准作答：问题编号 + 回答正文。

    `key` 只认那三个固定编号、`answer` 至少 2 字，判定在 `domain/review.py`
    （这里只声明形状，不在路由里重复规则）。
    """

    key: str
    answer: str


class ReviewAnswersIn(BaseModel):
    """POST /review/answers 的请求体：1–3 条作答 + 可选操作者。

    `answers` 为空数组不在这里拦（形状合法），由用例层报「至少要回答一个问题」。
    """

    answers: list[ReviewAnswerIn]
    actor: str | None = None


def get_lead_service() -> Iterator[LeadService]:
    """每个请求装配一次：数据库路径随环境变量变化，测试可直接 monkeypatch 环境变量。

    仓储每次操作自开自闭连接，依赖里不需要持有、也不需要显式关闭连接。
    """
    yield build_lead_service()


def get_flow_service() -> Iterator[FlowService]:
    yield build_flow_service()


def get_followup_service() -> Iterator[FollowupService]:
    yield build_followup_service()


def get_ingest_service() -> Iterator[IngestService]:
    yield build_ingest_service()


def get_draft_service() -> Iterator[DraftService]:
    yield build_draft_service()


def get_assignment_service() -> Iterator[AssignmentService]:
    yield build_assignment_service()


def get_reminder_service() -> Iterator[ReminderService]:
    yield build_reminder_service()


def get_reclaim_service() -> Iterator[ReclaimService]:
    yield build_reclaim_service()


def get_dashboard_service() -> Iterator[DashboardService]:
    yield build_dashboard_service()


def get_import_service() -> Iterator[ImportService]:
    yield build_import_service()


def get_sorting_service() -> Iterator[SortingService]:
    yield build_sorting_service()


def get_card_service() -> Iterator[CardService]:
    yield build_card_service()


def get_grading_service() -> Iterator[GradingService]:
    yield build_grading_service()


def get_todo_service() -> Iterator[TodoService]:
    yield build_todo_service()


def get_review_service() -> Iterator[ReviewService]:
    yield build_review_service()


def get_shelf_service() -> Iterator[ShelfService]:
    yield build_shelf_service()


LeadDep = Annotated[LeadService, Depends(get_lead_service)]
FlowDep = Annotated[FlowService, Depends(get_flow_service)]
FollowupDep = Annotated[FollowupService, Depends(get_followup_service)]
IngestDep = Annotated[IngestService, Depends(get_ingest_service)]
DraftDep = Annotated[DraftService, Depends(get_draft_service)]
AssignmentDep = Annotated[AssignmentService, Depends(get_assignment_service)]
ReminderDep = Annotated[ReminderService, Depends(get_reminder_service)]
ReclaimDep = Annotated[ReclaimService, Depends(get_reclaim_service)]
DashboardDep = Annotated[DashboardService, Depends(get_dashboard_service)]
ImportDep = Annotated[ImportService, Depends(get_import_service)]
SortingDep = Annotated[SortingService, Depends(get_sorting_service)]
CardDep = Annotated[CardService, Depends(get_card_service)]
GradingDep = Annotated[GradingService, Depends(get_grading_service)]
TodoDep = Annotated[TodoService, Depends(get_todo_service)]
ReviewDep = Annotated[ReviewService, Depends(get_review_service)]
ShelfDep = Annotated[ShelfService, Depends(get_shelf_service)]


def _issue_body(issues: list[ValidationIssue]) -> dict:
    return {"issues": [{"location": i.location, "message": i.message} for i in issues]}


def _lead_body(lead: Lead) -> dict:
    """一条线索的完整视图。

    `fields` 是推进时要看的字段（键是块契约的 `requires`），前端据它算「还缺什么」；
    `contact` / `source` 同时是实体属性，不会重复放进 `fields`。
    """
    return {
        "id": lead.id,
        "name": lead.name,
        "contact": lead.contact,
        "source": lead.source,
        "stage": lead.stage,
        "owner": lead.owner,
        "created_at": lead.created_at,
        "template": lead.template,
        "fields": dict(lead.fields),
    }


def _stage_event_body(event: StageEvent) -> dict:
    return {
        "id": event.id,
        "lead_id": event.lead_id,
        "from_stage": event.from_stage,
        "to_stage": event.to_stage,
        "at": event.at,
    }


def _followup_body(item: Followup) -> dict:
    return {
        "id": item.id,
        "lead_id": item.lead_id,
        "conclusion": item.conclusion,
        "objection": item.objection,
        "next_followup_at": item.next_followup_at,
        "value_provided": item.value_provided,
        "at": item.at,
    }


def _raw_body(item: RawItem) -> dict:
    return {
        "id": item.id,
        "lead_id": item.lead_id,
        "source": item.source,
        "content": item.content,
        "content_hash": item.content_hash,
        "at": item.at,
    }


def _fact_body(fact: Fact, *, model: str | None = None) -> dict:
    """一条事实：`model` 只在 `source=llm` 且有账本时给（前端徽标显示 `AI·<模型名>`）。

    `card_section` 是这条事实归信息卡哪一块（空字符串 = 还没归块，v1 的事实都是空）。
    """
    return {
        "id": fact.id,
        "lead_id": fact.lead_id,
        "raw_id": fact.raw_id,
        "field": fact.field,
        "value": fact.value,
        "source": fact.source,
        "source_span": fact.source_span,
        "confidence": fact.confidence,
        "locked": fact.locked,
        "card_section": fact.card_section,
        "model": model,
        "at": fact.at,
    }


def _run_body(run: ExtractRun) -> dict:
    return {
        "id": run.id,
        "raw_id": run.raw_id,
        "model": run.model,
        "prompt_version": run.prompt_version,
        "input_hash": run.input_hash,
        "output_hash": run.output_hash,
        "candidates": run.candidates,
        "accepted": run.accepted,
        "skipped": run.skipped,
        "token": run.token,
        "seconds": run.seconds,
        "at": run.at,
    }


def _reminder_body(reminder: Reminder, *, lead_name: str) -> dict:
    """一条提醒：带 `lead_name`，前端不必为了显示名字再查一遍线索。"""
    return {
        "lead_id": reminder.lead_id,
        "lead_name": lead_name,
        "kind": reminder.kind,
        "level": reminder.level,
        "message": reminder.message,
        "since_minutes": reminder.since_minutes,
    }


def _dashboard_body(dashboard: Dashboard) -> dict:
    """把指标实体映射成 JSON：浮点保留 4 位（前端只负责显示百分比）。"""
    return {
        "total": dashboard.total,
        "pool": dashboard.pool,
        "new_this_week": dashboard.new_this_week,
        "followups_this_week": dashboard.followups_this_week,
        "first_response_24h_rate": round(dashboard.first_response_24h_rate, 4),
        "stale_ratio": round(dashboard.stale_ratio, 4),
        "silent_count": dashboard.silent_count,
        "reminders_by_level": dict(dashboard.reminders_by_level),
    }


def _card_body(card: Card) -> dict:
    """信息卡视图：覆盖度 + 四个分区 + 缺项（每项带「下次该问什么」）。

    `coverage` 保留 4 位小数；分区顺序就是展示顺序（basic → needs → decision → followup），
    `missing` 的顺序与之一致，前端按顺序问就能逐项补齐。
    """
    return {
        "coverage": round(card.coverage, 4),
        "sections": [
            {
                "key": section.key,
                "label": section.label,
                "filled": section.filled,
                "total": section.total,
                "fields": [
                    {
                        "key": item.key,
                        "label": item.label,
                        "question": item.question,
                        "value": item.value,
                        "source": item.source,
                        "span": item.span,
                        "locked": item.locked,
                    }
                    for item in section.fields
                ],
            }
            for section in card.sections
        ],
        "missing": [dict(item) for item in card.missing],
    }


def _grading_body(result: GradingResult) -> dict:
    """一次分级的结果：档次、上下界、四维明细、中文理由与原文依据。

    `low` / `high` 保留 4 位小数：四维齐全时两者相等，跨档时人一眼看出「差哪一档」。
    """
    return {
        "value": result.value,
        "low": round(result.low, 4),
        "high": round(result.high, 4),
        "dims": [
            {
                "key": dim.key,
                "label": dim.label,
                "weight": dim.weight,
                "known": dim.known,
                "score": dim.score,
            }
            for dim in result.dims
        ],
        "reason": result.reason,
        "basis": list(result.basis),
    }


def _grading_record_body(record: GradingRecord) -> dict:
    """一行分级留痕：AI 值 / 人工值 / 理由三列 ＋ 模式、操作者与时间。"""
    return {
        "id": record.id,
        "lead_id": record.lead_id,
        "mode": record.mode,
        "at": record.at,
        "ai_value": record.ai_value,
        "human_value": record.human_value,
        "reason": record.reason,
        "actor": record.actor,
    }


def _shelf_body(entry: ShelfEntry) -> dict:
    """货架上的一张客户小卡：名称 / 归属人 / 档位 / 覆盖度 / 缺项数 / 下次约定 / 风险色点。

    字段名与 `domain/shelf.py` 的 `ShelfEntry` 一一对应，不做改名——前端只认一套词。
    `coverage` 保留 4 位小数（前端画比例条与百分比文字）；
    `reminder_level` 是提醒层的严重度原样透出（`red` / `yellow` / `gray` / 空串），
    前端只拿它上色，不据此重判超时。
    """
    return {
        "lead_id": entry.lead_id,
        "name": entry.name,
        "owner": entry.owner,
        "grade": entry.grade,
        "coverage": round(entry.coverage, 4),
        "filled": entry.filled,
        "total": entry.total,
        "missing_count": entry.missing_count,
        "next_appointment": entry.next_appointment,
        "reminder_level": entry.reminder_level,
    }


def _todo_body(item: TodoItem) -> dict:
    """一条待办：排序说明（`priority` / `due_kind`）+ 为什么跟 + 做什么 + 给什么。

    字段与 `domain/todo.py` 的 `TodoItem` 一一对应，不做改名——前端只需要认一套词。
    """
    return {
        "lead_id": item.lead_id,
        "lead_name": item.lead_name,
        "grade": item.grade,
        "priority": item.priority,
        "due_kind": item.due_kind,
        "reason": item.reason,
        "action": item.action,
        "value_offered": item.value_offered,
        "missing": list(item.missing),
    }


def _review_body(summary: ReviewSummary) -> dict:
    """一张周复盘的原样视图：字段名与 `ReviewSummary` 一一对应，不做改名、不做换算。

    `answered` 不在这里拼：它是「这周答过没有」的接口拼装件，由路由单独加。
    """
    return asdict(summary)


def _assignment_status(issues: list[ValidationIssue]) -> int:
    """归属接口的状态码：没有这条线索 404、状态冲突（已归属/尚未归属）409、其余入参问题 422。"""
    if any(issue.location == "lead_id" for issue in issues):
        return 404
    if any("已归属" in issue.message or "尚未归属" in issue.message for issue in issues):
        return 409
    return 422


@app.exception_handler(RequestValidationError)
async def _request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """把 pydantic 的 422 统一成业务语言，前端只认 `issues` 一种结构。"""
    issues = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"] if part != "body")
        label = FIELD_LABELS.get(location, location or "请求体")
        if error["type"] == "missing":
            message = f"缺少必填字段：{label}"
        elif error["type"] == "string_type":
            message = f"{label}必须是文本"
        else:
            message = f"{label}填写有误"
        issues.append({"location": location, "message": message})
    return JSONResponse(status_code=422, content={"issues": issues})


@app.get("/health")
def health() -> dict:
    """健康检查：进程是否活着 + 当前是真模型还是假模型（演示时一眼能看出）。"""
    info = mode_info()
    return {
        "status": "ok",
        "mode": info["mode"],
        "models": {"chat": info["chat"], "embed": info["embed"]},
    }


def _template_body(key: str, data: dict) -> dict:
    """模板清单的一项：名字 + 阶段顺序 + 每块的中文名 + 每块的必填字段。

    中文名先取模板 YAML 里该块的 `label`（行业叫法只允许出现在这里），
    没有才退回 `domain/labels.py` 的通用叫法；两者都没有就用块 id。
    `requires` 原样透出块契约（`domain/blocks.py`），前端据此算「还缺什么」；
    判定规则仍只在 `domain/rules.py`，路由不做任何判断。
    """
    steps = [
        step
        for step in data.get("flow", [])
        if isinstance(step, dict) and isinstance(step.get("block"), str)
    ]
    labels: dict[str, str] = {}
    requires: dict[str, list[str]] = {}
    for step in steps:
        block_id = step["block"]
        label = step.get("label")
        labels[block_id] = (
            label if isinstance(label, str) and label.strip() else block_label(block_id)
        )
        block = BLOCKS.get(block_id)
        requires[block_id] = [] if block is None else list(block.requires)
    return {
        "key": key,
        "name": str(data.get("name", "")),
        "flow": [step["block"] for step in steps],
        "labels": labels,
        "requires": requires,
    }


@app.get("/templates")
def list_templates() -> list[dict]:
    """模板清单：名字 + 阶段顺序 + 每块中文名（前端据此算「下一阶段」并显示中文名）。"""
    return [_template_body(key, data) for key, data in templates().items()]


def _mapping(value: object) -> dict:
    """模板里的配置段：是映射就原样透出，缺段或类型不对给空对象（不编默认值）。"""
    return dict(value) if isinstance(value, dict) else {}


def _card_field_count(data: dict) -> int:
    """卡面声明的字段总数：设置页只显示「这家模板要问几项」，不做字段级重算。"""
    sections = _mapping(data.get("card")).get("sections")
    if not isinstance(sections, dict):
        return 0
    total = 0
    for section in sections.values():
        fields = section.get("fields") if isinstance(section, dict) else None
        if isinstance(fields, list):
            total += len(fields)
    return total


@app.get("/settings")
def get_settings() -> dict:
    """设置页的只读数据：运行模式 + 模型名 + 每份模板的阈值 / 分级 / 卡面字段数。

    只读：不写配置、不热切模式（模式是进程级开关，改它要改 `SALESCOPILOT_LIVE` 并重启）。
    `thresholds` / `grading` 原样透出模板里的 `thresholds:` / `grading:` 段，
    前端只负责排版成人话，不在这里翻译或改口径。
    """
    info = mode_info()
    return {
        "mode": info["mode"],
        "models": {"chat": info["chat"], "embed": info["embed"]},
        "templates": [
            {
                "key": key,
                "name": str(data.get("name", "")),
                "thresholds": _mapping(data.get("thresholds")),
                "grading": _mapping(data.get("grading")),
                "card_fields": _card_field_count(data),
            }
            for key, data in templates().items()
        ],
    }


@app.post("/leads", status_code=201)
def create_lead(payload: LeadIn, service: LeadDep):
    """新建线索：成功 201 返回完整实体，校验失败 422 返回问题列表。"""
    result = service.create(
        name=payload.name,
        contact=payload.contact,
        source=payload.source,
        owner=payload.owner,
        template=payload.template,
    )
    if isinstance(result, list):
        return JSONResponse(status_code=422, content=_issue_body(result))
    return _lead_body(result)


@app.get("/leads")
def list_leads(service: LeadDep) -> list[dict]:
    """线索列表：按创建时间倒序；空表返回 `[]`。每条都带 `fields`（前端算「还缺什么」）。"""
    return [_lead_body(lead) for lead in service.list()]


@app.patch("/leads/{lead_id}/fields")
def patch_lead_fields(lead_id: str, payload: LeadFieldsIn, service: LeadDep):
    """人工补录线索字段：成功 200 返回更新后的线索（含 `fields`）。

    线索不存在 404；键不在白名单 / 取值为空 / 日期不是 `YYYY-MM-DD` 一律 422 且不落库。
    只覆盖传入的键，其余字段保持原样（合并口径在 `domain/rules.py` 的 `merge_fields`）。
    """
    lead, issues = service.update_fields(lead_id, payload.fields)
    if lead is None:
        return JSONResponse(status_code=404, content=_issue_body(issues))
    if issues:
        return JSONResponse(status_code=422, content=_issue_body(issues))
    return _lead_body(lead)


@app.post("/leads/import")
def import_leads(payload: ImportIn, service: ImportDep) -> dict:
    """CSV 导入：返回 `{"imported", "skipped"}`；同联系方式已存在则跳过（幂等）。

    缺字段、来源不合法的行只记入 `skipped`，不会让整批导入失败。
    """
    return service.import_csv(payload.csv_text, default_owner=payload.default_owner)


@app.get("/leads/pool")
def list_lead_pool(service: AssignmentDep) -> list[dict]:
    """公海：没有归属人的线索，按创建时间倒序；空表返回 `[]`。"""
    return [_lead_body(lead) for lead in service.pool()]


@app.get("/leads/shelf")
def list_lead_shelf(service: ShelfDep) -> list[dict]:
    """客户货架：每条客户一张小卡（档位 / 覆盖度 / 缺项数 / 下次约定），一次取完。

    只读、不写库；顺序与 `GET /leads` 一致。货架上的分组与数字由前端只负责分箱，
    档位与覆盖度都从后端取，不在浏览器里重算。
    """
    return [_shelf_body(entry) for entry in service.list()]


@app.post("/leads/{lead_id}/claim")
def claim_lead(lead_id: str, payload: ClaimIn, service: AssignmentDep):
    """认领一条公海线索：成功 200 返回更新后的线索。

    线索不存在 404；已在别人名下 409；归属人为空 422——都带中文 issues。
    """
    lead, issues = service.claim(lead_id, payload.owner)
    if issues:
        return JSONResponse(status_code=_assignment_status(issues), content=_issue_body(issues))
    return _lead_body(lead)


@app.post("/leads/{lead_id}/release")
def release_lead(lead_id: str, service: AssignmentDep):
    """把线索放回公海：成功 200 返回更新后的线索；线索不存在 404、本来就在公海 409。"""
    lead, issues = service.release(lead_id)
    if issues:
        return JSONResponse(status_code=_assignment_status(issues), content=_issue_body(issues))
    return _lead_body(lead)


@app.get("/reminders")
def list_reminders(service: ReminderDep) -> list[dict]:
    """全部提醒：按严重度倒序；每条带 `lead_name` 便于前端直接展示。"""
    names = service.lead_names()
    return [
        _reminder_body(item, lead_name=names.get(item.lead_id, "")) for item in service.list_all()
    ]


@app.get("/leads/{lead_id}/reminders")
def list_lead_reminders(lead_id: str, service: ReminderDep):
    """该线索的提醒；线索不存在 404，存在但没有提醒返回 `[]`。"""
    items = service.for_lead(lead_id)
    if items is None:
        return JSONResponse(
            status_code=404,
            content={"issues": [{"location": "lead_id", "message": "线索不存在"}]},
        )
    names = service.lead_names()
    return [_reminder_body(item, lead_name=names.get(item.lead_id, "")) for item in items]


@app.get("/dashboard")
def dashboard_summary(service: DashboardDep) -> dict:
    """一屏指标：总数 / 公海 / 本周新增与跟进 / 首响率 / 无下次跟进占比 / 分级提醒数。

    口径全部写在 `domain/dashboard.py` 的字段注释里；这里只做浮点保留 4 位的格式转换。
    """
    return _dashboard_body(service.summary())


@app.post("/reclaim/scan")
def reclaim_scan(service: ReclaimDep) -> dict:
    """首响超时自动回收：显式触发一次扫描，返回被回收的线索；没有回收返回空列表。"""
    return {"reclaimed": service.scan()}


@app.post("/leads/{lead_id}/advance")
def advance_lead(lead_id: str, payload: AdvanceIn, service: FlowDep):
    """推进阶段：成功 200 返回更新后的线索 + 新写的那条阶段历史。

    线索不存在 404；校验不过 422（跳步、缺必填等都在 `domain/rules.py` 判）。
    """
    result = service.advance(lead_id, payload.target_stage)
    if result.lead is None:
        return JSONResponse(status_code=404, content=_issue_body(result.issues))
    if not result.ok or result.event is None:
        return JSONResponse(status_code=422, content=_issue_body(result.issues))

    return {"lead": _lead_body(result.lead), "stage_event": _stage_event_body(result.event)}


@app.post("/leads/{lead_id}/followups", status_code=201)
def create_followup(lead_id: str, payload: FollowupIn, service: FollowupDep):
    """记录跟进：成功 201 返回留痕（含 `value_provided`）；三要素不合法 422，线索不存在 404。"""
    ok, issues, item = service.record(
        lead_id,
        conclusion=payload.conclusion,
        objection=payload.objection,
        next_followup_at=payload.next_followup_at,
        value_provided=payload.value_provided,
    )
    if not ok or item is None:
        missing_lead = any(issue.location == "lead_id" for issue in issues)
        status = 404 if missing_lead else 422
        return JSONResponse(status_code=status, content=_issue_body(issues))
    return _followup_body(item)


@app.get("/leads/{lead_id}/followups")
def list_followups(lead_id: str, service: FollowupDep) -> list[dict]:
    """该线索的跟进记录：按时间正序；没有记录返回 `[]`。"""
    return [_followup_body(item) for item in service.list_for(lead_id)]


@app.post("/leads/{lead_id}/raws")
def create_raw(lead_id: str, payload: RawIn, service: IngestDep):
    """登记一段原文：新内容 201，同线索同内容 200 且原样返回已有那条。

    两种情况都带 `duplicated` 标志，前端据此提示「这段已经录过了」。
    """
    item, duplicated, issues = service.ingest(
        lead_id, content=payload.content, source=payload.source
    )
    if item is None:
        missing_lead = any(issue.location == "lead_id" for issue in issues)
        status = 404 if missing_lead else 422
        return JSONResponse(status_code=status, content=_issue_body(issues))

    status = 200 if duplicated else 201
    return JSONResponse(status_code=status, content={**_raw_body(item), "duplicated": duplicated})


@app.post("/raws/{raw_id}/extract")
def extract_raw(raw_id: str, service: IngestDep):
    """对一条原文跑提炼：返回采纳的事实、被丢弃的原因、这次运行的账、以及自动补上的线索字段。

    素材不存在 404；重复提炼不报错，只是 `accepted` 为空（候选全命中「重复值」）。
    `field_updates` 是这次提炼补齐的线索字段（可能为 `{}`），前端据此提示「已自动补齐」。
    """
    outcome = service.extract(raw_id)
    if not outcome.ok:
        missing_raw = any(issue.location == "raw_id" for issue in outcome.issues)
        status = 404 if missing_raw else 422
        return JSONResponse(status_code=status, content=_issue_body(outcome.issues))

    return {
        "accepted": [
            _fact_body(fact, model=None if outcome.run is None else outcome.run.model)
            for fact in outcome.accepted
        ],
        "skipped": [
            {"location": issue.location, "message": issue.message} for issue in outcome.skipped
        ],
        "run": None if outcome.run is None else _run_body(outcome.run),
        "field_updates": dict(outcome.field_updates),
    }


@app.get("/leads/{lead_id}/raws")
def list_raws(lead_id: str, service: IngestDep):
    """该线索的原始素材：按登记时间正序；线索不存在 404，没有素材返回 `[]`。"""
    items, issues = service.list_raws(lead_id)
    if issues:
        return JSONResponse(status_code=404, content=_issue_body(issues))
    return [_raw_body(item) for item in items]


@app.get("/leads/{lead_id}/facts")
def list_facts(lead_id: str, service: IngestDep):
    """该线索的事实：按时间正序；线索不存在 404，没有事实返回 `[]`。

    每条事实额外带 `model`：模型产出的取自对应提炼账本（前端显示 `AI·<模型名>`），
    人工补录的为 null（前端显示「人工」），不会把两者混成一句「AI」。
    """
    facts, issues = service.list_facts(lead_id)
    if issues:
        return JSONResponse(status_code=404, content=_issue_body(issues))
    return [_fact_body(fact, model=service.model_for(fact)) for fact in facts]


@app.get("/leads/{lead_id}/card")
def get_lead_card(lead_id: str, service: CardDep):
    """该线索的信息卡：覆盖度 + 四要素分区 + 缺项问题句；线索不存在 404。

    字段与问题句全部来自该线索自己的模板（`card.sections`），本路由不做任何判断；
    值来自该客户的事实，没采集到的字段进 `missing`，每项带一句可直接问客户的话。
    """
    card, issues = service.card(lead_id)
    if card is None:
        return JSONResponse(status_code=404, content=_issue_body(issues))
    return _card_body(card)


# ------------------------------------------------------------------ 客户分级


def _grading_status(issues: list[ValidationIssue]) -> int:
    """分级接口的状态码：线索不存在 404，其余（取值非法 / 缺理由 / 模式不认识）422。"""
    if any(issue.location == "lead_id" for issue in issues):
        return 404
    return 422


@app.post("/leads/{lead_id}/grading/propose")
def propose_grading(lead_id: str, service: GradingDep, mode: str | None = None):
    """按规则出一次分级建议：返回档次、上下界、四维明细、理由与原文依据。

    `mode` 不传就用模板 `grading.default_mode`；传 `auto` 时建议会立刻被采纳
    （`grading_history` 落两行：建议 + 采纳）。线索不存在 404，模式非法 422。
    """
    result, issues = service.propose(lead_id, mode=mode)
    if result is None:
        return JSONResponse(status_code=_grading_status(issues), content=_issue_body(issues))
    return _grading_body(result)


@app.post("/leads/{lead_id}/grading/decide", status_code=201)
def decide_grading(lead_id: str, payload: GradingDecideIn, service: GradingDep):
    """人工定档：成功 201 返回新写的那行留痕（AI 值 / 人工值 / 理由）。

    改档必须写理由（≥2 字），不改档可空；`needs_info` 直接定档同样要理由。
    """
    decision, issues = service.decide(
        lead_id,
        value=payload.value,
        reason=payload.reason,
        actor=payload.actor,
        mode=payload.mode,
    )
    if decision is None:
        return JSONResponse(status_code=_grading_status(issues), content=_issue_body(issues))
    return _grading_record_body(decision.record)


@app.get("/leads/{lead_id}/grading/history")
def list_grading_history(lead_id: str, service: GradingDep):
    """该客户的分级留痕：按时间倒序（最新一行在最前）；线索不存在 404。"""
    rows, issues = service.history(lead_id)
    if issues:
        return JSONResponse(status_code=404, content=_issue_body(issues))
    return rows


@app.get("/leads/{lead_id}/grading/latest")
def get_grading_latest(lead_id: str, service: GradingDep):
    """该客户当前的档位与四维分解：**只读，不落库**（看一次详情不会多写一行留痕）。

    - `current`：当前档位（人工值优先、AI 值次之、都没判过是 `unknown`）；
    - `record`：最近一条留痕；从没分级过的客户是 `null`，接口仍回 200 让页面显示「未分级」；
    - `result`：按当前事实现算的四维分解（`low` / `high` / `dims` / `reason` / `basis`），
      与 `POST .../grading/propose` 返回的是同一份形状，区别只在「写不写留痕」。
    线索不存在 404。
    """
    snapshot, issues = service.latest(lead_id)
    if snapshot is None:
        return JSONResponse(status_code=_grading_status(issues), content=_issue_body(issues))
    return {
        "current": snapshot.current,
        "record": None if snapshot.record is None else _grading_record_body(snapshot.record),
        "result": _grading_body(snapshot.result),
    }


# ---------------------------------------------------------------- 今日待办


@app.get("/todo")
def list_todo(service: TodoDep) -> list[dict]:
    """今日待办：分级 → 到期 → 缺项 → 最久未联系四层排序，每家最多一条。

    每条都带建议动作与价值点（`value_offered` 一定非空，取不到价值的客户不出现）；
    线索为空时返回 `[]`，不返回 204、也不编一条占位。
    """
    return [_todo_body(item) for item in service.list()]


@app.patch("/leads/{lead_id}/facts/{field}")
def patch_fact(lead_id: str, field: str, payload: FactPatchIn, service: IngestDep):
    """人工改一个字段：来源记 manual 且锁定，后续提炼不再覆盖它。

    字段非法或取值不合格 422；线索或该字段的事实不存在 404。
    """
    fact, issues = service.set_manual(lead_id, field, payload.value)
    if fact is None:
        missing = any(issue.location in ("lead_id", "fact") for issue in issues)
        status = 404 if missing else 422
        return JSONResponse(status_code=status, content=_issue_body(issues))
    return _fact_body(fact)


@app.delete("/leads/{lead_id}/facts/{field}/lock")
def unlock_fact(lead_id: str, field: str, service: IngestDep):
    """解除人工锁定：只把 locked 改回 false，来源仍记 manual。"""
    fact, issues = service.unlock(lead_id, field)
    if fact is None:
        missing = any(issue.location in ("lead_id", "fact") for issue in issues)
        status = 404 if missing else 422
        return JSONResponse(status_code=status, content=_issue_body(issues))
    return _fact_body(fact)


# ---------------------------------------------------------------- 话术草稿


def _draft_status(issues: list[ValidationIssue]) -> int:
    """草稿接口的状态码：线索/草稿不存在 404、状态冲突 409、其余校验不过 422。"""
    if any(issue.location == "lead_id" for issue in issues):
        return 404
    if any(issue.location == "draft" and "不存在" in issue.message for issue in issues):
        return 404
    if any(issue.location == "draft" and "不能再次操作" in issue.message for issue in issues):
        return 409
    return 422


@app.post("/leads/{lead_id}/draft")
def generate_draft(lead_id: str, service: DraftDep):
    """基于事实与资料生成一条草稿：成功 200；被门槛拦下时 422 并列出原因。

    `citations` 只有模型真正引用的资料片段（编号来源由服务层换算），
    前端展示时必须与 `text` 一起看，避免把无引用的草稿当成结论。
    """
    outcome, issues = service.generate(lead_id)
    if not outcome.ok:
        return JSONResponse(status_code=_draft_status(issues), content=_issue_body(issues))
    return {
        "draft_id": outcome.draft_id,
        "text": outcome.text,
        "citations": outcome.citations,
        "model": outcome.model,
    }


@app.post("/leads/{lead_id}/drafts/{draft_id}/confirm", status_code=201)
def confirm_draft(lead_id: str, draft_id: str, payload: DraftConfirmIn, service: DraftDep):
    """确认草稿落库成跟进留痕：成功 201 返回那条跟进，并带确认后的线索 `fields`。

    确认时把草稿正文同时写进线索的 `conclusion` 字段（`close` 块的必填项）。
    草稿不存在 404，已处理过 409。
    """
    outcome = service.confirm(
        lead_id,
        draft_id,
        objection=payload.objection,
        next_followup_at=payload.next_followup_at,
        value_provided=payload.value_provided,
    )
    if outcome.item is None:
        return JSONResponse(
            status_code=_draft_status(outcome.issues), content=_issue_body(outcome.issues)
        )
    return {**_followup_body(outcome.item), "fields": dict(outcome.fields)}


@app.delete("/leads/{lead_id}/drafts/{draft_id}", status_code=204)
def discard_draft(lead_id: str, draft_id: str, service: DraftDep):
    """丢弃草稿：只改状态不删记录；成功 204，草稿不存在 404，已处理过 409。"""
    draft, issues = service.discard(lead_id, draft_id)
    if draft is None:
        return JSONResponse(status_code=_draft_status(issues), content=_issue_body(issues))
    return Response(status_code=204)


# ---------------------------------------------------------------- 分拣单


def _suggestion_body(suggestion: SortingSuggestion | None) -> dict | None:
    """已校验的三块建议：要素（已过滤）/ 分级（已兜底）/ 出库建议（已按缺项重算）。

    与分拣单里的 `suggestion_json`（模型原文）刻意不同：这里是给人看的版本。
    """
    if suggestion is None:
        return None
    raw = asdict(suggestion)
    return {
        "elements": raw["elements"],
        "grading": raw["grading"],
        "next_action": raw["next_action"],
    }


@app.post("/sorting", status_code=201)
def submit_sorting(payload: SortingIn, service: SortingDep):
    """一次投料 → 一张分拣单：201 返回归属 / 要素 / 分级 / 出库四块、归属候选与丢弃记录。

    归属：传了 `lead_id` 就是它；没传则跑三级漏斗，只有精确键命中才自动归属，
    灰区与新客户一律进待认领池（`status=pending_claim`，`lead_id` 为 null）。
    `lead_candidates` 是这次落库的候选（`[{"lead_id","name","score","scorer"}]`），
    与 `GET /sorting/batches/{id}` 回的是同一份，没有候选就是 `[]`。
    原文不合格、模型失败或没有任何可用要素时 422（线索不存在 404），不生成分拣单。
    """
    outcome = service.sort(content=payload.content, source=payload.source, lead_id=payload.lead_id)
    if outcome.batch is None:
        status = 404 if any(issue.location == "lead_id" for issue in outcome.issues) else 422
        return JSONResponse(status_code=status, content=_issue_body(outcome.issues))

    batch = outcome.batch
    return {
        "batch_id": batch.id,
        "status": batch.status,
        "lead_id": batch.lead_id,
        "confidence": batch.confidence,
        "scorer": batch.scorer,
        "created_at": batch.created_at,
        "suggestion": _suggestion_body(outcome.suggestion),
        "lead_candidates": outcome.lead_candidates,
        "dropped": outcome.dropped,
    }


@app.get("/sorting/batches")
def list_sorting_batches(service: SortingDep, status: str | None = None, limit: int = 50):
    """分拣单列表（新→旧）：`status` 可过滤（如 `pending_claim` 即待认领池）。

    每条带 `lead_name`（归属未定时为 null）、`dropped_count`（丢了几条要素，整数）
    与 `content_preview`（原文前 60 字，超出加 `…`）；
    状态不认识或 `limit` 越界时 422。
    """
    items, issues = service.list_batches(status=status, limit=limit)
    if issues:
        return JSONResponse(status_code=422, content=_issue_body(issues))
    return items


@app.get("/sorting/batches/{batch_id}")
def get_sorting_batch(batch_id: str, service: SortingDep):
    """单张分拣单：带模型原文（`suggestion_json`）、解析后的视图（`suggestion`）、
    投料原文全文（`content`）、归属候选（`lead_candidates`）与丢弃诊断（`dropped`）。"""
    body, issues = service.get_batch(batch_id)
    if body is None:
        return JSONResponse(status_code=404, content=_issue_body(issues))
    return body


def _batch_view(service: SortingService, batch_id: str) -> dict | None:
    """取一张分拣单的完整视图：与 `GET /sorting/batches/{id}` 同一份口径。

    处置/认领成功后要回「改完的单」，这里不另拼字段，免得两处字段慢慢走偏。
    """
    body, _issues = service.get_batch(batch_id)
    return body


@app.post("/sorting/{batch_id}/confirm")
def confirm_sorting_batch(batch_id: str, payload: SortingConfirmIn, service: SortingDep):
    """处置一张分拣单：同意（要素落成事实）/ 改归属 / 拒绝。

    200 返回 `{"batch", "applied", "skipped"}`：`applied` 是这次写进事实层的条数，
    `skipped` 每项 `{"field", "reason"}`（出处复核不过 / 人工锁定 / 重复值）。
    分拣单不存在 404；状态不对、缺理由、目标客户不存在一律 422 且不落库。
    """
    outcome = service.confirm(
        batch_id, action=payload.action, lead_id=payload.lead_id, reason=payload.reason
    )
    if outcome.batch is None:
        return JSONResponse(status_code=404, content=_issue_body(outcome.issues))
    if outcome.issues:
        return JSONResponse(status_code=422, content=_issue_body(outcome.issues))

    return {
        "batch": _batch_view(service, batch_id),
        "applied": outcome.applied,
        "skipped": outcome.skipped,
    }


@app.post("/sorting/{batch_id}/claim")
def claim_sorting_batch(batch_id: str, payload: SortingClaimIn, service: SortingDep):
    """从待认领池里认领：挂已有客户，或按名字新建客户。

    200 返回 `{"batch", "lead_id", "warnings"}`；`warnings` 是「客户已建但要补录」这类
    提醒（例如要素里没有联系方式）。分拣单不存在 404；状态不对、两个参数都没给或都给、
    客户不存在一律 422。认领**不会**自动同意要素，仍要再调一次 confirm。
    """
    batch, issues = service.claim(
        batch_id,
        lead_id=payload.lead_id,
        create_name=payload.create_name,
        owner=payload.owner,
    )
    if batch is None:
        status = 404 if any(issue.location == "batch_id" for issue in issues) else 422
        return JSONResponse(status_code=status, content=_issue_body(issues))

    return {
        "batch": _batch_view(service, batch_id),
        "lead_id": batch.lead_id,
        "warnings": [{"location": issue.location, "message": issue.message} for issue in issues],
    }


@app.get("/sorting/{batch_id}/review")
def review_sorting_batch(batch_id: str, service: SortingDep):
    """灰区复核：待认领且候选 1–3 家时，返回建议归给谁与理由。

    结果**只进响应、不落库**（不写 `lead_id`、不改 `suggestion_json`）：归属仍要人认领。
    分拣单不存在 404；状态不对、没有候选（请直接新建）一律 422。
    """
    outcome = service.review_candidates(batch_id)
    if outcome.issues:
        status = 404 if any(issue.location == "batch_id" for issue in outcome.issues) else 422
        return JSONResponse(status_code=status, content=_issue_body(outcome.issues))
    return {"suggested_lead_id": outcome.lead_id, "reason": outcome.reason}


# ---------------------------------------------------------------- 周复盘


@app.get("/review")
def weekly_review(service: ReviewDep) -> dict:
    """本周复盘：推进 / 关闭 / 卡点 / 待补全 / 缺项前三 / 分级变动 / 无价值跟进 + 三个问题。

    窗口是滚动 7 天（`domain/review.py` 的 `REVIEW_WINDOW_DAYS`），所有数字共用同一个窗口；
    `answered` 表示这周是否已经答过校准问题，`answers` 是这周已作答的样本
    （`[{"key","question","answer","at"}]`，同一个窗口），都由服务判定后拼在这里。
    没有客户时照样 200：数字全 0、问题照给（复盘页永远可打开）。
    """
    return {
        **_review_body(service.summary()),
        "answered": service.answered_this_week(),
        "answers": service.answers_this_week(),
    }


@app.post("/review/answers", status_code=201)
def save_review_answers(payload: ReviewAnswersIn, service: ReviewDep):
    """保存校准作答：成功 201 返回 `{"saved": n}`，一条问题一行 `preference_samples`。

    至少 1 条、最多 3 条；问题编号不认识或回答短于 2 字一律 422 且**一条都不写**
    （半条写进去会让「这周答过没有」说不清）。
    """
    saved, issues = service.answer(
        [item.model_dump() for item in payload.answers], actor=payload.actor
    )
    if issues:
        return JSONResponse(status_code=422, content=_issue_body(issues))
    return {"saved": saved}
