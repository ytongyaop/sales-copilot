"""CSV 导入用例：正常导入 3 条、重复 contact 跳过（幂等）、默认 owner 生效。"""

from pathlib import Path

from salescopilot.adapters.db.sqlite import SqliteLeadRepository
from salescopilot.services.import_service import ImportService

NOW = "2026-11-16T12:00:00+00:00"

CSV = "姓名,联系方式,来源,归属\n张三,13800001111,转介绍,王五\n李四,13800002222,活动,\n王五,13800003333,other,\n"


def _service(tmp_path: Path) -> ImportService:
    return ImportService(
        SqliteLeadRepository(tmp_path / "import.sqlite3"),
        clock=lambda: NOW,
    )


def test_import_three_rows(tmp_path: Path) -> None:
    result = _service(tmp_path).import_csv(CSV)

    assert result == {"imported": 3, "skipped": []}
    leads = SqliteLeadRepository(tmp_path / "import.sqlite3").list()
    assert {lead.contact for lead in leads} == {"13800001111", "13800002222", "13800003333"}
    assert {lead.created_at for lead in leads} == {NOW}


def test_same_contact_is_skipped_second_time(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.import_csv(CSV)

    again = service.import_csv(CSV)

    assert again["imported"] == 0
    assert [item["reason"] for item in again["skipped"]] == ["联系方式已存在"] * 3
    assert len(SqliteLeadRepository(tmp_path / "import.sqlite3").list()) == 3


def test_duplicate_contact_inside_one_file_is_skipped(tmp_path: Path) -> None:
    text = "姓名,联系方式,来源\n张三,13800001111,inbound\n重复张三,13800001111,inbound\n"

    result = _service(tmp_path).import_csv(text)

    assert result == {"imported": 1, "skipped": [{"row": 3, "reason": "联系方式已存在"}]}


def test_default_owner_applies_only_when_row_owner_is_empty(tmp_path: Path) -> None:
    text = "姓名,联系方式,来源,归属\n张三,13800001111,inbound,王五\n李四,13800002222,inbound,\n"

    result = _service(tmp_path).import_csv(text, default_owner="赵六")

    assert result["imported"] == 2
    owners = {
        lead.contact: lead.owner
        for lead in SqliteLeadRepository(tmp_path / "import.sqlite3").list()
    }
    assert owners == {"13800001111": "王五", "13800002222": "赵六"}
