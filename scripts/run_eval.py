"""提炼基线评测：离线自检（--fake）与真模型基线（--live）。

用法（在仓库根执行，无需先设 PYTHONPATH）：

    python scripts/run_eval.py --suite extract --fake    # 离线自检：只用假模型，不联网
    python scripts/run_eval.py --suite extract --live    # 真模型基线：需要项目根 .env 里的 DEEPSEEK_API_KEY

三条边界：
1. **默认不联网**：不带 `--live` 就一定用假模型；真调用只发生在显式 `--live`；
2. **口径只有一份**：指标实现在 `domain/eval_metrics.py`（纯函数、可单测），本脚本只负责
   读样本、跑提炼器、汇总、写报告；
3. **样本是合成的**：`data/evalsets/extract_cases.jsonl` 为**合成样本，非真实机构数据**，
   仅用于建立基线与观察漂移，不代表真实客户分布；每条期望值都只写能从原文常识判断的字段。

成本：`estimated_cost` 用 token 乘固定估算单价，只是量级参考，**不是账单口径**。
"""

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    # 脚本可能脱离 pytest 直接跑，先把 src 放进 import 路径（pytest 由 pyproject 配好了）。
    sys.path.insert(0, str(SRC_DIR))

from salescopilot.adapters.llm.fake import FakeExtractor
from salescopilot.config import ConfigError
from salescopilot.domain.eval_metrics import (
    match_count,
    pairs_of,
    precision_recall_counts,
    span_valid,
)
from salescopilot.domain.facts import validate_candidates
from salescopilot.domain.models import RawItem
from salescopilot.entrypoints.bootstrap import build_live_extractor

EVALSET_PATH = REPO_ROOT / "data" / "evalsets" / "extract_cases.jsonl"
REPORTS_DIR = REPO_ROOT / "data" / "reports"

# 估算单价（人民币 / token）：按「约 3 元 / 百万 token」的输入输出混合量级粗估。
# 只用于报告里换算量级，**不是账单口径**；真实费用以 DeepSeek 后台账单为准。
ESTIMATED_PRICE_PER_TOKEN = 3.0e-6

_EVAL_LEAD_ID = "eval-lead"
_EVAL_AT = "1970-01-01T00:00:00+00:00"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cases = load_cases(EVALSET_PATH)

    extractor = _build_extractor(args.live)
    if extractor is None:
        return 2

    report = evaluate(cases, extractor, mode="live" if args.live else "fake")
    path = write_report(report, Path(args.out_dir) if args.out_dir else REPORTS_DIR)
    print(_summary_line(report, path))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="提炼基线评测")
    parser.add_argument(
        "--suite", default="extract", choices=["extract"], help="评测套件（目前只有 extract）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fake", action="store_true", help="用假模型，离线自检（默认）")
    mode.add_argument(
        "--live", action="store_true", help="用真模型，需项目根 .env 配 DEEPSEEK_API_KEY"
    )
    parser.add_argument("--out-dir", default=None, help="报告输出目录（默认 data/reports）")
    return parser.parse_args(argv)


def _build_extractor(live: bool):
    """`--live` 才装配真模型；缺 key 时打印中文原因并返回 None（退出码 2）。"""
    if not live:
        return FakeExtractor()
    try:
        return build_live_extractor()
    except ConfigError as exc:
        print(f"[extract/live] 无法跑真模型：{exc}", file=sys.stderr)
        return None


def load_cases(path: Path) -> list[dict]:
    """逐行读 jsonl；结构不对直接报行号退出（不猜、不跳过坏样本）。"""
    if not path.is_file():
        raise SystemExit(f"样本文件不存在：{path}")

    cases: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"样本第 {lineno} 行不是合法 JSON：{exc.msg}") from exc
        _check_case(case, lineno)
        cases.append(case)
    return cases


