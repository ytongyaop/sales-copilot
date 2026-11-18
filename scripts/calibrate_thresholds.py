"""阈值校准：在标注客户对子上扫 L1 阈值，给出 `auto` / `review` 建议值。

用法（在仓库根执行，无需先设 PYTHONPATH）：

    python scripts/calibrate_thresholds.py                      # 两套模型都打印（建议值按 l1）
    python scripts/calibrate_thresholds.py --model funnel       # 只看 funnel
    python scripts/calibrate_thresholds.py --json out.json      # 另存 JSON 报告（默认含两套）
    python scripts/calibrate_thresholds.py --write-template building_materials

三条边界：

1. **合成样本，仅用于阈值校准，不代表真实业务分布**：`data/evalsets/entity_pairs.jsonl`
   里的名称全部自拟、不含真实客户数据，且刻意压了难例（简称、同区不同公司、别字、
   仅组织形式不同），真实流量里这类比例不会这么高，所以下面的阈值偏保守。
2. **两种模型，口径与 `domain/matching.py` 的分支顺序对齐**：
   - `l1`（向后兼容的旧口径）：只看 `fuzzy_score(normalize_org(a), normalize_org(b))`
     加 `suffix_only_difference`，`fuzzy ≥ 阈值` 且非「仅组织形式不同」才算 auto；
   - `funnel`：先看 **L0 精确键**（`normalize_org` 相同且不是仅组织形式不同 → auto / `exact`），
     再看 `suffix_only_difference` 强制 review，最后才落到上面的 L1 阈值。
   L2 向量不在本次范围（不做嵌入，不联网）。
3. **默认只打印**：只有显式传 `--write-template <模板名>` 才改
   `src/salescopilot/templates/<模板名>.yaml` 的 `matching:` 段两行
   （`l1_auto` / `l1_review`），其余内容与注释一字不动；重复写只改值、不追加段。

判定口径：`label=true` 为正类。「自动合并」在 `l1` 模型下当且仅当 `fuzzy ≥ t`
且 `suffix_only_difference` 为假——后者是不同法人主体，按规则必须进复核，
不许自动合并；`funnel` 模型额外把 L0 精确键命中的对子直接算 auto。
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

# 两种判定模型：`l1` 是旧口径（不认精确键），`funnel` 与 domain/matching.py 的分支顺序一致。
MODEL_L1 = "l1"
MODEL_FUNNEL = "funnel"
MODELS = (MODEL_L1, MODEL_FUNNEL)
MODEL_CHOICES = (*MODELS, "both")

# 模板里的嵌套段与两个键名（写回时只动这一段）。
MATCHING_SECTION = "matching"
L1_AUTO_KEY = "l1_auto"
L1_REVIEW_KEY = "l1_review"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    pairs = load_pairs(EVALSET_PATH)
    scored = score_pairs(pairs)

    models = list(MODELS) if args.model == "both" else [args.model]
    results = {model: analyse(scored, model) for model in models}

    _print_header(scored)
    for model in models:
        _print_model_section(model, results[model], scored)
    if len(models) > 1:
        _print_comparison(scored, results)

    if args.json:
        report = build_json_report(scored, results, models)
        path = write_report(report, Path(args.json))
        print(f"\nJSON 报告已写出：{path}")
    if args.write_template:
        # 模板写回默认按 l1（保持旧行为）；要按 funnel 落值必须显式 --model funnel。
        primary = MODEL_L1 if args.model == "both" else args.model
        _print_template_changes(
            write_template(
                args.write_template,
                results[primary]["auto"]["threshold"],
                results[primary]["review"]["threshold"],
            ),
            primary,
        )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="归属阈值校准（纯本地，不联网）")
    parser.add_argument(
        "--model",
        choices=MODEL_CHOICES,
        default="both",
        help="判定模型：l1（旧口径）/ funnel（含 L0 精确键）/ both（默认，两套都打印）",
    )
    parser.add_argument("--json", default=None, help="把完整报告写到这个路径")
    parser.add_argument(
        "--write-template",
        default=None,
        metavar="模板名",
        help="把建议值写进该模板的 matching 段（默认不写；默认按 l1 的建议值）",
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


def is_exact_hit(a: str, b: str) -> bool:
    """L0 精确键：规范化全称相同，且两串不是「仅组织形式不同」。

    「有限公司」与「集团」规范化后同名，却是两个法人，必须留给人复核，
    所以 `suffix_only_difference` 为真时**不算**精确命中（与 `domain/matching.py`
    的分支顺序一致：精确键之外，suffix_only 优先于所有 auto 分支）。
    """
    if normalize_org(a) != normalize_org(b):
        return False
    return not suffix_only_difference(a, b)


def score_pairs(pairs: list[dict]) -> list[dict]:
    """给每对算 L1 分数、精确键命中与「仅组织形式不同」标志；不改入参。"""
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
                "exact": is_exact_hit(pair["a"], pair["b"]),
            }
        )
    return scored


def is_auto(item: dict, threshold: int, model: str = MODEL_L1) -> bool:
    """一对样本在某模型、某阈值下是否算「自动合并」（只分 auto / 非 auto）。"""
    if model not in MODELS:
        raise ValueError(f"未知模型：{model}")
    if model == MODEL_FUNNEL and item.get("exact"):
        # L0 精确键不看阈值：规范化全称相同就是同一家。
        return True
    return item["fuzzy"] >= threshold and not item["suffix_only"]


def evaluate_at(scored: list[dict], threshold: int, *, model: str = MODEL_L1) -> dict:
    """某一阈值下的混淆矩阵与三指标；「自动合并」口径见 `is_auto`。"""
    predicted = [item for item in scored if is_auto(item, threshold, model)]
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


def scan(scored: list[dict], *, model: str = MODEL_L1) -> list[dict]:
    """`fuzzy_auto` 从 70 扫到 99，逐点算指标。"""
    return [
        evaluate_at(scored, threshold, model=model) for threshold in range(SCAN_LOW, SCAN_HIGH + 1)
    ]


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


def _covered_by_review(item: dict, threshold: int, model: str) -> bool:
    """灰区口径：分数够（或精确键命中）就算被这一档接住。

    与 auto 混淆矩阵不同，这里**不排除**「仅组织形式不同」的对子——它们本来就要人复核，
    算在 review 档的覆盖里。
    """
    if model == MODEL_FUNNEL and item.get("exact"):
        return True
    return item["fuzzy"] >= threshold


def suggest_review(scored: list[dict], auto_threshold: int, *, model: str = MODEL_L1) -> dict:
    """在 auto 阈值之下往下找：使「≥review 阈值」的正例召回达到目标的最高阈值（灰区下沿）。

    从 auto 往下一格一格试，第一个满足召回目标的阈值就是**最高**的可用值；
    这样灰区尽量窄，又不漏掉任何一种已知写法。
    """
    positives = [item for item in scored if item["label"]]
    if not positives:
        return {"threshold": 0, "recall": 0.0, "covered": 0, "total": 0}

    for threshold in range(int(auto_threshold), -1, -1):
        covered = sum(1 for item in positives if _covered_by_review(item, threshold, model))
        if covered / len(positives) >= RECALL_TARGET:
            return {
                "threshold": threshold,
                "recall": round(covered / len(positives), 4),
                "covered": covered,
                "total": len(positives),
            }
    return {"threshold": 0, "recall": 1.0, "covered": len(positives), "total": len(positives)}


def analyse(scored: list[dict], model: str) -> dict:
    """跑完一个模型：扫描表 + auto 建议 + review 建议。"""
    rows = scan(scored, model=model)
    auto = recommend_auto(rows)
    review = suggest_review(scored, auto["threshold"], model=model)
    return {"model": model, "rows": rows, "auto": auto, "review": review}


def build_report(scored: list[dict], result: dict) -> dict:
    """拼单个模型的 JSON 报告：样本数、每对明细、扫描表、建议值。"""
    auto = result["auto"]
    review = result["review"]
    model = result["model"]
    return {
        "model": model,
        "suite": "entity_pairs",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "evalset": str(EVALSET_PATH.relative_to(REPO_ROOT)),
        "samples": len(scored),
        "positives": sum(1 for item in scored if item["label"]),
        "negatives": sum(1 for item in scored if not item["label"]),
        "suffix_only_pairs": sum(1 for item in scored if item["suffix_only"]),
        "exact_hits": sum(1 for item in scored if item["exact"]),
        "scan_range": [SCAN_LOW, SCAN_HIGH],
        "scan": result["rows"],
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
                "exact": item["exact"],
                "auto_at_recommended": is_auto(item, auto["threshold"], model),
                "note": item["note"],
            }
            for item in scored
        ],
    }


def build_json_report(scored: list[dict], results: dict[str, dict], models: list[str]) -> dict:
    """单模型时直接是一份报告；两套时按 `{"l1": {...}, "funnel": {...}}` 并列。"""
    if len(models) == 1:
        return build_report(scored, results[models[0]])
    return {
        "model": "both",
        "l1": build_report(scored, results[MODEL_L1]),
        "funnel": build_report(scored, results[MODEL_FUNNEL]),
    }


def write_report(report: dict, path: Path) -> Path:
    """写 JSON 报告；目录不存在就建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _find_section(lines: list[str], name: str) -> tuple[int, int, int] | None:
    """定位顶格 `name:` 段：返回 (段头, 最后一条子行, 段尾) 的行号。

    段尾是下一条顶格非空行（或文件尾）；`last_child` 跳过段尾空行，
    这样插入新键时不会跑到空行后面，也不会把下一段的注释挤乱。
    """
    header = f"{name}:"
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip() == header and not line.startswith((" ", "\t"))
        ),
        None,
    )
    if start is None:
        return None

    last_child = start
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t"))):
        if lines[end].strip():
            last_child = end
        end += 1
    return start, last_child, end


