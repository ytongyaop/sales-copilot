"""FastAPI 应用实例。

路由只做三件事：解析入参 → 调 service → 把结果映射成 HTTP 响应。校验规则一律在
`domain/rules.py`，这里不重复判断任何字段；业务语言的中文提示也在这里统一收口，
保证前端只需要认一种错误体：`{"issues": [{"location", "message"}]}`。
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from salescopilot.domain.labels import block_label
from salescopilot.domain.models import (
    DEFAULT_TEMPLATE,
    ExtractRun,
    Fact,
    Followup,
    Lead,
    RawItem,
    StageEvent,
)
from salescopilot.domain.rules import ValidationIssue
from salescopilot.entrypoints.bootstrap import (
    build_flow_service,
    build_followup_service,
    build_ingest_service,
    build_lead_service,
    templates,
)
from salescopilot.services.flow import FlowService
from salescopilot.services.followup import FollowupService
from salescopilot.services.ingest import IngestService
from salescopilot.services.leads import LeadService

app = FastAPI(title="SalesCopilot", version="0.1.0")

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


class FollowupIn(BaseModel):
    """POST /leads/{id}/followups 的请求体：跟进三要素。"""

    conclusion: str
    objection: str
    next_followup_at: str


class RawIn(BaseModel):
    """POST /leads/{id}/raws 的请求体：一段原文 + 来源。"""

    content: str
    source: str


class FactPatchIn(BaseModel):
    """PATCH /leads/{id}/facts/{field} 的请求体：只给新取值。"""

    value: str


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


LeadDep = Annotated[LeadService, Depends(get_lead_service)]
FlowDep = Annotated[FlowService, Depends(get_flow_service)]
FollowupDep = Annotated[FollowupService, Depends(get_followup_service)]
IngestDep = Annotated[IngestService, Depends(get_ingest_service)]


def _issue_body(issues: list[ValidationIssue]) -> dict:
    return {"issues": [{"location": i.location, "message": i.message} for i in issues]}


def _lead_body(lead: Lead) -> dict:
    return {
        "id": lead.id,
        "name": lead.name,
        "contact": lead.contact,
        "source": lead.source,
        "stage": lead.stage,
        "owner": lead.owner,
        "created_at": lead.created_at,
        "template": lead.template,
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


def _fact_body(fact: Fact) -> dict:
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
def health() -> dict[str, str]:
    """健康检查：只回答进程是否活着，不含任何业务判断。"""
    return {"status": "ok"}


def _template_body(key: str, data: dict) -> dict:
    """模板清单的一项：名字 + 阶段顺序 + 每块的中文名。

    中文名先取模板 YAML 里该块的 `label`（行业叫法只允许出现在这里），
    没有才退回 `domain/labels.py` 的通用叫法；两者都没有就用块 id。
    """
    steps = [
        step
        for step in data.get("flow", [])
        if isinstance(step, dict) and isinstance(step.get("block"), str)
    ]
    labels: dict[str, str] = {}
    for step in steps:
        block_id = step["block"]
        label = step.get("label")
        labels[block_id] = (
            label if isinstance(label, str) and label.strip() else block_label(block_id)
        )
    return {
        "key": key,
        "name": str(data.get("name", "")),
        "flow": [step["block"] for step in steps],
        "labels": labels,
    }


@app.get("/templates")
def list_templates() -> list[dict]:
    """模板清单：名字 + 阶段顺序 + 每块中文名（前端据此算「下一阶段」并显示中文名）。"""
    return [_template_body(key, data) for key, data in templates().items()]


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
    """线索列表：按创建时间倒序；空表返回 `[]`。"""
    return [_lead_body(lead) for lead in service.list()]


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
    """记录跟进：成功 201 返回留痕；三要素不合法 422，线索不存在 404。"""
    ok, issues, item = service.record(
        lead_id,
        conclusion=payload.conclusion,
        objection=payload.objection,
        next_followup_at=payload.next_followup_at,
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
    """对一条原文跑提炼：返回采纳的事实、被丢弃的原因、以及这次运行的账。

    素材不存在 404；重复提炼不报错，只是 `accepted` 为空（候选全命中「重复值」）。
    """
    outcome = service.extract(raw_id)
    if not outcome.ok:
        missing_raw = any(issue.location == "raw_id" for issue in outcome.issues)
        status = 404 if missing_raw else 422
        return JSONResponse(status_code=status, content=_issue_body(outcome.issues))

    return {
        "accepted": [_fact_body(fact) for fact in outcome.accepted],
        "skipped": [
            {"location": issue.location, "message": issue.message} for issue in outcome.skipped
        ],
        "run": None if outcome.run is None else _run_body(outcome.run),
    }


@app.get("/leads/{lead_id}/facts")
def list_facts(lead_id: str, service: IngestDep):
    """该线索的事实：按时间正序；线索不存在 404，没有事实返回 `[]`。"""
    facts, issues = service.list_facts(lead_id)
    if issues:
        return JSONResponse(status_code=404, content=_issue_body(issues))
    return [_fact_body(fact) for fact in facts]


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
