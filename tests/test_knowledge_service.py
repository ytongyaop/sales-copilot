"""资料索引用例的行为测试：建库计数、空库检索、模型不一致拦截（全程离线）。"""

from pathlib import Path

import pytest

from salescopilot.adapters.knowledge.chroma_store import ChromaStore
from salescopilot.adapters.knowledge.embedding import FakeEmbedder
from salescopilot.adapters.knowledge.manifest import manifest_path_of, read_manifest
from salescopilot.domain.errors import KnowledgeError
from salescopilot.services.knowledge import KnowledgeService

DOCS = [
    ("a.md", "# 甲\n\n甲段落的正文内容，讲的是退费规则。"),
    ("b.md", "# 乙\n\n乙段落的正文内容，讲的是上课形式。"),
]


def _service(tmp_path: Path, *, model: str = "fake") -> KnowledgeService:
    return KnowledgeService(
        FakeEmbedder(),
        ChromaStore(tmp_path / "index"),
        manifest_path_of(tmp_path / "index"),
        model,
        clock=lambda: "2026-11-14T10:00:00+00:00",
    )


def test_build_counts_chunks_and_writes_manifest(tmp_path: Path) -> None:
    service = _service(tmp_path)

    count, warnings = service.build(DOCS)

    assert count == 4  # 两份资料各 2 块（标题 1 + 正文 1）
    assert warnings == []
    assert service.count() == 4
    manifest = read_manifest(manifest_path_of(tmp_path / "index"))
    assert manifest == {
        "embed_model": "fake",
        "chunk_count": 4,
        "built_at": "2026-11-14T10:00:00+00:00",
    }


def test_build_is_rebuild_not_append(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.build(DOCS)
    count, _ = service.build(DOCS)

    assert count == 4
    assert service.count() == 4, "重复建索引必须是整份重建，不能累加"


def test_search_empty_store_returns_empty(tmp_path: Path) -> None:
    service = _service(tmp_path)

    assert service.search("退费规则") == []


def test_search_returns_hits_with_source_and_score(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.build(DOCS)

    hits = service.search("退费规则", k=2)

    assert len(hits) == 2
    assert all(set(hit) == {"text", "source", "score"} for hit in hits)
    assert all(hit["source"] in {"a.md", "b.md"} for hit in hits)
    originals = "".join(text for _, text in DOCS)
    assert all(hit["text"] in originals for hit in hits)
    assert all(hit["score"] <= 1.0 for hit in hits)


def test_search_rejects_mismatched_manifest_model(tmp_path: Path) -> None:
    other = _service(tmp_path, model="another-model")
    other.build(DOCS)

    mismatched = _service(tmp_path, model="fake")

    with pytest.raises(KnowledgeError) as excinfo:
        mismatched.search("退费规则")
    assert "索引模型与当前 embedding 配置不一致，请重建索引" in str(excinfo.value)


def test_search_blank_query_returns_empty(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.build(DOCS)

    assert service.search("   ") == []


def test_build_reports_warning_for_empty_document(tmp_path: Path) -> None:
    service = _service(tmp_path)

    count, warnings = service.build([("empty.md", "   \n\n  ")])

    assert count == 0
    assert warnings == ["资料没有可用片段，已跳过：empty.md"]
