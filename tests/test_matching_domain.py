"""归属三级漏斗的规则测试（纯规则，不联网、不落库）。

覆盖规范化（含「科技」不当后缀）、相似度、五条判定规则及其反证、
候选排序与审计记录结构。每条断言都对应 `domain/matching.py` 的一条规则。
"""

import json

import pytest

from salescopilot.domain.matching import (
    ORG_SUFFIXES,
    Candidate,
    MatchDecision,
    MatchThresholds,
    audit_record,
    decide_band,
    fuzzy_score,
    normalize_org,
    normalize_person,
    normalize_with_alias,
    rank_candidates,
    suffix_only_difference,
)

# 规则用例统一用默认阈值，避免每条都手写一份。
THRESHOLDS = MatchThresholds()


def test_normalize_org_strips_width_whitespace_case_and_suffix() -> None:
    """全角括号、多余空格、大小写、公司/集团后缀都被规范化掉。"""
    assert normalize_org("  华远（北京） 建设集团有限公司 ") == "华远建设"
    assert normalize_org("华远建设集团") == "华远建设"
    assert normalize_org("华远建设有限公司") == "华远建设"
    assert normalize_org("ACME  Trading  CO., LTD".lower()) == normalize_org(
        "acme trading co., ltd"
    )
    # 大小写不敏感：英文名两种写法落成同一个键，且结果里没有大写。
    plain = normalize_org("ACME Trading")
    assert plain == normalize_org("acme trading")
    assert plain == plain.lower()


def test_tech_word_is_not_treated_as_suffix() -> None:
    """「科技」不是后缀：删了会把两家不同公司判成同一家（调研结论）。"""
    assert "科技" not in ORG_SUFFIXES
    company = normalize_org("华远科技有限公司")
    group = normalize_org("华远科技集团")
    assert "科技" in company, f"「科技」被当后缀删掉了：{company!r}"
    assert "科技" in group, f"「科技」被当后缀删掉了：{group!r}"
    assert company != "华远"
    assert group != "华远"


def test_normalize_with_alias_replaces_before_normalizing() -> None:
    """别名表先替换再规范化：简称与全称走同一个键。"""
    aliases = {"华远": "华远建设集团"}
    assert normalize_with_alias("华远", aliases) == normalize_org("华远建设集团")
    assert normalize_with_alias("华远", aliases) == "华远建设"
    # 没配置别名时原样规范化，不会凭空映射。
    assert normalize_with_alias("长江物流", aliases) == normalize_org("长江物流")


def test_normalize_person_keeps_name_clean() -> None:
    """人名：全角与括号注记被清掉，姓名本身不变。"""
    assert normalize_person(" 张 三（采购） ") == "张三"
    assert normalize_person("LI  Si") == "lisi"


def test_fuzzy_score_bounds_and_partial_match() -> None:
    """相同串 100、无关串接近 0、简称部分匹配落在合理区间。"""
    assert fuzzy_score("华远建设", "华远建设") == pytest.approx(100.0)
    assert fuzzy_score("华远建设", "长江物流") < 10.0
    assert fuzzy_score("", "华远建设") == 0.0

    partial = fuzzy_score("华远建设", "华远建设工程")
    assert 70.0 <= partial < 100.0, f"部分匹配分数异常：{partial}"


def test_rule1_exact_hit_wins_and_ignores_others() -> None:
    """规则 1：精确键命中直接 auto/exact，fuzzy 与 vector 都不再参与。"""
    decision = decide_band(
        exact_hit=True,
        fuzzy=10.0,
        vector=0.10,
        corroborated=False,
        thresholds=THRESHOLDS,
    )
    assert (decision.band, decision.scorer, decision.score) == ("auto", "exact", 100.0)
    assert "精确键命中" in decision.reason


def test_rule2_high_fuzzy_with_corroboration_is_auto() -> None:
    """规则 2：字符串 96 且有佐证 → auto/fuzzy。"""
    decision = decide_band(
        exact_hit=False,
        fuzzy=96.0,
        vector=None,
        corroborated=True,
        thresholds=THRESHOLDS,
    )
    assert (decision.band, decision.scorer) == ("auto", "fuzzy")
    assert decision.score == pytest.approx(96.0)
    assert "佐证" in decision.reason


def test_rule3_high_fuzzy_without_corroboration_goes_review() -> None:
    """规则 3：字符串 96 但无佐证 → review，reason 说明缺佐证。"""
    decision = decide_band(
        exact_hit=False,
        fuzzy=96.0,
        vector=None,
        corroborated=False,
        thresholds=THRESHOLDS,
    )
    assert decision.band == "review"
    assert decision.scorer == "fuzzy"
    assert "缺第二字段佐证" in decision.reason


def test_rule4_mid_fuzzy_goes_review() -> None:
    """规则 4：字符串 90 落在 85-95 复核区 → review。"""
    decision = decide_band(
        exact_hit=False,
        fuzzy=90.0,
        vector=None,
        corroborated=True,
        thresholds=THRESHOLDS,
    )
    assert decision.band == "review"
    assert decision.scorer == "fuzzy"
    assert "复核区" in decision.reason


