"""领域实体与通用取值集合（纯数据，无 IO、无校验逻辑）。

这里只回答「一条线索长什么样」以及引擎认的通用枚举；校验一律在 `domain/rules.py`，
落库一律在 `adapters/db`。实体里不含任何行业叫法。
"""

from dataclasses import dataclass, field

# 新线索的初始阶段：与流程块契约的首块同名（见 `domain/blocks.py` 的 FIRST_BLOCK_ID）。
INITIAL_STAGE = "intake"

# 每条线索默认用哪份模板；取值是模板文件名（不含后缀），见 `templates/*.yaml`。
DEFAULT_TEMPLATE = "edu_training"

# 线索来源只认这几个通用入口；扩展枚举要改这里，不要在别处另写一份。
ALLOWED_SOURCES: tuple[str, ...] = ("referral", "inbound", "outbound", "event", "other")

# 异议类型：通用分类，不是行业词；扩展枚举同样只改这里。
ALLOWED_OBJECTIONS: tuple[str, ...] = ("price", "time", "effect", "trust", "other")

# 归属变更原因：为什么这条线索换了/没了归属人；扩展枚举同样只改这里。
ALLOWED_ASSIGNMENT_REASONS: tuple[str, ...] = ("claim", "release", "auto_reclaim", "manual")

# 草稿状态：生成即 pending；确认落库后 confirmed；人工丢弃后 discarded（不再可确认）。
DRAFT_PENDING = "pending"
DRAFT_CONFIRMED = "confirmed"
DRAFT_DISCARDED = "discarded"


@dataclass(frozen=True)
class Lead:
    """一条线索。

    - `stage`：当前阶段，取值是流程块 id。
    - `owner`：归属人；None 表示尚未分配。
    - `created_at`：ISO8601 字符串，由调用方注入的时钟产生（实体自己不碰时钟）。
    - `template`：这条线索按哪份模板推进（模板文件名，不含后缀）。
    - `fields`：已采集的通用字段（键是块契约 `requires` 里的字段名）；
      事实层落地前先放在这里，推进时用它判断必填是否齐备。
    """

    id: str
    name: str
    contact: str
    source: str
    stage: str
    owner: str | None
    created_at: str
    template: str = DEFAULT_TEMPLATE
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StageEvent:
    """一次阶段转移的留痕：谁（哪条线索）从哪个阶段走到哪个阶段、什么时候。"""

    id: str
    lead_id: str
    from_stage: str
    to_stage: str
    at: str


@dataclass(frozen=True)
class Followup:
    """一条跟进留痕：三要素（结论 / 异议 / 下次跟进时间）＋ 写入时间。"""

    id: str
    lead_id: str
    conclusion: str
    objection: str
    next_followup_at: str
    at: str


@dataclass(frozen=True)
class AssignmentEvent:
    """一次归属变更的留痕：从谁名下到谁名下、为什么。

    - `reason`：`claim`（认领）/ `release`（释放）/ `auto_reclaim`（首响超时自动回公海）
      / `manual`（人工手工调整）；扩展枚举只改 `ALLOWED_ASSIGNMENT_REASONS`。
    - `from_owner` / `to_owner`：None 表示「没有归属人」（公海）。
    """

    id: str
    lead_id: str
    from_owner: str | None
    to_owner: str | None
    reason: str
    at: str


@dataclass(frozen=True)
class RawItem:
    """一条原始素材（消息/通话记录等）：只追加，`content_hash` 用于同线索去重。"""

    id: str
    lead_id: str
    source: str
    content: str
    content_hash: str
    at: str


@dataclass(frozen=True)
class Fact:
    """一条已核实的事实。

    - `source`：谁产出的——`llm`（模型提炼）、`manual`（人工改）、`rule`（规则推导）；
    - `source_span`：原文片段，必须非空（无原文出处的候选不允许落库）；
    - `locked`：人工锁定后，后续提炼一律跳过该字段。
    """

    id: str
    lead_id: str
    raw_id: str
    field: str
    value: str
    source: str
    source_span: str
    confidence: float
    locked: bool
    at: str


@dataclass(frozen=True)
class ExtractionResult:
    """一次提炼的原始产出：候选 ＋ 产出它们的证据（谁出的、多贵、多久、哈希）。

    - `candidates`：提炼器原样吐出的候选，每项形如
      `{"field", "value", "source_span", "confidence"}`；合规与否由 `domain/facts.py` 判；
    - `model` / `prompt_version`：写进账本，用于回放与漂移对比；
    - `token` / `seconds`：真实耗用（假模型固定 0，真模型由适配器测量）；
    - `raw_output_hash`：对模型返回文本算的哈希，同输出必同哈希。
    """

    candidates: list[dict]
    model: str
    prompt_version: str
    token: int
    seconds: float
    raw_output_hash: str


@dataclass(frozen=True)
class ExtractRun:
    """一次提炼的账本：哪个模型、哪版提示词、进出哈希、候选/采纳/丢弃计数与耗用。"""

    id: str
    raw_id: str
    model: str
    prompt_version: str
    input_hash: str
    output_hash: str
    candidates: int
    accepted: int
    skipped: int
    token: int
    seconds: float
    at: str


@dataclass(frozen=True)
class DraftResult:
    """一次话术生成的原样产出：正文、用到的资料编号、以及这次生成的成本证据。

    - `used_hit_indexes`：用到的检索结果下标（**0 基**，指向 `hits[i]`）；空列表表示
      模型没引用任何资料——用例层据此拒绝入库，不允许「无出处的草稿」；
    - `model` / `prompt_version`：写进草稿记录，前端据此显示 `AI·<模型名>`；
    - `token` / `seconds`：假模型固定 0，真模型由适配器自行测量。
    """

    text: str
    used_hit_indexes: list[int]
    model: str
    prompt_version: str
    token: int
    seconds: float


@dataclass(frozen=True)
class Draft:
    """一条话术草稿：正文 ＋ 引用 ＋ 状态（pending / confirmed / discarded）与成本证据。"""

    id: str
    lead_id: str
    text: str
    citations: list[dict]
    status: str
    model: str
    prompt_version: str
    token: int
    seconds: float
    created_at: str
