"""SQLite 仓储：业务库的唯一写入点（IO 只在这一层）。

schema 与读写都不含业务规则：字段合法性由 `domain/rules.py` 判定，这里只负责
「目录建好、表建好、行进去/出来」。表结构可重复执行，重复调用不会炸。
"""

import os
import sqlite3
from pathlib import Path

from salescopilot.domain.models import Lead

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    contact TEXT NOT NULL,
    source TEXT NOT NULL,
    stage TEXT NOT NULL,
    owner TEXT,
    created_at TEXT NOT NULL
)
"""

_SELECT = "SELECT id, name, contact, source, stage, owner, created_at FROM leads"
_COLUMNS = ("id", "name", "contact", "source", "stage", "owner", "created_at")
DB_PATH_ENV = "SALESCOPILOT_DB"


class SqliteLeadRepository:
    """`domain/ports.py` 的 `LeadRepository` 的 SQLite 实现。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def add(self, lead: Lead) -> None:
        placeholders = ", ".join("?" for _ in _COLUMNS)
        columns = ", ".join(_COLUMNS)
        values = tuple(getattr(lead, column) for column in _COLUMNS)
        self._conn.execute(f"INSERT INTO leads ({columns}) VALUES ({placeholders})", values)
        self._conn.commit()

    def list(self) -> list[Lead]:
        rows = self._conn.execute(f"{_SELECT} ORDER BY created_at DESC").fetchall()
        return [self._to_lead(row) for row in rows]

    def get(self, lead_id: str) -> Lead | None:
        row = self._conn.execute(f"{_SELECT} WHERE id = ?", (lead_id,)).fetchone()
        return None if row is None else self._to_lead(row)

    @staticmethod
    def _to_lead(row: sqlite3.Row) -> Lead:
        return Lead(
            id=row["id"],
            name=row["name"],
            contact=row["contact"],
            source=row["source"],
            stage=row["stage"],
            owner=row["owner"],
            created_at=row["created_at"],
        )


def default_db_path() -> Path:
    """数据库路径：优先环境变量 `SALESCOPILOT_DB`，否则仓库根的 `data/app.sqlite3`。"""
    override = os.environ.get(DB_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return _repo_root() / "data" / "app.sqlite3"


def _repo_root() -> Path:
    """按模块位置推仓库根，不依赖当前工作目录。

    `adapters/db/` 比 `salescopilot/` 多一层，所以仓库根是 `src/` 的父目录；
    先把这条目录链断言掉，路径写法一变就立刻报错，而不是悄悄写到别处。
    """
    module_dir = Path(__file__).resolve().parent  # src/salescopilot/adapters/db
    adapters_dir = module_dir.parent  # src/salescopilot/adapters
    package_dir = adapters_dir.parent  # src/salescopilot
    src_dir = package_dir.parent  # src
    actual = (module_dir.name, adapters_dir.name, package_dir.name, src_dir.name)
    expected = ("db", "adapters", "salescopilot", "src")
    if actual != expected:
        raise RuntimeError(f"无法定位仓库根：期望目录链 {expected}，实际 {actual}")
    return src_dir.parent
