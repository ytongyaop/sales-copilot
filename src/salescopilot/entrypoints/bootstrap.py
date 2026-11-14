"""装配点：把端口与实现接起来。

全项目只有这里允许同时 import `services` 与 `adapters`；换库、换时钟只改这个文件，
业务代码与路由都不用动。
"""

from functools import lru_cache
from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteExtractRunRepository,
    SqliteFactRepository,
    SqliteFollowupRepository,
    SqliteLeadRepository,
    SqliteRawItemRepository,
    SqliteStageEventRepository,
    default_db_path,
)
from salescopilot.adapters.llm.deepseek import DeepSeekExtractor
from salescopilot.adapters.llm.fake import FakeExtractor
from salescopilot.adapters.template.loader import TemplateError, load_all
from salescopilot.config import ConfigError, Settings, load_settings, require_chat_key
from salescopilot.domain.ports import (
    ExtractRunRepository,
    FactExtractor,
    FactRepository,
    FollowupRepository,
    LeadRepository,
    RawItemRepository,
    StageEventRepository,
)
from salescopilot.domain.rules import validate_template
from salescopilot.services.flow import FlowService
from salescopilot.services.followup import FollowupService
from salescopilot.services.ingest import IngestService
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


def build_extractor(live: bool = False) -> FactExtractor:
    """决定这次用假模型还是真模型：默认假模型，`live=True` 且配置齐才换真模型。

    配置不齐时退回假模型而不是启动失败——账本里的 `model` 字段会写明到底是哪个，
    评测脚本则走 `build_live_extractor()`，那里缺 key 会直接报错（基线不能拿假模型冒充）。
    """
    if not live:
        return FakeExtractor()
    try:
        return build_live_extractor()
    except ConfigError:
        return FakeExtractor()


def build_live_extractor() -> DeepSeekExtractor:
    """真模型提炼器：`load_settings()` 只在这条路径上调用，缺 key 抛中文 `ConfigError`。"""
    settings: Settings = load_settings()
    return DeepSeekExtractor(
        api_key=require_chat_key(settings),
        model=settings.chat_model,
    )


def build_ingest_service(live: bool = False) -> IngestService:
    """采集与提炼用例：四条仓储 + 提炼器（`live=True` 才可能接真模型）。

    提炼器由 `build_extractor` 决定；换成真模型时用例与路由一行都不用改。
    """
    db = default_db_path()
    leads: LeadRepository = SqliteLeadRepository(db)
    raws: RawItemRepository = SqliteRawItemRepository(db)
    facts: FactRepository = SqliteFactRepository(db)
    runs: ExtractRunRepository = SqliteExtractRunRepository(db)
    extractor: FactExtractor = build_extractor(live)
    return IngestService(leads, raws, facts, runs, extractor)
