"""中文名查表函数的纯函数测试：命中给中文名，未命中原样返回。"""

from salescopilot.domain.blocks import BLOCKS
from salescopilot.domain.labels import (
    BLOCK_LABELS,
    FIELD_LABELS,
    block_label,
    field_label,
)


def test_block_label_hit() -> None:
    assert block_label("qualify") == "需求确认"
    assert block_label("close") == BLOCK_LABELS["close"]


def test_block_label_miss_returns_id() -> None:
    assert block_label("nonexistent") == "nonexistent"


def test_field_label_hit() -> None:
    assert field_label("decision_maker") == "决策人"
    assert field_label("appointment_at") == "约定时间"


def test_field_label_miss_returns_name() -> None:
    assert field_label("unknown_field") == "unknown_field"


def test_tables_cover_all_blocks_and_required_fields() -> None:
    """块契约里出现的块与必填字段都必须有中文名，否则提示会漏回原名。"""
    missing_blocks = [block_id for block_id in BLOCKS if block_id not in BLOCK_LABELS]
    missing_fields = [
        name for block in BLOCKS.values() for name in block.requires if name not in FIELD_LABELS
    ]
    assert missing_blocks == []
    assert missing_fields == []
