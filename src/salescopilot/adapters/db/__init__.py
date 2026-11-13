"""数据库适配器：SQLite 仓储（端口实现）。"""

from .sqlite import SCHEMA, SqliteLeadRepository, default_db_path

__all__ = ["SCHEMA", "SqliteLeadRepository", "default_db_path"]
