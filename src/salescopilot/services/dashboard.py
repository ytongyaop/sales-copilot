"""仪表盘用例：取线索与跟进 → 按默认模板阈值算一屏指标。

指标计算全在 `domain/dashboard.py`（纯函数、注入 now）；本层只负责取数与把时钟字符串
翻译成 datetime，不自己写任何口径。

仪表盘固定用默认模板（`DEFAULT_TEMPLATE`）的阈值：一屏里只允许一套口径，
避免同屏的线索各自用不同模板阈值、数字互相打架。
"""

from collections.abc import Callable
from datetime import UTC, datetime

from salescopilot.domain.dashboard import Dashboard, compute_dashboard
from salescopilot.domain.models import DEFAULT_TEMPLATE
from salescopilot.domain.ports import FollowupRepository, LeadRepository
from salescopilot.domain.reminders import parse_moment
from salescopilot.services.leads import utc_now_iso


class DashboardService:
    """仪表盘边界：一次调用返回一份 `Dashboard`。"""

    def __init__(
        self,
        leads: LeadRepository,
        followups: FollowupRepository,
        template_loader: Callable[[str], dict],
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._leads = leads
        self._followups = followups
        self._template_loader = template_loader
        self._clock = clock if clock is not None else utc_now_iso

    def summary(self) -> Dashboard:
        """按当前时刻算一屏指标；模板读不到就按空阈值算（提醒计数全 0，不抛错）。"""
        now = _as_now(self._clock())
        leads = self._leads.list()
        followups = [item for lead in leads for item in self._followups.list_for(lead.id)]
        return compute_dashboard(
            leads=leads,
            followups=followups,
            template=self._template(DEFAULT_TEMPLATE),
            now=now,
        )

    def _template(self, key: str) -> dict:
        try:
            return self._template_loader(key)
        except KeyError:
            return {}


def _as_now(raw: str) -> datetime:
    """时钟字符串 → 带时区的 datetime；解析不了就退回当前时刻（不让接口 500）。"""
    moment = parse_moment(raw)
    return moment if moment is not None else datetime.now(UTC)
