"""信息卡用例：模板声明 + 该客户当前事实 → 四要素卡片（覆盖度 + 缺项问题句）。

分工固定：算覆盖度与缺项是 `domain/card.py` 的纯函数；本层只做两件事——
按线索自己的模板取出声明、按线索取出当前事实，然后把两者交给纯函数。
本层不写库、不认识 SQLite，也不认识任何行业字段。
"""

from collections.abc import Callable

from salescopilot.domain.card import Card, build_card
from salescopilot.domain.ports import FactRepository, LeadRepository
from salescopilot.domain.rules import ValidationIssue


class CardService:
    """信息卡边界：线索 id 进，（卡片或问题）出；不抛业务异常。"""

    def __init__(
        self,
        leads: LeadRepository,
        facts: FactRepository,
        template_loader: Callable[[str], dict],
    ) -> None:
        """`template_loader` 由装配点注入（已校验过的模板清单），本层不认识文件系统。"""
        self._leads = leads
        self._facts = facts
        self._template_loader = template_loader

    def card(self, lead_id: str) -> tuple[Card | None, list[ValidationIssue]]:
        """取一张线索的信息卡；线索不存在或模板缺失时返回 `(None, 问题)`。

        字段值只来自该线索的事实：模板声明了但没采集到的字段进 `missing`，
        每个缺项自带一句「下次该问什么」。
        """
        lead = self._leads.get(lead_id)
        if lead is None:
            return None, [ValidationIssue("lead_id", "线索不存在")]

        try:
            spec = self._template_loader(lead.template)
        except KeyError:
            return None, [ValidationIssue("template", f"未知模板：「{lead.template}」")]
        if not isinstance(spec, dict):
            return None, [ValidationIssue("template", f"模板「{lead.template}」不是映射")]

        return build_card(spec, self._facts.list_for(lead_id)), []
