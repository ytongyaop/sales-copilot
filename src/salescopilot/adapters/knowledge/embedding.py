"""向量化适配器：DashScope（真模型）与确定性假实现。

边界：
- 只有这里能 import `openai` SDK；`domain/` 与 `services/` 一律不认识它；
- 密钥由装配点传入，本文件不读环境变量、不读 `.env`、不打印任何 key；
- 假实现必须**确定性**：同一段文本永远同一向量，否则离线测试什么也证明不了。
"""

import hashlib

from openai import OpenAI

from salescopilot.domain.errors import KnowledgeError

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_EMBED_MODEL = "text-embedding-v3"
FAKE_DIM = 32
FAKE_MODEL = "fake"

# 单次请求最多送这么多条文本（DashScope 兼容接口的批量上限按 10 条保守取）。
_BATCH_SIZE = 10


class DashScopeEmbedder:
    """DashScope 文本向量：走 OpenAI 兼容协议，`embeddings.create` 一次可送一批。"""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_EMBED_MODEL,
        base_url: str = DASHSCOPE_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self._model = model

    @property
    def model(self) -> str:
        """写进索引清单的模型名：查询与建索引必须用同一个，否则向量不可比。"""
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化；任何失败都翻译成中文 `KnowledgeError`，不把响应灌进日志。"""
        items = [text if isinstance(text, str) else "" for text in texts]
        if not items:
            return []

        vectors: list[list[float]] = []
        for start in range(0, len(items), _BATCH_SIZE):
            batch = items[start : start + _BATCH_SIZE]
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        try:
            response = self._client.embeddings.create(model=self._model, input=batch)
        except Exception as exc:  # 网络/鉴权/超时走同一条中文错误
            raise KnowledgeError(
                f"向量化失败：{type(exc).__name__}（请检查 DASHSCOPE_API_KEY）"
            ) from exc

        data = getattr(response, "data", None)
        if not isinstance(data, list) or len(data) != len(batch):
            raise KnowledgeError("向量化失败：返回条数与输入不一致")

        vectors: list[list[float]] = []
        for item in data:
            embedding = getattr(item, "embedding", None)
            if not isinstance(embedding, list) or not embedding:
                raise KnowledgeError("向量化失败：返回里缺少向量")
            vectors.append([float(value) for value in embedding])
        return vectors


class FakeEmbedder:
    """确定性假向量：sha256(text) 派生 32 维单位向量，离线测试与自检用。

    不是「随便给个数」——同一段文本必得同一向量，近词不保证近邻，但胜在可复现，
    足以验证「建索引→查询→引用」整条管道是否接通。
    """

    model = FAKE_MODEL

    def __init__(self, dim: int = FAKE_DIM) -> None:
        self._dim = max(2, int(dim))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_vector_of(text if isinstance(text, str) else "", self._dim) for text in texts]


def _vector_of(text: str, dim: int) -> list[float]:
    """把文本哈希摊成 `dim` 维、长度归一的向量（同文本同结果，跨进程也一致）。"""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    for index in range(dim):
        byte = digest[index % len(digest)]
        values.append((byte / 255.0) - 0.5)
    norm = sum(value * value for value in values) ** 0.5
    if norm == 0:
        return [0.0] * dim
    return [value / norm for value in values]
