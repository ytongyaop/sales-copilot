"""端口协议：领域声明能力，实现放在 `adapters/`（结构性类型，不做运行时检查）。

`services` 只依赖这里声明的协议，不认识具体数据库；换存储只换适配器与装配。
"""

from typing import Protocol

from salescopilot.domain.models import Lead


class LeadRepository(Protocol):
    """线索仓储端口：写一条、按时间倒序列全部、按 id 查单条。"""

    def add(self, lead: Lead) -> None: ...

    def list(self) -> list[Lead]: ...

    def get(self, lead_id: str) -> Lead | None: ...
