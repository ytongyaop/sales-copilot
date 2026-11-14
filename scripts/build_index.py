"""建资料索引：切块 → 向量化 → 写入 Chroma → 写清单。

用法（在仓库根执行，无需先设 PYTHONPATH）：

    python scripts/build_index.py --fake    # 离线自检：假向量，清单写 model=fake
    python scripts/build_index.py           # 真向量：需项目根 .env 里的 DASHSCOPE_API_KEY

边界：
1. **默认不联网**只有带 `--fake` 才离线；不带参数时真的调向量服务（缺 key 直接退出码 2）；
2. 资料目录固定 `data/knowledge/**/*.md|*.txt`，索引目录默认 `data/index/`，
   可用环境变量 `SALESCOPILOT_INDEX_DIR` 指到别处（测试就是这么做的）；
3. 建索引会**整份重建**：先清空库再写，避免旧块留在里面，和清单里的条数对不上。
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from salescopilot.adapters.knowledge.chroma_store import ChromaStore
from salescopilot.adapters.knowledge.embedding import (  # sys.path 插好之后才 import
    FAKE_MODEL,
    DashScopeEmbedder,
    FakeEmbedder,
)
from salescopilot.adapters.knowledge.manifest import manifest_path_of
from salescopilot.config import load_settings
from salescopilot.entrypoints.bootstrap import index_dir
from salescopilot.services.knowledge import KnowledgeService, iter_docs

DEFAULT_DOCS_DIR = REPO_ROOT / "data" / "knowledge"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    docs_dir = Path(args.docs_dir) if args.docs_dir else DEFAULT_DOCS_DIR
    if not docs_dir.is_dir():
        print(f"[index] 资料目录不存在：{docs_dir}", file=sys.stderr)
        return 2

    embedder = _build_embedder(args.fake)
    if embedder is None:
        return 2

    model = _model_name(embedder)
    target = Path(args.index_dir) if args.index_dir else index_dir()
    service = KnowledgeService(
        embedder,
        ChromaStore(target),
        manifest_path_of(target),
        model,
    )

    docs = list(iter_docs(docs_dir))
    if not docs:
        print(f"[index] 资料目录里没有 .md/.txt：{docs_dir}", file=sys.stderr)
        return 2

    count, warnings = service.build(docs)
    for warning in warnings:
        print(f"[index] 警告：{warning}", file=sys.stderr)
    print(f"INDEX_OK chunks={count} model={model}")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="建资料索引")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fake", action="store_true", help="用确定性假向量，离线自检（不联网）")
    parser.add_argument("--docs-dir", default=None, help="资料目录（默认 data/knowledge）")
    parser.add_argument("--index-dir", default=None, help="索引目录（默认 data/index）")
    return parser.parse_args(argv)


def _build_embedder(fake: bool):
    """`--fake` 直接给假实现；否则要求 `DASHSCOPE_API_KEY`，缺了打印中文提示并返回 None。"""
    if fake:
        return FakeEmbedder()

    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - 读配置失败一律按「没配好」处理
        print(f"[index] 读取配置失败：{type(exc).__name__}", file=sys.stderr)
        return None

    if not settings.dashscope_api_key:
        print(
            "[index] 缺少 DASHSCOPE_API_KEY：请在项目根 .env 中配置，或先跑 --fake 做离线自检",
            file=sys.stderr,
        )
        return None

    return DashScopeEmbedder(
        api_key=settings.dashscope_api_key,
        model=settings.embed_model,
    )


def _model_name(embedder) -> str:
    model = getattr(embedder, "model", None)
    return model if isinstance(model, str) and model.strip() else FAKE_MODEL


if __name__ == "__main__":
    raise SystemExit(main())
