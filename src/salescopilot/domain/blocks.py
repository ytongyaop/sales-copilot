"""流程块契约（纯数据，无 IO）。

块是数据契约而不是代码类：只声明必填的通用字段名、允许的后继块、以及关联的阈值参数名。
块里没有分支逻辑，也没有任何行业名词——行业叫法只存在于模板的 `label` 取值里。
v1 固定 6 个块，封顶不扩。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Block:
    """一个流程块的静态契约。

    - `requires`：进入下一步前必须已填的通用字段名。
    - `exits`：允许的后继块 id；空元组表示终止块。
    - `timeout_key`：关联的阈值参数名；None 表示该块无超时。
    """

    id: str
    requires: tuple[str, ...]
    exits: tuple[str, ...]
    timeout_key: str | None = None


BLOCKS: dict[str, Block] = {
    "intake": Block(
        id="intake",
        requires=("contact", "source"),
        exits=("first_contact",),
    ),
    "first_contact": Block(
        id="first_contact",
        requires=("contact",),
        exits=("qualify",),
        timeout_key="first_response_minutes",
    ),
    "qualify": Block(
        id="qualify",
        requires=("decision_maker", "need"),
        exits=("appointment", "proposal"),
    ),
    "appointment": Block(
        id="appointment",
        requires=("appointment_at",),
        exits=("proposal",),
        timeout_key="feedback_window_hours",
    ),
    "proposal": Block(
        id="proposal",
        requires=("amount",),
        exits=("close",),
    ),
    "close": Block(
        id="close",
        requires=("conclusion",),
        exits=(),
    ),
}


def get_block(block_id: str) -> Block:
    """按 id 取块契约；未知 id 直接抛 KeyError，不返回默认块。"""
    return BLOCKS[block_id]
