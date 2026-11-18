"""分拣接口的行为测试：临时库 + 假模型，全部离线。

覆盖：POST 201 结构（要素出处能在原文定位）、列表按状态过滤、单条查得到、
坏参数 422。路由只调用例，判据都在 `domain/sorting.py`，这里只验「接线对不对」。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.entrypoints.api.app import app

LEAD = {"name": "华远建设有限公司", "contact": "13800001111", "source": "referral"}
PHONE_CONTENT = "13800001111 那边说预算三十万，下周一下午再聊"
NEW_CONTENT = "完全不相干的一通闲聊，预算还在谈，下周再说"
# 超过 60 字的长原文：验列表摘要的截断（60 字 + 「…」）。
LONG_CONTENT = (
    "华远建设有限公司的刘经理说，下周一上午十点方便再聊一次，预算大概三十万左右，"
    "重点是想把三个班先排出来，后面再看要不要扩班，负责人是张校长，联系方式 13800002222"
)
# 名字与已有客户只差两个字：走字符相似（灰区），归属候选非空。
FUZZY_CONTENT = "华远建设工程有限公司那边说预算四十万，下周三再联系张校长"


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "sorting_api.sqlite3"))
    yield TestClient(app)


def _lead_id(client: TestClient) -> str:
    response = client.post("/leads", json=LEAD)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _sort(client: TestClient, content: str, lead_id: str | None = None):
    payload = {"content": content, "source": "paste"}
    if lead_id is not None:
        payload["lead_id"] = lead_id
    return client.post("/sorting", json=payload)


def test_post_sorting_returns_201_with_four_blocks(client: TestClient) -> None:
    """指定线索投料：201 返回归属 / 要素 / 分级 / 出库四块，要素出处能在原文定位。"""
    lead_id = _lead_id(client)

    response = _sort(client, PHONE_CONTENT, lead_id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) >= {
        "batch_id",
        "status",
        "lead_id",
        "confidence",
        "scorer",
        "suggestion",
        "dropped",
    }
    assert body["status"] == "pending_review"
    assert body["lead_id"] == lead_id
    assert body["scorer"] == "exact"
    assert body["confidence"] == 100.0
    assert body["dropped"] == []

    suggestion = body["suggestion"]
    assert set(suggestion) == {"elements", "grading", "next_action"}
    assert suggestion["elements"]
    for element in suggestion["elements"]:
        assert set(element) == {
            "card_section",
            "field",
            "value",
            "source_span",
            "confidence",
        }
        assert element["source_span"] in PHONE_CONTENT, element
    assert suggestion["grading"]["value"] in ("A", "B", "C")
    assert suggestion["grading"]["basis"]
    assert suggestion["next_action"]["action"]
    assert suggestion["next_action"]["value_offered"]


def test_post_sorting_without_lead_goes_to_pending_claim(client: TestClient) -> None:
    """不指定线索、也没有任何候选：进待认领池，`lead_id` 为空。"""
    _lead_id(client)

    response = _sort(client, NEW_CONTENT)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending_claim"
    assert body["lead_id"] is None
    assert body["scorer"] == "none"


def test_get_batches_filters_by_status(client: TestClient) -> None:
    """列表按状态过滤：待认领池只剩 `pending_claim` 的单。"""
    lead_id = _lead_id(client)
    _sort(client, PHONE_CONTENT, lead_id)
    _sort(client, NEW_CONTENT)

    everything = client.get("/sorting/batches")
    assert everything.status_code == 200, everything.text
    assert {item["status"] for item in everything.json()} == {"pending_review", "pending_claim"}

    pool = client.get("/sorting/batches", params={"status": "pending_claim", "limit": 50})
    assert pool.status_code == 200, pool.text
    items = pool.json()
    assert len(items) == 1
    assert items[0]["status"] == "pending_claim"
    assert items[0]["lead_id"] is None
    assert items[0]["lead_name"] is None
    assert items[0]["created_at"]
    assert items[0]["confidence"] == 0.0


def test_get_batches_reports_dropped_count(client: TestClient) -> None:
    """列表每条带 `dropped_count`（整数）：假模型不编造出处，干净单就是 0。"""
    lead_id = _lead_id(client)
    _sort(client, PHONE_CONTENT, lead_id)
    _sort(client, NEW_CONTENT)

    response = client.get("/sorting/batches")

    assert response.status_code == 200, response.text
    items = response.json()
    assert len(items) == 2
    for item in items:
        assert isinstance(item["dropped_count"], int)
        assert item["dropped_count"] == 0


def test_get_single_batch_keeps_model_text_and_parses_it(client: TestClient) -> None:
    """单条：既有模型原文（`suggestion_json`），也有解析后的视图。"""
    lead_id = _lead_id(client)
    batch_id = _sort(client, PHONE_CONTENT, lead_id).json()["batch_id"]

    response = client.get(f"/sorting/batches/{batch_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == batch_id
    assert body["lead_name"] == LEAD["name"]
    assert isinstance(body["suggestion_json"], str)
    assert body["suggestion_json"].lstrip().startswith("{")
    assert set(body["suggestion"]) == {"elements", "grading", "next_action"}


def test_get_unknown_batch_404(client: TestClient) -> None:
    response = client.get("/sorting/batches/not-there")

    assert response.status_code == 404
    assert response.json()["issues"][0]["location"] == "batch_id"


def test_list_batches_carries_truncated_content_preview(client: TestClient) -> None:
    """列表每条带 `content_preview`：原文前 60 字，超出补 `…`。"""
    lead_id = _lead_id(client)
    _sort(client, LONG_CONTENT, lead_id)

    response = client.get("/sorting/batches")

    assert response.status_code == 200, response.text
    items = response.json()
    assert len(items) == 1
    assert len(LONG_CONTENT) > 60
    assert items[0]["content_preview"] == LONG_CONTENT[:60] + "…"
    # 列表不背全文：候选与原文都只在单条接口里。
    assert "content" not in items[0]
    assert "lead_candidates" not in items[0]


def test_get_single_batch_carries_content_and_lead_candidates(client: TestClient) -> None:
    """单条：带完整原文（`content`）与归属候选（`lead_candidates`），投料响应同口径。"""
    lead_id = _lead_id(client)

    created = _sort(client, FUZZY_CONTENT)
    assert created.status_code == 201, created.text
    body = created.json()

    response = client.get(f"/sorting/batches/{body['batch_id']}")
    assert response.status_code == 200, response.text
    detail = response.json()

    assert detail["content"] == FUZZY_CONTENT
    assert detail["lead_candidates"]
    assert detail["lead_candidates"][0]["lead_id"] == lead_id
    assert set(detail["lead_candidates"][0]) == {"lead_id", "name", "score", "scorer"}
    # 投料响应与单条视图回的是同一份候选（前端不用两套口径）。
    assert body["lead_candidates"] == detail["lead_candidates"]


def test_batch_without_candidates_returns_empty_list(client: TestClient) -> None:
    """没有任何线索时投料：`lead_candidates` 是 `[]`，不是 null、也不是缺字段。"""
    created = _sort(client, NEW_CONTENT)
    assert created.status_code == 201, created.text
    body = created.json()

    response = client.get(f"/sorting/batches/{body['batch_id']}")
    assert response.status_code == 200, response.text

    assert body["lead_candidates"] == []
    assert response.json()["lead_candidates"] == []


def test_bad_parameters_are_422(client: TestClient) -> None:
    """坏参数一律 422：内容太短、状态不认识、条数越界、缺必填字段。"""
    short = client.post("/sorting", json={"content": "太短", "source": "paste"})
    assert short.status_code == 422
    assert short.json()["issues"][0]["location"] == "content"

    missing = client.post("/sorting", json={"source": "paste"})
    assert missing.status_code == 422
    assert missing.json()["issues"][0]["location"] == "content"

    bad_status = client.get("/sorting/batches", params={"status": "unknown"})
    assert bad_status.status_code == 422
    assert bad_status.json()["issues"][0]["location"] == "status"

    bad_limit = client.get("/sorting/batches", params={"limit": 0})
    assert bad_limit.status_code == 422
    assert bad_limit.json()["issues"][0]["location"] == "limit"


def test_unknown_lead_404(client: TestClient) -> None:
    response = _sort(client, PHONE_CONTENT, "not-a-lead")

    assert response.status_code == 404
    assert response.json()["issues"][0]["location"] == "lead_id"
