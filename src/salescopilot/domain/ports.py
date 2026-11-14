"""端口协议：领域声明能力，实现放在 `adapters/`（结构性类型，不做运行时检查）。

`services` 只依赖这里声明的协议，不认识具体数据库；换存储只换适配器与装配。
"""

from typing import Protocol

from salescopilot.domain.models import Followup, Lead, StageEvent


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