def write_template(
    name: str, auto: int, review: int, *, templates_dir: Path = TEMPLATES_DIR
) -> list[tuple[int, str, str]]:
    """把 `l1_auto` / `l1_review` 写进模板的 `matching:` 段，返回改动清单。

    段不存在就追加到文件末尾；键不存在就插在段内最后一条子行之后；键已存在只改值。
    保留文件其余内容与注释，重复写不追加第二段。模板不存在一律抛 `SystemExit`，
    不做「猜一个文件来写」的事。
    """
    path = Path(templates_dir) / f"{name}.yaml"
    if not path.is_file():
        raise SystemExit(f"模板不存在：{path}")

    lines = path.read_text(encoding="utf-8").splitlines()
    changes: list[tuple[int, str, str]] = []

    bounds = _find_section(lines, MATCHING_SECTION)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{MATCHING_SECTION}:")
        changes.append((len(lines), "(新增)", f"{MATCHING_SECTION}:"))
        bounds = (len(lines) - 1, len(lines) - 1, len(lines))
    start, last_child, end = bounds

    for key, value in ((L1_AUTO_KEY, auto), (L1_REVIEW_KEY, review)):
        new_line = f"  {key}: {value}"
        index = next(
            (
                line_no
                for line_no in range(start + 1, end)
                if lines[line_no].strip().startswith(f"{key}:")
            ),
            None,
        )
        if index is None:
            insert_at = last_child + 1
            lines.insert(insert_at, new_line)
            changes.append((insert_at + 1, "(新增)", new_line))
            last_child = insert_at
            end += 1
        elif lines[index] != new_line:
            changes.append((index + 1, lines[index], new_line))
            lines[index] = new_line

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
        "口径：auto = L0 精确键命中（仅 funnel），或 fuzzy ≥ 阈值 且非「仅组织形式不同」；"
        "正例 = 同一家。（合成样本，仅用于阈值校准，不代表真实业务分布）"
    )


