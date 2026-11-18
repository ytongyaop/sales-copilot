"""v2 T001 数据层迁移：旧库平滑升级 / 幂等 / 新表可写读 / 只追加（临时库，全程离线）。

四条反证：
1. v1 形态的库（`facts` 没有 `card_section`）用新仓储打开后，列被补上、旧数据一条不少；
2. 初始化跑两次不报错，也不会重复加列；
3. 三张新表各插一条能原样查回（含 JSON 原样、空归属、空处置列）；
4. 三个新端口与实现都没有 update / delete，审计流水不可改写。
"""

import sqlite3
from dataclasses import replace
from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteFactRepository,
    SqliteGradingRepository,
    SqliteLeadRepository,
    SqlitePreferenceSampleRepository,
    SqliteSortingBatchRepository,
)
from salescopilot.domain import ports
from salescopilot.domain.models import (
    CARD_SECTIONS,
    Fact,
    GradingRecord,
    PreferenceSample,
    SortingBatch,
)

# v1 的库形态：只有 leads / facts 两张表，`facts` 没有 `card_section`。
V1_SCHEMA = """
CREATE TABLE leads (
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
CREATE TABLE facts (
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
"""

NEW_TABLES = ("sorting_batches", "grading_history", "preference_samples")


def _make_v1_db(path: Path) -> None:
    """手工造一个 v1 库：1 条线索 + 1 条事实，字段值与 v1 完全一致。"""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(V1_SCHEMA)
        conn.execute(
            "INSERT INTO leads (id, name, contact, source, stage, owner, created_at,"
            " template, fields_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "lead-1",
                "老客户",
                "13800000000",
                "referral",
                "intake",
                None,
                "2026-11-01T09:00:00+00:00",
                "edu_training",
                '{"need": "旧需求"}',
            ),
        )
        conn.execute(
            "INSERT INTO facts (id, lead_id, raw_id, field, value, source, source_span,"
            " confidence, locked, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "fact-1",
                "lead-1",
                "raw-1",
                "need",
                "旧需求",
                "llm",
                "想提升成绩",
                0.8,
                0,
                "2026-11-01T09:05:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _columns(path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_old_v1_db_upgrades_without_losing_data(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite3"
    _make_v1_db(db)

    facts = SqliteFactRepository(db)  # 构造即迁移：补 card_section、建新表
    leads = SqliteLeadRepository(db)

    assert "card_section" in _columns(db, "facts")

    fact = facts.get_by_field("lead-1", "need")
    assert fact is not None
    assert fact.id == "fact-1"
    assert fact.value == "旧需求"
    assert fact.source_span == "想提升成绩"
    assert fact.locked is False
    # 旧事实没有归块信息：迁移后是空字符串，不是 NULL、也不是被猜出来的块
    assert fact.card_section == ""

    lead = leads.get("lead-1")
    assert lead is not None
    assert lead.name == "老客户"
    assert lead.fields == {"need": "旧需求"}
    assert _count(db, "leads") == 1
    assert _count(db, "facts") == 1


def test_migration_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "twice.sqlite3"
    _make_v1_db(db)

    SqliteFactRepository(db)
    SqliteFactRepository(db)  # 第二次初始化：不报错，也不重复加列
    SqliteLeadRepository(db)

    assert _columns(db, "facts").count("card_section") == 1
    assert _count(db, "facts") == 1
    assert set(NEW_TABLES) <= _table_names(db)


def test_card_sections_contract() -> None:
    """四要素取值是 T003/T005 的共同依赖，顺序也固定（展示顺序）。"""
    assert CARD_SECTIONS == ("basic", "needs", "decision", "followup")


def test_fact_card_section_roundtrip(tmp_path: Path) -> None:
    """`facts` 读写都带 `card_section`：新增能存，整条改也能写回（T003 依赖）。"""
    db = tmp_path / "fact_section.sqlite3"
    facts = SqliteFactRepository(db)

    fact = Fact(
        id="fact-1",
        lead_id="lead-1",
        raw_id="raw-1",
        field="need",
        value="想扩班",
        source="llm",
        source_span="想扩班",
        confidence=0.8,
        locked=False,
        at="2026-11-18T10:00:00+00:00",
        card_section="needs",
    )
    facts.add(fact)
    assert facts.get_by_field("lead-1", "need") == fact

    updated = replace(
        fact,
        value="想扩两个班",
        source="manual",
        locked=True,
        card_section="decision",
        at="2026-11-18T10:30:00+00:00",
    )
    facts.update(updated)
    assert facts.get_by_field("lead-1", "need") == updated


def test_new_tables_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "new.sqlite3"
    batches = SqliteSortingBatchRepository(db)
    grading = SqliteGradingRepository(db)
    samples = SqlitePreferenceSampleRepository(db)

    suggestion = '{"lead": {"band": "review", "candidates": []}, "priority": "B"}'
    batch = SortingBatch(
        id="batch-1",
        raw_id="raw-1",
        status="pending_claim",
        suggestion_json=suggestion,
        confidence=0.87,
        scorer="fuzzy",
        created_at="2026-11-18T10:00:00+00:00",
    )
    batches.add(batch)
    got_batch = batches.get("batch-1")
    assert got_batch is not None
    assert got_batch == batch
    # JSON 按原样保存：不解析、不重新排序键
    assert got_batch.suggestion_json == suggestion
    # 归属未定 → None，不是空字符串
    assert got_batch.lead_id is None
    assert batches.list_by_status("pending_claim") == [batch]
    assert batches.list_by_status("accepted") == []

    record = GradingRecord(
        id="grade-1",
        lead_id="lead-1",
        mode="propose",
        at="2026-11-18T10:05:00+00:00",
        ai_value="B",
        human_value="A",
        reason="预算比模型看到的高",
        actor="owner-1",
    )
    grading.add(record)
    assert grading.list_for("lead-1") == [record]
    assert grading.list_for("lead-2") == []

    sample = PreferenceSample(
        id="sample-1",
        question="这类客户你会先跟谁聊？",
        answer="先找老板",
        at="2026-11-18T10:10:00+00:00",
        lead_id="lead-1",
    )
    samples.add(sample)
    assert samples.list_all() == [sample]


def test_append_only_repositories_have_no_update_or_delete(tmp_path: Path) -> None:
    """三张流水表只追加：端口协议与实现都不给 update / delete 入口。"""
    protocol_names = (
        "SortingBatchRepository",
        "GradingRepository",
        "PreferenceSampleRepository",
    )
    impl_types = (
        SqliteSortingBatchRepository,
        SqliteGradingRepository,
        SqlitePreferenceSampleRepository,
    )

    for name in protocol_names:
        protocol = getattr(ports, name)
        assert hasattr(protocol, "add"), f"{name} 缺少 add"
        assert not hasattr(protocol, "update"), f"{name} 不该有 update"
        assert not hasattr(protocol, "delete"), f"{name} 不该有 delete"

    db = tmp_path / "append_only.sqlite3"
    for impl in impl_types:
        repo = impl(db)
        assert hasattr(repo, "add")
        assert not hasattr(repo, "update")
        assert not hasattr(repo, "delete")
