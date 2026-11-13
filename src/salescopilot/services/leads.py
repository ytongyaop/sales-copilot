"""线索用例：新建与列表。

编排顺序固定：先 `domain/rules.py` 校验 → 有问题原样返回问题列表（不抛异常、不落库）
→ 没问题才造实体并调用端口落库。本层不认识 SQLite，只认识 `domain/ports.py` 的协议。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from salescopilot.domain.models import INITIAL_STAGE, Lead
from salescopilot.domain.ports import LeadRepository
from salescopilot.domain.rules import ValidationIssue, validate_new_lead


def utc_now_iso() -> str:
    """默认时钟：UTC 的 ISO8601 字符串（秒精度）。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


class LeadService:
    """线索用例边界：入参进、实体或问题列表出。"""

    def __init__(self, repo: LeadRepository, clock: Callable[[], str] | None = None) -> None:
        self._repo = repo
        self._clock = clock if clock is not None else utc_now_iso

    def create(
        self,
        *,
        name: str,
        contact: str,
        source: str,
        owner: str | None = None,
    ) -> Lead | list[ValidationIssue]:
        """新建一条线索；校验失败返回问题列表，成功返回落库后的实体。"""
        issues = validate_new_lead(name=name, contact=contact, source=source)
        if issues:
            return issues

        lead = Lead(
            id=str(uuid.uuid4()),
            name=name.strip(),
            contact=contact.strip(),
            source=source.strip(),
            stage=INITIAL_STAGE,
            owner=(owner.strip() or None) if owner is not None else None,
            created_at=self._clock(),
        )
        self._repo.add(lead)
        return lead

    def list(self) -> list[Lead]:
        """全部线索，按创建时间倒序（顺序由仓储实现保证）。"""
        return self._repo.list()
