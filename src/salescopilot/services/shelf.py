"""客户货架用例：把每条客户打包成一张小卡（档位 + 覆盖度 + 缺项数 + 下次约定）。

分工固定：
- 覆盖度与缺项数是 `domain/card.py` 的产出：本层直接复用 `CardService`，不另算一遍；
- 档位口径是 `domain/grading.current_value`（人工值优先），与今日待办、详情页同源；
- 本层只做取数与拼装，不写库、不认识 SQLite，也不认识任何行业字段。

为什么要有这个只读接口：货架要「每家一行覆盖度 + 档位」，让浏览器逐家去问就是 3N 次请求，
而且三次响应之间客户可能已经变了；一次取完，页面上的分组和数字才自洽。
"""

from salescopilot.domain.grading import current_value
from salescopilot.domain.ports import FollowupRepository, GradingRepository, LeadRepository
from salescopilot.domain.shelf import ShelfEntry, build_entry
from salescopilot.services.card import CardService


class ShelfService:
    """货架边界：无入参、客户小卡列表出（没有客户就是空列表）。"""

    def __init__(
        self,
        leads: LeadRepository,
        cards: CardService,
        followups: FollowupRepository,
        grading_history: GradingRepository,
    ) -> None:
        """信息卡用 `CardService`（模板声明 + 事实），货架不重新实现覆盖度。"""
        self._leads = leads
        self._cards = cards
        self._followups = followups
        self._grading_history = grading_history

    def list(self) -> list[ShelfEntry]:
        """全部客户的小卡，顺序与 `GET /leads` 一致（创建时间倒序）。

        模板缺失（磁盘上没有这份模板）的客户跳过：算不出覆盖度就不摆一张数字骗人的卡。
        """
        entries: list[ShelfEntry] = []
        for lead in self._leads.list():
            card, _issues = self._cards.card(lead.id)
            if card is None:
                continue
            entries.append(
                build_entry(
                    lead=lead,
                    card=card,
                    grade=current_value(self._grading_history.list_for(lead.id)),
                    followups=self._followups.list_for(lead.id),
                )
            )
        return entries
