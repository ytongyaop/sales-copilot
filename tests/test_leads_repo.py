"""SQLite 仓储的行为测试：用临时目录建库，绝不写进 data/app.sqlite3。"""

from pathlib import Path

from salescopilot.adapters.db.sqlite import SqliteLeadRepository, default_db_path
from salescopilot.domain.models import INITIAL_STAGE, Lead


def _lead(lead_id: str, created_at: str) -> Lead:
    return Lead(
        id=lead_id,
        name=f"线索-{lead_id}",
        contact="13800001111",
        source="inbound",
        stage=INITIAL_STAGE,
        owner=None,
        created_at=created_at,
    )


def _repo(tmp_path: Path) -> SqliteLeadRepository:
    return SqliteLeadRepository(tmp_path / "nested" / "leads.sqlite3")


def test_add_then_list_is_desc_by_created_at(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.add(_lead("a", "2026-11-13T10:00:00+00:00"))
    repo.add(_lead("b", "2026-11-13T11:00:00+00:00"))

    assert [lead.id for lead in repo.list()] == ["b", "a"]


def test_get_hit_and_miss(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.add(_lead("a", "2026-11-13T10:00:00+00:00"))

    assert repo.get("a") is not None
    assert repo.get("a").name == "线索-a"
    assert repo.get("missing") is None


def test_empty_db_lists_nothing(tmp_path: Path) -> None:
    assert _repo(tmp_path).list() == []


def test_default_db_path_is_repo_relative(monkeypatch) -> None:
    monkeypatch.delenv("SALESCOPILOT_DB", raising=False)
    path = default_db_path()

    assert path.is_absolute()
    assert path.as_posix().endswith("/data/app.sqlite3")


def test_env_override_wins(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "override.sqlite3"))

    assert default_db_path() == tmp_path / "override.sqlite3"
