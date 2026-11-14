"""评测脚本的端到端离线测试：`--fake` 跑 20 条合成样本，不联网、不调模型。

两份断言：
1. 样本文件本身的数量与覆盖（缺一类就说明基线口径被悄悄改了）；
2. 脚本能跑通、报告 JSON 字段齐全、样本数对得上。

样本是**合成样本，非真实机构数据**，只用于建立基线与看漂移。
"""

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_eval.py"
EVALSET = REPO_ROOT / "data" / "evalsets" / "extract_cases.jsonl"

REQUIRED_KEYS = {
    "suite",
    "mode",
    "model",
    "prompt_version",
    "cases",
    "field_precision",
    "field_recall",
    "span_valid_rate",
    "avg_seconds",
    "total_token",
    "estimated_cost",
    "per_case",
}

EXPECTED_OBJECTION_TYPES = ("price", "time", "effect", "trust", "other")


def _cases() -> list[dict]:
    lines = [line for line in EVALSET.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def test_evalset_has_20_cases_with_required_coverage() -> None:
    cases = _cases()

    assert len(cases) == 20
    assert {case["id"] for case in cases} == {f"e{index:02d}" for index in range(1, 21)}

    counts: Counter[tuple[str, str]] = Counter()
    for case in cases:
        for item in case["expected"]:
            counts[(item["field"], item["value"] if item["field"] == "objection" else "*")] += 1

    for objection in EXPECTED_OBJECTION_TYPES:
        assert counts[("objection", objection)] == 3, objection
    assert counts[("decision_maker", "*")] == 3
    assert counts[("amount", "*")] == 2
    assert counts[("appointment_at", "*")] == 3


def test_fake_eval_writes_report_with_metrics(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--suite",
            "extract",
            "--fake",
            "--out-dir",
            str(tmp_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "cases=20" in completed.stdout

    reports = sorted(tmp_path.glob("extract-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))

    assert REQUIRED_KEYS <= set(report)
    assert report["suite"] == "extract"
    assert report["mode"] == "fake"
    assert report["cases"] == len(_cases())
    assert len(report["per_case"]) == report["cases"]
    for key in ("field_precision", "field_recall", "span_valid_rate"):
        assert 0.0 <= report[key] <= 1.0
    assert report["total_token"] == 0  # 假模型不耗 token
    assert report["estimated_cost"] == 0.0
