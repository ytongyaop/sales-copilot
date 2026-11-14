"""SQLite 仓储：业务库的唯一写入点（IO 只在这一层）。

schema 与读写都不含业务规则：字段合法性由 `domain/rules.py` 判定，这里只负责
「目录建好、表建好、行进去/出来」。表结构可重复执行，重复调用不会炸。

连接一律「每次操作自开自闭」：`sqlite3` 的 `with conn` 只管事务提交/回滚、**不关连接**，
所以这里显式 `close()`，顺手还掉「连接只开不关」这笔债。
"""

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from salescopilot.domain.models import DEFAULT_TEMPLATE, Followup, Lead, StageEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    contact TEXT NOT NULL,
    source TEXT NOT NULL,
    stage TEXT NOT NULL,
    owner TEXT,
    created_at TEXT NOT NULL,
    template TEXT NOT NULL DEFAULT 'edu_training',
    fields_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS stage_history (
    id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    from_stage TEXT NOT NULL,
    to_stage TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS followups (
    id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    conclusion TEXT NOT NULL,
    objection TEXT NOT NULL,
    next_followup_at TEXT NOT NULL,
    at TEXT NOT NULL
);
"""

_LEADS_COLUMNS = (
    "id",
    "name",
    "contact",
    "source",
    "stage",
    "owner",
    "created_at",
    "template",
    "fields_json",
)
_SELECT_LEAD = (
    "SELECT id, name, contact, source, stage, owner, created_at, template, fields_json FROM leads"
)
_SELECT_EVENT = "SELECT id, lead_id, from_stage, to_stage, at FROM stage_history"
_SELECT_FOLLOWUP = "SELECT id, lead_id, conclusion, objection, next_followup_at, at FROM followups"
DB_PATH_ENV = "SALESCOPILOT_DB"


class _SqliteRepository:
    """三个仓储的共同底座：建表只做一次，之后每次操作各开一个短连接。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


class SqliteLeadRepository(_SqliteRepository):
    """`domain/ports.py` 的 `LeadRepository` 的 SQLite 实现。"""

    def add(self, lead: Lead) -> None:
        placeholders = ", ".join("?" for _ in _LEADS_COLUMNS)
        columns = ", ".join(_LEADS_COLUMNS)
        values = (
            lead.id,
            lead.name,
            lead.contact,
            lead.source,
            lead.stage,
            lead.owner,
            lead.created_at,
            lead.template,
            json.dumps(lead.fields, ensure_ascii=False),
        )
        with self._open() as conn:
            conn.execute(f"INSERT INTO leads ({columns}) VALUES ({placeholders})", values)

    def list(self) -> list[Lead]:
        with self._open() as conn:
            rows = conn.execute(f"{_SELECT_LEAD} ORDER BY created_at DESC").fetchall()
        return [self._to_lead(row) for row in rows]

    def get(self, lead_id: str) -> Lead | None:
        with self._open() as conn:
            row = conn.execute(f"{_SELECT_LEAD} WHERE id = ?", (lead_id,)).fetchone()
        return None if row is None else self._to_lead(row)

    def update_stage(self, lead_id: str, stage: str) -> None:
        """只改当前阶段这一个字段；是否允许改由 `domain/rules.py` 判定，这里不做判断。"""
        with self._open() as conn:
            conn.execute("UPDATE leads SET stage = ? WHERE id = ?", (stage, lead_id))

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
            template=row["template"] or DEFAULT_TEMPLATE,
            fields=json.loads(row["fields_json"] or "{}"),
        )


class SqliteStageEventRepository(_SqliteRepository):
    """`domain/ports.py` 的 `StageEventRepository` 的 SQLite 实现。"""

    def add(self, event: StageEvent) -> None:
        with self._open() as conn:
            conn.execute(
                "INSERT INTO stage_history (id, lead_id, from_stage, to_stage, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (event.id, event.lead_id, event.from_stage, event.to_stage, event.at),
            )

    def list_for(self, lead_id: str) -> list[StageEvent]:
        """按 `at` 正序；同一秒内按写入顺序（rowid）兜底，保证回放顺序稳定。"""
        with self._open() as conn:
            rows = conn.execute(
                f"{_SELECT_EVENT} WHERE lead_id = ? ORDER BY at ASC, rowid ASC",
                (lead_id,),
            ).fetchall()
        return [
            StageEvent(
                id=row["id"],
                lead_id=row["lead_id"],
                from_stage=row["from_stage"],
                to_stage=row["to_stage"],
                at=row["at"],
            )
            for row in rows
        ]


class SqliteFollowupRepository(_SqliteRepository):
    """`domain/ports.py` 的 `FollowupRepository` 的 SQLite 实现。"""

    def add(self, item: Followup) -> None:
        with self._open() as conn:
            conn.execute(
                "INSERT INTO followups "
                "(id, lead_id, conclusion, objection, next_followup_at, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.lead_id,
                    item.conclusion,
                    item.objection,
                    item.next_followup_at,
                    item.at,
                ),
            )

    def list_for(self, lead_id: str) -> list[Followup]:
        """按 `at` 正序；同一秒内按写入顺序（rowid）兜底。"""
        with self._open() as conn:
            rows = conn.execute(
                f"{_SELECT_FOLLOWUP} WHERE lead_id = ? ORDER BY at ASC, rowid ASC",
                (lead_id,),
            ).fetchall()
        return [
            Followup(
                id=row["id"],
                lead_id=row["lead_id"],
                conclusion=row["conclusion"],
                objection=row["objection"],
                next_followup_at=row["next_followup_at"],
                at=row["at"],
            )
            for row in rows
        ]


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
