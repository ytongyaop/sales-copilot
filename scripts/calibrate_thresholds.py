"""阈值校准：在标注客户对子上扫 L1 阈值，给出 `auto` / `review` 建议值。

用法（在仓库根执行，无需先设 PYTHONPATH）：

    python scripts/calibrate_thresholds.py                      # 只打印
    python scripts/calibrate_thresholds.py --json out.json      # 另存 JSON 报告
    python scripts/calibrate_thresholds.py --write-template building_materials

三条边界：

1. **合成样本，仅用于阈值校准，不代表真实业务分布**：`data/evalsets/entity_pairs.jsonl`
   里的名称全部自拟、不含真实客户数据，且刻意压了难例（简称、同区不同公司、别字、
   仅组织形式不同），真实流量里这类比例不会这么高，所以下面的阈值偏保守。
2. **只算 L1**：`fuzzy_score(normalize_org(a), normalize_org(b))` 加
   `suffix_only_difference`；L2 向量不在本次范围（不做嵌入，不联网），
   因此「同名只差空格/全半角」这类本来会先被精确键命中的样本，在这里一律按
   「仅组织形式不同」判进复核，`auto` 召回因此被低估。
3. **默认只打印**：只有显式传 `--write-template <模板名>` 才改
   `src/salescopilot/templates/<模板名>.yaml` 的 `thresholds` 两行
   （`match_fuzzy_auto` / `match_fuzzy_review`），其余文件一律不动。

判定口径（与 `domain/matching.py` 一致）：一对样本在阈值 `t` 下被算作「自动合并」，
当且仅当 `fuzzy ≥ t` **且** `suffix_only_difference` 为假——后者是不同法人主体，
按规则必须进复核，不许自动合并。`label=true` 为正类。
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    # 脚本可能脱离 pytest 直接跑，先把 src 放进 import 路径（pytest 由 pyproject 配好了）。
    sys.path.insert(0, str(SRC_DIR))

from salescopilot.domain.matching import (
    fuzzy_score,
    normalize_org,
    suffix_only_difference,
)

EVALSET_PATH = REPO_ROOT / "data" / "evalsets" / "entity_pairs.jsonl"
TEMPLATES_DIR = REPO_ROOT / "src" / "salescopilot" / "templates"

# 扫描区间与口径：auto 从 70 扫到 99（步长 1）；precision 是硬约束，recall 是目标。
SCAN_LOW = 70
SCAN_HIGH = 99
PRECISION_TARGET = 0.95
RECALL_TARGET = 0.95

# 模板 `thresholds` 段里的两个键名（写回时只动这两行）。
AUTO_KEY = "match_fuzzy_auto"
REVIEW_KEY = "match_fuzzy_review"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    pairs = load_pairs(EVALSET_PATH)
    scored = score_pairs(pairs)

    rows = scan(scored)
    auto = recommend_auto(rows)
    review = suggest_review(scored, auto["threshold"])

    _print_header(scored)
    _print_table(rows, auto["threshold"], review["threshold"])
    _print_recommendation(scored, auto, review)

    if args.json:
        report = build_report(scored, rows, auto, review)
        path = write_report(report, Path(args.json))
        print(f"\nJSON 报告已写出：{path}")
    if args.write_template:
        _print_template_changes(
            write_template(args.write_template, auto["threshold"], review["threshold"])
        )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="归属 L1 阈值校准（纯本地，不联网）")
    parser.add_argument("--json", default=None, help="把完整报告写到这个路径")
    parser.add_argument(
        "--write-template",
        default=None,
        metavar="模板名",
        help="把建议值写进该模板的 thresholds 段（默认不写）",
    )
    return parser.parse_args(argv)


def load_pairs(path: Path) -> list[dict]:
    """逐行读 jsonl；结构不对直接报行号退出（不猜、不跳过坏样本）。"""
    if not path.is_file():
        raise SystemExit(f"样本文件不存在：{path}")

    pairs: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            pair = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"样本第 {lineno} 行不是合法 JSON：{exc.msg}") from exc
        _check_pair(pair, lineno)
        pairs.append(pair)
    if not pairs:
        raise SystemExit(f"样本为空：{path}")
    return pairs


def _check_pair(pair: object, lineno: int) -> None:
    """校验一条样本：字段齐、类型对、id 不重复交给调用方看整体。"""
    if not isinstance(pair, dict):
        raise SystemExit(f"样本第 {lineno} 行不是 JSON 对象")
    for key in ("id", "a", "b", "note"):
        value = pair.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"样本第 {lineno} 行缺少 {key}")
    if not isinstance(pair.get("label"), bool):
        raise SystemExit(f"样本第 {lineno} 行的 label 必须是 true / false")


def score_pairs(pairs: list[dict]) -> list[dict]:
    """给每对算 L1 分数与「仅组织形式不同」标志；不改入参。"""
    scored: list[dict] = []
    for pair in pairs:
        scored.append(
            {
                "id": pair["id"],
                "a": pair["a"],
                "b": pair["b"],
                "label": pair["label"],
                "note": pair["note"],
                "fuzzy": round(fuzzy_score(normalize_org(pair["a"]), normalize_org(pair["b"])), 2),
                "suffix_only": suffix_only_difference(pair["a"], pair["b"]),
            }
        )
    return scored


def evaluate_at(scored: list[dict], threshold: int) -> dict:
    """某一阈值下的混淆矩阵与三指标；「自动合并」= 分数够且非仅组织形式不同。"""
    predicted = [item for item in scored if item["fuzzy"] >= threshold and not item["suffix_only"]]
    tp = sum(1 for item in predicted if item["label"])
    fp = len(predicted) - tp
    positives = sum(1 for item in scored if item["label"])
    fn = positives - tp

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / positives if positives else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "threshold": threshold,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "fp_ids": [item["id"] for item in predicted if not item["label"]],
    }


def scan(scored: list[dict]) -> list[dict]:
    """`fuzzy_auto` 从 70 扫到 99，逐点算指标。"""
    return [evaluate_at(scored, threshold) for threshold in range(SCAN_LOW, SCAN_HIGH + 1)]


def recommend_auto(rows: list[dict]) -> dict:
    """在 precision 达标的候选里选 recall 最优；都不达标就取 precision 最高并提示补样本。

    同分取舍：recall 相同时取**更低**的阈值——precision 已经安全的前提下，宁可多合并一点。
    """
    candidates = [row for row in rows if row["precision"] >= PRECISION_TARGET]
    if candidates:
        best = max(candidates, key=lambda row: (row["recall"], -row["threshold"]))
        return {**best, "target_met": True, "note": ""}

    best = max(rows, key=lambda row: (row["precision"], row["recall"], row["threshold"]))
    return {
        **best,
        "target_met": False,
        "note": f"未达到 {PRECISION_TARGET:.2f}，建议补充样本",
    }


def suggest_review(scored: list[dict], auto_threshold: int) -> dict:
    """在 auto 阈值之下往下找：使「≥review 阈值」的正例召回达到目标的最高阈值（灰区下沿）。

    从 auto 往下一格一格试，第一个满足召回目标的阈值就是**最高**的可用值；
    这样灰区尽量窄，又不漏掉任何一种已知写法。
    """
    positives = [item["fuzzy"] for item in scored if item["label"]]
    if not positives:
        return {"threshold": 0, "recall": 0.0, "covered": 0, "total": 0}

    for threshold in range(int(auto_threshold), -1, -1):
        covered = sum(1 for score in positives if score >= threshold)
        if covered / len(positives) >= RECALL_TARGET:
            return {
                "threshold": threshold,
                "recall": round(covered / len(positives), 4),
                "covered": covered,
                "total": len(positives),
            }
    return {"threshold": 0, "recall": 1.0, "covered": len(positives), "total": len(positives)}


def build_report(scored: list[dict], rows: list[dict], auto: dict, review: dict) -> dict:
    """拼 JSON 报告：样本数、每对明细、扫描表、建议值。"""
    return {
        "suite": "entity_pairs",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "evalset": str(EVALSET_PATH.relative_to(REPO_ROOT)),
        "samples": len(scored),
        "positives": sum(1 for item in scored if item["label"]),
        "negatives": sum(1 for item in scored if not item["label"]),
        "suffix_only_pairs": sum(1 for item in scored if item["suffix_only"]),
        "scan_range": [SCAN_LOW, SCAN_HIGH],
        "scan": rows,
        "recommendation": {
            "auto": auto["threshold"],
            "review": review["threshold"],
            "precision": auto["precision"],
            "recall": auto["recall"],
            "f1": auto["f1"],
            "tp": auto["tp"],
            "fp": auto["fp"],
            "fn": auto["fn"],
            "fp_ids": auto["fp_ids"],
            "precision_target": PRECISION_TARGET,
            "target_met": auto["target_met"],
            "note": auto["note"],
            "review_recall": review["recall"],
            "review_covered": review["covered"],
        },
        "pairs": [
            {
                "id": item["id"],
                "a": item["a"],
                "b": item["b"],
                "label": item["label"],
                "fuzzy": item["fuzzy"],
                "suffix_only": item["suffix_only"],
                "auto_at_recommended": item["fuzzy"] >= auto["threshold"]
                and not item["suffix_only"],
                "note": item["note"],
            }
            for item in scored
        ],
    }


def write_report(report: dict, path: Path) -> Path:
    """写 JSON 报告；目录不存在就建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_template(name: str, auto: int, review: int) -> list[tuple[int, str, str]]:
    """把两行阈值写进模板 `thresholds` 段，返回改动清单（行号 / 旧行 / 新行）。

    键已存在就改值，不存在就插在 `thresholds:` 后；模板不存在或没有 thresholds 段
    一律抛 `SystemExit`，不做「猜一个文件来写」的事。
    """
    path = TEMPLATES_DIR / f"{name}.yaml"
    if not path.is_file():
        raise SystemExit(f"模板不存在：{path}")

    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == "thresholds:")
    except StopIteration:
        raise SystemExit(f"{path}: 没有 thresholds 段，拒绝写入") from None

    # thresholds 段的结束位置：下一条顶格行（或文件尾）。
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t"))):
        end += 1

    changes: list[tuple[int, str, str]] = []
    for key, value in ((AUTO_KEY, auto), (REVIEW_KEY, review)):
        new_line = f"  {key}: {value}"
        for index in range(start + 1, end):
            if lines[index].strip().startswith(f"{key}:"):
                old_line = lines[index]
                lines[index] = new_line
                changes.append((index + 1, old_line, new_line))
                break
        else:
            lines.insert(end, new_line)
            end += 1
            changes.append((end, "(新增)", new_line))

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changes


