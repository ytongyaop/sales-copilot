"""向量库适配器：ChromaDB 持久化实现（余弦距离，`score = 1 - distance`）。

纪律：
- `chromadb` 只在本文件与同层适配器里出现；`services` 只拿 `query` 回来的普通 dict；
- 建库时关掉匿名统计（`anonymized_telemetry=False`），不往外部发任何东西；
- 本层不做校验：块合不合法、来源对不对，都由用例层决定后再进来。
"""

from pathlib import Path

import chromadb
from chromadb.config import Settings

from salescopilot.domain.knowledge import Chunk

# collection 建的时候就要定好距离度量，之后再改只能重建：余弦对文本向量最直观。
_DISTANCE_METADATA = {"hnsw:space": "cosine"}


class ChromaStore:
    """一个持久化 collection：`reset` 清空、`add` 批量写入、`query` 取 top-k。"""

    def __init__(self, path: Path, collection: str = "knowledge") -> None:
        self._path = Path(path)
        self._path.mkdir(parents=True, exist_ok=True)
        self._name = collection
        self._client = chromadb.PersistentClient(
            path=str(self._path), settings=Settings(anonymized_telemetry=False)
        )
        self._collection = self._client.get_or_create_collection(
            name=self._name, metadata=_DISTANCE_METADATA
        )

    def reset(self) -> None:
        """删掉重建：索引必须整份重建（增量删除容易出现「清单说 N 条、库里还有旧块」）。"""
        self._client.delete_collection(self._name)
        self._collection = self._client.get_or_create_collection(
            name=self._name, metadata=_DISTANCE_METADATA
        )

    def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        """批量写入；条数对不上就当场报错，不猜、不补。"""
        if len(chunks) != len(vectors):
            raise ValueError(f"块与向量条数不一致：{len(chunks)} vs {len(vectors)}")
        if not chunks:
            return
        self._collection.add(
            ids=[chunk.id for chunk in chunks],
            embeddings=[list(vector) for vector in vectors],
            documents=[chunk.text for chunk in chunks],
            metadatas=[
                {"source": chunk.source, "start": chunk.start, "end": chunk.end} for chunk in chunks
            ],
        )

    def query(self, vector: list[float], k: int = 4) -> list[dict]:
        """取最相近的 k 条；空库返回 `[]`（Chroma 对空集合查询会报错，这里先挡掉）。"""
        if self.count() == 0:
            return []

        result = self._collection.query(
            query_embeddings=[list(vector)],
            n_results=max(1, int(k)),
            include=["documents", "metadatas", "distances"],
        )
        documents = _first(result.get("documents"))
        metadatas = _first(result.get("metadatas"))
        distances = _first(result.get("distances"))

        hits: list[dict] = []
        for text, metadata, distance in zip(documents, metadatas, distances, strict=False):
            if not isinstance(text, str) or not text.strip():
                continue
            source = metadata.get("source") if isinstance(metadata, dict) else ""
            hits.append(
                {
                    "text": text,
                    "source": source if isinstance(source, str) else "",
                    # 余弦距离 = 1 - 余弦相似度，所以分数按 1 - distance 还原。
                    "score": 1.0 - float(distance),
                }
            )
        return hits

    def count(self) -> int:
        return self._collection.count()


def _first(value: object) -> list:
    """Chroma 的结果是「按查询分组」的列表，单条查询取第 0 组；形状不对就当空。"""
    if isinstance(value, list) and value and isinstance(value[0], list):
        return list(value[0])
    return []
