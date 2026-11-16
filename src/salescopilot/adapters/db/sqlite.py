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

from salescopilot.domain.models import (
    DEFAULT_TEMPLATE,
    AssignmentEvent,
    Draft,
    ExtractRun,
    Fact,
    Followup,
    Lead,
    RawItem,
    StageEvent,
)

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
CREATE TABLE IF NOT EXISTS raw_items (
    id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    source TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_raw_hash ON raw_items(lead_id, content_hash);
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    raw_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    source_span TEXT NOT NULL,
    confidence REAL NOT NULL,
    locked INTEGER NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extract_runs (
    id TEXT PRIMARY KEY,
    raw_id TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    candidates INTEGER NOT NULL,
    accepted INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    token INTEGER NOT NULL,
    seconds REAL NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    text TEXT NOT NULL,
    citations_json TEXT NOT NULL,
    status TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    token INTEGER NOT NULL,
    seconds REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignment_events (
    id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    from_owner TEXT,
    to_owner TEXT,
    reason TEXT NOT NULL,
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
_SELECT_ASSIGNMENT = "SELECT id, lead_id, from_owner, to_owner, reason, at FROM assignment_events"
_SELECT_FOLLOWUP = "SELECT id, lead_id, conclusion, objection, next_followup_at, at FROM followups"
_SELECT_RAW = "SELECT id, lead_id, source, content, content_hash, at FROM raw_items"
_SELECT_FACT = (
    "SELECT id, lead_id, raw_id, field, value, source, source_span, confidence, locked, at"
    " FROM facts"
)
_SELECT_RUN = (
    "SELECT id, raw_id, model, prompt_version, input_hash, output_hash, candidates,"
    " accepted, skipped, token, seconds, at FROM extract_runs"
)
_SELECT_DRAFT = (
    "SELECT id, lead_id, text, citations_json, status, model, prompt_version,"
    " token, seconds, created_at FROM drafts"
)
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

    def update_owner(self, lead_id: str, owner: str | None) -> None:
        """只改归属人；传 None 即回公海。能不能认领/释放由 `domain/assignment.py` 判定。"""
        with self._open() as conn:
            conn.execute("UPDATE leads SET owner = ? WHERE id = ?", (owner, lead_id))

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


class SqliteAssignmentEventRepository(_SqliteRepository):
    """`domain/ports.py` 的 `AssignmentEventRepository` 的 SQLite 实现（只追加）。"""

    def add(self, event: AssignmentEvent) -> None:
        with self._open() as conn:
            conn.execute(
                "INSERT INTO assignment_events (id, lead_id, from_owner, to_owner, reason, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.lead_id,
                    event.from_owner,
                    event.to_owner,
                    event.reason,
                    event.at,
                ),
            )

    def list_for(self, lead_id: str) -> list[AssignmentEvent]:
        """按 `at` 正序；同一秒内按写入顺序（rowid）兜底。"""
        with self._open() as conn:
            rows = conn.execute(
                f"{_SELECT_ASSIGNMENT} WHERE lead_id = ? ORDER BY at ASC, rowid ASC",
                (lead_id,),
            ).fetchall()
        return [
            AssignmentEvent(
                id=row["id"],
                lead_id=row["lead_id"],
                from_owner=row["from_owner"],
                to_owner=row["to_owner"],
                reason=row["reason"],
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


class SqliteRawItemRepository(_SqliteRepository):
    """`domain/ports.py` 的 `RawItemRepository` 的 SQLite 实现。

    `(lead_id, content_hash)` 上有唯一索引：同一线索的同一段内容只能有一条，
    去重判断交给用例层（先查后写），索引只兜底防并发重复。
    """

    def add(self, item: RawItem) -> None:
        with self._open() as conn:
            conn.execute(
                "INSERT INTO raw_items (id, lead_id, source, content, content_hash, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.lead_id,
                    item.source,
                    item.content,
                    item.content_hash,
                    item.at,
                ),
            )

    def get(self, raw_id: str) -> RawItem | None:
        with self._open() as conn:
            row = conn.execute(f"{_SELECT_RAW} WHERE id = ?", (raw_id,)).fetchone()
        return None if row is None else self._to_raw(row)

    def find_by_hash(self, lead_id: str, content_hash: str) -> RawItem | None:
        with self._open() as conn:
            row = conn.execute(
                f"{_SELECT_RAW} WHERE lead_id = ? AND content_hash = ? "
                "ORDER BY at ASC, rowid ASC LIMIT 1",
                (lead_id, content_hash),
            ).fetchone()
        return None if row is None else self._to_raw(row)

    def list_for(self, lead_id: str) -> list[RawItem]:
        with self._open() as conn:
            rows = conn.execute(
                f"{_SELECT_RAW} WHERE lead_id = ? ORDER BY at ASC, rowid ASC",
                (lead_id,),
            ).fetchall()
        return [self._to_raw(row) for row in rows]

    @staticmethod
    def _to_raw(row: sqlite3.Row) -> RawItem:
        return RawItem(
            id=row["id"],
            lead_id=row["lead_id"],
            source=row["source"],
            content=row["content"],
            content_hash=row["content_hash"],
            at=row["at"],
        )


class SqliteFactRepository(_SqliteRepository):
    """`domain/ports.py` 的 `FactRepository` 的 SQLite 实现。

    一个字段只留一条事实：`get_by_field` 取最早写入的那条，`update` 按 id 原地改。
    """

    def add(self, fact: Fact) -> None:
        with self._open() as conn:
            conn.execute(
                "INSERT INTO facts "
                "(id, lead_id, raw_id, field, value, source, source_span, confidence, locked, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fact.id,
                    fact.lead_id,
                    fact.raw_id,
                    fact.field,
                    fact.value,
                    fact.source,
                    fact.source_span,
                    fact.confidence,
                    1 if fact.locked else 0,
                    fact.at,
                ),
            )

    def list_for(self, lead_id: str) -> list[Fact]:
        with self._open() as conn:
            rows = conn.execute(
                f"{_SELECT_FACT} WHERE lead_id = ? ORDER BY at ASC, rowid ASC",
                (lead_id,),
            ).fetchall()
        return [self._to_fact(row) for row in rows]

    def get_by_field(self, lead_id: str, field: str) -> Fact | None:
        with self._open() as conn:
            row = conn.execute(
                f"{_SELECT_FACT} WHERE lead_id = ? AND field = ? "
                "ORDER BY at ASC, rowid ASC LIMIT 1",
                (lead_id, field),
            ).fetchone()
        return None if row is None else self._to_fact(row)

    def update(self, fact: Fact) -> None:
        """整条改（值 / 原文编号 / 来源 / 片段 / 置信度 / 锁定 / 时间）；按 id 定位。

        `raw_id` 必须一起改：一条字段的「当前事实」换了出处，追溯就要指到新的那条原文；
        漏掉它会让 UI 与账本对不上「这个值到底是哪段话提炼出来的」。
        """
        with self._open() as conn:
            conn.execute(
                "UPDATE facts SET raw_id = ?, value = ?, source = ?, source_span = ?, "
                "confidence = ?, locked = ?, at = ? WHERE id = ?",
                (
                    fact.raw_id,
                    fact.value,
                    fact.source,
                    fact.source_span,
                    fact.confidence,
                    1 if fact.locked else 0,
                    fact.at,
                    fact.id,
                ),
            )

    @staticmethod
    def _to_fact(row: sqlite3.Row) -> Fact:
        return Fact(
            id=row["id"],
            lead_id=row["lead_id"],
            raw_id=row["raw_id"],
            field=row["field"],
            value=row["value"],
            source=row["source"],
            source_span=row["source_span"],
            confidence=row["confidence"],
            locked=bool(row["locked"]),
            at=row["at"],
        )


class SqliteExtractRunRepository(_SqliteRepository):
    """`domain/ports.py` 的 `ExtractRunRepository` 的 SQLite 实现（只追加）。"""

    def add(self, run: ExtractRun) -> None:
        with self._open() as conn:
            conn.execute(
                "INSERT INTO extract_runs "
                "(id, raw_id, model, prompt_version, input_hash, output_hash, candidates, "
                "accepted, skipped, token, seconds, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.id,
                    run.raw_id,
                    run.model,
                    run.prompt_version,
                    run.input_hash,
                    run.output_hash,
                    run.candidates,
                    run.accepted,
                    run.skipped,
                    run.token,
                    run.seconds,
                    run.at,
                ),
            )

    def list_for_raw(self, raw_id: str) -> list[ExtractRun]:
        with self._open() as conn:
            rows = conn.execute(
                f"{_SELECT_RUN} WHERE raw_id = ? ORDER BY at ASC, rowid ASC",
                (raw_id,),
            ).fetchall()
        return [
            ExtractRun(
                id=row["id"],
                raw_id=row["raw_id"],
                model=row["model"],
                prompt_version=row["prompt_version"],
                input_hash=row["input_hash"],
                output_hash=row["output_hash"],
                candidates=row["candidates"],
                accepted=row["accepted"],
                skipped=row["skipped"],
                token=row["token"],
                seconds=row["seconds"],
                at=row["at"],
            )
            for row in rows
        ]


class SqliteDraftRepository(_SqliteRepository):
    """`domain/ports.py` 的 `DraftRepository` 的 SQLite 实现。

    引用列表整段存 JSON（形状由 `domain/draft.py` 统一），本层只负责存取与改状态；
    状态流转（pending → confirmed/discarded）由用例层判定，SQL 不做约束。
    """

    def add(self, draft: Draft) -> None:
        with self._open() as conn:
            conn.execute(
                "INSERT INTO drafts "
                "(id, lead_id, text, citations_json, status, model, prompt_version, "
                "token, seconds, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    draft.id,
                    draft.lead_id,
                    draft.text,
                    json.dumps(draft.citations, ensure_ascii=False),
                    draft.status,
                    draft.model,
                    draft.prompt_version,
                    draft.token,
                    draft.seconds,
                    draft.created_at,
                ),
            )

    def get(self, draft_id: str) -> Draft | None:
        with self._open() as conn:
            row = conn.execute(f"{_SELECT_DRAFT} WHERE id = ?", (draft_id,)).fetchone()
        return None if row is None else self._to_draft(row)

    def list_for(self, lead_id: str) -> list[Draft]:
        with self._open() as conn:
            rows = conn.execute(
                f"{_SELECT_DRAFT} WHERE lead_id = ? ORDER BY created_at ASC, rowid ASC",
                (lead_id,),
            ).fetchall()
        return [self._to_draft(row) for row in rows]

    def update_status(self, draft_id: str, status: str) -> None:
        with self._open() as conn:
            conn.execute("UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id))

    @staticmethod
    def _to_draft(row: sqlite3.Row) -> Draft:
        try:
            citations = json.loads(row["citations_json"] or "[]")
        except json.JSONDecodeError:
            citations = []
        if not isinstance(citations, list):
            citations = []
        return Draft(
            id=row["id"],
            lead_id=row["lead_id"],
            text=row["text"],
            citations=citations,
            status=row["status"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            token=row["token"],
            seconds=row["seconds"],
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
