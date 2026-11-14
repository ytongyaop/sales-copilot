"""装配点：把端口与实现接起来。

全项目只有这里允许同时 import `services` 与 `adapters`；换库、换时钟只改这个文件，
业务代码与路由都不用动。
"""

from functools import lru_cache
from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteFollowupRepository,
    SqliteLeadRepository,
    SqliteStageEventRepository,
    default_db_path,
)
from salescopilot.adapters.template.loader import TemplateError, load_all
from salescopilot.domain.ports import FollowupRepository, LeadRepository, StageEventRepository
from salescopilot.domain.rules import validate_template
from salescopilot.services.flow import FlowService
from salescopilot.services.followup import FollowupService
from salescopilot.services.leads import LeadService

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


@lru_cache(maxsize=1)
def templates() -> dict[str, dict]:
    """启动时读一次模板目录并校验；非法模板直接抛错，不静默降级成默认流程。

    结果缓存供每个请求复用（模板是只读数据，不会在运行时被改）。
    """
    loaded = load_all(TEMPLATES_DIR)
    if not loaded:
        raise TemplateError(f"{TEMPLATES_DIR}: 模板目录里没有任何 yaml")

    problems: list[str] = []
    for key, data in loaded.items():
        issues = validate_template(data)
        if issues:
            detail = "；".join(f"{issue.location} {issue.message}" for issue in issues)
            problems.append(f"{key}：{detail}")
    if problems:
        raise TemplateError("模板校验失败：" + " | ".join(problems))
    return loaded


def template_loader(key: str) -> dict:
    """按模板名取模板数据；未知模板抛 `KeyError`，由调用方翻译成业务语言。"""
    return templates()[key]


def build_lead_service() -> LeadService:
    """读配置 → 建 SQLite 仓储 → 注入用例服务（允许的模板 key 取自启动时已加载的清单）。"""
    repo: LeadRepository = SqliteLeadRepository(default_db_path())
    return LeadService(repo, allowed_templates=frozenset(templates()))


def build_flow_service() -> FlowService:
    """推进用例：线索仓储 + 阶段历史仓储 + 模板加载器（启动时已校验）。"""
    leads: LeadRepository = SqliteLeadRepository(default_db_path())
    events: StageEventRepository = SqliteStageEventRepository(default_db_path())
    return FlowService(leads, events, template_loader)


def build_followup_service() -> FollowupService:
    """跟进用例：线索仓储 + 跟进仓储。"""
    leads: LeadRepository = SqliteLeadRepository(default_db_path())
    followups: FollowupRepository = SqliteFollowupRepository(default_db_path())
    return FollowupService(leads, followups)
