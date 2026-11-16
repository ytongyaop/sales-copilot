"""线索用例：新建与列表。

编排顺序固定：先 `domain/rules.py` 校验 → 有问题原样返回问题列表（不抛异常、不落库）
→ 没问题才造实体并调用端口落库。本层不认识 SQLite，只认识 `domain/ports.py` 的协议。
"""

import uuid
from collections.abc import Callable, Collection
from dataclasses import replace
from datetime import UTC, datetime

from salescopilot.domain.models import DEFAULT_TEMPLATE, INITIAL_STAGE, Lead
from salescopilot.domain.ports import LeadRepository
from salescopilot.domain.rules import (
    ValidationIssue,
    merge_fields,
    validate_lead_fields,
    validate_new_lead,
    validate_template_key,
)


def utc_now_iso() -> str:
    """默认时钟：UTC 的 ISO8601 字符串（秒精度）。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


class LeadService:
    """线索用例边界：入参进、实体或问题列表出。

    `allowed_templates` 是已加载模板的 key 集合，由装配点注入；本层不认识文件系统，
    也不硬编码有哪些模板。
    """

    def __init__(
        self,
        repo: LeadRepository,
        allowed_templates: Collection[str] = (DEFAULT_TEMPLATE,),
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._repo = repo
        self._allowed_templates = allowed_templates
        self._clock = clock if clock is not None else utc_now_iso

    def create(
        self,
        *,
        name: str,
        contact: str,
        source: str,
        owner: str | None = None,
        template: str = DEFAULT_TEMPLATE,
    ) -> Lead | list[ValidationIssue]:
        """新建一条线索；校验失败返回问题列表，成功返回落库后的实体。"""
        resolved_template = (template or "").strip() or DEFAULT_TEMPLATE
        issues = validate_new_lead(name=name, contact=contact, source=source)
        issues.extend(validate_template_key(resolved_template, self._allowed_templates))
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
            template=resolved_template,
        )
        self._repo.add(lead)
        return lead

    def list(self) -> list[Lead]:
        """全部线索，按创建时间倒序（顺序由仓储实现保证）。"""
        return self._repo.list()

    def update_fields(
        self, lead_id: str, patch: dict[str, str]
    ) -> tuple[Lead | None, list[ValidationIssue]]:
        """人工补录线索字段：校验 → 合并 → 落库；返回（更新后的线索, 问题列表）。

        线索不存在时返回 `(None, 问题)`，问题位置是 `lead_id`（路由据此回 404）；
        其余问题（键不在白名单、取值为空、日期不是 `YYYY-MM-DD`）一律不落库，
        位置是 `fields.<字段名>`，与推进校验的错误位置同一口径。
        """
        lead = self._repo.get(lead_id)
        if lead is None:
            return None, [ValidationIssue("lead_id", "线索不存在")]

        issues = validate_lead_fields(patch)
        if issues:
            return lead, issues

        merged = merge_fields(lead.fields, patch)
        self._repo.update_fields(lead_id, merged)
        return replace(lead, fields=merged), []
