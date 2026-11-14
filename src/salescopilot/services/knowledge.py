"""资料索引用例：建库（切块 → 向量化 → 写入 → 写清单）与检索。

一条硬规矩：**查询前先验清单**。清单里的 embedding 模型和当前配置不一致，
说明库里的向量与新查询向量不在同一空间，这时返回任何结果都是假的——
宁可抛 `KnowledgeError` 让用户重建索引，也不返回「看起来像命中」的噪音。

分层说明：清单是本地小 JSON，本层直接读写；向量库与模型 SDK 一律留在
`adapters/`（import-linter 的 `services must not import adapters` 这里必须为 KEPT）。
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from salescopilot.domain.errors import KnowledgeError
from salescopilot.domain.knowledge import (
    MANIFEST_KEYS,
    Chunk,
    chunk_markdown,
    is_citation_valid,
)
from salescopilot.domain.ports import Embedder, VectorStore

DEFAULT_MAX_CHARS = 500
DEFAULT_OVERLAP = 0
MISMATCH_MESSAGE = "索引模型与当前 embedding 配置不一致，请重建索引"


def utc_now_iso() -> str:
    """默认时钟：UTC 的 ISO8601 字符串（秒精度）。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


class KnowledgeService:
    """资料库的用例边界：`build` 建索引、`search` 取片段；问题用中文错误说清。"""

    def __init__(
        self,
        embedder: Embedder,
        store: VectorStore,
        manifest_path: Path,
        expected_model: str,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
        overlap: int = DEFAULT_OVERLAP,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._manifest_path = Path(manifest_path)
        self._expected_model = expected_model
        self._max_chars = max_chars
        self._overlap = overlap
        self._clock = clock if clock is not None else utc_now_iso

    # ------------------------------------------------------------------ 建库

    def build(self, docs: list[tuple[str, str]]) -> tuple[int, list[str]]:
        """整份重建索引；返回（块数, 警告）。

        每份资料按 `(source, text)` 传入。块为空、或切片数与原文对不上（说明切块有 bug）
        都记一条警告返回，不静默吞掉；库仍然整份重建，清单一定重写。
        """
        chunks: list[Chunk] = []
        warnings: list[str] = []

        for source, text in docs:
            name = str(source)
            pieces = chunk_markdown(text, max_chars=self._max_chars, overlap=self._overlap)
            if not pieces:
                warnings.append(f"资料没有可用片段，已跳过：{name}")
                continue
            grounded = []
            for piece in pieces:
                if is_citation_valid(piece, text):
                    grounded.append(replace(piece, id=f"{name}#{piece.id}", source=name))
            if len(grounded) != len(pieces):
                warnings.append(
                    f"资料切片与原文对不上，已丢弃 {len(pieces) - len(grounded)} 块：{name}"
                )
            chunks.extend(grounded)

        self._store.reset()
        if chunks:
            vectors = self._embedder.embed([chunk.text for chunk in chunks])
            if len(vectors) != len(chunks):
                warnings.append("向量条数与块数不一致，本次只写入对得上的部分")
            self._store.add(chunks, vectors)

        self._write_manifest(len(chunks))
        return len(chunks), warnings

    # ------------------------------------------------------------------ 检索

    def search(self, query: str, k: int = 4) -> list[dict]:
        """按语义取最相近的片段；空库/空查询/无命中的情况一律返回 `[]`。"""
        text = query.strip() if isinstance(query, str) else ""
        if not text:
            return []
        if self._store.count() == 0:
            return []

        self._ensure_same_model()

        vector = self._embedder.embed([text])[0]
        hits = self._store.query(vector, k=k)
        return hits or []

    def count(self) -> int:
        """库里现有块数（给脚本/自检用，不参与业务判断）。"""
        return self._store.count()

    # ------------------------------------------------------------------ 清单

    def _ensure_same_model(self) -> None:
        """清单的模型与当前配置不一致 → 抛中文错误；清单坏掉同样按不一致处理。"""
        manifest = self._read_manifest()
        model = manifest.get("embed_model")
        if not isinstance(model, str) or model != self._expected_model:
            raise KnowledgeError(MISMATCH_MESSAGE)

    def _read_manifest(self) -> dict:
        if not self._manifest_path.is_file():
            return {}
        try:
            data = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {key: data.get(key) for key in MANIFEST_KEYS}

    def _write_manifest(self, chunk_count: int) -> None:
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "embed_model": self._expected_model,
            "chunk_count": int(chunk_count),
            "built_at": self._clock(),
        }
        self._manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def iter_docs(root: Path) -> Iterable[tuple[str, str]]:
    """递归读 `root` 下的 `.md` / `.txt`，yield `(相对路径, 文本)`；读不出来的跳过并报一行。

    路径按 `/` 归一，保证 Windows 上建出来的引用显示也一致。
    """
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in (".md", ".txt") or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"[knowledge] 跳过读不出来的资料：{path}（{exc}）")
            continue
        yield path.relative_to(root).as_posix(), text
