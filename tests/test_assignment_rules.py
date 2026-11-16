"""归属规则的三条纯函数：公海判定、能不能认领、能不能释放（各分支全覆盖）。"""

from dataclasses import replace

from salescopilot.domain.assignment import can_claim, can_release, is_in_pool
from salescopilot.domain.models import INITIAL_STAGE, Lead


def _lead(owner: str | None) -> Lead:
    return Lead(
        id="lead-1",
        name="线索-测试",
        contact="13800001111",
        source="inbound",
        stage=INITIAL_STAGE,
        owner=owner,
        created_at="2026-11-16T09:00:00+00:00",
    )


def test_is_in_pool_when_owner_is_none_or_blank() -> None:
    assert is_in_pool(_lead(None)) is True
    assert is_in_pool(_lead("   ")) is True
    assert is_in_pool(replace(_lead(None), owner="")) is True


def test_is_in_pool_is_false_when_someone_owns_it() -> None:
    assert is_in_pool(_lead("王五")) is False


def test_can_claim_only_when_in_pool() -> None:
    allowed, reason = can_claim(_lead(None))
    assert allowed is True
    assert reason == ""


def test_can_claim_rejects_lead_already_owned() -> None:
    allowed, reason = can_claim(_lead("王五"))
    assert allowed is False
    assert reason == "该线索已归属「王五」"


def test_can_release_only_when_owned() -> None:
    allowed, reason = can_release(_lead("王五"))
    assert allowed is True
    assert reason == ""


def test_can_release_rejects_lead_already_in_pool() -> None:
    allowed, reason = can_release(_lead(None))
    assert allowed is False
    assert reason == "该线索尚未归属"