def _check_case(case: object, lineno: int) -> None:
    if not isinstance(case, dict):
        raise SystemExit(f"样本第 {lineno} 行不是 JSON 对象")
    if not isinstance(case.get("id"), str) or not case["id"].strip():
        raise SystemExit(f"样本第 {lineno} 行缺少 id")
    if not isinstance(case.get("content"), str) or not case["content"].strip():
        raise SystemExit(f"样本第 {lineno} 行缺少 content")
    expected = case.get("expected", [])
    if not isinstance(expected, list) or any(not isinstance(item, dict) for item in expected):
        raise SystemExit(f"样本第 {lineno} 行 expected 必须是对象列表")


def evaluate(cases: list[dict], extractor, *, mode: str) -> dict:
    """逐条跑提炼并汇总；precision / recall 按条数**微平均**（总命中 / 总条数）。"""
    started = time.perf_counter()
    per_case: list[dict] = []
    matched_total = 0
    predicted_total = 0
    expected_total = 0
    token_total = 0
    seconds_total = 0.0
    span_ok_total = 0
    span_all_total = 0
    model = ""
    prompt_version = ""

    for case in cases:
        content = case["content"]
        expected = case.get("expected", [])
        result = extractor.extract(raw=_raw_of(case), facts=[])
        accepted, problems = validate_candidates(result.candidates)

        matched = match_count(expected, accepted)
        precision, recall = precision_recall_counts(
            matched=matched,
            predicted=len(pairs_of(accepted)),
            expected=len(pairs_of(expected)),
        )
        span_ok = sum(1 for item in accepted if span_valid(content, item.get("source_span")))
        span_rate = span_ok / len(accepted) if accepted else 0.0

        matched_total += matched
        predicted_total += len(pairs_of(accepted))
        expected_total += len(pairs_of(expected))
        token_total += result.token
        seconds_total += result.seconds
        span_ok_total += span_ok
        span_all_total += len(accepted)
        model = result.model
        prompt_version = result.prompt_version

        per_case.append(
            {
                "id": case["id"],
                "expected": len(pairs_of(expected)),
                "accepted": len(accepted),
                "matched": matched,
                "field_precision": round(precision, 4),
                "field_recall": round(recall, 4),
                "span_valid_rate": round(span_rate, 4),
                "candidates": len(result.candidates),
                "skipped": [f"{issue.location}: {issue.message}" for issue in problems],
                "token": result.token,
                "seconds": round(result.seconds, 4),
            }
        )

    precision_total, recall_total = precision_recall_counts(
        matched=matched_total, predicted=predicted_total, expected=expected_total
    )
    report = {
        "suite": "extract",
        "mode": mode,
        "model": model,
        "prompt_version": prompt_version,
        "cases": len(cases),
        "field_precision": round(precision_total, 4),
        "field_recall": round(recall_total, 4),
        "span_valid_rate": round(span_ok_total / span_all_total if span_all_total else 0.0, 4),
        "avg_seconds": round(seconds_total / len(cases), 4) if cases else 0.0,
        "total_token": token_total,
        "estimated_cost": round(token_total * ESTIMATED_PRICE_PER_TOKEN, 6),
        "estimated_price_per_token": ESTIMATED_PRICE_PER_TOKEN,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "duration_seconds": round(time.perf_counter() - started, 4),
        "evalset": str(EVALSET_PATH.relative_to(REPO_ROOT)),
        "per_case": per_case,
    }
    return report


def _raw_of(case: dict) -> RawItem:
    """把一条样本包成原始素材：id 与内容哈希都由样本自身推导，同一份样本必同 id。"""
    content = case["content"]
    return RawItem(
        id=f"eval-{case['id']}",
        lead_id=_EVAL_LEAD_ID,
        source="eval",
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        at=_EVAL_AT,
    )


def write_report(report: dict, out_dir: Path) -> Path:
    """写 `extract-<YYYYMMDD-HHMMSS>.json`；目录不存在就建。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"extract-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _summary_line(report: dict, path: Path) -> str:
    return (
        f"[extract/{report['mode']}] cases={report['cases']} "
        f"precision={report['field_precision']:.3f} recall={report['field_recall']:.3f} "
        f"span_valid={report['span_valid_rate']:.3f} avg_s={report['avg_seconds']:.4f} "
        f"total_token={report['total_token']} cost~Y{report['estimated_cost']:.6f} -> {path}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