def _print_model_section(model: str, result: dict, scored: list[dict]) -> None:
    label = (
        "l1（只按 L1 字符串 + 仅组织形式兜底）"
        if model == MODEL_L1
        else "funnel（L0 精确键 → 仅组织形式复核 → L1 阈值）"
    )
    print()
    print(f"===== 模型 {label} =====")
    _print_table(result["rows"], result["auto"]["threshold"], result["review"]["threshold"])
    _print_recommendation(scored, result["auto"], result["review"], model)


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


def _print_recommendation(scored: list[dict], auto: dict, review: dict, model: str) -> None:
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

    if model == MODEL_FUNNEL:
        exact_ids = [item["id"] for item in scored if item["exact"]]
        shown = "、".join(exact_ids) if exact_ids else "无"
        print(f"- L0 精确键命中 {len(exact_ids)} 对（{shown}），这批不看阈值直接 auto。")

    positives = sum(1 for item in scored if item["label"])
    suffix_only = sum(1 for item in scored if item["suffix_only"])
    fuzzy_positives = sum(1 for item in scored if item["label"] and not item["suffix_only"])
    print(
        f"- review={review['threshold']}：在 auto 之下往下找，使「≥review」的正例召回 ≥ "
        f"{RECALL_TARGET:.2f} 的最高阈值（灰区下沿）；实测召回 {review['recall']:.3f}"
        f"（覆盖 {review['covered']}/{review['total']} 对正例）。"
    )
    print(
        f"- 读法：auto 档召回上限受「仅组织形式不同必须复核」约束——本样本有 {suffix_only} 对"
        f"仅组织形式不同（正例 {positives - fuzzy_positives} 对天生进复核）；其余 "
        f"{fuzzy_positives} 对正例靠字符串相似度，简称类还要靠模板别名表兜底。"
    )


