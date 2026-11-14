"""`domain/facts.py` 的纯规则测试：候选校验 + 合并判定（不碰数据库、不碰模型）。"""

import pytest

from salescopilot.domain.facts import (
    ALLOWED_FACT_FIELDS,
    MAX_FACT_VALUE_LEN,
    decide_merge,
    validate_candidates,
)
from salescopilot.domain.models import Fact


def _candidate(**overrides) -> dict:
    base = {
        "field": "need",
        "value": "想先了解流程",
        "source_span": "想先了解流程",
        "confidence": 0.8,
    }
    base.update(overrides)
    return base


def test_allowed_fields_are_the_generic_five() -> None:
    assert ALLOWED_FACT_FIELDS == (
        "decision_maker",
        "need",
        "objection",
        "amount",
        "appointment_at",
    )


def test_unknown_field_is_dropped() -> None:
    passed, issues = validate_candidates([_candidate(field="hobby")])

    assert passed == []
    assert [issue.location for issue in issues] == ["candidates[0].field"]
    assert "hobby" in issues[0].message


def test_empty_source_span_is_dropped() -> None:
    """反证 1 的规则层：没有原文片段的候选一律不要。"""
    passed, issues = validate_candidates(
        [_candidate(field="need", value="无据猜测", source_span="   ")]
    )

    assert passed == []
    assert issues[0].location == "candidates[0].source_span"
    assert "原文片段" in issues[0].message


def test_too_long_value_is_dropped() -> None:
    long_value = "长" * (MAX_FACT_VALUE_LEN + 1)
    passed, issues = validate_candidates([_candidate(value=long_value, source_span=long_value)])

    assert passed == []
    assert issues[0].location == "candidates[0].value"
    assert str(MAX_FACT_VALUE_LEN) in issues[0].message


def test_objection_outside_enum_is_dropped() -> None:
    passed, issues = validate_candidates([_candidate(field="objection", value="太麻烦")])

    assert passed == []
    assert issues[0].location == "candidates[0].value"
    assert "异议" in issues[0].message


@pytest.mark.parametrize("confidence", [-0.1, 1.5, "0.8", None, True])
def test_confidence_out_of_range_or_wrong_type_is_dropped(confidence) -> None:
    passed, issues = validate_candidates([_candidate(confidence=confidence)])

    assert passed == []
    assert issues[0].location == "candidates[0].confidence"


def test_valid_candidate_passes_and_is_normalized() -> None:
    passed, issues = validate_candidates(
        [
            {
                "field": " need ",
                "value": "  想先了解流程 ",
                "source_span": " 想先了解流程 ",
                "confidence": 1,
            }
        ]
    )

    assert issues == []
    assert passed == [
        {
            "field": "need",
            "value": "想先了解流程",
            "source_span": "想先了解流程",
            "confidence": 1.0,
        }
    ]


def test_index_in_location_points_at_the_skipped_candidate() -> None:
    """位置要指回模型原始输出的第几条，而不是过滤后的顺序。"""
    passed, issues = validate_candidates(
        [
            _candidate(field="need", value="想先了解流程"),
            _candidate(field="objection", value="price", source_span="贵"),
            _candidate(field="need", value="无据猜测", source_span=""),
        ]
    )

    assert [candidate["field"] for candidate in passed] == ["need", "objection"]
    assert [issue.location for issue in issues] == ["candidates[2].source_span"]


def _existing(**overrides) -> Fact:
    base = {
        "id": "fact-1",
        "lead_id": "lead-1",
        "raw_id": "raw-0",
        "field": "objection",
        "value": "price",
        "source": "llm",
        "source_span": "贵",
        "confidence": 0.8,
        "locked": False,
        "at": "2026-11-14T00:00:00+00:00",
    }
    base.update(overrides)
    return Fact(**base)


def test_decide_merge_skips_locked_fact() -> None:
    fact, reason = decide_merge(
        _existing(locked=True),
        _candidate(field="objection", value="effect", source_span="效果"),
        lead_id="lead-1",
        raw_id="raw-1",
        at="2026-11-14T01:00:00+00:00",
    )

    assert fact is None
    assert reason == "人工锁定，跳过"


def test_decide_merge_skips_same_value() -> None:
    fact, reason = decide_merge(
        _existing(),
        _candidate(field="objection", value="price", source_span="贵"),
        lead_id="lead-1",
        raw_id="raw-1",
        at="2026-11-14T01:00:00+00:00",
    )

    assert fact is None
    assert reason == "重复值"


def test_decide_merge_creates_new_fact_when_absent() -> None:
    fact, reason = decide_merge(
        None,
        _candidate(field="objection", value="price", source_span="贵"),
        lead_id="lead-1",
        raw_id="raw-1",
        at="2026-11-14T01:00:00+00:00",
    )

    assert reason == "ok"
    assert fact is not None
    assert fact.lead_id == "lead-1"
    assert fact.raw_id == "raw-1"
    assert fact.field == "objection"
    assert fact.value == "price"
    assert fact.source == "llm"
    assert fact.source_span == "贵"
    assert fact.confidence == 0.8
    assert fact.locked is False
    assert fact.at == "2026-11-14T01:00:00+00:00"


def test_decide_merge_creates_new_fact_when_value_differs() -> None:
    fact, reason = decide_merge(
        _existing(value="time"),
        _candidate(field="objection", value="price", source_span="贵"),
        lead_id="lead-1",
        raw_id="raw-1",
        at="2026-11-14T01:00:00+00:00",
    )

    assert reason == "ok"
    assert fact is not None
    assert fact.value == "price"
    assert fact.locked is False
