"""装配点：把端口与实现接起来。

全项目只有这里允许同时 import `services` 与 `adapters`；换库、换时钟、换模型只改这个
文件，业务代码与路由都不用动。密钥只在这里从配置读出来，再交给适配器。
"""

import os
from functools import lru_cache
from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteAssignmentEventRepository,
    SqliteDraftRepository,
    SqliteExtractRunRepository,
    SqliteFactRepository,
    SqliteFollowupRepository,
    SqliteLeadRepository,
    SqliteRawItemRepository,
    SqliteSortingBatchRepository,
    SqliteStageEventRepository,
    default_db_path,
)
from salescopilot.adapters.knowledge.chroma_store import ChromaStore
from salescopilot.adapters.knowledge.embedding import DashScopeEmbedder, FakeEmbedder
from salescopilot.adapters.knowledge.manifest import manifest_path_of
from salescopilot.adapters.llm.deepseek import DeepSeekExtractor, DeepSeekSorting
from salescopilot.adapters.llm.draft import DeepSeekDrafter, FakeDrafter
from salescopilot.adapters.llm.fake import FakeExtractor, FakeSorting
from salescopilot.adapters.template.loader import TemplateError, load_all
from salescopilot.config import (
    ConfigError,
    Settings,
    load_settings,
    require_chat_key,
    require_embed_key,
)
from salescopilot.domain.ports import (
    AssignmentEventRepository,
    DraftGenerator,
    DraftRepository,
    Embedder,
    ExtractRunRepository,
    FactExtractor,
    FactRepository,
    FollowupRepository,
    LeadRepository,
    RawItemRepository,
    SortingBatchRepository,
    SortingJudge,
    StageEventRepository,
    VectorStore,
)
from salescopilot.domain.rules import validate_template
from salescopilot.services.assignment import AssignmentService
from salescopilot.services.dashboard import DashboardService
from salescopilot.services.draft import DraftService
from salescopilot.services.flow import FlowService
from salescopilot.services.followup import FollowupService
from salescopilot.services.import_service import ImportService
from salescopilot.services.ingest import IngestService
from salescopilot.services.knowledge import KnowledgeService
from salescopilot.services.leads import LeadService
from salescopilot.services.reminder_service import ReclaimService, ReminderService
from salescopilot.services.sorting import SortingService

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
DATA_DIR = Path(__file__).resolve().parents[3] / "data"
INDEX_DIR_ENV = "SALESCOPILOT_INDEX_DIR"

# 没配 DASHSCOPE_API_KEY 时用假向量：能离线跑通「建索引 → 检索 → 引用」整条管道，
# 清单里写的也是 fake，查询时两边一致，不会互相打架。
FAKE_EMBED_MODEL = "fake"


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


def build_assignment_service() -> AssignmentService:
    """归属用例：线索仓储 + 归属事件仓储 + 模板加载器（当前不参与判定，先注入备用）。"""
    db = default_db_path()
    leads: LeadRepository = SqliteLeadRepository(db)
    events: AssignmentEventRepository = SqliteAssignmentEventRepository(db)
    return AssignmentService(leads, events, template_loader=template_loader)


def build_reminder_service() -> ReminderService:
    """提醒用例：线索 + 跟进仓储 + 模板加载器（阈值按每条线索自己的模板读）。"""
    db = default_db_path()
    leads: LeadRepository = SqliteLeadRepository(db)
    followups: FollowupRepository = SqliteFollowupRepository(db)
    return ReminderService(leads, followups, template_loader)


def build_reclaim_service() -> ReclaimService:
    """自动回收用例：线索 + 归属事件仓储 + 模板加载器（口径与提醒一致）。"""
    db = default_db_path()
    leads: LeadRepository = SqliteLeadRepository(db)
    events: AssignmentEventRepository = SqliteAssignmentEventRepository(db)
    return ReclaimService(leads, events, template_loader)


def build_dashboard_service() -> DashboardService:
    """仪表盘用例：线索 + 跟进仓储 + 模板加载器（指标口径取默认模板的阈值）。"""
    db = default_db_path()
    leads: LeadRepository = SqliteLeadRepository(db)
    followups: FollowupRepository = SqliteFollowupRepository(db)
    return DashboardService(leads, followups, template_loader)


def build_import_service() -> ImportService:
    """CSV 导入用例：线索仓储（去重与落库都由这个用例负责）。"""
    leads: LeadRepository = SqliteLeadRepository(default_db_path())
    return ImportService(leads)


def build_extractor(live: bool | None = None) -> FactExtractor:
    """决定这次用假模型还是真模型：默认跟随 `Settings.live`（默认假）。

    显式 `live=True` 但配置不齐时**直接抛 `ConfigError`**，不在运行中悄悄退回假模型——
    否则界面上的 `AI·fake` 很容易被当成「真模型跑过了」。
    """
    return build_live_extractor() if _resolve_live(live) else FakeExtractor()


def build_live_extractor() -> DeepSeekExtractor:
    """真模型提炼器：缺 key 抛中文 `ConfigError`。"""
    settings: Settings = load_settings()
    return DeepSeekExtractor(
        api_key=require_chat_key(settings),
        model=settings.chat_model,
    )