def _print_comparison(scored: list[dict], results: dict[str, dict]) -> None:
    """两套模型并列对比：差异只可能来自 L0 精确键。"""
    l1_auto = results[MODEL_L1]["auto"]
    funnel_auto = results[MODEL_FUNNEL]["auto"]
    l1_ids = {item["id"] for item in scored if is_auto(item, l1_auto["threshold"], MODEL_L1)}
    funnel_ids = {
        item["id"] for item in scored if is_auto(item, funnel_auto["threshold"], MODEL_FUNNEL)
    }
    gained = sorted(funnel_ids - l1_ids)
    lost = sorted(l1_ids - funnel_ids)
    exact_ids = sorted(item["id"] for item in scored if item["exact"])

    print()
    print("===== 两套对比 =====")
    print(
        f"l1     auto={l1_auto['threshold']}：TP {l1_auto['tp']} / FP {l1_auto['fp']} / "
        f"precision {l1_auto['precision']:.3f} / recall {l1_auto['recall']:.3f}"
    )
    print(
        f"funnel auto={funnel_auto['threshold']}：TP {funnel_auto['tp']} / FP "
        f"{funnel_auto['fp']} / precision {funnel_auto['precision']:.3f} / "
        f"recall {funnel_auto['recall']:.3f}"
    )
    print(
        f"差异来源：L0 精确键命中 {len(exact_ids)} 对（{'、'.join(exact_ids) or '无'}）；"
        f"funnel 净增 {len(gained)} 对（{'、'.join(gained) or '无'}）、"
        f"净减 {len(lost)} 对（{'、'.join(lost) or '无'}）。"
    )
    if not gained and not lost:
        print(
            "本样本上 L0 精确键没有改变 auto：精确命中的对子要么本来就以 100 分进 L1 auto，"
            "要么因「仅组织形式不同」被强制复核。"
        )


def _print_template_changes(changes: list[tuple[int, str, str]], model: str) -> None:
    if not changes:
        print(f"\n模板 matching 段已是 {model} 的建议值，未改动。")
        return
    print(f"\n模板已更新（matching 段，按 {model} 建议值）：")
    for lineno, old_line, new_line in changes:
        print(f"  第 {lineno} 行：{old_line.strip()} → {new_line.strip()}")


if __name__ == "__main__":
    raise SystemExit(main())
