"""GET /dashboard 的行为测试：结构齐全、数值与固定数据一致。

全部离线：临时库 + 直接写仓储造「很久以前创建」的线索（接口不提供改创建时间的入口）。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.adapters.db.sqlite import SqliteLeadRepository
from salescopilot.domain.models import INITIAL_STAGE, Lead
from salescopilot.entrypoints.api.app import app

FIELDS = {
    "total",
    "pool",
    "new_this_week",
    "followups_this_week",
    "first_response_24h_rate",
    "stale_ratio",
    "silent_count",
    "reminders_by_level",
}


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "dashboard.sqlite3"))
    yield TestClient(app)


def _insert_lead(tmp_path: Path, *, lead_id: str, stage: str, created_at: str) -> None:
    SqliteLeadRepository(tmp_path / "dashboard.sqlite3").add(
        Lead(
            id=lead_id,
            name=f"线索-{lead_id}",
            contact=f"1380000{lead_id}",
            source="inbound",
            stage=stage,
            owner=None,
            created_at=created_at,
        )
    )


def test_dashboard_has_all_fields_and_consistent_numbers(
    client: TestClient, tmp_path: Path
) -> None:
    # 入库阶段、很多年前创建：首响红；首响率分母计入、分子不计（还没离开 intake）
    _insert_lead(tmp_path, lead_id="1", stage=INITIAL_STAGE, created_at="2020-01-01T00:00:00+00:00")
    # 已离开 intake、很多年没有任何跟进：首响率分子计入；久未跟进灰
    _insert_lead(
        tmp_path, lead_id="2", stage="first_contact", created_at="2020-06-01T00:00:00+00:00"
    )

    response = client.get("/dashboard")
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == FIELDS
    assert body["total"] == 2
    assert body["pool"] == 2
    assert body["new_this_week"] == 0
    assert body["followups_this_week"] == 0
    # 分子=1（2 号已离开 intake）；分母=2
    assert body["first_response_24h_rate"] == 0.5
    # 分子=2（两条都没有跟进、都早已超过 7 天）；分母=2 条都未关闭
    assert body["stale_ratio"] == 1.0
    assert body["silent_count"] == 2
    assert body["reminders_by_level"] == {"red": 1, "yellow": 0, "gray": 1}


def test_dashboard_on_empty_book_returns_zeroes(client: TestClient) -> None:
    body = client.get("/dashboard").json()

    assert body == {
        "total": 0,
        "pool": 0,
        "new_this_week": 0,
        "followups_this_week": 0,
        "first_response_24h_rate": 0.0,
        "stale_ratio": 0.0,
        "silent_count": 0,
        "reminders_by_level": {"red": 0, "yellow": 0, "gray": 0},
    }
