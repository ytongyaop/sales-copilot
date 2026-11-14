"""块与字段的通用中文名（纯数据，无 IO）。

块 id 与字段名是机器契约（流程、校验、接口都用 id 传递）；这里只提供给人看的通用叫法。
行业叫法不在这里：它只存在于 `templates/*.yaml` 的 `label`，由 `GET /templates` 叠加在上层。
查不到就原样返回，绝不给未知 id 编一个名字。
"""

BLOCK_LABELS = {
    "intake": "线索入库",
    "first_contact": "首次联系",
    "qualify": "需求确认",
    "appointment": "约定到访",
    "proposal": "方案报价",
    "close": "成交/关闭",
}

FIELD_LABELS = {
    "contact": "联系方式",
    "source": "来源",
    "decision_maker": "决策人",
    "need": "核心需求",
    "appointment_at": "约定时间",
    "amount": "金额",
    "conclusion": "结论",
}


def block_label(block_id: str) -> str:
    """块的中文名；未知块返回原 id（不静默改写成别的阶段）。"""
    return BLOCK_LABELS.get(block_id, block_id)


def field_label(field_name: str) -> str:
    """字段的中文名；未知字段返回原名。"""
    return FIELD_LABELS.get(field_name, field_name)