def test_rule5a_vector_with_corroboration_is_auto() -> None:
    """规则 5a：字符串低分 + 向量 0.95 + 佐证 → auto/vector。"""
    decision = decide_band(
        exact_hit=False,
        fuzzy=70.0,
        vector=0.95,
        corroborated=True,
        thresholds=THRESHOLDS,
    )
    assert (decision.band, decision.scorer) == ("auto", "vector")
    assert decision.score == pytest.approx(0.95)


def test_rule5a_counterproof_vector_alone_cannot_auto() -> None:
    """规则 5a 反证：向量 0.95 但无佐证时绝不能 auto，只能 review。

    这是「向量永不单独授权合并」的唯一机器判据；去掉 `corroborated` 检查时本用例必须失败。
    """
    decision = decide_band(
        exact_hit=False,
        fuzzy=70.0,
        vector=0.95,
        corroborated=False,
        thresholds=THRESHOLDS,
    )
    assert decision.band == "review"
    assert decision.scorer == "vector"
    assert "向量不得单独自动归属" in decision.reason


def test_rule5b_mid_vector_goes_review() -> None:
    """规则 5b：字符串低分 + 向量 0.85 → review。"""
    decision = decide_band(
        exact_hit=False,
        fuzzy=70.0,
        vector=0.85,
        corroborated=True,
        thresholds=THRESHOLDS,
    )
    assert decision.band == "review"
    assert decision.scorer == "vector"
    assert "复核区" in decision.reason


@pytest.mark.parametrize("vector", [0.5, None])
def test_rule5c_no_candidate_reaches_floor(vector: float | None) -> None:
    """规则 5c：字符串低分 + 向量不足（或无向量）→ reject/none，视为新客户。"""
    decision = decide_band(
        exact_hit=False,
        fuzzy=70.0,
        vector=vector,
        corroborated=True,
        thresholds=THRESHOLDS,
    )
    assert decision.band == "reject"
    assert decision.scorer == "none"
    assert "视为新客户" in decision.reason


def test_suffix_only_difference_detects_company_vs_group() -> None:
    """同一核心串、只差组织形式后缀 → True：「有限公司」与「集团」是两个法人。"""
    assert suffix_only_difference("华远科技有限公司", "华远科技集团") is True


def test_suffix_only_forces_review_even_at_full_score_with_corroboration() -> None:
    """仅组织形式不同时，字符串 100 分且有佐证也只能 review；精确键仍然最高优先。"""
    decision = decide_band(
        exact_hit=False,
        fuzzy=100.0,
        vector=None,
        corroborated=True,
        thresholds=THRESHOLDS,
        suffix_only=True,
    )
    assert decision.band == "review"
    assert decision.scorer == "fuzzy"
    assert decision.score == pytest.approx(100.0)
    assert "不能自动合并" in decision.reason

    # 精确键命中说明确实是同一家（如统一社会信用代码一致），此时与后缀无关，仍 auto。
    exact = decide_band(
        exact_hit=True,
        fuzzy=100.0,
        vector=None,
        corroborated=True,
        thresholds=THRESHOLDS,
        suffix_only=True,
    )
    assert (exact.band, exact.scorer) == ("auto", "exact")


def test_suffix_only_difference_false_for_identical_names() -> None:
    """完全同名不算「仅组织形式不同」：没有差异就没有复核理由。"""
    assert suffix_only_difference("华远科技", "华远科技") is False


def test_suffix_only_difference_false_when_core_differs() -> None:
    """核心串不同（多一个「远」字）时不算：那是近乎同名的另一家，不是同一家的两种写法。"""
    assert suffix_only_difference("华远科技", "华远远科技") is False


def test_rank_candidates_orders_by_score_then_scorer_and_truncates() -> None:
    """同分按 exact > fuzzy > vector，再按入参顺序；limit 截断生效。"""
    candidates = [
        Candidate(lead_id="l1", name="甲", score=95.0, scorer="vector"),
        Candidate(lead_id="l2", name="乙", score=95.0, scorer="exact"),
        Candidate(lead_id="l3", name="丙", score=88.0, scorer="fuzzy"),
        Candidate(lead_id="l4", name="丁", score=99.0, scorer="fuzzy"),
    ]

    ranked = rank_candidates(candidates, limit=3)
    assert [c.lead_id for c in ranked] == ["l4", "l2", "l1"]
    assert rank_candidates(candidates, limit=2) == ranked[:2]
    assert rank_candidates(candidates, limit=0) == []
    # 不改动入参顺序。
    assert [c.lead_id for c in candidates] == ["l1", "l2", "l3", "l4"]


def test_audit_record_structure_is_json_serializable() -> None:
    """审计记录含 decision/candidates/thresholds 三块，且能直接 json.dumps 落库。"""
    decision = MatchDecision(band="review", scorer="fuzzy", score=92.4, reason="测试用理由")
    candidates = [Candidate(lead_id="l1", name="华远建设", score=92.4, scorer="fuzzy")]

    record = audit_record(decision, candidates, thresholds=MatchThresholds(fuzzy_review=86.0))

    assert set(record) == {"decision", "candidates", "thresholds"}
    assert record["decision"]["band"] == "review"
    assert record["candidates"][0]["lead_id"] == "l1"
    assert record["thresholds"]["fuzzy_review"] == pytest.approx(86.0)

    payload = json.dumps(record, ensure_ascii=False)
    assert json.loads(payload) == record
