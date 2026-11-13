"""装配点：把端口与实现接起来。

全项目只有这里允许同时 import `services` 与 `adapters`；换库、换时钟只改这个文件，
业务代码与路由都不用动。
"""

from salescopilot.adapters.db.sqlite import SqliteLeadRepository, default_db_path
from salescopilot.domain.ports import LeadRepository
from salescopilot.services.leads import LeadService


def build_lead_service() -> LeadService:
    """读配置 → 建 SQLite 仓储 → 注入用例服务。"""
    repo: LeadRepository = SqliteLeadRepository(default_db_path())
    return LeadService(repo)
