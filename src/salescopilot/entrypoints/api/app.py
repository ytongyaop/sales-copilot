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

from salescopilot.domain.models import Lead
from salescopilot.domain.rules import ValidationIssue
from salescopilot.entrypoints.bootstrap import build_lead_service
from salescopilot.services.leads import LeadService

app = FastAPI(title="SalesCopilot", version="0.1.0")

# pydantic 层校验失败时，把字段名翻译成业务语言（不含行业词，只有通用字段）。
FIELD_LABELS = {
    "name": "名称",
    "contact": "联系方式",
    "source": "来源",
    "owner": "归属人",
}


class LeadIn(BaseModel):
    """POST /leads 的请求体：只声明形状，取值合法性交给 domain 规则。"""

    name: str
    contact: str
    source: str
    owner: str | None = None


def get_lead_service() -> Iterator[LeadService]:
    """每个请求装配一次：数据库路径随环境变量变化，测试可直接 monkeypatch 环境变量。"""
    yield build_lead_service()


LeadDep = Annotated[LeadService, Depends(get_lead_service)]


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


@app.post("/leads", status_code=201)
def create_lead(payload: LeadIn, service: LeadDep):
    """新建线索：成功 201 返回完整实体，校验失败 422 返回问题列表。"""
    result = service.create(
        name=payload.name,
        contact=payload.contact,
        source=payload.source,
        owner=payload.owner,
    )
    if isinstance(result, list):
        return JSONResponse(status_code=422, content=_issue_body(result))
    return _lead_body(result)


@app.get("/leads")
def list_leads(service: LeadDep) -> list[dict]:
    """线索列表：按创建时间倒序；空表返回 `[]`。"""
    return [_lead_body(lead) for lead in service.list()]
