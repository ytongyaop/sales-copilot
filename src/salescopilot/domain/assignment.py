"""归属规则的纯函数：公海判定 + 能不能认领 / 释放（无 IO、不抛异常）。

规则只有一条主线：**一个线索同时只有一个归属人**。
- 归属人为空 = 在公海，谁都能认领；
- 已在别人名下 = 不能再被认领（要换人必须先释放，或者由归属人自己让出）；
所有提示文案在这里收口，用例层与路由不另写一套说法。
"""

from salescopilot.domain.models import Lead


def _owner_text(lead: Lead) -> str:
    """归属人的有效取值：None 或纯空格都视为「没有归属人」。"""
    if lead.owner is None:
        return ""
    return lead.owner.strip()


def is_in_pool(lead: Lead) -> bool:
    """是否在公海：没有归属人。"""
    return _owner_text(lead) == ""


def can_claim(lead: Lead) -> tuple[bool, str]:
    """能不能认领；返回（能不能, 不能时的中文原因）。"""
    owner = _owner_text(lead)
    if owner == "":
        return True, ""
    return False, f"该线索已归属「{owner}」"


def can_release(lead: Lead) -> tuple[bool, str]:
    """能不能释放；返回（能不能, 不能时的中文原因）。"""
    owner = _owner_text(lead)
    if owner == "":
        return False, "该线索尚未归属"
    return True, ""
