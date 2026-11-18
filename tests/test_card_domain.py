"""信息卡纯函数的测试：覆盖度、缺项顺序、缺项问题句、同字段取当前那条。

全部是 `domain/card.py` 的纯函数：不建库、不联网、不读文件（模板声明用内联规格）。
"""

from salescopilot.domain.card import (
    Card,
    build_card,
    declared_field_keys,
)
from salescopilot.domain.models import Fact

# 四个分区、五个字段：顺序与 `CARD_SECTIONS` 一致（basic → needs → decision → followup）。
SPEC: dict = {
    "card": {
        "sections": {
            "basic": {
                "label": "基础信息",
                "fields": [
                    {"key": "company", "label": "公司全称", "question": "对方公司全称是什么？"}
                ],
            },
            "needs": {
                "label": "需求与痛点",
                "fields": [
                    {"key": "need", "label": "核心需求", "question": "他们最想解决什么？"},
                    {"key": "pain_strength", "label": "痛点强度", "question": "不解决会怎样？"},
                ],
            },
            "decision": {
                "label": "决策流程与预算",
                "fields": [
                    {"key": "approval_flow", "label": "决策流程", "question": "内部走什么流程？"}
                ],
            },
            "followup": {
                "label": "跟进记录",
                "fields": [{"key": "next_step", "label": "下一步动作", "question": "下次谈什么？"}],
            },
        }
    }
}

ALL_KEYS = ["company", "need", "pain_strength", "approval_flow", "next_step"]


def _fact(
    field: str,
    value: str,
    *,
    source: str = "llm",
    span: str = "客户原话",
    locked: bool = False,
    at: str = "2026-11-18T10:00:00+00:00",
) -> Fact:
    return Fact(
        id=f"fact-{field}-{at}",
        lead_id="lead-1",
        raw_id="raw-1",
        field=field,
        value=value,
        source=source,
        source_span=span,
        confidence=0.8,
        locked=locked,
        at=at,
        card_section="",
    )


def test_empty_facts_cover_no_field_and_list_every_question() -> None:
    """一条事实都没有：覆盖度 0.0，缺项覆盖全部字段、顺序固定、每项都有问题句。"""
    card = build_card(SPEC, [])

    assert isinstance(card, Card)
    assert card.coverage == 0.0
    assert [section.key for section in card.sections] == [
        "basic",
        "needs",
        "decision",
        "followup",
    ]
    assert [item["field"] for item in card.missing] == ALL_KEYS
    assert all(item["question"].strip() for item in card.missing)
    assert all(item["section_label"] for item in card.missing)
    assert all(section.filled == 0 for section in card.sections)
    assert [section.total for section in card.sections] == [1, 2, 1, 1]
    # 没填的字段：值与来源都是空，不是 None
    assert all(item.value == "" and item.source == "" for s in card.sections for item in s.fields)


def test_partial_facts_report_remaining_missing_only() -> None:
    """填了两个字段：覆盖度按声明总数算，缺项只剩没填的那几个，顺序不变。"""
    card = build_card(SPEC, [_fact("company", "华远建设有限公司"), _fact("need", "要降成本")])

    assert card.coverage == 2 / 5
    assert [item["field"] for item in card.missing] == [
        "pain_strength",
        "approval_flow",
        "next_step",
    ]
    assert [section.filled for section in card.sections] == [1, 1, 0, 0]
    company = card.sections[0].fields[0]
    assert company.value == "华远建设有限公司"
    assert company.source == "llm"
    assert company.span == "客户原话"
    assert company.locked is False
    assert company.question == "对方公司全称是什么？"


def test_manual_fact_keeps_source_span_and_lock() -> None:
    """人工补录的字段：来源 manual、片段是人工标记、带着锁定标志。"""
    manual = _fact("need", "人工填的需求", source="manual", span="(人工录入)", locked=True)

    card = build_card(SPEC, [manual])

    field = card.sections[1].fields[0]
    assert field.value == "人工填的需求"
    assert field.source == "manual"
    assert field.span == "(人工录入)"
    assert field.locked is True


def test_same_key_takes_the_current_fact() -> None:
    """同字段多条事实：取当前那条（列表里最后出现的），覆盖度只算一次。"""
    card = build_card(
        SPEC,
        [
            _fact("company", "旧名字有限公司", at="2026-11-18T09:00:00+00:00"),
            _fact("company", "新名字有限公司", at="2026-11-18T11:00:00+00:00"),
        ],
    )

    fields = [field for section in card.sections for field in section.fields]
    assert [field.value for field in fields if field.key == "company"] == ["新名字有限公司"]
    assert card.coverage == 1 / 5
    assert "company" not in [item["field"] for item in card.missing]


def test_template_without_fields_reports_zero_coverage() -> None:
    """模板声明了分区但没字段：覆盖度 0.0（分母为 0 不报错），缺项为空。"""
    spec = {
        "card": {
            "sections": {
                "basic": {"label": "基础信息", "fields": []},
                "needs": {"label": "需求与痛点", "fields": []},
                "decision": {"label": "决策流程与预算", "fields": []},
                "followup": {"label": "跟进记录", "fields": []},
            }
        }
    }

    card = build_card(spec, [_fact("company", "华远建设有限公司")])

    assert card.coverage == 0.0
    assert card.missing == []
    assert all(section.total == 0 for section in card.sections)


def test_declared_field_keys_follow_display_order() -> None:
    """白名单推导用的字段名：按展示顺序去重；没有模板声明时是空元组。"""
    assert declared_field_keys(SPEC) == tuple(ALL_KEYS)
    assert declared_field_keys({}) == ()
    assert declared_field_keys(None) == ()