def _print_header(scored: list[dict]) -> None:
    positives = sum(1 for item in scored if item["label"])
    suffix_only = sum(1 for item in scored if item["suffix_only"])
    print(
        f"[entity_pairs] 样本 {len(scored)} 对（正例 {positives} / 负例 "
        f"{len(scored) - positives}），其中 {suffix_only} 对仅组织形式不同"
    )
    print(
        "口径：fuzzy ≥ 阈值 且 非「仅组织形式不同」才算自动合并；"
        "正例 = 同一家。（合成样本，仅用于阈值校准，不代表真实业务分布）"
    )


def _print_table(rows: list[dict], auto_threshold: int, review_threshold: int) -> None:
    """打印阈值扫描表；建议的 auto / review 行标箭头。"""
    print()
    print("阈值  precision  recall     F1     TP   FP   FN")
    for row in rows:
        mark = ""
        if row["threshold"] == auto_threshold:
            mark = " ← auto"
        elif row["threshold"] == review_threshold:
            mark = " ← review"
        print(
            f"{row['threshold']:>4}  {row['precision']:>9.3f}  {row['recall']:>6.3f}  "
            f"{row['f1']:>5.3f}  {row['tp']:>4}  {row['fp']:>4}  {row['fn']:>4}{mark}"
        )


