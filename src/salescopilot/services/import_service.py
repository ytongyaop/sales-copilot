"""CSV 导入用例：解析（`domain/csv_import.py`）→ 按联系方式去重 → 落库。

幂等口径：**同一联系方式（contact）已存在就跳过**（含同一次文件里重复出现的行），
原因文案固定为「联系方式已存在」，前端据此提示。解析阶段的问题（缺字段、来源不合法）
原样带到返回里，批次不会因为某一行坏了就整体失败。
"""

import uuid
from collections.abc import Callable

from salescopilot.domain.csv_import import parse_csv
from salescopilot.domain.models import DEFAULT_TEMPLATE, INITIAL_STAGE, Lead
from salescopilot.domain.ports import LeadRepository
from salescopilot.services.leads import utc_now_iso


class ImportService:
    """导入边界：CSV 文本进，`{"imported", "skipped"}` 出。"""

    def __init__(self, leads: LeadRepository, clock: Callable[[], str] | None = None) -> None:
        self._leads = leads
        self._clock = clock if clock is not None else utc_now_iso

    def import_csv(self, text: str, *, default_owner: str | None = None) -> dict:
        """导入一段 CSV：成功条数 + 跳过明细（`[{"row", "reason"}]`）。

        行里的归属人优先；该列为空时才用 `default_owner`（纯空格的默认值视为没有）。
        """
        rows, issues = parse_csv(text)
        skipped = [{"row": issue.row_no, "reason": issue.message} for issue in issues]

        existing = {lead.contact for lead in self._leads.list()}
        fallback_owner = (default_owner or "").strip() or None
        imported = 0

        for row in rows:
            if row.contact in existing:
                skipped.append({"row": row.row_no, "reason": "联系方式已存在"})
                continue

            lead = Lead(
                id=str(uuid.uuid4()),
                name=row.name,
                contact=row.contact,
                source=row.source,
                stage=INITIAL_STAGE,
                owner=row.owner or fallback_owner,
                created_at=self._clock(),
                template=DEFAULT_TEMPLATE,
            )
            self._leads.add(lead)
            existing.add(lead.contact)
            imported += 1

        return {"imported": imported, "skipped": skipped}
