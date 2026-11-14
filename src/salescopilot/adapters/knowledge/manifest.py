"""索引清单：记录「这份库是用哪个 embedding 模型建的、有多少块、什么时候建的」。

为什么需要它：换了 embedding 模型，库里的旧向量和新查询向量不在同一个空间，
相似度全是噪音。清单就是那道闸——模型对不上，宁可报「请重建索引」，也不返回假结果。
"""

import json
from pathlib import Path

from salescopilot.domain.knowledge import MANIFEST_KEYS

MANIFEST_NAME = "manifest.json"


def manifest_path_of(index_dir: Path) -> Path:
    """约定：清单固定叫 `manifest.json`，和库文件放同一目录。"""
    return Path(index_dir) / MANIFEST_NAME


def write_manifest(path: Path, *, embed_model: str, chunk_count: int, built_at: str) -> None:
    """写清单；父目录不存在就建。键固定三个，多写的字段读取时一律忽略。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "embed_model": str(embed_model),
        "chunk_count": int(chunk_count),
        "built_at": str(built_at),
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_manifest(path: Path) -> dict:
    """读清单；文件不存在或不是合法 JSON 一律返回 `{}`（当作「还没建过索引」）。

    这里不抛异常：清单坏掉和使用户「查不到资料」是同一种结果，由用例层统一处理。
    """
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: data.get(key) for key in MANIFEST_KEYS}
