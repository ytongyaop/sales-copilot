"""行业词禁令的机器实现。

这条测试是 ADR-0004（引擎不含行业词）的机器实现：扫 `src/salescopilot/domain/`
与 `src/salescopilot/services/` 下所有 `*.py`，逐行比对 `tests/fixtures/industry_words.txt`
里的词表，命中即失败。词表同时供人工 grep 使用，保证两处判据是同一份清单。
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORDS_FILE = Path(__file__).parent / "fixtures" / "industry_words.txt"
SCAN_ROOTS = (
    REPO_ROOT / "src" / "salescopilot" / "domain",
    REPO_ROOT / "src" / "salescopilot" / "services",
)


def _load_words() -> list[str]:
    """读词表；空表视为配置错误，直接失败而不是静默通过。"""
    if not WORDS_FILE.is_file():
        pytest.fail(f"词表不存在：{WORDS_FILE}（ADR-0004 判据依赖这份清单）")

    words = [line.strip() for line in WORDS_FILE.read_text(encoding="utf-8").splitlines()]
    words = [w for w in words if w and not w.startswith("#")]
    if not words:
        pytest.fail(f"词表为空：{WORDS_FILE}")
    return words


def _iter_source_files() -> list[Path]:
    """收集要扫描的 .py；扫描根缺失时失败，避免目录改名后测试变成永远绿。"""
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if not root.is_dir():
            pytest.fail(f"扫描根目录不存在：{root}")
        files.extend(sorted(root.rglob("*.py")))
    return files


def test_domain_and_services_have_no_industry_words() -> None:
    words = _load_words()
    files = _iter_source_files()

    hits: list[str] = []
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, text in enumerate(lines, start=1):
            for word in words:
                if word in text:
                    rel = path.relative_to(REPO_ROOT)
                    hits.append(f"{rel}:{lineno}: 命中「{word}」 -> {text.strip()}")

    if hits:
        pytest.fail(
            "domain/ 与 services/ 出现行业词（ADR-0004）。"
            "这些词只能出现在 templates/*.yaml 或 web/：\n" + "\n".join(hits)
        )
