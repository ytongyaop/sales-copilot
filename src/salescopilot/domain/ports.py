"""端口协议：领域声明能力，实现放在 `adapters/`（结构性类型，不做运行时检查）。

`services` 只依赖这里声明的协议，不认识具体数据库；换存储只换适配器与装配。
"""

from typing import Protocol

from salescopilot.domain.models import (
    ExtractionResult,
    ExtractRun,
    Fact,
    Followup,
    Lead,
    RawItem,
    StageEvent,
)


class LeadRepository(Protocol):
    """线索仓储端口：写一条、按时间倒序列全部、按 id 查单条、改当前阶段。"""

    def add(self, lead: Lead) -> None: ...

    def list(self) -> list[Lead]: ...

    def get(self, lead_id: str) -> Lead | None: ...

    def update_stage(self, lead_id: str, stage: str) -> None: ...


class StageEventRepository(Protocol):
    """阶段历史端口：只追加、按线索按时间正序回查。"""

    def add(self, event: StageEvent) -> None: ...

    def list_for(self, lead_id: str) -> list[StageEvent]: ...


class FollowupRepository(Protocol):
    """跟进留痕端口：只追加、按线索按时间正序回查。"""

    def add(self, item: Followup) -> None: ...

    def list_for(self, lead_id: str) -> list[Followup]: ...


class RawItemRepository(Protocol):
    """原始素材端口：追加、按 id 查单条、按（线索, 内容哈希）查重、按线索回查。"""

    def add(self, item: RawItem) -> None: ...

    def get(self, raw_id: str) -> RawItem | None: ...

    def find_by_hash(self, lead_id: str, content_hash: str) -> RawItem | None: ...

    def list_for(self, lead_id: str) -> list[RawItem]: ...


class FactRepository(Protocol):
    """事实端口：追加、按线索回查、按字段取单条（一个字段一条）、整条改。"""

    def add(self, fact: Fact) -> None: ...

    def list_for(self, lead_id: str) -> list[Fact]: ...

    def get_by_field(self, lead_id: str, field: str) -> Fact | None: ...

    def update(self, fact: Fact) -> None: ...


class ExtractRunRepository(Protocol):
    """提炼账本端口：只追加、按原始素材回查。"""

    def add(self, run: ExtractRun) -> None: ...

    def list_for_raw(self, raw_id: str) -> list[ExtractRun]: ...


class FactExtractor(Protocol):
    """提炼器端口：只知道「原文 + 已有事实」，返回候选与这次产出的证据。

    `ExtractionResult.candidates` 的每项形如 `{"field", "value", "source_span", "confidence"}`；
    候选是否合规、是否落库由 `domain/facts.py` 与用例层判定，提炼器不做取舍。
    `token` / `seconds` / `raw_output_hash` 由实现自行测量（假模型给确定性的 0）。
    失败（网络、超时、输出解析不了）一律抛 `domain/errors.py` 的 `ExtractError`。
    """

    def extract(self, *, raw: RawItem, facts: list[Fact]) -> ExtractionResult: ...