def _print_recommendation(scored: list[dict], auto: dict, review: dict) -> None:
    print()
    print(f"建议 auto={auto['threshold']} review={review['threshold']}")
    print(
        f"- auto={auto['threshold']}：precision {auto['precision']:.3f} / "
        f"recall {auto['recall']:.3f} / F1 {auto['f1']:.3f}，FP {auto['fp']} 对"
    )
    if auto["target_met"]:
        print(
            f"  选法：在 precision ≥ {PRECISION_TARGET:.2f} 的候选里取 recall 最高的阈值"
            "（同分取更低阈值，尽量多合并）。"
        )
    else:
        print(f"  选法：没有阈值能达到 precision ≥ {PRECISION_TARGET:.2f}，已取 precision 最高者。")
        print(f"  ⚠ {auto['note']}")
    if auto["fp_ids"]:
        print(f"  自动合并的误配：{', '.join(auto['fp_ids'])}")

    positives = sum(1 for item in scored if item["label"])
    suffix_only = sum(1 for item in scored if item["suffix_only"])
    print(
        f"- review={review['threshold']}：在 auto 之下往下找，使「≥review」的正例召回 ≥ "
        f"{RECALL_TARGET:.2f} 的最高阈值（灰区下沿）；实测召回 {review['recall']:.3f}"
        f"（覆盖 {review['covered']}/{review['total']} 对正例）。"
    )
    print(
        f"- 读法：auto 档召回上限受「仅组织形式不同必须复核」约束——本样本有 {suffix_only} 对"
        f"正例天生进复核，所以 auto 召回不必追高；剩下 {positives - suffix_only} 对靠字符串"
        "相似度，简称类还要靠模板别名表兜底。"
    )


def _print_template_changes(changes: list[tuple[int, str, str]]) -> None:
    print("\n模板已更新（只动下面两行）：")
    for lineno, old_line, new_line in changes:
        print(f"  第 {lineno} 行：{old_line.strip()} → {new_line.strip()}")


if __name__ == "__main__":
    raise SystemExit(main())
