"""话术接口的端到端测试：临时库 + 临时索引 + 假向量，全程离线、不调模型。

四条关键行为：
1. 没有事实的线索 → POST /draft 返回 422 与中文原因；
2. 有事实但索引为空 → 422「没有检索到资料依据」；
3. 正常生成 → 200 带正文与引用；确认落库 → 201，跟进列表出现该记录；
4. 确认缺下次跟进时间 → 422（pydantic 层直接拦下）。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.adapters.knowledge.chroma_store import ChromaStore
from salescopilot.adapters.knowledge.embedding import FakeEmbedder
from salescopilot.adapters.knowledge.manifest import manifest_path_of
from salescopilot.entrypoints.api.app import app
from salescopilot.services.knowledge import KnowledgeService

LEAD = {"name": "张伟", "contact": "13800001111", "source": "referral"}

DOCS = [
    (
        "service_policy.md",
        "# 服务政策\n\n退费在受理后 15 个工作日内原路退回。\n\n试听需提前 1 天预约。",
    ),
    ("course.md", "# 班型说明\n\n标准班共 24 次课，价格区间 6800–9800 元。"),
]


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    index_dir = tmp_path / "index"
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("SALESCOPILOT_INDEX_DIR", str(index_dir))
    # 先建一份「模型 = fake」的索引，查询侧的假向量才和它对得上。
    KnowledgeService(
        FakeEmbedder(),
        ChromaStore(index_dir),
        manifest_path_of(index_dir),
        "fake",
    ).build(DOCS)
    yield TestClient(app)


def _lead_id(client: TestClient) -> str:
    response = client.post("/leads", json=LEAD)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _lead_with_fact(client: TestClient) -> str:
    lead_id = _lead_id(client)
    patched = client.patch(f"/leads/{lead_id}/facts/need", json={"value": "想了解退费规则"})
    assert patched.status_code == 200, patched.text
    return lead_id


def test_generate_rejected_when_lead_has_no_facts(client: TestClient) -> None:
    lead_id = _lead_id(client)

    response = client.post(f"/leads/{lead_id}/draft")

    assert response.status_code == 422, response.text
    issues = response.json()["issues"]
    assert [issue["message"] for issue in issues] == ["还没有客户事实，先提炼或补录"]


def test_generate_rejected_when_index_has_no_hits(
    client: TestClient, monkeypatch, tmp_path: Path
) -> None:
    # 换一个空索引目录：清单与库都是空的，检索必然没有命中。
    monkeypatch.setenv("SALESCOPILOT_INDEX_DIR", str(tmp_path / "empty-index"))
    lead_id = _lead_with_fact(client)

    response = client.post(f"/leads/{lead_id}/draft")

    assert response.status_code == 422, response.text
    assert [issue["message"] for issue in response.json()["issues"]] == [
        "没有检索到资料依据，无法生成话术"
    ]


def test_generate_then_confirm_writes_followup(client: TestClient) -> None:
    lead_id = _lead_with_fact(client)

    generated = client.post(f"/leads/{lead_id}/draft")

    assert generated.status_code == 200, generated.text
    body = generated.json()
    assert set(body) == {"draft_id", "text", "citations", "model"}
    assert body["text"]
    assert body["model"] == "fake"
    assert body["citations"]
    assert set(body["citations"][0]) == {"source", "text", "score"}
    assert body["citations"][0]["source"].endswith(".md")

    confirmed = client.post(
        f"/leads/{lead_id}/drafts/{body['draft_id']}/confirm",
        json={"objection": "price", "next_followup_at": "2026-11-20"},
    )

    assert confirmed.status_code == 201, confirmed.text
    followup = confirmed.json()
    assert followup["conclusion"] == body["text"]
    assert followup["objection"] == "price"
    assert followup["next_followup_at"] == "2026-11-20"

    listed = client.get(f"/leads/{lead_id}/followups").json()
    assert [item["id"] for item in listed] == [followup["id"]]


def test_confirm_carries_value_provided(client: TestClient) -> None:
    """确认草稿可带「本次提供的价值」：原样透传到跟进留痕；不填就是空串。"""
    lead_id = _lead_with_fact(client)
    first = client.post(f"/leads/{lead_id}/draft").json()["draft_id"]
    second = client.post(f"/leads/{lead_id}/draft").json()["draft_id"]

    with_value = client.post(
        f"/leads/{lead_id}/drafts/{first}/confirm",
        json={
            "objection": "price",
            "next_followup_at": "2026-11-20",
            "value_provided": " 退费流程说明 ",
        },
    )
    without = client.post(
        f"/leads/{lead_id}/drafts/{second}/confirm",
        json={"objection": "time", "next_followup_at": "2026-11-21"},
    )

    assert with_value.status_code == 201, with_value.text
    assert with_value.json()["value_provided"] == "退费流程说明"
    assert without.status_code == 201, without.text
    assert without.json()["value_provided"] == ""
    listed = client.get(f"/leads/{lead_id}/followups").json()
    assert [item["value_provided"] for item in listed] == ["退费流程说明", ""]


def test_confirm_without_next_date_is_rejected(client: TestClient) -> None:
    lead_id = _lead_with_fact(client)
    draft_id = client.post(f"/leads/{lead_id}/draft").json()["draft_id"]

    response = client.post(
        f"/leads/{lead_id}/drafts/{draft_id}/confirm", json={"objection": "price"}
    )

    assert response.status_code == 422, response.text
    issue = response.json()["issues"][0]
    assert issue["location"] == "next_followup_at"
    assert "缺少必填字段" in issue["message"]


def test_confirm_twice_is_conflict_and_discard_is_204(client: TestClient) -> None:
    lead_id = _lead_with_fact(client)
    draft_id = client.post(f"/leads/{lead_id}/draft").json()["draft_id"]
    payload = {"objection": "price", "next_followup_at": "2026-11-20"}

    first = client.post(f"/leads/{lead_id}/drafts/{draft_id}/confirm", json=payload)
    second = client.post(f"/leads/{lead_id}/drafts/{draft_id}/confirm", json=payload)

    assert first.status_code == 201
    assert second.status_code == 409

    other = client.post(f"/leads/{lead_id}/draft").json()["draft_id"]
    discarded = client.delete(f"/leads/{lead_id}/drafts/{other}")
    assert discarded.status_code == 204
    assert client.delete(f"/leads/{lead_id}/drafts/{other}").status_code == 409


def test_list_raws_returns_items_ascending(client: TestClient) -> None:
    lead_id = _lead_id(client)
    first = client.post(
        f"/leads/{lead_id}/raws", json={"content": "第一段原文，客户说太贵了", "source": "inbound"}
    )
    second = client.post(
        f"/leads/{lead_id}/raws",
        json={"content": "第二段原文，客户说下周再聊", "source": "inbound"},
    )

    listed = client.get(f"/leads/{lead_id}/raws").json()

    assert [item["id"] for item in listed] == [first.json()["id"], second.json()["id"]]
    assert [item["at"] for item in listed] == sorted(item["at"] for item in listed)


def test_list_raws_unknown_lead_404(client: TestClient) -> None:
    response = client.get("/leads/missing/raws")

    assert response.status_code == 404
    assert response.json()["issues"][0]["location"] == "lead_id"


def test_facts_carry_model_name(client: TestClient) -> None:
    lead_id = _lead_id(client)
    raw = client.post(
        f"/leads/{lead_id}/raws", json={"content": "客户说太贵了，下周再商量", "source": "inbound"}
    ).json()
    client.post(f"/raws/{raw['id']}/extract")

    facts = client.get(f"/leads/{lead_id}/facts").json()

    assert facts
    assert all(fact["model"] == "fake" for fact in facts)
