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
