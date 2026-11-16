"""线索字段补录用例的行为测试：补丁累积、非法补丁不落库（临时库，全程离线）。"""

from pathlib import Path

from salescopilot.adapters.db.sqlite import SqliteLeadRepository
from salescopilot.domain.models import Lead
from salescopilot.services.leads import LeadService


def _build(tmp_path: Path) -> tuple[LeadService, SqliteLeadRepository]:
    repo = SqliteLeadRepository(tmp_path / "leads.sqlite3")
    return LeadService(repo), repo


def _new_lead(service: LeadService) -> Lead:
    result = service.create(name="李校长", contact="13800001111", source="referral")
    assert not isinstance(result, list), result
    return result


def test_patch_fields_accumulates(tmp_path: Path) -> None:
    service, repo = _build(tmp_path)
    lead = _new_lead(service)

    updated, issues = service.update_fields(
        lead.id, {"decision_maker": "李校长", "need": "想提高数学成绩"}
    )
    assert issues == []
    assert updated is not None
    assert updated.fields == {"decision_maker": "李校长", "need": "想提高数学成绩"}

    second, second_issues = service.update_fields(lead.id, {"amount": "1 万"})
    assert second_issues == []
    # 只覆盖传入的键：前两个字段仍在（不是整份替换）
    assert second.fields == {
        "decision_maker": "李校长",
        "need": "想提高数学成绩",
        "amount": "1 万",
    }

    # 真落库了：重新读一遍与返回值一致
    assert repo.get(lead.id).fields == second.fields
    # 基础属性不受影响
    assert repo.get(lead.id).name == "李校长"
    assert repo.get(lead.id).stage == "intake"


def test_invalid_patch_does_not_write(tmp_path: Path) -> None:
    service, repo = _build(tmp_path)
    lead = _new_lead(service)

    updated, issues = service.update_fields(
        lead.id,
        {"need": "   ", "hobby": "钓鱼", "appointment_at": "下周一"},
    )

    assert [issue.location for issue in issues] == [
        "fields.need",
        "fields.hobby",
        "fields.appointment_at",
    ]
    assert updated is not None
    # 整份补丁被拒：一条都没写进去
    assert repo.get(lead.id).fields == {}


def test_unknown_lead_returns_none_with_lead_id_issue(tmp_path: Path) -> None:
    service, _ = _build(tmp_path)

    lead, issues = service.update_fields("missing", {"need": "提高成绩"})

    assert lead is None
    assert [issue.location for issue in issues] == ["lead_id"]