def build_ingest_service(live: bool | None = None) -> IngestService:
    """采集与提炼用例：四条仓储 + 提炼器（默认跟随 `Settings.live`）。

    提炼器由 `build_extractor` 决定；换成真模型时用例与路由一行都不用改。
    """
    resolved = _resolve_live(live)
    db = default_db_path()
    leads: LeadRepository = SqliteLeadRepository(db)
    raws: RawItemRepository = SqliteRawItemRepository(db)
    facts: FactRepository = SqliteFactRepository(db)
    runs: ExtractRunRepository = SqliteExtractRunRepository(db)
    extractor: FactExtractor = build_extractor(resolved)
    return IngestService(leads, raws, facts, runs, extractor)


def index_dir() -> Path:
    """索引目录：优先环境变量 `SALESCOPILOT_INDEX_DIR`，否则仓库根 `data/index`。

    测试靠这个环境变量把索引指到临时目录，避免把仓库里的索引写脏。
    """
    override = os.environ.get(INDEX_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return DATA_DIR / "index"


def build_embedder(live: bool | None = None) -> Embedder:
    """默认假向量（不联网、不花钱）；默认跟随 `Settings.live`，开了才接 DashScope 真向量。

    真向量缺 key 直接抛 `ConfigError`：静默退回假向量会让「引用的片段」和索引对不上。
    """
    if not _resolve_live(live):
        return FakeEmbedder()
    settings: Settings = load_settings()
    return DashScopeEmbedder(api_key=require_embed_key(settings), model=settings.embed_model)


def embed_model_name(embedder: Embedder) -> str:
    """这个 embedder 应该写进清单的模型名：假实现固定 fake，真实现用配置里的模型名。"""
    return getattr(embedder, "model", FAKE_EMBED_MODEL)


def build_knowledge_service(live: bool | None = None) -> KnowledgeService:
    """资料检索用例：向量化 + 向量库 + 清单路径 + 当前模型名（一致性校验用）。"""
    embedder = build_embedder(live)
    store: VectorStore = ChromaStore(index_dir())
    return KnowledgeService(
        embedder,
        store,
        manifest_path_of(index_dir()),
        embed_model_name(embedder),
    )


def build_draft_generator(live: bool | None = None) -> DraftGenerator:
    """话术生成器：默认假实现；`live=True` 缺 key 直接抛 `ConfigError`，不静默退回。"""
    if not _resolve_live(live):
        return FakeDrafter()
    settings: Settings = load_settings()
    return DeepSeekDrafter(api_key=require_chat_key(settings), model=settings.chat_model)


def build_draft_service(live: bool | None = None) -> DraftService:
    """话术用例：线索 + 事实 + 跟进 + 草稿仓储 + 资料检索 + 生成器。

    真/假由同一个 `live` 决定（向量与生成器不会一半真一半假）；默认跟随 `Settings.live`。
    """
    resolved = _resolve_live(live)
    db = default_db_path()
    leads: LeadRepository = SqliteLeadRepository(db)
    facts: FactRepository = SqliteFactRepository(db)
    followups: FollowupRepository = SqliteFollowupRepository(db)
    drafts: DraftRepository = SqliteDraftRepository(db)
    return DraftService(
        leads,
        facts,
        build_knowledge_service(resolved),
        build_draft_generator(resolved),
        drafts,
        followups=followups,
    )


def build_sorting_judge(live: bool | None = None) -> SortingJudge:
    """分拣模型：默认假实现；`live=True` 缺 key 直接抛 `ConfigError`，不静默退回。"""
    if not _resolve_live(live):
        return FakeSorting()
    settings: Settings = load_settings()
    return DeepSeekSorting(api_key=require_chat_key(settings), model=settings.chat_model)


def build_sorting_service(live: bool | None = None) -> SortingService:
    """投料与分拣用例：线索 + 原文 + 分拣单 + 事实四条仓储 + 分拣模型。

    向量本增量不装（`vector_score=None`）：归属只走精确键与字符串两级，
    灰区一律进待认领池。事实仓储是 T004 的「同意」要用：要素落成事实前还要复核出处。
    """
    db = default_db_path()
    leads: LeadRepository = SqliteLeadRepository(db)
    raws: RawItemRepository = SqliteRawItemRepository(db)
    batches: SortingBatchRepository = SqliteSortingBatchRepository(db)
    facts: FactRepository = SqliteFactRepository(db)
    return SortingService(leads, raws, batches, build_sorting_judge(live), facts=facts)


def _resolve_live(live: bool | None) -> bool:
    """没显式传就取配置里的开关；显式传 bool 时以传入为准（测试与脚本用）。"""
    if live is None:
        return load_settings().live
    return bool(live)


def check_live_config() -> None:
    """启动前自检：开了真模型却缺 key，打印中文提示并抛 `ConfigError`（不静默降级）。"""
    settings: Settings = load_settings()
    if not settings.live:
        return
    try:
        require_chat_key(settings)
        require_embed_key(settings)
    except ConfigError as exc:
        print(f"[启动失败] SALESCOPILOT_LIVE=1，但配置不全：{exc}", flush=True)
        raise


def mode_info() -> dict[str, str]:
    """当前运行模式与模型名，供 `GET /health` 展示；不返回任何密钥内容。"""
    settings: Settings = load_settings()
    if not settings.live:
        return {"mode": "fake", "chat": "fake", "embed": "fake"}
    return {"mode": "live", "chat": settings.chat_model, "embed": settings.embed_model}
